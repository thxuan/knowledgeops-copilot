"""Owner-controlled lifecycle; PostgreSQL is the authority for active versions and ACLs."""
import json
import time
from app.core.errors import AppError
from app.core.identity import LOCAL_IDENTITIES


class DocumentRepository:
    def __init__(self, ingestion, accounts=None):
        self.ingestion = ingestion
        self.accounts = accounts

    @staticmethod
    def _owner(db, document_id, identity):
        row = db.execute("""SELECT d.*, j.status FROM documents d
            JOIN ingestion_jobs j USING(document_id)
            WHERE d.document_id=%s AND d.tenant_id=%s AND (d.uploaded_by=%s OR %s) AND d.active=1 FOR UPDATE OF d""",
            (document_id, identity.tenant_id, identity.user_id, 'manage_platform' in identity.platform_capabilities)).fetchone()
        if row is None:
            raise AppError("NOT_FOUND", "Document not found", 404)
        return row

    def check_owner(self, document_id, identity):
        with self.ingestion.connection() as db:
            self._owner(db, document_id, identity)

    def update(self, document_id, identity, filename, content, version, expected_version):
        with self.ingestion.connection() as db:
            self._owner(db,document_id,identity)
        raise AppError('REVIEW_REQUIRED', '请创建版本草稿并提交发布审批，旧更新接口已停用。', 409)

    def delete(self, document_id, identity):
        with self.ingestion.connection() as db:
            db.execute("BEGIN")
            self._owner(db, document_id, identity)
            db.execute("UPDATE documents SET active=0,revision=revision+1,updated_at=%s WHERE document_id=%s",
                       (time.time(), document_id))
            db.execute("DELETE FROM document_chunk_heads WHERE document_id=%s", (document_id,))

    def permissions(self, document_id, identity, user_ids):
        try:
            users = [self.accounts.user(user_id) if self.accounts else LOCAL_IDENTITIES.get(user_id) for user_id in user_ids]
        except AppError:
            raise AppError('INVALID_PRINCIPAL','授权对象必须是当前租户的启用账号。',422) from None
        if any(user is None or user.tenant_id != identity.tenant_id for user in users):
            raise AppError("INVALID_PRINCIPAL", "Only users in the server-owned tenant are allowed", 422)
        with self.ingestion.connection() as db:
            db.execute("BEGIN")
            self._owner(db, document_id, identity)
            before = [r["user_id"] for r in db.execute("SELECT user_id FROM document_access WHERE document_id=%s", (document_id,))]
            if set(user_ids) - set(before):
                raise AppError('REVIEW_REQUIRED', '扩大共享范围请在版本列表提交发布审批。', 409)
            from app.db.document_reviews import DocumentReviews
            DocumentReviews.invalidate(db, document_id, identity.user_id, '阅读权限已变更。')
            db.execute("DELETE FROM document_access WHERE document_id=%s", (document_id,))
            db.cursor().executemany("INSERT INTO document_access VALUES (%s,%s)", [(document_id, u) for u in sorted(set(user_ids))])
            from app.db.document_versions import DocumentVersions
            DocumentVersions.event(db, document_id, identity.user_id, 'permissions_changed',
                                   {'before': sorted(before), 'after': sorted(set(user_ids))})
            return {"document_id": document_id, "user_ids": sorted(set(user_ids))}

    def citation(self, chunk_id, chunk_set_id, identity):
        with self.ingestion.connection() as db:
            db.execute("BEGIN")
            row = db.execute("""SELECT c.payload FROM chunks c
                JOIN document_chunk_heads h ON h.chunk_set_id=c.chunk_set_id
                JOIN documents d ON d.document_id=h.document_id
                JOIN ingestion_jobs j ON j.document_id=d.document_id
                WHERE c.chunk_id=%s AND c.chunk_set_id=%s AND d.tenant_id=%s AND (EXISTS(SELECT 1 FROM document_access a WHERE a.document_id=d.document_id AND a.user_id=%s) OR %s)
                    AND d.active=1 AND d.publication_state='published' AND d.withdrawn=0 AND j.status='succeeded'""",
                (chunk_id, chunk_set_id, identity.tenant_id, identity.user_id, 'manage_platform' in identity.platform_capabilities)).fetchone()
            if row is None:
                raise AppError("NOT_FOUND", "Citation not found", 404)
            return json.loads(row["payload"])
