"""Durable intake, unique constraints, atomic claims and fenced completion."""

import json
import time
from app.db.postgres import connection
from uuid import uuid4

from app.core.errors import AppError
from app.core.identity import Identity
from app.ingestion.parser import ParsedDocument


class IngestionRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def connection(self):
        return connection(self.database_url)

    def initialize(self) -> None:
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(52003001)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    filename TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    file_hash TEXT NOT NULL, parser_version TEXT NOT NULL,
                    source TEXT NOT NULL, version TEXT, uploaded_by TEXT NOT NULL,
                    content BYTEA NOT NULL, text TEXT, title TEXT, metadata TEXT,
                    created_at DOUBLE PRECISION NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
                    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                    UNIQUE(tenant_id, file_hash, parser_version)
                );
                CREATE TABLE IF NOT EXISTS document_access (
                    document_id TEXT NOT NULL REFERENCES documents(document_id), user_id TEXT NOT NULL,
                    PRIMARY KEY(document_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS ingestion_jobs (
                    task_id TEXT PRIMARY KEY, document_id TEXT NOT NULL UNIQUE REFERENCES documents(document_id),
                    status TEXT NOT NULL, stage TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    lease_until DOUBLE PRECISION, claim_token TEXT, error_code TEXT, error_message TEXT,
                    created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingestion_events (
                    event_id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL REFERENCES ingestion_jobs(task_id),
                    status TEXT NOT NULL, stage TEXT NOT NULL, attempt INTEGER NOT NULL,
                    error_code TEXT, created_at DOUBLE PRECISION NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_schedule ON ingestion_jobs(status, lease_until);
            """)

        from app.db.document_versions import DocumentVersions
        DocumentVersions(self).initialize()
        from app.db.index_jobs import IndexJobs
        IndexJobs(self).initialize()

    @staticmethod
    def event(db, task_id: str) -> None:
        db.execute("""
            INSERT INTO ingestion_events(task_id, status, stage, attempt, error_code, created_at)
            SELECT task_id, status, stage, attempts, error_code, updated_at
            FROM ingestion_jobs WHERE task_id = %s
        """, (task_id,))

    def submit(self, *, identity: Identity, filename: str, content: bytes, file_hash: str,
               parser_version: str, source: str, version: str | None) -> dict:
        now = time.time()
        with self.connection() as db:
            # Serialize the same upload key; unrelated uploads proceed independently.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                       (json.dumps([identity.tenant_id, file_hash, parser_version]),))
            document_id, task_id = str(uuid4()), str(uuid4())
            cursor = db.execute("""
                INSERT INTO documents(document_id, tenant_id, filename, size_bytes, file_hash,
                    parser_version, source, version, uploaded_by, content, created_at, publication_state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft')
                ON CONFLICT(tenant_id, file_hash, parser_version) DO NOTHING
            """, (document_id, identity.tenant_id, filename, len(content), file_hash,
                  parser_version, source, version, identity.user_id, content, now))
            duplicate = cursor.rowcount == 0
            if duplicate:
                document_id = db.execute("""
                    SELECT document_id FROM documents
                    WHERE tenant_id = %s AND file_hash = %s AND parser_version = %s
                """, (identity.tenant_id, file_hash, parser_version)).fetchone()["document_id"]
                existing = db.execute("""SELECT d.active FROM documents d
                    JOIN document_access a USING(document_id)
                    WHERE d.document_id=%s AND a.user_id=%s FOR SHARE OF d, a""",
                    (document_id, identity.user_id)).fetchone()
                if existing is None or not existing["active"]:
                    raise AppError("NOT_FOUND", "Document not found", 404)
            else:
                db.execute("UPDATE documents SET updated_at=%s WHERE document_id=%s", (now, document_id))
                db.execute("""
                    INSERT INTO ingestion_jobs(task_id, document_id, status, stage, created_at, updated_at)
                    VALUES (%s, %s, 'queued', 'validated', %s, %s)
                """, (task_id, document_id, now, now))
                self.event(db, task_id)
            # Duplicate uploads cannot grant access or undo revocation.
            db.execute("INSERT INTO document_access VALUES (%s, %s) ON CONFLICT DO NOTHING",
                       (document_id, identity.user_id))
            row = db.execute("""
                SELECT d.document_id, d.filename, d.size_bytes, d.file_hash, d.source, d.version,
                    d.parser_version, j.task_id, j.status
                FROM documents d JOIN ingestion_jobs j USING(document_id) WHERE d.document_id = %s
            """, (document_id,)).fetchone()
            return {**dict(row), "duplicate": duplicate}

    def get_task(self, task_id: str, identity: Identity) -> dict:
        with self.connection() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            row = db.execute("""
                SELECT j.*, d.filename, d.file_hash, d.source, d.version, d.parser_version,
                    d.title, length(d.text) AS text_length
                FROM ingestion_jobs j JOIN documents d USING(document_id)
                WHERE j.task_id = %s AND d.tenant_id = %s AND d.active=1 AND
                    (d.uploaded_by=%s OR %s OR (d.publication_state='published' AND EXISTS
                    (SELECT 1 FROM document_access a WHERE a.document_id=d.document_id AND a.user_id=%s)))
            """, (task_id, identity.tenant_id, identity.user_id, 'manage_platform' in identity.platform_capabilities,identity.user_id)).fetchone()
            if row is None:
                raise AppError("NOT_FOUND", "Task not found", 404)
            result = dict(row)
            result.pop("claim_token")
            result["events"] = [dict(event) for event in db.execute("""
                SELECT status, stage, attempt, error_code, created_at FROM ingestion_events
                WHERE task_id = %s ORDER BY event_id
            """, (task_id,))]
            return result

    def retry(self, task_id: str, identity: Identity, max_attempts: int) -> dict:
        with self.connection() as db:
            allowed = db.execute("""SELECT 1 FROM ingestion_jobs j
                JOIN documents d USING(document_id)
                WHERE j.task_id=%s AND d.tenant_id=%s AND (d.uploaded_by=%s OR %s) AND d.active=1
                FOR SHARE OF d""", (task_id, identity.tenant_id, identity.user_id, 'manage_platform' in identity.platform_capabilities)).fetchone()
            if allowed is None:
                raise AppError("NOT_FOUND", "Task not found", 404)
            cursor = db.execute("""
                UPDATE ingestion_jobs SET status = 'queued', stage = 'validated',
                    error_code = NULL, error_message = NULL, lease_until = NULL,
                    claim_token = NULL, updated_at = %s
                WHERE task_id = %s AND status = 'failed' AND attempts < %s
            """, (time.time(), task_id, max_attempts))
            if not cursor.rowcount:
                raise AppError("RETRY_NOT_ALLOWED", "Only failed tasks below the attempt limit can retry", 409)
            self.event(db, task_id)
        return self.get_task(task_id, identity)

    def claim(self, lease_seconds: float, max_attempts: int, *, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN")
            row = db.execute("""
                SELECT * FROM ingestion_jobs WHERE status = 'queued'
                    OR (status = 'processing' AND lease_until <= %s)
                ORDER BY created_at, task_id LIMIT 1 FOR UPDATE SKIP LOCKED
            """, (now,)).fetchone()
            if row is None:
                return None
            if row["attempts"] >= max_attempts:
                db.execute("""
                    UPDATE ingestion_jobs SET status = 'failed', error_code = 'ATTEMPT_LIMIT',
                        error_message = 'Processing attempt limit reached', claim_token = NULL,
                        lease_until = NULL, updated_at = %s WHERE task_id = %s
                """, (now, row["task_id"]))
                self.event(db, row["task_id"])
                return None
            token = str(uuid4())
            db.execute("""
                UPDATE ingestion_jobs SET status = 'processing', stage = 'parsing',
                    attempts = attempts + 1, claim_token = %s, lease_until = %s, updated_at = %s,
                    error_code = NULL, error_message = NULL WHERE task_id = %s
            """, (token, now + lease_seconds, now, row["task_id"]))
            self.event(db, row["task_id"])
            document = dict(db.execute("SELECT * FROM documents WHERE document_id = %s",
                                       (row["document_id"],)).fetchone())
            return {**document, "task_id": row["task_id"], "claim_token": token}

    def advance(self, task: dict, stage: str) -> bool:
        with self.connection() as db:
            cursor = db.execute("""
                UPDATE ingestion_jobs SET stage = %s, updated_at = %s
                WHERE task_id = %s AND claim_token = %s AND status = 'processing'
            """, (stage, time.time(), task["task_id"], task["claim_token"]))
            if cursor.rowcount:
                self.event(db, task["task_id"])
            return bool(cursor.rowcount)

    def complete(self, task: dict, parsed: ParsedDocument) -> bool:
        with self.connection() as db:
            cursor = db.execute("""
                UPDATE ingestion_jobs SET status = 'succeeded', stage = 'completed',
                    lease_until = NULL, claim_token = NULL, updated_at = %s
                WHERE task_id = %s AND claim_token = %s AND status = 'processing'
            """, (time.time(), task["task_id"], task["claim_token"]))
            if not cursor.rowcount:
                return False
            db.execute("""
                UPDATE documents SET text = %s, title = %s, metadata = %s, version = COALESCE(version, %s)
                WHERE document_id = %s
            """, (parsed.text, parsed.title, json.dumps(parsed.metadata, ensure_ascii=False),
                  parsed.metadata.get("version", "unversioned"), task["document_id"]))
            self.event(db, task["task_id"])
            return True

    def fail(self, task: dict, code: str, message: str) -> None:
        with self.connection() as db:
            cursor = db.execute("""
                UPDATE ingestion_jobs SET status = 'failed', error_code = %s, error_message = %s,
                    claim_token = NULL, lease_until = NULL, updated_at = %s
                WHERE task_id = %s AND claim_token = %s AND status = 'processing'
            """, (code, message, time.time(), task["task_id"], task["claim_token"]))
            if cursor.rowcount:
                self.event(db, task["task_id"])

