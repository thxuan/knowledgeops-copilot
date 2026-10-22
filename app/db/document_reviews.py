"""Version-bound publication review. All transitions lock the document before the review."""
import hashlib
import json
import time
from uuid import uuid4
from contextlib import nullcontext
from psycopg.types.json import Jsonb
from app.core.errors import AppError
from app.core.identity import LOCAL_IDENTITIES
from app.db.documents import DocumentRepository


def is_admin(identity):
    return 'manage_platform' in identity.platform_capabilities


def artifact_hash(artifact):
    return hashlib.sha256(json.dumps(json.loads(artifact), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


class DocumentReviews:
    def __init__(self, ingestion, accounts=None):
        self.ingestion, self.accounts = ingestion, accounts

    def initialize(self, transaction=None):
        with (nullcontext(transaction) if transaction is not None else self.ingestion.connection()) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS document_reviews (
                review_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents,
                version_id TEXT NOT NULL REFERENCES document_versions,
                expected_revision INTEGER NOT NULL, file_hash TEXT NOT NULL, artifact_hash TEXT NOT NULL,
                user_ids JSONB NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','invalidated')),
                submitted_by TEXT NOT NULL, submitted_at DOUBLE PRECISION NOT NULL,
                reviewed_by TEXT, reviewed_at DOUBLE PRECISION, comment TEXT NOT NULL DEFAULT '',
                result_revision INTEGER);
                CREATE UNIQUE INDEX IF NOT EXISTS document_review_pending ON document_reviews(document_id)
                    WHERE status='pending';
            ''')

    @staticmethod
    def invalidate(db, doc_id, actor, reason):
        from app.db.document_versions import DocumentVersions
        rows = db.execute("UPDATE document_reviews SET status='invalidated',comment=%s,reviewed_at=%s WHERE document_id=%s AND status='pending' RETURNING version_id", (reason, time.time(), doc_id)).fetchall()
        for row in rows:
            DocumentVersions.event(db, doc_id, actor, 'review_invalidated', {'reason': reason}, row['version_id'])

    def validate_users(self, identity, users):
        users = sorted(set(users))
        for user_id in users:
            try:
                user = self.accounts.user(user_id) if self.accounts else LOCAL_IDENTITIES.get(user_id)
            except AppError:
                user = None
            if user is None or user.tenant_id != identity.tenant_id:
                raise AppError('INVALID_PRINCIPAL', '共享对象必须是当前组织的有效账号。', 422)
        return users

    def submit(self, doc_id, version_id, identity, expected_revision, users):
        from app.db.document_versions import DocumentVersions
        with self.ingestion.connection() as db:
            row = DocumentRepository._owner(db, doc_id, identity)
            if row['status'] != 'succeeded' or row['revision'] != expected_revision:
                raise AppError('VERSION_CONFLICT', '文档未就绪或已变化，请刷新。', 409)
            v = DocumentVersions.version(db, doc_id, version_id)
            head = db.execute('SELECT version_id FROM document_version_heads WHERE document_id=%s', (doc_id,)).fetchone()
            current = head and head['version_id'] == version_id and row['publication_state'] == 'published'
            if not current and v['base_revision'] != row['revision']:
                raise AppError('VERSION_CONFLICT', '发布基线已变化，请创建新草稿。', 409)
            b = db.execute("SELECT * FROM document_version_builds WHERE version_id=%s AND status='ready'", (version_id,)).fetchone()
            if not b:
                raise AppError('INDEX_NOT_READY', '请先构建此版本的草稿索引，再提交审批。', 409)
            users = self.validate_users(identity, [*users, row['uploaded_by']])
            existing = db.execute("SELECT * FROM document_reviews WHERE document_id=%s AND status='pending'", (doc_id,)).fetchone()
            digest = artifact_hash(b['artifact'])
            if existing and existing['version_id'] == version_id and existing['user_ids'] == users and existing['artifact_hash'] == digest and existing['expected_revision'] == expected_revision:
                return dict(existing)
            self.invalidate(db, doc_id, identity.user_id, '提交了新的版本或共享范围。')
            review_id = str(uuid4())
            db.execute('''INSERT INTO document_reviews
                (review_id,document_id,version_id,expected_revision,file_hash,artifact_hash,user_ids,status,submitted_by,submitted_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)''',
                (review_id,doc_id,version_id,expected_revision,v['file_hash'],digest,Jsonb(users),identity.user_id,time.time()))
            DocumentVersions.event(db,doc_id,identity.user_id,'review_submitted',{'review_id':review_id,'user_ids':users},version_id)
            return dict(db.execute('SELECT * FROM document_reviews WHERE review_id=%s',(review_id,)).fetchone())

    def list(self, identity, limit=100):
        with self.ingestion.connection() as db:
            rows = db.execute('''SELECT r.*,d.title,d.filename,d.uploaded_by,d.withdrawn,v.label
                FROM document_reviews r JOIN documents d USING(document_id)
                JOIN document_versions v USING(version_id)
                WHERE d.tenant_id=%s AND d.active=1 AND (%s OR d.uploaded_by=%s)
                ORDER BY (r.status='pending') DESC,r.submitted_at DESC LIMIT %s''',
                (identity.tenant_id,is_admin(identity),identity.user_id,limit)).fetchall()
            return {'items':[dict(r) for r in rows]}

    def decide(self, review_id, identity, action, comment, vectors):
        from app.db.document_versions import DocumentVersions
        if not is_admin(identity):
            raise AppError('PERMISSION_DENIED','仅平台管理员可以审批文档发布。',403)
        with self.ingestion.connection() as db:
            # Lookup is tenant-scoped, then acquire locks in the same order as submission/building.
            r = db.execute('''SELECT r.document_id FROM document_reviews r JOIN documents d USING(document_id)
                WHERE r.review_id=%s AND d.tenant_id=%s AND d.active=1''',(review_id,identity.tenant_id)).fetchone()
            if not r:raise AppError('NOT_FOUND','审批记录不存在。',404)
            row = DocumentRepository._owner(db,r['document_id'],identity)
            review = db.execute('SELECT * FROM document_reviews WHERE review_id=%s FOR UPDATE',(review_id,)).fetchone()
            if review['status']=='approved' and action=='approve' and row['revision']==review['result_revision']:
                return {'review_id':review_id,'status':'approved','revision':row['revision'],'reused':True}
            if review['status']!='pending':raise AppError('REVIEW_NOT_PENDING','审批已处理或失效，请刷新。',409)
            if row['revision']!=review['expected_revision']:
                raise AppError('REVIEW_STALE','文档已变化，请重新提交审批。',409)
            if action=='reject':
                if not comment.strip():raise AppError('REASON_REQUIRED','请填写退回原因。',422)
                result={'revision':row['revision']}
            else:
                self.validate_users(identity,review['user_ids'])
                result=DocumentVersions(self.ingestion).publish(row['document_id'],review['version_id'],identity,
                    review['expected_revision'],vectors,review_id=review_id,transaction=db)
            status='approved' if action=='approve' else 'rejected'
            db.execute('''UPDATE document_reviews SET status=%s,reviewed_by=%s,reviewed_at=%s,comment=%s,result_revision=%s
                WHERE review_id=%s''',(status,identity.user_id,time.time(),comment,result['revision'],review_id))
            DocumentVersions.event(db,row['document_id'],identity.user_id,'review_'+status,
                {'review_id':review_id,'comment':comment,'user_ids':review['user_ids']},review['version_id'])
            return {**result,'review_id':review_id,'status':status}
