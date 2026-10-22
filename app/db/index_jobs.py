"""Durable index queue, owner-scoped reads and fenced state transitions."""
import time
from uuid import uuid4
from psycopg.types.json import Jsonb
from app.core.errors import AppError
from app.db.documents import DocumentRepository
from app.db.document_versions import DocumentVersions

class IndexJobs:
    def __init__(self, ingestion):self.ingestion=ingestion
    def initialize(self):
        with self.ingestion.connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(52003003)')
            db.execute('''CREATE TABLE IF NOT EXISTS ko_index_jobs (
                job_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents,
                version_id TEXT REFERENCES document_versions, tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,
                config JSONB NOT NULL,signature JSONB NOT NULL,collection TEXT NOT NULL,
                force BOOLEAN NOT NULL DEFAULT false,status TEXT NOT NULL,stage TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,completed INTEGER NOT NULL DEFAULT 0,total INTEGER NOT NULL DEFAULT 0,
                token TEXT,lease_until DOUBLE PRECISION,error_code TEXT,
                created_at DOUBLE PRECISION NOT NULL,updated_at DOUBLE PRECISION NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS ko_index_active ON ko_index_jobs(document_id,COALESCE(version_id,''))
                    WHERE status IN ('queued','running','blocked');
                CREATE INDEX IF NOT EXISTS ko_index_schedule ON ko_index_jobs(status,lease_until);
            ''')

    @staticmethod
    def public(row):
        return {k:row[k] for k in ('job_id','document_id','version_id','status','stage','attempts','completed','total',
                                  'error_code','created_at','updated_at','collection')}

    def enqueue(self,doc_id,version_id,identity,config,embedding,force=False):
        with self.ingestion.connection() as db:
            d=DocumentRepository._owner(db,doc_id,identity)
            if d['status']!='succeeded' or (not version_id and d['withdrawn']):
                raise AppError('DOCUMENT_NOT_READY','原文尚未解析或已撤回。',409)
            if not version_id and d['publication_state'] != 'published':
                version_id=DocumentVersions.snapshot_current(db,d)
            if version_id:
                v=DocumentVersions.version(db,doc_id,version_id)
                if (v['origin']=='legacy_snapshot' and d['publication_state']=='published') or db.execute("SELECT 1 FROM document_lifecycle_events WHERE version_id=%s AND kind='published'",(version_id,)).fetchone():
                    raise AppError('VERSION_IMMUTABLE','请创建新草稿后构建。',409)
            existing=db.execute("SELECT * FROM ko_index_jobs WHERE document_id=%s AND version_id IS NOT DISTINCT FROM %s AND status IN ('queued','running','blocked')",(doc_id,version_id)).fetchone()
            if existing:
                if existing['config']!=config or existing['signature']!=embedding.signature or existing['collection']!=embedding.collection or existing['force']!=force:
                    raise AppError('INDEX_JOB_EXISTS','已有不同参数的索引任务，请先取消或等待完成。',409)
                return {**self.public(existing),'reused':True}
            if version_id:
                building=db.execute("SELECT 1 FROM document_version_builds WHERE version_id=%s AND status='building' AND lease_until>%s",(version_id,time.time())).fetchone()
                if building:raise AppError('BUILD_RUNNING','该版本已有构建任务。',409)
            job_id=str(uuid4());now=time.time()
            db.execute('''INSERT INTO ko_index_jobs
                (job_id,document_id,version_id,tenant_id,user_id,config,signature,collection,force,status,stage,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued','queued',%s,%s)''',
                (job_id,doc_id,version_id,identity.tenant_id,identity.user_id,Jsonb(config),Jsonb(embedding.signature),embedding.collection,force,now,now))
            if version_id:
                db.execute("INSERT INTO document_version_builds VALUES (%s,'queued',NULL,NULL,NULL,NULL,%s) ON CONFLICT(version_id) DO UPDATE SET status='queued',token=NULL,artifact=NULL,error_code=NULL,updated_at=excluded.updated_at",(version_id,now))
            return self.public(db.execute('SELECT * FROM ko_index_jobs WHERE job_id=%s',(job_id,)).fetchone())

    def list(self,identity,limit=50):
        with self.ingestion.connection() as db:
            rows=db.execute('''SELECT j.*,d.title,d.filename,v.label FROM ko_index_jobs j JOIN documents d USING(document_id)
                LEFT JOIN document_versions v ON v.version_id=j.version_id
                WHERE j.tenant_id=%s AND (j.user_id=%s OR %s) AND d.active=1 AND (d.uploaded_by=%s OR %s)
                ORDER BY j.created_at DESC LIMIT %s''',(identity.tenant_id,identity.user_id,'manage_platform' in identity.platform_capabilities,identity.user_id,'manage_platform' in identity.platform_capabilities,limit)).fetchall()
            return {'items':[{**self.public(r),'title':r['title'] or r['filename'],'version':r['label']} for r in rows]}

    def owned(self,db,job_id,identity):
        row=db.execute('''SELECT j.* FROM ko_index_jobs j JOIN documents d USING(document_id)
            WHERE j.job_id=%s AND j.tenant_id=%s AND (j.user_id=%s OR %s) AND (d.uploaded_by=%s OR %s) AND d.active=1''',
            (job_id,identity.tenant_id,identity.user_id,'manage_platform' in identity.platform_capabilities,identity.user_id,'manage_platform' in identity.platform_capabilities)).fetchone()
        if row is None:raise AppError('NOT_FOUND','索引任务不存在。',404)
        return row

    def read(self,job_id,identity):
        with self.ingestion.connection() as db:return self.public(self.owned(db,job_id,identity))

    def control(self,job_id,identity,action,embedding):
        with self.ingestion.connection() as db:
            row=self.owned(db,job_id,identity)
            DocumentRepository._owner(db,row['document_id'],identity)
            row=db.execute('SELECT * FROM ko_index_jobs WHERE job_id=%s FOR UPDATE',(job_id,)).fetchone()
            if action=='cancel':
                if row['status'] in {'succeeded','failed','cancelled'}:return self.public(row)
                db.execute("UPDATE ko_index_jobs SET status='cancelled',stage='cancelled',token=NULL,lease_until=NULL,updated_at=%s WHERE job_id=%s",(time.time(),job_id))
            else:
                other=db.execute("SELECT 1 FROM ko_index_jobs WHERE document_id=%s AND version_id IS NOT DISTINCT FROM %s AND job_id<>%s AND status IN ('queued','running','blocked')",(row['document_id'],row['version_id'],job_id)).fetchone()
                if other:raise AppError('INDEX_JOB_EXISTS','已有索引任务，请先等待或取消。',409)
                if row['status'] not in {'failed','cancelled','blocked'} or row['attempts']>=3:
                    raise AppError('RETRY_NOT_ALLOWED','仅失败、取消或配置阻塞的任务可重试，最多 3 次。',409)
                db.execute("UPDATE ko_index_jobs SET status='queued',stage='queued',error_code=NULL,signature=%s,collection=%s,token=NULL,lease_until=NULL,updated_at=%s WHERE job_id=%s",(Jsonb(embedding.signature),embedding.collection,time.time(),job_id))
            if row['version_id']:
                db.execute("UPDATE document_version_builds SET status=%s,token=NULL,artifact=NULL,error_code=%s,updated_at=%s WHERE version_id=%s",('cancelled' if action=='cancel' else 'queued','CANCELLED' if action=='cancel' else None,time.time(),row['version_id']))
            return self.public(db.execute('SELECT * FROM ko_index_jobs WHERE job_id=%s',(job_id,)).fetchone())

    def claim(self):
        now=time.time()
        with self.ingestion.connection() as db:
            row=db.execute("SELECT * FROM ko_index_jobs WHERE status='queued' OR (status='running' AND lease_until<%s) ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED",(now,)).fetchone()
            if row is None:return None
            if row['attempts']>=3:
                db.execute("UPDATE ko_index_jobs SET status='failed',stage='failed',error_code='ATTEMPT_LIMIT',token=NULL,updated_at=%s WHERE job_id=%s",(now,row['job_id']))
                return None
            token=str(uuid4())
            return dict(db.execute("UPDATE ko_index_jobs SET status='running',stage='chunking',attempts=attempts+1,completed=0,total=0,token=%s,lease_until=%s,updated_at=%s WHERE job_id=%s RETURNING *",(token,now+120,now,row['job_id'])).fetchone())

    def renew(self,job,build_token=None):
        with self.ingestion.connection() as db:
            live=bool(db.execute("UPDATE ko_index_jobs SET lease_until=%s,updated_at=%s WHERE job_id=%s AND token=%s AND status='running' AND lease_until>%s",(time.time()+120,time.time(),job['job_id'],job['token'],time.time())).rowcount)
            if live and build_token:
                db.execute("UPDATE document_version_builds SET lease_until=%s WHERE version_id=%s AND token=%s AND status='building'",(time.time()+120,job['version_id'],build_token))
            return live

    def progress(self,job,completed,total):
        with self.ingestion.connection() as db:
            if not db.execute("UPDATE ko_index_jobs SET stage='embedding',completed=%s,total=%s,updated_at=%s WHERE job_id=%s AND token=%s AND status='running' AND lease_until>%s",(completed,total,time.time(),job['job_id'],job['token'],time.time())).rowcount:
                raise AppError('INDEX_JOB_CANCELLED','任务已取消或租约已过期。',409)

    @staticmethod
    def fenced(db,job):
        row=db.execute('SELECT * FROM ko_index_jobs WHERE job_id=%s FOR UPDATE',(job['job_id'],)).fetchone()
        if row['token']!=job['token'] or row['status']!='running' or row['lease_until']<=time.time():
            raise AppError('INDEX_JOB_CANCELLED','任务已取消或租约已过期。',409)

    def settle(self,job,status,code=None):
        with self.ingestion.connection() as db:
            db.execute('UPDATE ko_index_jobs SET status=%s,stage=%s,error_code=%s,token=NULL,lease_until=NULL,updated_at=%s WHERE job_id=%s AND token=%s AND status=\'running\'',(status,status,code,time.time(),job['job_id'],job['token']))

    def unfinished(self):
        with self.ingestion.connection() as db:
            return db.execute("SELECT 1 FROM ko_index_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone() is not None
