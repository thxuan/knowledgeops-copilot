"""PostgreSQL checkpoints, fenced leases, approvals and atomic local effects."""
import hashlib
import time
from contextlib import contextmanager
from uuid import uuid4
import psycopg
from app.db.postgres import connection_options
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from app.agent.contracts import SYSTEM, dump, event, redact
from app.core.errors import AppError

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
                PRIMARY KEY(tenant_id,user_id,query));''')

    def seed_demo(self, identity):
        """Explicit setup only; never restore revoked permissions on app startup."""
        with self.connection() as db:
            db.execute("INSERT INTO ko_agent_permissions VALUES (%s,%s,true) ON CONFLICT DO NOTHING",
                       (identity.tenant_id, identity.user_id))
            for query, payload in {"device_status": {"compliant": False, "reason": "模拟设备补丁待更新"},
                "ticket_durations": {"triage_minutes": 12, "resolution_minutes": 28, "target_minutes": 60}}.items():
                db.execute("INSERT INTO ko_agent_business VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                           (identity.tenant_id, identity.user_id, query, Jsonb(payload)))

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
                          "status": "committed", "approval_status": "approved"}
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

    def cancel(self, run_id, identity):
        with self.connection() as db:
            state = self._row(db, run_id, identity, True)["state"]
            if state["status"] not in TERMINAL:
                state.update(status="cancelled", outcome="user_cancelled")
                db.execute("UPDATE ko_agent_actions SET status='cancelled' WHERE run_id=%s AND status='pending'", (run_id,))
                event(state, "final", status="cancelled", outcome="user_cancelled")
                self._save(db, state, True)
            return state

    def business(self, identity, query):
        with self.connection(readonly=True) as db:
            row = db.execute("SELECT payload FROM ko_agent_business WHERE tenant_id=%s AND user_id=%s AND query=%s LIMIT 1",
                             (identity.tenant_id, identity.user_id, query)).fetchone()
            if not row:
                raise AppError("BUSINESS_NOT_FOUND", "No authorized simulated business record", 404)
            return {"simulated": True, "query": query, "data": row["payload"]}
