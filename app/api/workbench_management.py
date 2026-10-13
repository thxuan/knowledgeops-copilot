"""Local connection administration and authorized knowledge inspection."""
import asyncio
import re
import time
from uuid import uuid4
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from app.core.identity import Identity, get_identity
from app.core.errors import AppError
from app.core.embedding import make_embedding_client, validate_result
from app.db.postgres import connection, database_identity
from app.api.schemas import ChunkBuildRequest
from app.ingestion.chunker import ChunkConfig, chunk_document
from app.retrieval.rerank import make_rerank_client
router = APIRouter()
UI = Path(__file__).resolve().parents[1] / 'web'

@router.get('/knowledge', include_in_schema=False)
@router.get('/settings', include_in_schema=False)
async def page():
    return FileResponse(UI / 'management.html')

class DatabaseConnection(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=55432, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=63)
    user: str = Field(min_length=1, max_length=63)
    password: str | None = Field(default=None, max_length=4096)
    schema_name: str = Field(default='public', pattern=r'^[a-zA-Z_][a-zA-Z0-9_]{0,62}$')

class ProviderConnection(BaseModel):
    base_url: str
    model_id: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=4096)
    @field_validator('base_url')
    @classmethod
    def url(cls, value):
        p = urlsplit(value)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError('A complete HTTPS endpoint without credentials is required')
        return value.rstrip('/')
    @field_validator('model_id')
    @classmethod
    def real_model(cls, value):
        if 'fake' in value.lower():
            raise ValueError('Configure a real model')
        return value

class EmbeddingConnection(ProviderConnection):
    dimensions: int = Field(default=1024, ge=1, le=2000)
    collection: str = Field(min_length=1, max_length=150, pattern=r'^[a-zA-Z0-9_.-]+$')

class Connections(BaseModel):
    database: DatabaseConnection
    embedding: EmbeddingConnection
    rerank: ProviderConnection | None = None

def db_view(url):
    c = conninfo_to_dict(url)
    match = re.search(r'search_path[= ]([^ ,]+)', c.get('options', ''))
    return dict(host=c.get('host', ''), port=int(c.get('port', '5432')), database=c.get('dbname', ''),
                user=c.get('user', ''), schema_name=match.group(1) if match else 'public',
                password_present=bool(c.get('password')))

def database_url(app, body):
    current = conninfo_to_dict(app.state.settings.database_url)
    same = all(str(getattr(body, k)) == str(current.get(v, '5432' if v == 'port' else ''))
               for k, v in [('host', 'host'), ('port', 'port'), ('database', 'dbname'), ('user', 'user')])
    password = body.password if body.password is not None else current.get('password') if same else None
    if not password:
        raise AppError('DATABASE_PASSWORD_REQUIRED', '请填写数据库密码；更换服务器或用户后需重新输入。', 422)
    return make_conninfo(host=body.host, port=body.port, dbname=body.database, user=body.user,
                         password=password, options=f'-c search_path={body.schema_name},public')

def provider_key(body, current_url, current_key):
    key = body.api_key or (current_key if body.base_url == current_url.rstrip('/') else None)
    if not key:
        raise AppError('PROVIDER_KEY_REQUIRED', '请填写 API Key；更换服务地址后需重新输入。', 422)
    return key

def embedding_candidate(app, body):
    e = app.state.settings.embedding
    result = replace(e, provider='compatible', model_id=body.model_id, base_url=body.base_url,
                     api_key=provider_key(body, e.base_url, e.api_key), dimensions=body.dimensions, collection=body.collection)
    result.validate()
    return result

def rerank_candidate(app, body):
    r = app.state.settings.retrieval.rerank
    if body is None:
        return replace(r, provider='none', api_key=None)
    result = replace(r, provider='compatible', model_id=body.model_id, endpoint=body.base_url,
                     api_key=provider_key(body, r.endpoint, r.api_key))
    result.validate()
    return result

@router.get('/workbench/connections')
async def connections(request: Request, identity: Identity = Depends(get_identity)):
    s = request.app.state.settings
    e, r = s.embedding, s.retrieval.rerank
    return {'database': db_view(s.database_url),
            'embedding': {'base_url': e.base_url if e.provider == 'compatible' else '',
                          'model_id': e.model_id if e.provider == 'compatible' else '', 'dimensions': e.dimensions,
                          'collection': e.collection, 'key_present': bool(e.api_key), 'configured': e.provider == 'compatible'},
            'rerank': {'base_url': r.endpoint if r.provider == 'compatible' else '', 'model_id': r.model_id,
                       'key_present': bool(r.api_key), 'configured': r.provider == 'compatible'}, 'storage': 'server_memory'}

def inspect_database(url, schema):
    with connection(url) as db:
        schemas = [r['schema_name'] for r in db.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT LIKE 'pg_%' AND schema_name <> 'information_schema' ORDER BY schema_name")]
        if schema not in schemas:
            raise AppError('SCHEMA_NOT_FOUND', '目标 schema 不存在，请选择已有 schema。', 422)
        tables = {r['table_name'] for r in db.execute('SELECT table_name FROM information_schema.tables WHERE table_schema=%s', (schema,))}
        # Partial schemas can otherwise fall through to public tables and mix data.
        required = {'documents','document_access','ingestion_jobs','chunk_sets','chunks','document_chunk_heads','ko_collections','ko_vector_heads','ko_chunks','ko_documents','ko_embeddings','ko_embedding_batches','ingestion_events','conversations','conversation_messages'}
        if tables and not required.issubset(tables):
            raise AppError('SCHEMA_INCOMPLETE', '目标 schema 缺少知识库表，请使用完整的知识库 schema。', 422)
        collections = [dict(r) for r in db.execute('SELECT collection,signature,dimensions FROM ko_collections ORDER BY collection')] if 'ko_collections' in tables else []
        return {'ok': True, 'schemas': schemas, 'collections': collections, 'empty': not tables}

@router.post('/workbench/connections/database/test')
async def test_database(body: DatabaseConnection, request: Request, identity: Identity = Depends(get_identity)):
    url = database_url(request.app, body)
    try:
        return await asyncio.to_thread(inspect_database, url, body.schema_name)
    except AppError:
        raise
    except Exception:
        raise AppError('DATABASE_CONNECTION_FAILED', '数据库连接失败，请检查地址、认证、网络及访问权限。', 502) from None

@router.post('/workbench/connections/embedding/test')
async def test_embedding(body: EmbeddingConnection, request: Request, identity: Identity = Depends(get_identity)):
    e = embedding_candidate(request.app, body)
    try:
        result = await make_embedding_client(e).embed(['知识库连接测试'])
        validate_result(result, e, 1)
    except Exception:
        raise AppError('EMBEDDING_TEST_FAILED', 'Embedding 调用失败或返回维度不匹配。', 502) from None
    return {'ok': True, 'dimensions': e.dimensions}

@router.post('/workbench/connections/rerank/test')
async def test_rerank(body: ProviderConnection, request: Request, identity: Identity = Depends(get_identity)):
    r = rerank_candidate(request.app, body)
    try:
        await make_rerank_client(r).rerank('如何申请权限', ['提交权限申请。', '天气晴朗。'])
    except Exception:
        raise AppError('RERANK_TEST_FAILED', 'Rerank 调用失败，请检查完整接口地址、模型和密钥。', 502) from None
    return {'ok': True}

@router.put('/workbench/connections')
async def apply_connections(body: Connections, request: Request, identity: Identity = Depends(get_identity)):
    from app.main import create_app
    from app.api.workbench_session import unfinished
    app = request.app
    if app.state.management_active_requests != 1 or app.state.management_changing:
        raise AppError('WORKSPACE_BUSY', '有请求正在执行，请结束当前操作后再应用连接。', 409)
    app.state.management_changing = True
    try:
        if await asyncio.to_thread(unfinished, app):
            raise AppError('ACTIVE_TASK', '请先完成或取消未结束的 Agent 任务。', 409)
        url = database_url(app, body.database)
        e, r = embedding_candidate(app, body.embedding), rerank_candidate(app, body.rerank)
        discovery = await asyncio.to_thread(inspect_database, url, body.database.schema_name)
        if discovery['empty']:
            raise AppError('SCHEMA_EMPTY', '请先通过项目初始化命令初始化目标 schema，再应用连接。', 422)
        selected = next((c for c in discovery['collections'] if c['collection'] == e.collection), None)
        if selected and selected['signature'] != e.signature:
            raise AppError('EMBEDDING_CONFIG_MISMATCH', '集合与 Embedding 的模型、维度或地址不匹配。请恢复原配置或使用新集合。', 409)
        s = app.state.settings
        rs = replace(s.retrieval, rerank=r, strategy=s.retrieval.strategy.removesuffix('_rerank') if r.provider == 'none' else s.retrieval.strategy)
        business_url = s.agent.business_database_url
        if business_url:
            business_url = (make_conninfo(business_url, options=f'-c search_path={body.database.schema_name},public')
                            if database_identity(business_url) == database_identity(url) else None)
        settings = replace(s, database_url=url, vector_database_url=url, embedding=e, retrieval=rs,
                           agent=replace(s.agent, database_url=url, business_database_url=business_url))
        candidate = create_app(settings, generation_client=app.state.chat.client)
        for name in ['ingestion_repository', 'chunk_repository', 'conversations', 'vector_repository']:
            await asyncio.to_thread(getattr(candidate.state, name).initialize)
        if candidate.state.persistent_agent:
            repo = candidate.state.persistent_agent.repo
            pinned = repo.settings
            repo.settings = replace(pinned, database_url=make_conninfo(url, options=f'-c search_path={body.database.schema_name}'))
            try:
                await asyncio.to_thread(repo.initialize)
            finally:
                repo.settings = pinned
            if await asyncio.to_thread(unfinished, candidate):
                raise AppError('ACTIVE_TASK', '目标数据库存在未结束的真实 Agent 任务，请先恢复并处理原任务。', 409)
        app.state.ingestion_stop.set()
        if app.state.ingestion_task:
            await app.state.ingestion_task
        if app.state.persistent_agent:
            await app.state.persistent_agent.shutdown()
        for name in ['settings', 'ingestion_repository', 'chunk_repository', 'conversations', 'vector_repository',
                     'vector_service', 'retrieval_service', 'document_repository', 'documents', 'ingestion_worker', 'chat', 'persistent_agent']:
            setattr(app.state, name, getattr(candidate.state, name))
        app.state.ingestion_stop = asyncio.Event()
        app.state.ingestion_task = (asyncio.create_task(app.state.ingestion_worker.run(app.state.ingestion_stop)) if settings.ingestion_worker_enabled else None)
        return {'ok': True, 'message': '连接已应用。请新建会话并重新开始评测。'}
    except AppError:
        raise
    except Exception:
        raise AppError('CONNECTION_APPLY_FAILED', '配置未应用，请检查数据库权限与模型配置。', 502) from None
    finally:
        app.state.management_changing = False

def document_rows(app, identity):
    with app.state.ingestion_repository.connection() as db:
        return [dict(row) for row in db.execute('''SELECT d.document_id,d.filename,d.title,d.source,d.version,
            d.uploaded_by,j.status,j.task_id,j.error_code,h.chunk_set_id,
            (SELECT count(*) FROM chunks c WHERE c.chunk_set_id=h.chunk_set_id) AS chunks,
            COALESCE(v.chunk_count,0) AS indexed_chunks,(d.uploaded_by=%s) AS owned,
            CASE WHEN d.uploaded_by=%s THEN ARRAY(SELECT user_id FROM document_access da WHERE da.document_id=d.document_id) ELSE ARRAY[]::text[] END AS user_ids
            FROM documents d JOIN document_access a USING(document_id) JOIN ingestion_jobs j USING(document_id)
            LEFT JOIN document_chunk_heads h USING(document_id)
            LEFT JOIN ko_vector_heads v ON v.document_id=d.document_id AND v.chunk_set_id=h.chunk_set_id AND v.collection=%s
            WHERE d.active=1 AND d.tenant_id=%s AND a.user_id=%s ORDER BY d.created_at DESC''',
            (identity.user_id, identity.user_id, app.state.settings.embedding.collection, identity.tenant_id, identity.user_id))]

@router.get('/workbench/knowledge')
async def knowledge(request: Request, identity: Identity = Depends(get_identity)):
    from app.evals.imported import runtime_fingerprint
    return {'documents': await asyncio.to_thread(document_rows, request.app, identity),
            'runtime_fingerprint': runtime_fingerprint(request.app),
            'max_document_bytes': request.app.state.settings.max_document_bytes,
            'embedding_ready': request.app.state.settings.embedding.provider == 'compatible'}

def readiness(app, identity, semantic=True):
    rows = document_rows(app, identity)
    indexed = sum(r['indexed_chunks'] > 0 for r in rows)
    reasons = []
    if app.state.chat.settings.provider != 'compatible':
        reasons.append('尚未配置真实生成模型')
    if semantic and app.state.settings.embedding.provider != 'compatible':
        reasons.append('尚未配置真实 Embedding')
    if not indexed:
        reasons.append('当前身份在所选集合中没有可检索的文档，请检查 schema、集合、权限与索引状态')
    return {'ready': not reasons, 'reasons': reasons, 'documents': len(rows), 'indexed_documents': indexed,
            'indexed_chunks': sum(r['indexed_chunks'] for r in rows),
            'schema': db_view(app.state.settings.database_url)['schema_name'], 'collection': app.state.settings.embedding.collection if app.state.settings.embedding.provider == 'compatible' else '未选择真实集合'}

@router.get('/workbench/readiness')
async def check_readiness(request: Request, semantic: bool = True, identity: Identity = Depends(get_identity)):
    return await asyncio.to_thread(readiness, request.app, identity, semantic)

@router.post('/workbench/knowledge/{document_id}/preview')
async def preview(document_id: str, body: ChunkBuildRequest, request: Request, identity: Identity = Depends(get_identity)):
    def read():
        with request.app.state.ingestion_repository.connection() as db:
            d = request.app.state.chunk_repository._document(db, document_id, identity)
        chunks = chunk_document(document_id, d['text'], title=d['title'], source=d['source'], version=d['version'],
            parser_version=d['parser_version'], config=ChunkConfig(**body.model_dump()), markdown=d['filename'].lower().endswith('.md'), generation=d['revision'])
        return {'text': d['text'], 'count': len(chunks), 'chunks': [asdict(c) for c in chunks[:100]],
                'truncated': len(chunks) > 100, 'revision': d['revision']}
    return await asyncio.to_thread(read)

class KnowledgeBuildRequest(ChunkBuildRequest):
    force: bool = False


@router.post('/workbench/knowledge/{document_id}/build')
async def build(document_id: str, body: KnowledgeBuildRequest, request: Request, identity: Identity = Depends(get_identity)):
    app = request.app
    if app.state.settings.embedding.provider != 'compatible':
        raise AppError('EMBEDDING_NOT_CONFIGURED', '请先配置真实 Embedding，再构建索引。', 422)
    await asyncio.to_thread(app.state.document_repository.check_owner, document_id, identity)
    started, run_id = time.perf_counter(), str(uuid4())
    if not body.force:
        try:
            current = await asyncio.to_thread(app.state.chunk_repository.get, document_id, identity)
        except AppError as exc:
            if exc.code != 'CHUNKS_NOT_BUILT':
                raise
        else:
            if current['count'] and await asyncio.to_thread(app.state.vector_repository.ready,
                    document_id, current['chunk_set_id'], current['count']):
                return {'count': current['count'], 'reused': True, 'duration_ms': 0, 'run_id': None}
    snapshot = await asyncio.to_thread(app.state.chunk_repository.build, document_id, identity,
                                      ChunkConfig(**body.model_dump(exclude={'force'})), publish=False)
    if not snapshot['count']:
        raise AppError('EMPTY_DOCUMENT', '没有可索引的切块。', 409)
    from app.core.embedding import chunk_input
    vectors = []
    service = app.state.vector_service
    size = service.settings.batch_size
    for number, start in enumerate(range(0, snapshot['count'], size)):
        vectors.extend(await service._embed([chunk_input(c) for c in snapshot['chunks'][start:start+size]],
                                             run_id, number, document_id, 'index'))
    def publish():
        with app.state.ingestion_repository.connection() as db:
            current = app.state.document_repository._owner(db, document_id, identity)
            app.state.chunk_repository._document(db, document_id, identity, lock=True)
            if current['revision'] != snapshot['document_revision']:
                raise AppError('DOCUMENT_CHANGED', '文档已变化，请重新预览并构建。', 409)
            app.state.vector_repository.publish(snapshot, identity.tenant_id, vectors, transaction=db)
            db.execute("""INSERT INTO document_chunk_heads VALUES (%s,%s)
                ON CONFLICT(document_id) DO UPDATE SET chunk_set_id=excluded.chunk_set_id""",
                (document_id, snapshot['chunk_set_id']))
    await asyncio.to_thread(publish)
    return {'count': snapshot['count'], 'reused': False, 'run_id': run_id, 'duration_ms': (time.perf_counter()-started)*1000}
