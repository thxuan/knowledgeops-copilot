"""Immutable originals and explicit publication of staged document indexes."""
import difflib
import hashlib
import json
import time
from dataclasses import asdict
from contextlib import nullcontext
from uuid import uuid4

from app.core.errors import AppError
from app.db.documents import DocumentRepository
from app.ingestion.chunker import chunk_document, text_revision
from app.ingestion.parser import PARSER_VERSION, ParseError, clean_document, parse_document


class DocumentVersions:
    def __init__(self, ingestion):
        self.ingestion = ingestion

    def initialize(self):
        with self.ingestion.connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(52003002)')
            had_publication = db.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='documents' AND column_name='publication_state'").fetchone()
            db.execute('''
                ALTER TABLE documents ADD COLUMN IF NOT EXISTS withdrawn INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE documents ADD COLUMN IF NOT EXISTS publication_state TEXT NOT NULL DEFAULT 'draft';
                CREATE TABLE IF NOT EXISTS document_versions (
                    version_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents,
                    sequence INTEGER NOT NULL, label TEXT NOT NULL, base_revision INTEGER NOT NULL,
                    filename TEXT NOT NULL, content BYTEA NOT NULL, file_hash TEXT NOT NULL,
                    parser_version TEXT NOT NULL, text TEXT NOT NULL, title TEXT NOT NULL,
                    metadata TEXT NOT NULL, source TEXT NOT NULL, author TEXT NOT NULL,
                    note TEXT NOT NULL, origin TEXT NOT NULL, restored_from TEXT,
                    created_at DOUBLE PRECISION NOT NULL,
                    UNIQUE(document_id, sequence), UNIQUE(document_id, label)
                );
                CREATE TABLE IF NOT EXISTS document_version_heads (
                    document_id TEXT PRIMARY KEY REFERENCES documents,
                    version_id TEXT NOT NULL REFERENCES document_versions,
                    revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS document_version_builds (
                    version_id TEXT PRIMARY KEY REFERENCES document_versions,
                    status TEXT NOT NULL, token TEXT, lease_until DOUBLE PRECISION,
                    artifact TEXT, error_code TEXT, updated_at DOUBLE PRECISION NOT NULL
                );
                CREATE TABLE IF NOT EXISTS document_lifecycle_events (
                    event_id BIGSERIAL PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents,
                    version_id TEXT REFERENCES document_versions, actor TEXT NOT NULL,
                    kind TEXT NOT NULL, detail TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL
                );
            ''')
            if not had_publication and db.execute("SELECT to_regclass('document_chunk_heads') AS name").fetchone()['name']:
                db.execute("UPDATE documents d SET publication_state='published' WHERE EXISTS(SELECT 1 FROM document_chunk_heads h WHERE h.document_id=d.document_id)")
            from app.db.document_reviews import DocumentReviews
            DocumentReviews(self.ingestion).initialize(transaction=db)
            for row in db.execute('''SELECT d.* FROM documents d JOIN ingestion_jobs j USING(document_id)
                    WHERE d.active=1 AND j.status='succeeded' FOR UPDATE OF d''').fetchall():
                self.snapshot_current(db, row)

    @staticmethod
    def event(db, doc_id, actor, kind, detail, version_id=None):
        db.execute('''INSERT INTO document_lifecycle_events
            (document_id,version_id,actor,kind,detail,created_at) VALUES (%s,%s,%s,%s,%s,%s)''',
            (doc_id, version_id, actor, kind, json.dumps(detail, ensure_ascii=False), time.time()))

    @staticmethod
    def snapshot_current(db, row):
        head = db.execute('SELECT * FROM document_version_heads WHERE document_id=%s',
                          (row['document_id'],)).fetchone()
        if head and head['revision'] == row['revision']:
            return head['version_id']
        label = row['version'] or 'unversioned'
        if db.execute('SELECT 1 FROM document_versions WHERE document_id=%s AND label=%s',
                      (row['document_id'], label)).fetchone():
            label = f'{label} (legacy {uuid4().hex[:8]})'
        initial_draft = row.get('publication_state') == 'draft'
        version_id = DocumentVersions.insert(db, row, label, row['uploaded_by'],
            '首次上传原文草稿。' if initial_draft else '现有当前原文快照；不代表原始发布时间。',
            'upload' if initial_draft else 'legacy_snapshot', row['revision'])
        db.execute('''INSERT INTO document_version_heads VALUES (%s,%s,%s)
            ON CONFLICT(document_id) DO UPDATE SET version_id=excluded.version_id,revision=excluded.revision''',
            (row['document_id'], version_id, row['revision']))
        DocumentVersions.event(db, row['document_id'], row['uploaded_by'], 'snapshot', {}, version_id)
        return version_id

    @staticmethod
    def insert(db, content, label, actor, note, origin, base_revision, restored_from=None):
        doc_id, version_id = content['document_id'], str(uuid4())
        sequence = db.execute('SELECT COALESCE(max(sequence),0)+1 AS n FROM document_versions WHERE document_id=%s',
                              (doc_id,)).fetchone()['n']
        if db.execute('SELECT 1 FROM document_versions WHERE document_id=%s AND label=%s', (doc_id, label)).fetchone():
            raise AppError('VERSION_CONFLICT', '版本标签已存在，请使用新标签。', 409)
        db.execute('''INSERT INTO document_versions
            (version_id,document_id,sequence,label,base_revision,filename,content,file_hash,parser_version,
             text,title,metadata,source,author,note,origin,restored_from,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (version_id, doc_id, sequence, label, base_revision, content['filename'], content['content'],
             content['file_hash'], content['parser_version'], content['text'], content['title'],
             content['metadata'], content['source'], actor, note, origin, restored_from, time.time()))
        return version_id

    def owner(self, db, doc_id, identity):
        row = DocumentRepository._owner(db, doc_id, identity)
        if row['status'] != 'succeeded':
            raise AppError('DOCUMENT_NOT_READY', '请先完成原文解析。', 409)
        self.snapshot_current(db, row)
        return row

    @staticmethod
    def version(db, doc_id, version_id):
        v = db.execute('SELECT * FROM document_versions WHERE document_id=%s AND version_id=%s',
                       (doc_id, version_id)).fetchone()
        if v is None:
            raise AppError('NOT_FOUND', 'Version not found', 404)
        return v

    def create(self, doc_id, identity, filename, content, label, note, expected_revision, restored_from=None):
        with self.ingestion.connection() as db:
            row = self.owner(db, doc_id, identity)
            if row['revision'] != expected_revision:
                raise AppError('VERSION_CONFLICT', '当前版本已变化，请刷新后重试。', 409)
            if restored_from:
                source = self.version(db, doc_id, restored_from)
            else:
                try:
                    parsed = clean_document(parse_document(filename, content))
                except ParseError as exc:
                    raise AppError(exc.code, str(exc), 422) from exc
                source = {**row, 'filename': filename, 'content': content,
                    'file_hash': hashlib.sha256(content).hexdigest(),
                    'parser_version': PARSER_VERSION + ':' + filename.lower().rsplit('.', 1)[-1],
                    'text': parsed.text, 'title': parsed.title,
                    'metadata': json.dumps(parsed.metadata, ensure_ascii=False)}
            from app.db.document_reviews import DocumentReviews
            DocumentReviews.invalidate(db, doc_id, identity.user_id, '已创建新的内容草稿。')
            version_id = self.insert(db, source, label, identity.user_id, note,
                'restore' if restored_from else 'draft', row['revision'], restored_from)
            self.event(db, doc_id, identity.user_id, 'draft_created', {'note': note}, version_id)
            return {'version_id': version_id, 'version': label, 'base_revision': row['revision']}

    def list(self, doc_id, identity):
        with self.ingestion.connection() as db:
            row = self.owner(db, doc_id, identity)
            self.expire(db, doc_id)
            versions = [dict(v) for v in db.execute('''SELECT v.version_id,v.sequence,v.label,v.base_revision,
                v.author,v.note,v.origin,v.restored_from,v.created_at,h.version_id IS NOT NULL AS current,
                COALESCE(b.status,'not_built') AS build_status,b.error_code,b.updated_at AS build_updated_at,
                EXISTS(SELECT 1 FROM document_lifecycle_events e WHERE e.version_id=v.version_id
                    AND e.kind='published') AS was_published
                FROM document_versions v LEFT JOIN document_version_heads h USING(version_id)
                LEFT JOIN document_version_builds b USING(version_id)
                WHERE v.document_id=%s ORDER BY v.sequence DESC''', (doc_id,))]
            events = [dict(e) for e in db.execute('''SELECT event_id,version_id,actor,kind,detail,created_at
                FROM document_lifecycle_events WHERE document_id=%s ORDER BY event_id DESC LIMIT 100''', (doc_id,))]
            for v in versions:
                review = db.execute('SELECT review_id,status,comment,user_ids FROM document_reviews WHERE version_id=%s ORDER BY submitted_at DESC LIMIT 1',(v['version_id'],)).fetchone()
                v['review'] = dict(review) if review else None
            return {'publication_state':row['publication_state'],'document_id': doc_id, 'revision': row['revision'], 'withdrawn': bool(row['withdrawn']),
                    'versions': versions, 'events': events, 'history_access': 'owner_and_platform_admin'}

    def read(self, doc_id, version_id, identity, compare_to=None):
        with self.ingestion.connection() as db:
            self.owner(db, doc_id, identity)
            v = self.version(db, doc_id, version_id)
            result = {k: v[k] for k in ('version_id','label','filename','text','source','note','created_at','file_hash')}
            result['original_text'] = bytes(v['content']).decode('utf-8-sig')
            if compare_to:
                before = self.version(db, doc_id, compare_to)
                result['diff'] = '\n'.join(difflib.unified_diff(before['text'].splitlines(),
                    v['text'].splitlines(), fromfile=before['label'], tofile=v['label'], lineterm=''))
            return result

    def expire(self, db, doc_id):
        rows = db.execute('''UPDATE document_version_builds b SET status='failed',token=NULL,
            error_code='BUILD_INTERRUPTED',updated_at=%s FROM document_versions v
            WHERE b.version_id=v.version_id AND v.document_id=%s AND b.status='building'
            AND b.lease_until<%s RETURNING b.version_id''', (time.time(), doc_id, time.time())).fetchall()
        for row in rows:
            self.event(db, doc_id, 'system', 'build_failed', {'code': 'BUILD_INTERRUPTED'}, row['version_id'])

    def start_build(self, doc_id, version_id, identity, config, *, takeover=False):
        with self.ingestion.connection() as db:
            row = self.owner(db, doc_id, identity)
            v = self.version(db, doc_id, version_id)
            if (v['origin'] == 'legacy_snapshot' and row['publication_state'] == 'published') or db.execute('''SELECT 1 FROM document_lifecycle_events
                    WHERE version_id=%s AND kind='published' ''', (version_id,)).fetchone():
                raise AppError('VERSION_IMMUTABLE', '请基于历史版本创建新草稿后构建。', 409)
            self.expire(db, doc_id)
            b = db.execute('SELECT status FROM document_version_builds WHERE version_id=%s', (version_id,)).fetchone()
            if b and b['status'] == 'building' and not takeover:
                raise AppError('BUILD_RUNNING', '该版本正在构建，请稍后刷新。', 409)
            from app.db.document_reviews import DocumentReviews
            DocumentReviews.invalidate(db, doc_id, identity.user_id, '版本索引正在重新构建。')
            token = str(uuid4())
            db.execute('''INSERT INTO document_version_builds VALUES (%s,'building',%s,%s,NULL,NULL,%s)
                ON CONFLICT(version_id) DO UPDATE SET status='building',token=excluded.token,
                lease_until=excluded.lease_until,artifact=NULL,error_code=NULL,updated_at=excluded.updated_at''',
                (version_id, token, time.time()+900, time.time()))
            self.event(db, doc_id, identity.user_id, 'build_started', asdict(config), version_id)
        chunks = [asdict(c) for c in chunk_document(doc_id, v['text'], title=v['title'], source=v['source'],
            version=v['label'], parser_version=v['parser_version'], config=config,
            markdown=v['filename'].lower().endswith('.md'), generation=version_id)]
        set_id = hashlib.sha256(json.dumps([version_id, config.config_id]).encode()).hexdigest()
        return token, {'document_id': doc_id, 'chunk_set_id': set_id, 'config_id': config.config_id,
            'text_hash': text_revision(v['text']), 'config': asdict(config), 'count': len(chunks), 'chunks': chunks}

    def finish_build(self, doc_id, version_id, identity, token, artifact=None, error_code=None, *, transaction=None):
        with (nullcontext(transaction) if transaction is not None else self.ingestion.connection()) as db:
            self.owner(db, doc_id, identity)
            cursor = db.execute('''UPDATE document_version_builds SET status=%s,artifact=%s,error_code=%s,
                token=NULL,lease_until=NULL,updated_at=%s WHERE version_id=%s AND token=%s
                AND status='building' AND lease_until>%s''',
                ('failed' if error_code else 'ready', json.dumps(artifact) if artifact else None,
                 error_code, time.time(), version_id, token, time.time()))
            if not cursor.rowcount:
                raise AppError('BUILD_EXPIRED', '构建已过期，请重试。', 409)
            self.event(db, doc_id, identity.user_id, 'build_failed' if error_code else 'build_ready',
                       {'code': error_code}, version_id)

    def publish(self, doc_id, version_id, identity, expected_revision, vectors, *, review_id=None, transaction=None):
        if not review_id or 'manage_platform' not in identity.platform_capabilities:
            raise AppError('REVIEW_REQUIRED', '必须由管理员通过待审批记录发布此版本。', 409)
        with (nullcontext(transaction) if transaction is not None else self.ingestion.connection()) as db:
            row = self.owner(db, doc_id, identity)
            v = self.version(db, doc_id, version_id)
            current = db.execute('SELECT version_id FROM document_version_heads WHERE document_id=%s', (doc_id,)).fetchone()
            from app.db.document_reviews import artifact_hash
            review = db.execute("SELECT * FROM document_reviews WHERE review_id=%s AND document_id=%s AND version_id=%s AND status='pending' FOR UPDATE", (review_id,doc_id,version_id)).fetchone()
            if not review or review['expected_revision'] != row['revision'] or review['file_hash'] != v['file_hash']:
                raise AppError('REVIEW_STALE', '发布审批已失效，请重新提交。', 409)
            if row['revision'] != expected_revision or (v['base_revision'] != row['revision'] and current['version_id'] != version_id):
                raise AppError('VERSION_CONFLICT', '发布基线已变化，请基于当前版本重新创建草稿。', 409)
            b = db.execute('SELECT * FROM document_version_builds WHERE version_id=%s', (version_id,)).fetchone()
            if not b or b['status'] != 'ready':
                raise AppError('INDEX_NOT_READY', '请先成功构建草稿索引。', 409)
            if artifact_hash(b['artifact']) != review['artifact_hash']:
                raise AppError('REVIEW_STALE', '索引已变化，请重新提交审批。', 409)
            artifact = json.loads(b['artifact'])
            if artifact['signature'] != vectors.settings.signature or artifact['collection'] != vectors.settings.collection:
                raise AppError('EMBEDDING_CONFIG_MISMATCH', '模型或集合已变化，请重新构建草稿。', 409)
            s = artifact['snapshot']
            other = db.execute('''SELECT 1 FROM documents WHERE tenant_id=%s AND file_hash=%s
                AND parser_version=%s AND document_id<>%s''',
                (identity.tenant_id, v['file_hash'], v['parser_version'], doc_id)).fetchone()
            if other:
                raise AppError('DUPLICATE_CONTENT', '相同内容已属于另一文档。', 409)
            db.execute('INSERT INTO chunk_sets VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (s['chunk_set_id'], doc_id, s['config_id'], s['text_hash'], json.dumps(s['config'])))
            db.cursor().executemany('INSERT INTO chunks VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                [(c['chunk_id'], s['chunk_set_id'], c['ordinal'], json.dumps(c, ensure_ascii=False)) for c in s['chunks']])
            vectors.publish(s, identity.tenant_id, artifact['vectors'], transaction=db)
            db.execute('''UPDATE documents SET filename=%s,content=%s,size_bytes=%s,file_hash=%s,parser_version=%s,
                text=%s,title=%s,metadata=%s,source=%s,version=%s,revision=revision+1,updated_at=%s,withdrawn=0,publication_state='published'
                WHERE document_id=%s''', (v['filename'], v['content'], len(v['content']), v['file_hash'],
                v['parser_version'], v['text'], v['title'], v['metadata'], v['source'], v['label'], time.time(), doc_id))
            db.execute('''INSERT INTO document_chunk_heads VALUES (%s,%s)
                ON CONFLICT(document_id) DO UPDATE SET chunk_set_id=excluded.chunk_set_id''', (doc_id, s['chunk_set_id']))
            db.execute('UPDATE document_version_heads SET version_id=%s,revision=%s WHERE document_id=%s',
                       (version_id, row['revision']+1, doc_id))
            db.execute('DELETE FROM document_access WHERE document_id=%s', (doc_id,))
            db.cursor().executemany('INSERT INTO document_access VALUES (%s,%s)', [(doc_id,u) for u in review['user_ids']])
            self.event(db, doc_id, identity.user_id, 'published', {'note': v['note']}, version_id)
            return {'version_id': version_id, 'version': v['label'], 'revision': row['revision']+1, 'reused': False}

    def withdraw(self, doc_id, identity, expected_revision, reason):
        with self.ingestion.connection() as db:
            row = self.owner(db, doc_id, identity)
            if row['revision'] != expected_revision:
                raise AppError('VERSION_CONFLICT', '当前版本已变化，请刷新。', 409)
            from app.db.document_reviews import DocumentReviews
            DocumentReviews.invalidate(db, doc_id, identity.user_id, '文档已撤回。')
            db.execute('UPDATE documents SET withdrawn=1,revision=revision+1,updated_at=%s WHERE document_id=%s',
                       (time.time(), doc_id))
            db.execute('DELETE FROM document_chunk_heads WHERE document_id=%s', (doc_id,))
            db.execute('UPDATE document_version_heads SET revision=%s WHERE document_id=%s', (row['revision']+1, doc_id))
            self.event(db, doc_id, identity.user_id, 'withdrawn', {'reason': reason})
            return {'withdrawn': True, 'revision': row['revision']+1}

    def access(self, doc_id, identity):
        admin = 'manage_platform' in identity.platform_capabilities
        with self.ingestion.connection() as db:
            row = db.execute("""SELECT d.* FROM documents d WHERE d.document_id=%s AND d.tenant_id=%s
                AND d.active=1 AND (d.uploaded_by=%s OR %s OR (d.withdrawn=0 AND d.publication_state='published'
                AND EXISTS(SELECT 1 FROM document_access a WHERE a.document_id=d.document_id AND a.user_id=%s))) FOR SHARE OF d""",
                (doc_id,identity.tenant_id,identity.user_id,admin,identity.user_id)).fetchone()
            if row is None:raise AppError('NOT_FOUND','Document not found',404)
            manager = row['uploaded_by']==identity.user_id or admin
            grants = [r['user_id'] for r in db.execute('SELECT user_id FROM document_access WHERE document_id=%s',(doc_id,))]
            return {'can_read': manager or (identity.user_id in grants and not row['withdrawn']),
                'can_manage':manager,'can_review':admin,'can_view_history':manager,
                'publication_state':row['publication_state'],
                'reason':'上传者和平台管理员可审阅草稿与历史；其他账号只能阅读获授权的已发布正文。',
                'history_rule':'历史原文与差异仅上传者和当前组织的平台管理员可查看。',
                'user_ids':grants if manager else [identity.user_id],'withdrawn':bool(row['withdrawn'])}
