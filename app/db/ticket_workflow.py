"""Tenant-scoped human triage and business approval, with no execution hook."""
import time
from psycopg.types.json import Jsonb
from app.agent.ticket_policy import CATEGORY_LABELS, STATUS_LABELS, review_capabilities
from app.core.errors import AppError

ROUTING = {"incident": [], "standard": [], "access": ["access_review"], "security": ["security_review"], "production": ["production_review"]}

class TicketWorkflowRepository:
    @staticmethod
    def workflow_view(row):
        workflow = row.get("workflow")
        if not workflow:
            return {"category": "uncertain", "category_label": "待人工分诊", "status": "pending_triage", "status_label": "待人工分诊",
                    "reason": "旧工单未记录业务分类，需要人工分诊。", "version": 0, "history": [], "execution_allowed": False}
        return {**workflow["policy"], "status": workflow["status"], "status_label": STATUS_LABELS[workflow["status"]],
                "version": workflow["version"], "history": workflow["history"], "execution_allowed": False}

    def review_queue(self, identity, limit=30, offset=0):
        capabilities = review_capabilities(identity)
        if not capabilities:
            raise AppError("PERMISSION_DENIED", "当前身份没有分诊或业务审批能力。", 403)
        with self.connection() as db:
            db.execute("SET TRANSACTION READ ONLY")
            predicate = "t.tenant_id=%s AND t.user_id<>%s AND w.status IN ('pending_triage','pending_approval','queued','approved') AND w.policy->'required_capabilities' <@ %s::jsonb"
            params = (identity.tenant_id, identity.user_id, Jsonb(sorted(capabilities)))
            total = db.execute("SELECT count(*) AS n FROM ko_agent_tickets t JOIN ko_ticket_workflows w USING(ticket_id) WHERE "+predicate, params).fetchone()["n"]
            rows = db.execute("SELECT t.*,to_jsonb(w) AS workflow FROM ko_agent_tickets t JOIN ko_ticket_workflows w USING(ticket_id) WHERE "+predicate+" ORDER BY w.created_at DESC,t.ticket_id LIMIT %s OFFSET %s", params+(limit,offset)).fetchall()
            return {"items": [self.review_ticket_view(row, identity) for row in rows], "total": total, "limit": limit, "offset": offset}

    def review_ticket(self, ticket_id, identity):
        with self.connection() as db:
            db.execute("SET TRANSACTION READ ONLY")
            row = db.execute("SELECT t.*,to_jsonb(w) AS workflow FROM ko_agent_tickets t JOIN ko_ticket_workflows w USING(ticket_id) WHERE t.ticket_id=%s AND t.tenant_id=%s", (ticket_id,identity.tenant_id)).fetchone()
            self.require_reviewer(row, identity)
            return self.review_ticket_view(row, identity, True)

    @staticmethod
    def require_reviewer(row, identity):
        capabilities = review_capabilities(identity)
        if not row or not capabilities or row["user_id"] == identity.user_id or not set(row["workflow"]["policy"]["required_capabilities"]).issubset(capabilities):
            raise AppError("TICKET_NOT_FOUND", "申请不存在或当前身份无权审核；申请人不能审核本人申请。", 404)

    def review_ticket_view(self, row, identity, detail=False):
        workflow = self.workflow_view(row)
        actions = {"pending_triage": ["triage", "reject"], "pending_approval": ["approve", "reject"], "queued": ["escalate"], "approved": ["escalate"]}.get(workflow["status"], [])
        item = {"ticket_id": row["ticket_id"], "title": row["arguments"]["title"], "user_id": row["user_id"],
                "created_at": row["workflow"]["created_at"], "simulated": True, "status": workflow["status"], "workflow": workflow, "review_actions": actions}
        if detail:
            item["description"] = row["arguments"]["description"]
        return item

    def decide_ticket(self, ticket_id, identity, decision):
        with self.connection() as db:
            row = db.execute("SELECT t.*,to_jsonb(w) AS workflow FROM ko_agent_tickets t JOIN ko_ticket_workflows w USING(ticket_id) WHERE t.ticket_id=%s AND t.tenant_id=%s FOR UPDATE OF w", (ticket_id,identity.tenant_id)).fetchone()
            self.require_reviewer(row, identity)
            workflow = row["workflow"]
            if workflow["version"] != decision.version:
                raise AppError("TICKET_VERSION_CONFLICT", "工单已被处理，请刷新后重新核对。", 409)
            policy = dict(workflow["policy"])
            if policy["category"] == "prohibited":
                raise AppError("TICKET_POLICY_DENIED", "禁止操作不能通过审批放行。", 403)
            action, status = decision.action, workflow["status"]
            if action in {"triage", "escalate"}:
                if "triage" not in review_capabilities(identity) or (action == "triage" and status != "pending_triage") or (action == "escalate" and status not in {"queued", "approved"}):
                    raise AppError("INVALID_STATE", "当前工单不能执行此分诊操作。", 409)
                scopes = ROUTING.get(decision.category)
                if scopes is None or (action == "escalate" and not scopes):
                    raise AppError("INVALID_TICKET_CATEGORY", "请选择有效分诊类型；升级必须选择敏感操作。", 422)
                if action == "escalate":
                    scopes = sorted(set(scopes) | set(policy["required_capabilities"]))
                if not set(policy["required_capabilities"]).issubset(scopes):
                    raise AppError("TICKET_POLICY_DENIED", "不能通过分诊降级已有敏感操作。", 403)
                category = "sensitive" if scopes else decision.category
                policy.update(category=category, category_label=CATEGORY_LABELS[category], required_capabilities=scopes, reason=decision.reason)
                next_status = "pending_approval" if scopes else "queued"
            elif action == "approve" and status == "pending_approval":
                next_status = "approved"
            elif action == "reject" and status in {"pending_triage", "pending_approval"}:
                next_status = "rejected"
            else:
                raise AppError("INVALID_STATE", "当前工单不能执行此审批操作。", 409)
            history = [*workflow["history"], {"action": action, "actor": identity.user_id, "time": time.time(), "reason": decision.reason,
                                           "from_status": status, "to_status": next_status, "version": workflow["version"]+1, "category": policy["category"]}]
            db.execute("UPDATE ko_ticket_workflows SET status=%s,policy=%s,version=version+1,history=%s WHERE ticket_id=%s",
                       (next_status,Jsonb(policy),Jsonb(history),ticket_id))
            return {"ticket_id": ticket_id, "status": next_status, "status_label": STATUS_LABELS[next_status], "version": workflow["version"]+1, "execution_allowed": False}
