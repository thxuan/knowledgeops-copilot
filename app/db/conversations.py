"""Owner-scoped state, optimistic turn commits and lossless extractive summaries."""
import json
import hashlib
import time
from uuid import uuid4
from app.core.errors import AppError
from app.generation.context import dump

class ConversationRepository:
    def __init__(self, ingestion):
        self.ingestion = ingestion

    def initialize(self):
        with self.ingestion.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(52007001)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '{}',
                    created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    message_id BIGSERIAL PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id), role TEXT NOT NULL, content TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL
                );
                CREATE INDEX IF NOT EXISTS conversation_message_order
                    ON conversation_messages(conversation_id, message_id);
                CREATE TABLE IF NOT EXISTS workbench_contexts (
                    context_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    question TEXT NOT NULL, snapshot TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL
                );
            """)

    @staticmethod
    def _owned(db, conversation_id, identity, lock=False):
        row = db.execute("""SELECT * FROM conversations
            WHERE conversation_id=%s AND tenant_id=%s AND user_id=%s""" + (" FOR UPDATE" if lock else ""),
            (conversation_id, identity.tenant_id, identity.user_id)).fetchone()
        if row is None:
            raise AppError("NOT_FOUND", "Conversation not found", 404)
        return dict(row)

    def load(self, conversation_id, identity):
        with self.ingestion.connection() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            if conversation_id is None:
                conversation_id = str(uuid4())
                now = time.time()
                db.execute("INSERT INTO conversations VALUES (%s,%s,%s,0,'{}',%s,%s)",
                           (conversation_id, identity.tenant_id, identity.user_id, now, now))
            row = self._owned(db, conversation_id, identity)
            recent = [dict(m) for m in db.execute("""SELECT * FROM conversation_messages
                WHERE conversation_id=%s ORDER BY message_id DESC LIMIT 20""", (conversation_id,))]
            return {**row, "summary": json.loads(row["summary"]), "messages": list(reversed(recent))}

    @staticmethod
    def evidence_current(db, evidence, identity):
        for hit in sorted(evidence, key=lambda h: (h["citation"].get("document_id", ""), h["citation"]["chunk_id"])):
            citation = hit["citation"]
            row = db.execute("""SELECT 1 FROM chunks c
                JOIN document_chunk_heads h ON h.chunk_set_id=c.chunk_set_id
                JOIN documents d ON d.document_id=h.document_id
                JOIN document_access a ON a.document_id=d.document_id
                JOIN ingestion_jobs j ON j.document_id=d.document_id
                WHERE c.chunk_id=%s AND c.chunk_set_id=%s AND d.tenant_id=%s AND a.user_id=%s
                AND d.active=1 AND j.status='succeeded' FOR SHARE OF d, a, h, j""",
                (citation["chunk_id"], citation["chunk_set_id"], identity.tenant_id, identity.user_id)).fetchone()
            if row is None:
                raise AppError("EVIDENCE_CHANGED", "Evidence changed or access was revoked; ask again", 409)

    def capture_context(self, snapshot, question, identity):
        history = {**{key: snapshot[key] for key in ("conversation_id", "revision", "summary")},
                   "messages": [{key: message[key] for key in ("message_id", "role", "content")}
                                for message in snapshot["messages"] if message["role"] == "user"]}
        context_id = str(uuid4())
        with self.ingestion.connection() as db:
            self._owned(db, snapshot["conversation_id"], identity)
            db.execute("INSERT INTO workbench_contexts VALUES (%s,%s,%s,%s,%s,%s)",
                       (context_id, identity.tenant_id, identity.user_id, question, dump(history), time.time()))
        return context_id, history

    def replay_context(self, context_id, question, identity):
        with self.ingestion.connection() as db:
            row = db.execute("SELECT question,snapshot FROM workbench_contexts WHERE context_id=%s AND tenant_id=%s AND user_id=%s",
                             (context_id, identity.tenant_id, identity.user_id)).fetchone()
            if row is None:
                raise AppError("NOT_FOUND", "原上下文不存在或无权访问。", 404)
            if row["question"] != question:
                raise AppError("REPLAY_QUESTION_MISMATCH", "原上下文重跑必须使用原问题。", 422)
            return json.loads(row["snapshot"])

    @staticmethod
    def context_view(context_id, snapshot):
        history = {key: snapshot[key] for key in ("messages", "summary")}
        fingerprint = hashlib.sha256(dump(history).encode()).hexdigest()
        return {"id": context_id, "fingerprint": fingerprint, "revision": snapshot["revision"], **history}

    def check_evidence(self, evidence, identity):
        with self.ingestion.connection() as db:
            db.execute("BEGIN")
            self.evidence_current(db, evidence, identity)

    def commit(self, snapshot, identity, question, response, evidence, summary_budget):
        with self.ingestion.connection() as db:
            db.execute("BEGIN")
            row = self._owned(db, snapshot["conversation_id"], identity, lock=True)
            if row["revision"] != snapshot["revision"]:
                raise AppError("CONVERSATION_CONFLICT", "Another turn completed; retry with updated history", 409)
            # Authorization and message commit use the same PostgreSQL transaction with row locks.
            self.evidence_current(db, evidence, identity)
            now = time.time()
            for role, content, payload in [("user", question, {}), ("assistant", response["answer"], {**response, "_evidence": [h["citation"] for h in evidence]})]:
                db.execute("""INSERT INTO conversation_messages
                    (conversation_id,role,content,payload,created_at) VALUES (%s,%s,%s,%s,%s)""",
                    (row["conversation_id"], role, content, dump(payload), now))
            total = db.execute("""SELECT count(*) AS value FROM conversation_messages
                WHERE conversation_id=%s AND role='user'""", (row["conversation_id"],)).fetchone()["value"]
            cutoff = db.execute("""SELECT min(message_id) AS value FROM
                (SELECT message_id FROM conversation_messages WHERE conversation_id=%s
                 ORDER BY message_id DESC LIMIT 20) recent_messages""", (row["conversation_id"],)).fetchone()["value"]
            records = []
            for message in db.execute("""SELECT message_id, content FROM conversation_messages
                WHERE conversation_id=%s AND role='user' AND message_id<%s ORDER BY message_id DESC LIMIT 20""",
                (row["conversation_id"], cutoff)):
                item = {"message_id": message["message_id"], "text": message["content"]}
                if len(dump([item, *records]).encode("utf-8")) > summary_budget:
                    break  # Never truncate a number/entity/negation or skip a later correction.
                records.insert(0, item)
            summary = {"strategy": "extractive-user-records-v1", "records": records,
                       "total_user_messages": total, "through_message_id": cutoff - 1,
                       "omitted_old_records": max(0, total - 10 - len(records))}
            db.execute("""UPDATE conversations SET revision=revision+1, summary=%s, updated_at=%s
                WHERE conversation_id=%s""", (dump(summary), now, row["conversation_id"]))

    def read(self, conversation_id, identity, after=0, limit=50):
        with self.ingestion.connection() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            row = self._owned(db, conversation_id, identity)
            messages = []
            for message in db.execute("""SELECT * FROM conversation_messages WHERE conversation_id=%s
                AND message_id>%s ORDER BY message_id LIMIT %s""", (conversation_id, after, limit)):
                item = dict(message)
                item["payload"] = json.loads(item["payload"])
                item.pop("conversation_id")
                item["redacted"] = False
                if item["role"] == "assistant":
                    try:
                        refs = item["payload"].pop("_evidence", item["payload"].get("citations", []))
                        self.evidence_current(db, [{"citation": c} for c in refs], identity)
                    except AppError:
                        item["content"] = "原回答的证据已失效或无权访问，请重新提问。"
                        item["payload"] = {"status": "redacted", "citations": [], "claims": []}
                        item["redacted"] = True
                messages.append(item)
            return {"conversation_id": conversation_id, "revision": row["revision"],
                    "summary": json.loads(row["summary"]), "messages": messages,
                    "next_after": messages[-1]["message_id"] if messages else after}


    def attach_agent_turn(self, conversation_id, identity, message, run):
        with self.ingestion.connection() as db:
            self._owned(db, conversation_id, identity, lock=True)
            exists = db.execute("SELECT 1 FROM conversation_messages WHERE conversation_id=%s AND payload::jsonb->>'agent_run_id'=%s", (conversation_id, run["run_id"])).fetchone()
            if exists:
                return
            now = time.time()
            for role, content in [("user", message), ("assistant", "任务正在执行。")]:
                db.execute("INSERT INTO conversation_messages (conversation_id,role,content,payload,created_at) VALUES (%s,%s,%s,%s,%s)",
                    (conversation_id, role, content, dump({"agent_run_id": run["run_id"], "status": "running"}), now))
            db.execute("UPDATE conversations SET revision=revision+1, updated_at=%s WHERE conversation_id=%s", (now, conversation_id))

    def sync_agent_turns(self, conversation_id, identity, require_finished=True):
        with self.ingestion.connection() as db:
            self._owned(db, conversation_id, identity, lock=True)
            rows = db.execute("SELECT message_id,payload FROM conversation_messages WHERE conversation_id=%s AND role='assistant' AND payload::jsonb ? 'agent_run_id'", (conversation_id,)).fetchall()
            changed = False
            for row in rows:
                payload = json.loads(row["payload"])
                run = db.execute("SELECT state FROM ko_agent_runs WHERE run_id=%s AND tenant_id=%s AND user_id=%s", (payload["agent_run_id"],identity.tenant_id,identity.user_id)).fetchone()
                if not run:
                    continue
                state = run["state"]
                if state["status"] not in {"completed", "failed", "cancelled"}:
                    if require_finished:
                        raise AppError("ACTIVE_TASK", "请先完成或取消当前会话中未结束的任务。", 409)
                    continue
                if payload.get("status") == state["status"]:
                    continue
                # Preserve evidence lineage for later history ACL checks.
                citations = []
                for trace in state.get("trace", []):
                    result = trace.get("result") or {}
                    for hit in result.get("untrusted_evidence", []) if isinstance(result, dict) else []:
                        if hit.get("citation"):
                            citations.append(hit["citation"])
                payload.update(status=state["status"], _evidence=citations)
                answer = state.get("answer") or ("任务已取消。" if state["status"] == "cancelled" else "任务执行失败：" + str(state.get("outcome", "未知错误")))
                db.execute("UPDATE conversation_messages SET content=%s,payload=%s WHERE message_id=%s", (answer,dump(payload),row["message_id"]))
                changed = True
            if changed:
                db.execute("UPDATE conversations SET revision=revision+1,updated_at=%s WHERE conversation_id=%s", (time.time(),conversation_id))
