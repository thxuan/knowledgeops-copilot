"""Management regression coverage with isolated PostgreSQL schemas."""
from dataclasses import replace
from fastapi.testclient import TestClient
from app.api.workbench_management import db_view
from app.core.embedding import EmbeddingError, EmbeddingResult
from tests.integration.test_workbench_api import workbench_app, HEADERS


def test_library_preview_readiness_acl_and_secret_redaction(workbench_app):
    app, client, receipts = workbench_app
    assert client.get('/knowledge').status_code == 200
    assert client.get('/settings').status_code == 200
    assert client.get('/workbench/knowledge').status_code == 401
    docs = client.get('/workbench/knowledge', headers=HEADERS).json()['documents']
    assert len(docs) == 2 and all(d['indexed_chunks'] > 0 for d in docs)
    doc_id = receipts[0]['document_id']
    before = client.get(f'/documents/{doc_id}/chunks', headers=HEADERS).json()
    preview = client.post(f'/workbench/knowledge/{doc_id}/preview', headers=HEADERS, json={'size': 80})
    assert preview.status_code == 200, preview.text
    assert preview.json()['count'] > before['count']
    assert client.get(f'/documents/{doc_id}/chunks', headers=HEADERS).json() == before
    other = {'X-Local-User-Id': 'u_employee_001'}
    assert client.get('/workbench/knowledge', headers=other).json()['documents'] == []
    assert client.post(f'/workbench/knowledge/{doc_id}/preview', headers=other, json={}).status_code == 404
    config = client.get('/workbench/connections', headers=HEADERS).json()
    assert 'password' not in config['database'] and 'api_key' not in config['embedding']
    ready = client.get('/workbench/readiness?semantic=false', headers=HEADERS).json()
    assert ready['ready'] and ready['indexed_documents'] == 2
    current = app.state.settings
    app.state.settings = replace(current, embedding=replace(current.embedding, provider='unconfigured'))
    assert not client.get('/workbench/readiness', headers=HEADERS).json()['ready']
    blocked = client.post('/workbench/chat', headers=HEADERS, json={'message': 'test'})
    assert blocked.status_code == 422 and blocked.json()['error']['code'] == 'EMBEDDING_NOT_CONFIGURED'
    app.state.settings = current


def test_connection_discovery_mismatch_switch_and_key_boundary(workbench_app):
    app, client, _ = workbench_app
    database = db_view(app.state.settings.database_url)
    database.pop('password_present')
    result = client.post('/workbench/connections/database/test', headers=HEADERS, json=database)
    assert result.status_code == 200, result.text
    assert len(result.json()['collections']) == 1
    old = app.state.settings
    body = {'database': database, 'embedding': {'base_url': 'https://embedding.example.org/v1',
            'model_id': 'real-embedding', 'api_key': 'test-secret', 'dimensions': 16, 'collection': old.embedding.collection}}
    mismatch = client.put('/workbench/connections', headers=HEADERS, json=body)
    assert mismatch.status_code == 409, mismatch.text
    assert app.state.settings == old
    body['embedding']['collection'] = 'new-real-collection'
    applied = client.put('/workbench/connections', headers=HEADERS, json=body)
    assert applied.status_code == 200, applied.text
    assert app.state.settings.embedding.provider == 'compatible'
    assert app.state.settings.agent.business_database_url is None
    assert app.state.chat.client is not None
    body['embedding']['base_url'] = 'https://other.example.org/v1'
    body['embedding']['api_key'] = None
    assert client.put('/workbench/connections', headers=HEADERS, json=body).status_code == 422
    config = client.get('/workbench/connections', headers=HEADERS)
    assert 'test-secret' not in config.text
    assert not client.get('/workbench/readiness', headers=HEADERS).json()['ready']


def test_failed_rebuild_preserves_heads_success_publishes_together(workbench_app):
    app, client, receipts = workbench_app
    # Use a protocol double only inside this test; exercise real configuration paths.
    embedding = replace(app.state.settings.embedding, provider='compatible', model_id='test-embedding',
                        api_key='test-only', collection='atomic-real')
    from app.db.vectors import VectorRepository
    repo = VectorRepository(app.state.settings.database_url, embedding)
    repo.initialize()
    app.state.settings = replace(app.state.settings, embedding=embedding)
    app.state.vector_repository = repo
    app.state.vector_service.vectors = repo
    app.state.vector_service.settings = embedding
    class Failing:
        async def embed(self, texts):
            raise EmbeddingError('test failure')
    app.state.vector_service.client = Failing()
    doc_id = receipts[0]['document_id']
    before = client.get(f'/documents/{doc_id}/chunks', headers=HEADERS).json()
    failed = client.post(f'/workbench/knowledge/{doc_id}/build', headers=HEADERS, json={'size': 80})
    assert failed.status_code == 502, failed.text
    assert client.get(f'/documents/{doc_id}/chunks', headers=HEADERS).json() == before
    class Success:
        async def embed(self, texts):
            return EmbeddingResult(embedding.model_id, [[1.0]+[0.0]*15 for _ in texts])
    app.state.vector_service.client = Success()
    success = client.post(f'/workbench/knowledge/{doc_id}/build', headers=HEADERS, json={'size': 80})
    assert success.status_code == 200, success.text
    after = client.get(f'/documents/{doc_id}/chunks', headers=HEADERS).json()
    assert after['chunk_set_id'] != before['chunk_set_id']
    assert repo.ready(doc_id, after['chunk_set_id'], after['count'])

def test_switch_schema_and_busy_guard(workbench_app):
    from app.main import create_app
    from app.core.config import Settings
    from conftest import pg_dsn
    app, client, _ = workbench_app
    target = pg_dsn('management_target')
    settings = replace(app.state.settings, database_url=target, vector_database_url=target)
    with TestClient(create_app(settings)):
        database = db_view(target)
        database.pop('password_present')
        body = {'database': database, 'embedding': {'base_url': app.state.settings.embedding.base_url,
                'model_id': 'test-embedding', 'api_key': 'test-only', 'dimensions': 16,
                'collection': app.state.settings.embedding.collection}}
        original = app.state.ingestion_repository
        app.state.management_active_requests = 1
        assert client.put('/workbench/connections', headers=HEADERS, json=body).status_code == 409
        assert app.state.ingestion_repository is original
        app.state.management_active_requests = 0
        switched = client.put('/workbench/connections', headers=HEADERS, json=body)
        assert switched.status_code == 200, switched.text
        assert client.get('/workbench/knowledge', headers=HEADERS).json()['documents'] == []
        assert app.state.ingestion_worker.repository is app.state.ingestion_repository
        assert app.state.chat.conversations is app.state.conversations
        with original.connection() as db:
            assert db.execute('SELECT count(*) AS n FROM documents').fetchone()['n'] == 2


def test_batch_build_skips_valid_index_and_force_rebuilds(workbench_app):
    app, client, receipts = workbench_app
    doc_id = receipts[0]['document_id']
    before = client.get(f'/documents/{doc_id}/chunks', headers=HEADERS).json()
    calls = []
    class Counted:
        async def embed(self, texts):
            calls.append(len(texts))
            return EmbeddingResult(app.state.settings.embedding.model_id, [[1.0]+[0.0]*15 for _ in texts])
    app.state.vector_service.client = Counted()
    path = f'/workbench/knowledge/{doc_id}/build'
    skipped = client.post(path, headers=HEADERS, json={'size': 80})
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()['reused'] and not calls
    assert client.get(f'/documents/{doc_id}/chunks', headers=HEADERS).json() == before
    rebuilt = client.post(path, headers=HEADERS, json={'size': 80, 'force': True})
    assert rebuilt.status_code == 200, rebuilt.text
    assert not rebuilt.json()['reused'] and calls
    assert client.post(path, headers={'X-Local-User-Id': 'u_employee_001'}, json={}).status_code == 404


def test_batch_workspace_guard_rejects_before_upload_or_build(workbench_app):
    app, client, receipts = workbench_app
    library = client.get('/workbench/knowledge', headers=HEADERS).json()
    assert library['embedding_ready'] and library['max_document_bytes'] > 0
    stale = {**HEADERS, 'X-Knowledge-Workspace': 'stale'}
    upload = client.post('/documents', headers=stale, files={'file': ('new.txt', b'new document')})
    assert upload.status_code == 409 and upload.json()['error']['code'] == 'WORKSPACE_CHANGED'
    path = f"/workbench/knowledge/{receipts[0]['document_id']}/build"
    assert client.post(path, headers=stale, json={}).status_code == 409
    valid = {**HEADERS, 'X-Knowledge-Workspace': library['runtime_fingerprint']}
    assert client.post(path, headers=valid, json={}).json()['reused']
    assert len(client.get('/workbench/knowledge', headers=HEADERS).json()['documents']) == 2
