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
                "version": workflow["version"], "history": workflow["history"], "assignee":workflow.get("assignee"), "resolution":workflow.get("resolution"), "updated_at":workflow.get("updated_at"), "execution_allowed": False}

    def review_queue(self, identity, limit=30, offset=0):
        capabilities = review_capabilities(identity)
        if not capabilities:
            raise AppError("PERMISSION_DENIED", "当前身份没有分诊或业务审批能力。", 403)
        with self.connection() as db:
            db.execute("SET TRANSACTION READ ONLY")
            predicate = "t.tenant_id=%s AND t.user_id<>%s AND w.status IN ('pending_triage','pending_approval','queued','approved','in_progress','resolved') AND w.policy->'required_capabilities' <@ %s::jsonb"
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
        actions = {"pending_triage": ["triage", "reject"], "pending_approval": ["approve", "reject"], "queued": ["escalate"], "approved": ["escalate"], "in_progress": ["escalate"]}.get(workflow["status"], [])
        item = {"ticket_id": row["ticket_id"], "title": row["arguments"]["title"], "user_id": row["user_id"],
                "created_at": row["workflow"]["created_at"], "simulated": True, "status": workflow["status"], "workflow": workflow, "review_actions": actions,"assignee":workflow.get("assignee"),"resolution":workflow.get("resolution")}
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
                if "triage" not in review_capabilities(identity) or (action == "triage" and status != "pending_triage") or (action == "escalate" and status not in {"queued", "approved", "in_progress"}):
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
            if action=='escalate':
                db.execute('UPDATE ko_ticket_workflows SET assignee=NULL,resolution=NULL WHERE ticket_id=%s',(ticket_id,))
            self.notify(db,row,identity,workflow["version"]+1,STATUS_LABELS[next_status])
            return {"ticket_id": ticket_id, "status": next_status, "status_label": STATUS_LABELS[next_status], "version": workflow["version"]+1, "execution_allowed": False}


    @staticmethod
    def notify(db,row,identity,version,message):
        recipients={row['user_id'],row['workflow'].get('assignee')}-{None,identity.user_id}
        for user_id in recipients:
            db.execute("""INSERT INTO ko_ticket_notifications(ticket_id,tenant_id,user_id,version,message,created_at)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(ticket_id,version,user_id) DO NOTHING""",
                (row['ticket_id'],row['tenant_id'],user_id,version,message,time.time()))

    def activity(self,ticket_id,identity,activity):
        with self.connection() as db:
            row=db.execute("""SELECT t.*,to_jsonb(w) AS workflow FROM ko_agent_tickets t
                JOIN ko_ticket_workflows w USING(ticket_id) WHERE t.ticket_id=%s AND t.tenant_id=%s
                FOR UPDATE OF w""",(ticket_id,identity.tenant_id)).fetchone()
            owner=row is not None and row['user_id']==identity.user_id
            handler=row is not None and 'handle' in identity.ticket_capabilities and not owner
            if not owner and not handler:raise AppError('TICKET_NOT_FOUND','工单不存在或当前身份无权处理。',404)
            w=row['workflow'];policy=w['policy'];action=activity.action;status=w['status']
            if handler and not set(policy['required_capabilities']).issubset(review_capabilities(identity)):
                raise AppError('TICKET_NOT_FOUND','当前身份不具备该业务范围的处理能力。',404)
            if w['version']!=activity.version:
                raise AppError('TICKET_VERSION_CONFLICT','工单已变化，请刷新后重新操作。',409)
            assignee,resolution=w.get('assignee'),w.get('resolution');next_status=status
            if action=='claim':
                if not handler or status not in {'queued','approved'} or (policy['required_capabilities'] and status!='approved'):
                    raise AppError('INVALID_STATE','仅已受理或已审批的工单可由支持人员领取。',409)
                assignee=identity.user_id;next_status='in_progress'
            elif action=='resolve':
                if not handler or assignee!=identity.user_id or status!='in_progress':
                    raise AppError('INVALID_STATE','仅当前处理人可记录解决结果。',409)
                resolution=activity.reason;next_status='resolved'
            elif action=='close':
                if not owner or status!='resolved':raise AppError('INVALID_STATE','仅申请人可确认关闭已解决的工单。',409)
                next_status='closed'
            elif action=='reopen':
                if not owner or status not in {'resolved','closed'}:raise AppError('INVALID_STATE','仅申请人可重开已解决或关闭的工单。',409)
                next_status='pending_approval' if policy['required_capabilities'] else 'queued';assignee=None;resolution=None
            elif action=='comment':
                if not owner and assignee!=identity.user_id:raise AppError('INVALID_STATE','请先领取工单再添加处理留言。',409)
            history=[*w['history'],{'action':action,'actor':identity.user_id,'time':time.time(),'reason':activity.reason,
                'from_status':status,'to_status':next_status,'version':w['version']+1}]
            db.execute('UPDATE ko_ticket_workflows SET status=%s,assignee=%s,resolution=%s,history=%s,version=version+1,updated_at=%s WHERE ticket_id=%s',
                (next_status,assignee,resolution,Jsonb(history),time.time(),ticket_id))
            self.notify(db,row,identity,w['version']+1,'新增留言' if action=='comment' else STATUS_LABELS[next_status])
            return {'ticket_id':ticket_id,'status':next_status,'version':w['version']+1,'assignee':assignee,
                    'resolution':resolution,'execution_allowed':False}

    def notifications(self,identity):
        with self.connection() as db:
            return {'items':[dict(r) for r in db.execute("""SELECT n.notification_id,n.ticket_id,n.message,n.created_at,n.read_at,(t.user_id=n.user_id) AS own_ticket
                FROM ko_ticket_notifications n JOIN ko_agent_tickets t USING(ticket_id) WHERE n.tenant_id=%s AND n.user_id=%s ORDER BY notification_id DESC LIMIT 50""",
                (identity.tenant_id,identity.user_id))]}

    def read_notification(self,notification_id,identity):
        with self.connection() as db:
            cursor=db.execute('UPDATE ko_ticket_notifications SET read_at=COALESCE(read_at,%s) WHERE notification_id=%s AND tenant_id=%s AND user_id=%s',
                (time.time(),notification_id,identity.tenant_id,identity.user_id))
            if not cursor.rowcount:raise AppError('NOT_FOUND','通知不存在。',404)
            return {'read':True}
