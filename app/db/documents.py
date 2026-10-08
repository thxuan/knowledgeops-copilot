"""Owner-controlled lifecycle; PostgreSQL is the authority for active versions and ACLs."""
import hashlib
import json
import psycopg
import time
from app.core.errors import AppError
from app.core.identity import LOCAL_IDENTITIES
from app.ingestion.parser import PARSER_VERSION, clean_document, parse_document, ParseError


class DocumentRepository:
    def __init__(self, ingestion):
        self.ingestion = ingestion

    @staticmethod
    def _owner(db, document_id, identity):
        row = db.execute("""SELECT d.*, j.status FROM documents d
            JOIN ingestion_jobs j USING(document_id)
            WHERE d.document_id=%s AND d.tenant_id=%s AND d.uploaded_by=%s AND d.active=1 FOR UPDATE OF d""",
            (document_id, identity.tenant_id, identity.user_id)).fetchone()
        if row is None:
            raise AppError("NOT_FOUND", "Document not found", 404)
        return row

    def check_owner(self, document_id, identity):
        with self.ingestion.connection() as db:
            self._owner(db, document_id, identity)

    def update(self, document_id, identity, filename, content, version, expected_version):
        try:
            parsed = clean_document(parse_document(filename, content))
        except ParseError as exc:
            raise AppError(exc.code, str(exc), 422) from exc
        extension = filename.lower().rsplit(".", 1)[-1]
        with self.ingestion.connection() as db:
            db.execute("BEGIN")
            row = self._owner(db, document_id, identity)
            if row["status"] != "succeeded":
                raise AppError("DOCUMENT_NOT_READY", "Parsing must succeed before updating", 409)
            if row["version"] != expected_version or version == row["version"]:
                raise AppError("VERSION_CONFLICT", "Expected current version and a new version label", 409)
            try:
                db.execute("""UPDATE documents SET filename=%s,content=%s,size_bytes=%s,file_hash=%s,
                    parser_version=%s,text=%s,title=%s,metadata=%s,version=%s,revision=revision+1,updated_at=%s
                    WHERE document_id=%s""", (filename, content, len(content), hashlib.sha256(content).hexdigest(),
                    PARSER_VERSION + ":" + extension, parsed.text, parsed.title,
                    json.dumps(parsed.metadata, ensure_ascii=False), version, time.time(), document_id))
            except psycopg.errors.UniqueViolation as exc:
                raise AppError("DUPLICATE_CONTENT", "Content already belongs to another document", 409) from exc
            # Invalidate old search and citations atomically, before any re-embedding.
            db.execute("DELETE FROM document_chunk_heads WHERE document_id=%s", (document_id,))
            return {"document_id": document_id, "version": version, "revision": row["revision"] + 1,
                    "active": True, "index_required": True}

    def delete(self, document_id, identity):
        with self.ingestion.connection() as db:
            db.execute("BEGIN")
            self._owner(db, document_id, identity)
            db.execute("UPDATE documents SET active=0,revision=revision+1,updated_at=%s WHERE document_id=%s",
                       (time.time(), document_id))
            db.execute("DELETE FROM document_chunk_heads WHERE document_id=%s", (document_id,))

    def permissions(self, document_id, identity, user_ids):
        users = [LOCAL_IDENTITIES.get(user_id) for user_id in user_ids]
        if any(user is None or user.tenant_id != identity.tenant_id for user in users):
            raise AppError("INVALID_PRINCIPAL", "Only users in the server-owned tenant are allowed", 422)
        with self.ingestion.connection() as db:
            db.execute("BEGIN")
            self._owner(db, document_id, identity)
            db.execute("DELETE FROM document_access WHERE document_id=%s", (document_id,))
            db.cursor().executemany("INSERT INTO document_access VALUES (%s,%s)", [(document_id, u) for u in sorted(set(user_ids))])
            return {"document_id": document_id, "user_ids": sorted(set(user_ids))}

    def citation(self, chunk_id, chunk_set_id, identity):
        with self.ingestion.connection() as db:
            db.execute("BEGIN")
            row = db.execute("""SELECT c.payload FROM chunks c
                JOIN document_chunk_heads h ON h.chunk_set_id=c.chunk_set_id
                JOIN documents d ON d.document_id=h.document_id
                JOIN document_access a ON a.document_id=d.document_id
                JOIN ingestion_jobs j ON j.document_id=d.document_id
                WHERE c.chunk_id=%s AND c.chunk_set_id=%s AND d.tenant_id=%s AND a.user_id=%s
                    AND d.active=1 AND j.status='succeeded'""",
                (chunk_id, chunk_set_id, identity.tenant_id, identity.user_id)).fetchone()
            if row is None:
                raise AppError("NOT_FOUND", "Citation not found", 404)
            return json.loads(row["payload"])
