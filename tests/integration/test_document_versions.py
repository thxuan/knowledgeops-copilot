"""End-to-end lifecycle invariants against real PostgreSQL; model doubles only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from app.core.embedding import EmbeddingError
from app.core.identity import LOCAL_IDENTITIES, Identity
from app.db.document_versions import DocumentVersions
from app.ingestion.chunker import ChunkConfig
from tests.integration.test_workbench_api import workbench_app, HEADERS

EMPLOYEE = {'X-Local-User-Id': 'u_employee_001'}
SUPPORT = {'X-Local-User-Id': 'u_support_001'}


def history(client, doc):
    r = client.get(f'/documents/{doc}/versions', headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def draft(client, doc, label='v2', minutes=5, revision=None):
    revision = revision or history(client, doc)['revision']
    text = f'---\ntitle: 演示政策\nversion: {label}\n---\n# 演示政策\n虚构 VPN 合规刷新时间为 {minutes} 分钟。\n'
    r = client.post(f'/documents/{doc}/versions', headers=HEADERS,
        files={'file': ('policy.md', text.encode())},
        data={'version': label, 'note': f'刷新调整为 {minutes} 分钟', 'expected_revision': revision})
    assert r.status_code == 201, r.text
    return r.json()


def build(client, doc, version):
    r = client.post(f'/documents/{doc}/versions/{version}/build', headers=HEADERS, json={})
    assert r.status_code == 202, r.text
    asyncio.run(client.app.state.index_worker.process_next())
    job=client.get('/index-jobs/'+r.json()['job_id'],headers=HEADERS).json()
    assert job['status']=='succeeded',job
    return job


def publish(client, doc, version, revision, users=None):
    cache=getattr(client,'_review_calls',{});client._review_calls=cache
    key=(doc,version,revision)
    if key not in cache:
        scope=users or client.get(f'/documents/{doc}/access',headers=HEADERS).json()['user_ids'] or ['u_engineer_001']
        r=client.post(f'/documents/{doc}/versions/{version}/review',headers=HEADERS,json={'expected_revision':revision,'user_ids':scope})
        if r.status_code!=202:return r
        cache[key]=r.json()['review_id']
    return client.post('/document-reviews/'+cache[key]+'/approve',headers={'X-Local-User-Id':'u_admin_001'},json={})


def grant(client, doc):
    v=draft(client,doc,label='scope-'+__import__('uuid').uuid4().hex[:8])
    build(client,doc,v['version_id'])
    return publish(client,doc,v['version_id'],v['base_revision'],['u_engineer_001','u_employee_001'])


def chunks(client, doc):
    r = client.get(f'/documents/{doc}/chunks', headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def retrieve(client, doc, headers=HEADERS):
    r = client.post('/retrieve', headers=headers, json={'query':'VPN 合规刷新', 'strategy':'hybrid',
        'top_k':5, 'filters':{'document_ids':[doc]}})
    assert r.status_code == 200, r.text
    return r.json()['hits']


def test_draft_is_private_search_stays_old_until_atomic_publish_and_restore(workbench_app):
    app, client, receipts = workbench_app
    doc = receipts[0]['document_id']
    initial = history(client, doc)
    old_version = initial['versions'][0]['version_id']
    old = chunks(client, doc)
    old_content = client.get(f'/documents/{doc}/versions/{old_version}', headers=HEADERS).json()
    new = draft(client, doc)
    assert chunks(client, doc) == old
    assert publish(client, doc, new['version_id'], initial['revision']).status_code == 409
    build(client, doc, new['version_id'])
    assert chunks(client, doc) == old  # Ready != published.
    assert all(h['version'] == old['chunks'][0]['version'] for h in retrieve(client, doc))
    assert all('虚构 VPN' not in h['text'] for h in retrieve(client, doc))
    assert app.state.vector_repository.ready(doc, old['chunk_set_id'], old['count'])
    result = publish(client, doc, new['version_id'], initial['revision'])
    assert result.status_code == 200, result.text
    current = chunks(client, doc)
    assert current['chunk_set_id'] != old['chunk_set_id']
    assert all(c['version'] == 'v2' for c in current['chunks'])
    hits=retrieve(client, doc)
    assert hits and all(h['version']=='v2' and h['citation']['version']=='v2' for h in hits)
    assert any('5 分钟' in c['text'] for c in current['chunks'])
    assert app.state.vector_repository.ready(doc, current['chunk_set_id'], current['count'])
    assert client.get(f"/citations/{old['chunks'][0]['chunk_id']}?chunk_set_id={old['chunk_set_id']}", headers=HEADERS).status_code == 404
    assert publish(client, doc, new['version_id'], initial['revision']).json()['reused'] is True
    diff = client.get(f"/documents/{doc}/versions/{new['version_id']}?compare_to={old_version}", headers=HEADERS).json()
    assert '+虚构 VPN 合规刷新时间为 5 分钟。' in diff['diff']
    assert client.get(f'/documents/{doc}/versions/{old_version}', headers=HEADERS).json() == old_content
    rollback = client.post(f'/documents/{doc}/versions/{old_version}/restore', headers=HEADERS,
        json={'version':'v3','note':'恢复原文','expected_revision':result.json()['revision']})
    assert rollback.status_code == 201, rollback.text
    build(client, doc, rollback.json()['version_id'])
    assert publish(client, doc, rollback.json()['version_id'], result.json()['revision']).status_code == 200
    assert len(history(client, doc)['versions']) == 3
    assert all(c['version'] == 'v3' for c in chunks(client, doc)['chunks'])
    assert client.get(f"/documents/{doc}/versions/{rollback.json()['version_id']}", headers=HEADERS).json()['original_text'] == old_content['original_text']


def test_failed_build_preserves_search_and_can_retry(workbench_app):
    app, client, receipts = workbench_app
    doc = receipts[0]['document_id']
    old = chunks(client, doc)
    v = draft(client, doc)['version_id']
    original = app.state.vector_service.client
    class Fails:
        async def embed(self, texts):
            raise EmbeddingError('test provider unavailable')
    app.state.vector_service.client = Fails()
    failed = client.post(f'/documents/{doc}/versions/{v}/build', headers=HEADERS, json={})
    assert failed.status_code == 202, failed.text
    asyncio.run(app.state.index_worker.process_next())
    assert client.get('/index-jobs/'+failed.json()['job_id'],headers=HEADERS).json()['status']=='failed'
    assert history(client, doc)['versions'][0]['build_status'] == 'failed'
    assert chunks(client, doc) == old
    app.state.vector_service.client = original
    build(client, doc, v)
    assert history(client, doc)['versions'][0]['build_status'] == 'ready'


def test_permission_explanation_history_is_private_and_revocation_blocks_reader_access(workbench_app):
    _, client, receipts = workbench_app
    doc = receipts[0]['document_id']
    v = history(client, doc)['versions'][0]['version_id']
    url = f'/documents/{doc}'
    assert client.get(url+'/access', headers=EMPLOYEE).status_code == 404
    assert client.put(url+'/permissions',headers=HEADERS,json={'user_ids':['u_engineer_001','u_employee_001']}).status_code==409
    r = grant(client,doc)
    assert r.status_code == 200
    rights = client.get(url+'/access', headers=EMPLOYEE).json()
    assert rights['can_read'] and not rights['can_view_history']
    assert rights['user_ids'] == ['u_employee_001']
    for suffix in ['/versions', f'/versions/{v}', f'/versions/{v}?compare_to={v}']:
        assert client.get(url+suffix, headers=EMPLOYEE).status_code == 404
    assert client.post(url+f'/versions/{v}/restore', headers=EMPLOYEE,
                       json={'version':'x','note':'x','expected_revision':1}).status_code == 404
    assert client.get(url+'/chunks', headers=EMPLOYEE).status_code == 200
    c = chunks(client, doc)
    cite = f"/citations/{c['chunks'][0]['chunk_id']}?chunk_set_id={c['chunk_set_id']}"
    assert client.get(cite, headers=EMPLOYEE).status_code == 200
    client.put(url+'/permissions', headers=HEADERS, json={'user_ids':['u_engineer_001']})
    for suffix in ['/access','/chunks','/versions',f'/versions/{v}']:
        assert client.get(url+suffix, headers=EMPLOYEE).status_code == 404
    assert client.get(cite, headers=EMPLOYEE).status_code == 404
    assert retrieve(client, doc, EMPLOYEE) == []
    assert client.get('/workbench/knowledge', headers=EMPLOYEE).json()['documents'] == []
    events = history(client, doc)['events']
    assert len([e for e in events if e['kind']=='permissions_changed']) == 1
    assert any(e['kind']=='review_approved' for e in events)
    # IT ticket capabilities confer no document/history rights.
    assert client.get(url+'/access', headers=SUPPORT).status_code == 404


def test_withdraw_blocks_legacy_rebuild_and_publish_requires_fresh_base(workbench_app):
    _, client, receipts = workbench_app
    doc = receipts[0]['document_id']
    initial = history(client, doc)
    version = draft(client, doc)['version_id']
    build(client, doc, version)
    c = chunks(client, doc)
    assert grant(client,doc).status_code==200
    initial=history(client,doc)
    r = client.post(f'/documents/{doc}/withdraw', headers=HEADERS,
                    json={'expected_revision': initial['revision'], 'reason':'政策错误，立即撤回'})
    assert r.status_code == 200, r.text
    assert client.get(f"/citations/{c['chunks'][0]['chunk_id']}?chunk_set_id={c['chunk_set_id']}", headers=HEADERS).status_code == 404
    for headers in [HEADERS, EMPLOYEE]:
        assert client.get(f'/documents/{doc}/chunks', headers=headers).status_code == 404
        assert client.post(f'/documents/{doc}/chunks', headers=headers, json={}).status_code == 404
    assert client.get('/workbench/knowledge', headers=EMPLOYEE).json()['documents'] == []
    assert history(client, doc)['withdrawn'] is True
    assert retrieve(client, doc) == []
    assert publish(client, doc, version, r.json()['revision']).status_code == 409
    fresh = draft(client, doc, label='v3')['version_id']
    build(client, doc, fresh)
    assert publish(client, doc, fresh, r.json()['revision']).status_code == 200
    assert not history(client, doc)['withdrawn']
    assert client.get(f'/documents/{doc}/chunks', headers=EMPLOYEE).status_code == 200


def test_concurrent_publish_conflicts_and_model_change_does_not_publish(workbench_app):
    app, client, receipts = workbench_app
    doc = receipts[0]['document_id']
    a, b = draft(client, doc, label='v2'), draft(client, doc, label='v3', minutes=7)
    build(client, doc, a['version_id']);build(client, doc, b['version_id'])
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda v: publish(client, doc, v['version_id'], v['base_revision']), [a,b]))
    assert sorted(r.status_code for r in results) == [200,409]
    state = history(client, doc)
    fresh = draft(client, doc, label='v4')['version_id']
    build(client, doc, fresh)
    saved = app.state.vector_repository.settings
    app.state.vector_repository.settings = replace(saved, collection='changed')
    result = publish(client, doc, fresh, state['revision'])
    assert result.status_code == 409, result.text
    assert history(client, doc)['revision'] == state['revision']
    app.state.vector_repository.settings = saved


def test_interrupted_build_fences_stale_completion_and_restart_preserves_history(workbench_app):
    app, client, receipts = workbench_app
    doc = receipts[0]['document_id']
    v = draft(client, doc)['version_id']
    r = DocumentVersions(app.state.ingestion_repository)
    identity = LOCAL_IDENTITIES['u_engineer_001']
    token, _ = r.start_build(doc, v, identity, ChunkConfig())
    assert client.post(f'/documents/{doc}/versions/{v}/build', headers=HEADERS, json={}).status_code == 409
    with app.state.ingestion_repository.connection() as db:
        db.execute('UPDATE document_version_builds SET lease_until=0 WHERE version_id=%s', (v,))
    assert history(client, doc)['versions'][0]['error_code'] == 'BUILD_INTERRUPTED'
    build(client, doc, v)
    import pytest
    from app.core.errors import AppError
    with pytest.raises(AppError, match='构建已过期'):
        r.finish_build(doc, v, identity, token, error_code='LATE_FAILURE')
    app.state.ingestion_repository.initialize()  # Idempotent migration/restart.
    assert len(history(client, doc)['versions']) == 2
    assert history(client, doc)['versions'][0]['build_status'] == 'ready'
    wrong_tenant = Identity(identity.user_id, 'other_tenant', frozenset())
    with pytest.raises(AppError):
        r.read(doc, v, wrong_tenant)


def test_legacy_update_cannot_bypass_review_and_revocation_does_not_strand_owner(workbench_app):
    app, client, receipts = workbench_app
    doc = receipts[0]['document_id']
    initial = history(client, doc)
    old = initial['versions'][0]
    old_text = client.get(f"/documents/{doc}/versions/{old['version_id']}", headers=HEADERS).json()['original_text']
    r = client.put(f'/documents/{doc}', headers=HEADERS,
        files={'file':('change.md',b'# Changed\nLegacy update\n')},
        data={'version':'legacy-v2','expected_version':old['label']})
    assert r.status_code == 409, r.text
    assert len(history(client, doc)['versions']) == 1
    assert client.get(f"/documents/{doc}/versions/{old['version_id']}", headers=HEADERS).json()['original_text'] == old_text
    client.put(f'/documents/{doc}/permissions', headers=HEADERS, json={'user_ids':[]})
    listed = client.get('/workbench/knowledge', headers=HEADERS).json()['documents']
    assert any(d['document_id']==doc and d['owned'] for d in listed)
    assert client.get(f'/documents/{doc}/access', headers=HEADERS).json()['can_read']
    assert client.get('/workbench/readiness', headers=HEADERS).json()['documents']==2
    assert history(client, doc)['versions']
    assert client.get(f'/documents/{doc}/chunks', headers=HEADERS).status_code == 200


def test_snapshot_roundtrip_preserves_originals_drafts_and_audit(workbench_app, tmp_path):
    from app.db.snapshots import evaluation_snapshot
    from psycopg import sql
    app, client, receipts = workbench_app
    doc=receipts[0]['document_id']
    v=draft(client, doc)['version_id']
    build(client, doc, v)
    source=DocumentVersions(app.state.ingestion_repository)
    identity=LOCAL_IDENTITIES['u_engineer_001']
    before=source.list(doc,identity)
    snapshot,schema=evaluation_snapshot(app.state.settings.database_url,tmp_path)
    try:
        restored=DocumentVersions(snapshot)
        assert restored.list(doc,identity)==before
        assert restored.read(doc,v,identity)==source.read(doc,v,identity)
    finally:
        with snapshot.connection() as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
