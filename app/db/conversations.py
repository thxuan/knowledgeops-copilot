"""Owner-scoped state, optimistic turn commits and lossless extractive summaries."""
import json
import base64
import hashlib
import time
from uuid import uuid4
from app.core.errors import AppError
from app.generation.context import dump, continuation_context
from app.db.postgres import database_identity

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
            db.execute("""ALTER TABLE conversations ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '';
                ALTER TABLE conversations ADD COLUMN IF NOT EXISTS archived INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE conversations ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'chat';
                CREATE INDEX IF NOT EXISTS conversation_owner_updated ON conversations(tenant_id,user_id,archived,updated_at DESC,conversation_id DESC);
            """)

    def list(self, identity, limit=20, cursor=None, archived=False):
        with self.ingestion.connection() as db:
            scope = db.execute("SELECT current_database() AS db,current_schema() AS schema").fetchone()
            workspace = hashlib.sha256(dump([database_identity(self.ingestion.database_url),dict(scope),identity.tenant_id]).encode()).hexdigest()
            params = [identity.tenant_id,identity.user_id,int(archived)]
            boundary = ''
            if cursor:
                try:
                    value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                    if not isinstance(value,list) or len(value)!=2 or type(value[0]) not in (int,float) or not isinstance(value[1],str):
                        raise ValueError()
                    boundary = ' AND (c.updated_at,c.conversation_id)<(%s,%s)'
                    params.extend(value)
                except Exception:
                    raise AppError('INVALID_CURSOR','历史会话分页标识无效，请刷新列表。',422) from None
            params.append(limit+1)
            rows = db.execute("""SELECT c.conversation_id,c.title,c.archived,c.created_at,c.updated_at,
                first_message.content AS first_question
                FROM conversations c JOIN LATERAL (
                    SELECT content FROM conversation_messages WHERE conversation_id=c.conversation_id AND role='user' ORDER BY message_id LIMIT 1
                ) first_message ON true
                WHERE c.tenant_id=%s AND c.user_id=%s AND c.archived=%s AND c.purpose='chat'""" + boundary +
                ' ORDER BY c.updated_at DESC,c.conversation_id DESC LIMIT %s',params).fetchall()
            items=[dict(row) for row in rows[:limit]]
            for item in items:
                item['title']=item['title'] or ' '.join(item.pop('first_question').split())[:60] or '未命名会话'
                item.pop('first_question',None);item['archived']=bool(item['archived'])
            next_cursor=None
            if len(rows)>limit:
                last=rows[limit-1]
                next_cursor=base64.urlsafe_b64encode(dump([last['updated_at'],last['conversation_id']]).encode()).decode()
            return {'items':items,'next_cursor':next_cursor,'workspace_id':workspace,'user_id':identity.user_id}

    def update(self, conversation_id, identity, title=None, archived=None):
        with self.ingestion.connection() as db:
            self._owned(db,conversation_id,identity,True)
            if archived is True and db.execute("SELECT to_regclass('ko_agent_runs') AS name").fetchone()['name']:
                active=db.execute("""SELECT 1 FROM ko_agent_runs WHERE tenant_id=%s AND user_id=%s
                    AND state->>'conversation_id'=%s AND state->>'status' IN ('running','waiting_approval','interrupted') LIMIT 1""",
                    (identity.tenant_id,identity.user_id,conversation_id)).fetchone()
                if active:raise AppError('ACTIVE_TASK','请先完成或取消此会话中的任务，再归档。',409)
            if title is not None:
                db.execute('UPDATE conversations SET title=%s WHERE conversation_id=%s',(title.strip(),conversation_id))
            if archived is not None:
                db.execute('UPDATE conversations SET archived=%s WHERE conversation_id=%s',(int(archived),conversation_id))
            return {'conversation_id':conversation_id,'updated':True}

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
                db.execute("INSERT INTO conversations(conversation_id,tenant_id,user_id,revision,summary,created_at,updated_at) VALUES (%s,%s,%s,0,'{}',%s,%s)",
                           (conversation_id, identity.tenant_id, identity.user_id, now, now))
            row = self._owned(db, conversation_id, identity)
            if row['archived']:
                raise AppError('CONVERSATION_ARCHIVED','请先取消归档，再继续此会话。',409)
            recent = [dict(m) for m in db.execute("""SELECT * FROM conversation_messages
                WHERE conversation_id=%s ORDER BY message_id DESC LIMIT 20""", (conversation_id,))]
            return {**row, "summary": json.loads(row["summary"]), "messages": list(reversed(recent)),
                    "pending_follow_up": self.pending_follow_up(db, list(reversed(recent)), identity)}

    @classmethod
    def pending_follow_up(cls, db, messages, identity):
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message['role'] == 'user':
                # A failed new topic must not resurrect an older pending question.
                if continuation_context(message['content'], {'active': True}) is None:
                    return None
                continue
            if message['role'] != 'assistant':
                continue
            payload = json.loads(message['payload'])
            if payload.get('status') == 'rejected':
                # Validation failure does not consume a previous clarification.
                continue
            question = payload.get('follow_up')
            if not isinstance(question, str) or not question.strip():
                return None  # Completed/refused turns end the old clarification.
            try:
                refs = payload.get('_evidence', payload.get('citations', []))
                cls.evidence_current(db, [{'citation': c} for c in refs], identity)
            except AppError:
                return None
            user = next((m for m in reversed(messages[:index]) if m['role'] == 'user'), None)
            if not user:
                return None
            origin = payload.get('context', {}).get('follow_up_origin') or user['content']
            return {'question': question, 'original_question': origin, 'source_message_id': message['message_id']}
        return None

    def checked_follow_up(self, db, pending, identity):
        if not pending:
            return None, []
        message = db.execute("""SELECT m.* FROM conversation_messages m JOIN conversations c USING(conversation_id)
            WHERE m.message_id=%s AND m.role='assistant' AND c.tenant_id=%s AND c.user_id=%s""",
            (pending.get('source_message_id'), identity.tenant_id, identity.user_id)).fetchone()
        if not message:
            return None, []
        user = db.execute("SELECT * FROM conversation_messages WHERE conversation_id=%s AND role='user' AND message_id<%s ORDER BY message_id DESC LIMIT 1",
                          (message['conversation_id'], message['message_id'])).fetchone()
        checked = self.pending_follow_up(db, [dict(user), dict(message)] if user else [], identity)
        if checked != pending:
            return None, []
        payload = json.loads(message['payload'])
        return checked, payload.get('_evidence', payload.get('citations', []))

    def check_follow_up(self, pending, identity):
        with self.ingestion.connection() as db:
            return self.checked_follow_up(db, pending, identity)[0]

    @staticmethod
    def evidence_current(db, evidence, identity):
        for hit in sorted(evidence, key=lambda h: (h["citation"].get("document_id", ""), h["citation"]["chunk_id"])):
            citation = hit["citation"]
            row = db.execute("""SELECT 1 FROM chunks c
                JOIN document_chunk_heads h ON h.chunk_set_id=c.chunk_set_id
                JOIN documents d ON d.document_id=h.document_id
                JOIN ingestion_jobs j ON j.document_id=d.document_id
                WHERE c.chunk_id=%s AND c.chunk_set_id=%s AND d.tenant_id=%s AND (EXISTS(SELECT 1 FROM document_access a WHERE a.document_id=d.document_id AND a.user_id=%s) OR %s)
                AND d.active=1 AND d.publication_state='published' AND d.withdrawn=0 AND j.status='succeeded' FOR SHARE OF d, h, j""",
                (citation["chunk_id"], citation["chunk_set_id"], identity.tenant_id, identity.user_id, 'manage_platform' in identity.platform_capabilities)).fetchone()
            if row is None:
                raise AppError("EVIDENCE_CHANGED", "Evidence changed or access was revoked; ask again", 409)

    def capture_context(self, snapshot, question, identity, purpose="chat"):
        history = {**{key: snapshot[key] for key in ("conversation_id", "revision", "summary")},
                   "pending_follow_up": snapshot.get("pending_follow_up"),
                   "messages": [{key: message[key] for key in ("message_id", "role", "content")}
                                for message in snapshot["messages"] if message["role"] == "user"]}
        context_id = str(uuid4())
        with self.ingestion.connection() as db:
            self._owned(db, snapshot["conversation_id"], identity)
            if purpose=='evaluation' and snapshot['revision']==0:
                db.execute("UPDATE conversations SET purpose='evaluation' WHERE conversation_id=%s",(snapshot['conversation_id'],))
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
            snapshot = json.loads(row["snapshot"])
            if snapshot.get("pending_follow_up"):
                snapshot["pending_follow_up"] = self.checked_follow_up(db, snapshot["pending_follow_up"], identity)[0]
            return snapshot

    @staticmethod
    def context_view(context_id, snapshot):
        history = {key: snapshot[key] for key in ("messages", "summary")}
        if snapshot.get("pending_follow_up"):
            history["pending_follow_up"] = snapshot["pending_follow_up"]
        fingerprint = hashlib.sha256(dump(history).encode()).hexdigest()
        return {"id": context_id, "fingerprint": fingerprint, "revision": snapshot["revision"], **history}

    def check_evidence(self, evidence, identity):
        with self.ingestion.connection() as db:
            db.execute("BEGIN")
            self.evidence_current(db, evidence, identity)

    def commit(self, snapshot, identity, question, response, evidence, summary_budget, pending_follow_up=None):
        with self.ingestion.connection() as db:
            db.execute("BEGIN")
            row = self._owned(db, snapshot["conversation_id"], identity, lock=True)
            if row['archived']:
                raise AppError('CONVERSATION_ARCHIVED','会话已归档，请取消归档后重试。',409)
            if row["revision"] != snapshot["revision"]:
                raise AppError("CONVERSATION_CONFLICT", "Another turn completed; retry with updated history", 409)
            # Authorization and message commit use the same PostgreSQL transaction with row locks.
            self.evidence_current(db, evidence, identity)
            inherited = []
            if pending_follow_up:
                checked, inherited = self.checked_follow_up(db, pending_follow_up, identity)
                if not checked:
                    raise AppError('EVIDENCE_CHANGED', '原追问的依据已失效或无权访问，请重新提问。', 409)
            refs = list({(c['chunk_id'], c['chunk_set_id']): c for c in [*[h['citation'] for h in evidence], *inherited]}.values())
            now = time.time()
            assistant_text = "\n\n".join(part for part in (response["answer"], response.get("follow_up")) if part)
            for role, content, payload in [("user", question, {}), ("assistant", assistant_text, {**response, "_evidence": refs})]:
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

    def read(self, conversation_id, identity, after=0, limit=50, before=None, latest=False):
        with self.ingestion.connection() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            row = self._owned(db, conversation_id, identity)
            messages = []
            descending=latest or before is not None
            if descending:
                condition=' AND message_id<%s' if before is not None else ''
                params=[conversation_id]+([before] if before is not None else [])+[limit+1]
                rows=db.execute('SELECT * FROM conversation_messages WHERE conversation_id=%s'+condition+' ORDER BY message_id DESC LIMIT %s',params).fetchall()
                has_more=len(rows)>limit;rows=list(reversed(rows[:limit]))
            else:
                rows=db.execute("""SELECT * FROM conversation_messages WHERE conversation_id=%s
                    AND message_id>%s ORDER BY message_id LIMIT %s""",(conversation_id,after,limit+1)).fetchall()
                has_more=len(rows)>limit;rows=rows[:limit]
            for message in rows:
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
            first=db.execute("SELECT content FROM conversation_messages WHERE conversation_id=%s AND role='user' ORDER BY message_id LIMIT 1",(conversation_id,)).fetchone()
            title=row['title'] or (' '.join(first['content'].split())[:60] if first else '未命名会话')
            return {"conversation_id": conversation_id, "revision": row["revision"],
                    "summary": json.loads(row["summary"]), "messages": messages,
                    "title":title,"archived":bool(row['archived']),"has_more":has_more,
                    "next_before":messages[0]['message_id'] if descending and has_more and messages else None,
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
