"""PostgreSQL checkpoints, fenced leases, approvals and atomic local effects."""
import hashlib
import json
import time
from contextlib import contextmanager
from uuid import uuid4
import psycopg
from app.db.postgres import connection_options
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from app.agent.contracts import SYSTEM, dump, event, redact
from app.core.errors import AppError
from app.db.business_fixtures import business_fixtures

TERMINAL = {"completed", "failed", "cancelled"}

class AgentRepository:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def connection(self, readonly=False):
        url = self.settings.business_database_url if readonly else self.settings.database_url
        if not url:
            raise AppError("AGENT_NOT_CONFIGURED", "Configure Agent PostgreSQL and read-only business connection", 503)
        with psycopg.connect(url, row_factory=dict_row, connect_timeout=3,
                             options=connection_options(url, statement_timeout=3000, lock_timeout=2000)) as db:
            if readonly:
                db.execute("SET TRANSACTION READ ONLY")
                writable = db.execute("SELECT has_table_privilege(current_user, 'ko_agent_business', 'INSERT,UPDATE,DELETE') AS allowed").fetchone()
                if writable["allowed"]:
                    raise AppError("UNSAFE_BUSINESS_ROLE", "Business query requires a SELECT-only database role", 503)
            yield db

    def initialize(self):
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(52010001)")
            db.execute('''CREATE TABLE IF NOT EXISTS ko_agent_runs (
                run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                request_key TEXT NOT NULL, request_hash TEXT NOT NULL, state JSONB NOT NULL,
                lease_token TEXT, lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL,
                UNIQUE(tenant_id,user_id,request_key));
                CREATE TABLE IF NOT EXISTS ko_agent_actions (
                action_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ko_agent_runs(run_id),
                version INTEGER NOT NULL, digest TEXT NOT NULL, arguments JSONB NOT NULL,
                call_id TEXT NOT NULL, status TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
                result JSONB, UNIQUE(run_id,call_id));
                CREATE TABLE IF NOT EXISTS ko_agent_tickets (
                ticket_id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE REFERENCES ko_agent_actions(action_id),
                tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, arguments JSONB NOT NULL);
                CREATE TABLE IF NOT EXISTS ko_agent_permissions (
                tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, can_write BOOLEAN NOT NULL,
                PRIMARY KEY(tenant_id,user_id));
                CREATE TABLE IF NOT EXISTS ko_agent_business (
                tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, query TEXT NOT NULL, payload JSONB NOT NULL,
                PRIMARY KEY(tenant_id,user_id,query));
                CREATE OR REPLACE VIEW ko_ticket_business AS
                SELECT t.tenant_id,t.user_id,t.ticket_id,t.arguments->>'title' AS title,
                    'created'::text AS status, NULL::text AS assignee, NULL::text AS estimated_completion,
                    (a.result->>'created_at')::double precision AS created_at
                FROM ko_agent_tickets t JOIN ko_agent_actions a USING(action_id)
                JOIN ko_agent_runs r ON r.run_id=a.run_id
                WHERE r.tenant_id=t.tenant_id AND r.user_id=t.user_id AND a.status='committed';''')

    def seed_demo(self, identity):
        """Explicit setup only; never restore revoked permissions on app startup."""
        with self.connection() as db:
            db.execute("INSERT INTO ko_agent_permissions VALUES (%s,%s,true) ON CONFLICT DO NOTHING",
                       (identity.tenant_id, identity.user_id))
            self._seed_business(db, identity)

    @staticmethod
    def _seed_business(db, identity):
        # Add missing demo fields while preserving every existing business value.
        for query, payload in business_fixtures(identity.user_id).items():
            existing = db.execute("SELECT payload FROM ko_agent_business WHERE tenant_id=%s AND user_id=%s AND query=%s",
                                  (identity.tenant_id, identity.user_id, query)).fetchone()
            if existing and query == "device_status":
                # Old compliance records have no trustworthy per-check timestamp/status.
                payload.update(patch_status="unknown", edr_status="unknown",
                               disk_encrypted=None, observed_at=None)
            db.execute("""INSERT INTO ko_agent_business VALUES (%s,%s,%s,%s)
                ON CONFLICT (tenant_id,user_id,query) DO UPDATE
                SET payload=EXCLUDED.payload || ko_agent_business.payload""",
                (identity.tenant_id, identity.user_id, query, Jsonb(payload)))

    def seed_business(self, identity):
        """Explicit data upgrade only; never creates/restores a write permission."""
        with self.connection() as db:
            self._seed_business(db, identity)

    def _row(self, db, run_id, identity, lock=False):
        row = db.execute("SELECT * FROM ko_agent_runs WHERE run_id=%s AND tenant_id=%s AND user_id=%s" +
            (" FOR UPDATE" if lock else ""), (run_id, identity.tenant_id, identity.user_id)).fetchone()
        if not row:
            raise AppError("NOT_FOUND", "Run not found", 404)
        return row

    def _save(self, db, state, release=False):
        db.execute("UPDATE ko_agent_runs SET state=%s" +
            (", lease_token=NULL,lease_until=0" if release else "") + " WHERE run_id=%s",
            (Jsonb(state), state["run_id"]))

    def create(self, identity, message, request_key, trace_id, *, history=None, conversation_id=None):
        digest = hashlib.sha256((message if conversation_id is None else dump([message, conversation_id])).encode()).hexdigest()
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (identity.tenant_id + identity.user_id,))
            row = db.execute("SELECT * FROM ko_agent_runs WHERE tenant_id=%s AND user_id=%s AND request_key=%s",
                (identity.tenant_id, identity.user_id, request_key)).fetchone()
            if row:
                if row["request_hash"] != digest:
                    raise AppError("IDEMPOTENCY_CONFLICT", "Request key already binds another message", 409)
                return row["state"]
            count = db.execute("SELECT count(*) AS n FROM ko_agent_runs WHERE tenant_id=%s AND user_id=%s AND created_at>%s",
                (identity.tenant_id, identity.user_id, time.time()-60)).fetchone()["n"]
            if count >= self.settings.requests_per_minute:
                raise AppError("RATE_LIMIT", "Agent request rate exceeded", 429)
            run_id = uuid4().hex
            state = {"run_id": run_id, "trace_id": trace_id, "status": "running", "phase": "queued",
                "outcome": None, "answer": None, "messages": [{"role": "system", "content": SYSTEM},
                *[{"role": h["role"], "content": redact(h["content"])} for h in (history or [])],
                {"role": "user", "content": redact(message)}], "steps": 0, "elapsed_seconds": 0,
                "reserved_tokens": 0, "reserved_cost": 0.0, "estimated_model_cost": 0.0, "actual_model_tokens": 0, "usage": [], "trace": [], "events": [],
                "pending": None, "seen": [], "policy": self.settings.policy, "model": self.settings.model_id, "created_at": time.time()}
            if conversation_id:
                state["conversation_id"] = conversation_id
            event(state, "accepted", trace_id=trace_id)
            db.execute("INSERT INTO ko_agent_runs(run_id,tenant_id,user_id,request_key,request_hash,state,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (run_id, identity.tenant_id, identity.user_id, request_key, digest, Jsonb(state), time.time()))
            return state

    def get(self, run_id, identity):
        with self.connection() as db:
            return self._row(db, run_id, identity)["state"]

    def claim(self, run_id, identity):
        with self.connection() as db:
            # Global DB limit also bounds multiple application processes.
            db.execute("SELECT pg_advisory_xact_lock(52010002)")
            row = self._row(db, run_id, identity, True)
            state = row["state"]
            if state["status"] != "running":
                return None
            if row["lease_until"] > time.time():
                raise AppError("RUN_BUSY", "Run already executing", 409)
            n = db.execute("SELECT count(*) AS n FROM ko_agent_runs WHERE lease_until>%s", (time.time(),)).fetchone()["n"]
            if n >= self.settings.concurrency:
                raise AppError("CONCURRENCY_LIMIT", "Executor capacity reached; resume this run later", 429)
            # A hard crash consumes wall time since the last checkpoint, conservatively
            # including downtime. Repeated restarts cannot reset the task time budget.
            checkpoint_at = state.get("checkpoint_at")
            if row["lease_token"] and checkpoint_at is not None:
                state["elapsed_seconds"] += max(0, time.time()-checkpoint_at)
            state["checkpoint_at"] = time.time()
            token = uuid4().hex
            self._save(db, state)
            db.execute("UPDATE ko_agent_runs SET lease_token=%s,lease_until=%s WHERE run_id=%s",
                (token, time.time()+self.settings.max_seconds+15, run_id))
            return state, token

    def checkpoint(self, state, identity, token, release=False):
        with self.connection() as db:
            row = self._row(db, state["run_id"], identity, True)
            if row["lease_token"] != token or row["lease_until"] <= time.time() or row["state"]["status"] != "running":
                raise AppError("LEASE_LOST", "Run stopped or executor lease expired", 409)
            state["checkpoint_at"] = time.time()
            self._save(db, state, release)

    def propose(self, state, identity, token, call_id, arguments):
        with self.connection() as db:
            row = self._row(db, state["run_id"], identity, True)
            if row["lease_token"] != token or row["lease_until"] <= time.time() or row["state"]["status"] != "running":
                raise AppError("LEASE_LOST", "Executor no longer owns run", 409)
            action_id = uuid4().hex
            digest = hashlib.sha256(dump({"run_id": state["run_id"], "tool": "create_ticket", "arguments": arguments, "version": 1}).encode()).hexdigest()
            pending = {"action_id": action_id, "version": 1, "digest": digest, "arguments": arguments,
                       "tool": "create_ticket", "expires_at": time.time()+self.settings.approval_seconds}
            db.execute("INSERT INTO ko_agent_actions VALUES (%s,%s,1,%s,%s,%s,'pending',%s,NULL)",
                (action_id, state["run_id"], digest, Jsonb(arguments), call_id, pending["expires_at"]))
            state.update(status="waiting_approval", phase="approval", pending=pending)
            event(state, "waiting_approval", **pending)
            self._save(db, state, True)
            return state

    def approve(self, run_id, identity, approval):
        started = time.perf_counter()
        with self.connection() as db:
            state = self._row(db, run_id, identity, True)["state"]
            action = db.execute("SELECT * FROM ko_agent_actions WHERE action_id=%s AND run_id=%s FOR UPDATE",
                                (approval.action_id, run_id)).fetchone()
            if not action or action["version"] != approval.version or action["digest"] != approval.digest:
                raise AppError("APPROVAL_MISMATCH", "Approval does not bind the current action parameters", 409)
            if action["status"] in {"committed", "rejected"}:
                return state
            if state["status"] != "waiting_approval" or action["status"] != "pending":
                raise AppError("INVALID_STATE", "Action cannot be approved", 409)
            if action["expires_at"] < time.time():
                raise AppError("APPROVAL_EXPIRED", "Action expired; propose a fresh task", 409)
            if not approval.approve:
                db.execute("UPDATE ko_agent_actions SET status='rejected' WHERE action_id=%s", (action["action_id"],))
                state.update(status="cancelled", outcome="rejected", pending=None)
                event(state, "final", status="cancelled", outcome="rejected")
            else:
                # Lock current permission through the same transaction as the write.
                permission = db.execute("SELECT can_write FROM ko_agent_permissions WHERE tenant_id=%s AND user_id=%s FOR SHARE",
                    (identity.tenant_id, identity.user_id)).fetchone()
                if not permission or not permission["can_write"]:
                    raise AppError("PERMISSION_DENIED", "Current identity cannot create tickets", 403)
                result = {"ticket_id": uuid4().hex, "action_id": action["action_id"], "simulated": True,
                          "status": "committed", "approval_status": "approved", "created_at": time.time(),
                          "title": action["arguments"]["title"]}
                db.execute("INSERT INTO ko_agent_tickets VALUES (%s,%s,%s,%s,%s)",
                    (result["ticket_id"], action["action_id"], identity.tenant_id, identity.user_id, Jsonb(action["arguments"])))
                db.execute("UPDATE ko_agent_actions SET status='committed',result=%s WHERE action_id=%s",
                           (Jsonb(result), action["action_id"]))
                state["messages"].append({"role": "tool", "tool_call_id": action["call_id"], "content": dump(result)})
                state.update(status="running", phase="after_commit", pending=None)
                state["trace"].append({"name": "create_ticket", "call_id": action["call_id"], "arguments": action["arguments"],
                                       "result": result, "status": "committed", "duration_ms": (time.perf_counter()-started)*1000})
                event(state, "tool_end", name="create_ticket", call_id=action["call_id"], result=result)
            self._save(db, state, True)
            return state

    def revise_proposal(self, run_id, identity, revision):
        # Serialize edits with approval/cancel, and bind every version to its exact content.
        with self.connection() as db:
            state = self._row(db, run_id, identity, True)["state"]
            action = db.execute("SELECT * FROM ko_agent_actions WHERE action_id=%s AND run_id=%s FOR UPDATE",
                                (revision.action_id, run_id)).fetchone()
            if not action or action["version"] != revision.version or action["digest"] != revision.digest:
                raise AppError("APPROVAL_MISMATCH", "提案已变化，请刷新后重新确认。", 409)
            if state["status"] != "waiting_approval" or action["status"] != "pending":
                raise AppError("INVALID_STATE", "只能修改待审批的工单。", 409)
            if action["expires_at"] < time.time():
                raise AppError("APPROVAL_EXPIRED", "提案已过期，请发起新任务。", 409)
            # Match the same redaction boundary used for model-proposed tool arguments.
            arguments = json.loads(redact(dump(revision.arguments.model_dump())))
            if not arguments["title"].strip() or not arguments["description"].strip():
                raise AppError("INVALID_TICKET", "工单标题和描述不能为空。", 422)
            if arguments == action["arguments"]:
                return state
            version = action["version"] + 1
            digest = hashlib.sha256(dump({"run_id": run_id, "tool": "create_ticket",
                                         "arguments": arguments, "version": version}).encode()).hexdigest()
            pending = {**state["pending"], "version": version, "digest": digest, "arguments": arguments}
            db.execute("UPDATE ko_agent_actions SET version=%s,digest=%s,arguments=%s WHERE action_id=%s",
                       (version, digest, Jsonb(arguments), action["action_id"]))
            # On resume the model sees the exact arguments approved by the human.
            for message in state["messages"]:
                for call in message.get("tool_calls", []):
                    if call["id"] == action["call_id"]:
                        call["function"]["arguments"] = dump(arguments)
            signature = dump(["create_ticket", arguments])
            if signature not in state["seen"]:
                state["seen"].append(signature)
            state["pending"] = pending
            event(state, "proposal_revised", action_id=action["action_id"], version=version,
                  arguments=arguments, previous_version=action["version"])
            self._save(db, state, True)
            return state

    @staticmethod
    def ticket_view(row, detail=False):
        result, state = row["result"] or {}, row["state"]
        committed = next((e for e in state.get("events", []) if e.get("type") == "tool_end"
                          and e.get("result", {}).get("ticket_id") == row["ticket_id"]), {})
        item = {"ticket_id": row["ticket_id"], "action_id": row["action_id"], "run_id": row["run_id"],
                "title": row["arguments"].get("title", ""), "status": "created", "simulated": True,
                "created_at": result.get("created_at") or committed.get("time"),
                "user_id": row["user_id"], "approval_version": row["version"]}
        if detail:
            prior_trace = []
            for trace in state.get("trace", []):
                if trace.get("result", {}).get("ticket_id") == row["ticket_id"]:
                    break
                if trace.get("name") != "create_ticket":
                    prior_trace.append(trace)
            timeline = [e for e in state.get("events", []) if e["type"] == "accepted" or
                        (e["type"] in {"waiting_approval", "proposal_revised"} and
                         e.get("action_id") == row["action_id"]) or
                        (e["type"] == "tool_end" and e.get("result", {}).get("ticket_id") == row["ticket_id"])]
            item.update(description=row["arguments"].get("description", ""),
                        run_status=state["status"], run_outcome=state.get("outcome"),
                        trace=prior_trace, timeline=timeline)
        return item

    def list_tickets(self, identity, limit=30, offset=0):
        with self.connection() as db:
            db.execute("SET TRANSACTION READ ONLY")
            total = db.execute("SELECT count(*) AS n FROM ko_agent_tickets WHERE tenant_id=%s AND user_id=%s",
                               (identity.tenant_id, identity.user_id)).fetchone()["n"]
            rows = db.execute("""SELECT t.*,a.run_id,a.version,a.result,r.state
                FROM ko_agent_tickets t JOIN ko_agent_actions a USING(action_id)
                JOIN ko_agent_runs r ON r.run_id=a.run_id
                WHERE t.tenant_id=%s AND t.user_id=%s AND r.tenant_id=%s AND r.user_id=%s
                ORDER BY COALESCE((a.result->>'created_at')::double precision,r.created_at) DESC,t.ticket_id
                LIMIT %s OFFSET %s""", (identity.tenant_id, identity.user_id, identity.tenant_id, identity.user_id,
                                      limit, offset)).fetchall()
            return {"items": [self.ticket_view(r) for r in rows], "total": total,
                    "limit": limit, "offset": offset}

    def get_ticket(self, ticket_id, identity):
        with self.connection() as db:
            db.execute("SET TRANSACTION READ ONLY")
            row = db.execute("""SELECT t.*,a.run_id,a.version,a.result,r.state
                FROM ko_agent_tickets t JOIN ko_agent_actions a USING(action_id)
                JOIN ko_agent_runs r ON r.run_id=a.run_id
                WHERE t.ticket_id=%s AND t.tenant_id=%s AND t.user_id=%s
                    AND r.tenant_id=%s AND r.user_id=%s""",
                (ticket_id, identity.tenant_id, identity.user_id, identity.tenant_id, identity.user_id)).fetchone()
            if row is None:
                raise AppError("TICKET_NOT_FOUND", "工单不存在或当前身份无权查看。", 404)
            return self.ticket_view(row, detail=True)

    def cancel(self, run_id, identity):
        with self.connection() as db:
            state = self._row(db, run_id, identity, True)["state"]
            if state["status"] not in TERMINAL:
                state.update(status="cancelled", outcome="user_cancelled")
                db.execute("UPDATE ko_agent_actions SET status='cancelled' WHERE run_id=%s AND status='pending'", (run_id,))
                event(state, "final", status="cancelled", outcome="user_cancelled")
                self._save(db, state, True)
            return state

    def business(self, identity, query, ticket_id=None):
        if query not in {"device_status", "account_status", "ticket_status", "ticket_durations"} or (ticket_id and query != "ticket_status"):
            raise AppError("INVALID_BUSINESS_QUERY", "不支持的业务查询。", 422)
        with self.connection(readonly=True) as db:
            if query == "ticket_status":
                # A SELECT-only view excludes approval digests, raw messages and tool traces.
                scope = (identity.tenant_id, identity.user_id)
                rows = db.execute("""SELECT ticket_id,title,status,assignee,estimated_completion,created_at
                    FROM ko_ticket_business WHERE tenant_id=%s AND user_id=%s""" +
                    (" AND ticket_id=%s" if ticket_id else "") +
                    " ORDER BY created_at DESC NULLS LAST,ticket_id LIMIT 10",
                    scope + ((ticket_id,) if ticket_id else ())).fetchall()
                if ticket_id and not rows:
                    raise AppError("TICKET_NOT_FOUND", "工单不存在或当前身份无权查看。", 404)
                count = db.execute("SELECT count(*) AS n FROM ko_ticket_business WHERE tenant_id=%s AND user_id=%s",
                                   scope).fetchone()["n"]
                data = {"items": rows, "total": count, "limit": 10,
                        "scope": "specific" if ticket_id else "latest",
                        "note": "本地审批创建的工单；尚未接入派单和处理系统，不推测处理人或完成时间。"}
            else:
                row = db.execute("SELECT payload FROM ko_agent_business WHERE tenant_id=%s AND user_id=%s AND query=%s LIMIT 1",
                                 (identity.tenant_id, identity.user_id, query)).fetchone()
                if not row:
                    raise AppError("BUSINESS_NOT_FOUND", "No authorized simulated business record", 404)
                data = row["payload"]
            return {"simulated": True, "query": query, "data": data, "queried_at": time.time()}

    def business_overview(self, identity):
        sections = {}
        for query in ("device_status", "account_status", "ticket_status", "ticket_durations"):
            try:
                sections[query] = self.business(identity, query)
            except AppError as exc:
                if exc.code != "BUSINESS_NOT_FOUND":
                    raise
                sections[query] = {"error": {"code": exc.code, "message": "该身份暂无此类演示记录。"}}
        return {"simulated": True, "user_id": identity.user_id, "sections": sections}
