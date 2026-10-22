"""HTTP/DB regressions for version-bound document publication review; no external models."""
import asyncio
from dataclasses import replace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.core.config import Settings
from app.core.embedding import EmbeddingSettings
from app.core.identity import Identity
from tests.doubles import HashEmbeddingDouble
from conftest import pg_dsn

OWNER={'X-Local-User-Id':'u_engineer_001'}
ADMIN={'X-Local-User-Id':'u_admin_001'}
READER={'X-Local-User-Id':'u_employee_001'}

@pytest.fixture
def review_app():
    url=pg_dsn('review')
    settings=Settings(app_env='test',database_url=url,vector_database_url=url,
        embedding=EmbeddingSettings(provider='compatible',model_id='test-embedding',api_key='offline',dimensions=16,collection='review-'+uuid4().hex),
        ingestion_worker_enabled=False,index_worker_enabled=False)
    app=create_app(settings)
    app.state.vector_service.client=HashEmbeddingDouble(settings.embedding)
    with TestClient(app) as client:yield app,client

def upload(app,client):
    r=client.post('/documents',headers=OWNER,files={'file':('policy.md','# VPN policy\nOnly approved technical support instructions.\n'.encode())})
    assert r.status_code==202,r.text
    app.state.ingestion_worker.process_next()
    return r.json()['document_id']

def build(app,client,doc,version=None):
    body={'document_id':doc}
    if version:body['version_id']=version
    r=client.post('/index-jobs',headers=OWNER,json=body);assert r.status_code==202,r.text
    assert asyncio.run(app.state.index_worker.process_next())
    job=client.get('/index-jobs/'+r.json()['job_id'],headers=OWNER).json()
    assert job['status']=='succeeded',job
    return job['version_id']

def submit(client,doc,version,revision=1,users=None):
    r=client.post(f'/documents/{doc}/versions/{version}/review',headers=OWNER,json={'expected_revision':revision,'user_ids':users or ['u_engineer_001','u_employee_001']})
    assert r.status_code==202,r.text
    return r.json()

def approve(client,review):
    return client.post('/document-reviews/'+review['review_id']+'/approve',headers=ADMIN,json={})

def hits(client,doc,headers=READER):
    r=client.post('/retrieve',headers=headers,json={'query':'VPN policy','filters':{'document_ids':[doc]}})
    assert r.status_code==200,r.text
    return r.json()['hits']

def test_new_upload_admin_visibility_staged_build_and_approved_scope(review_app):
    app,c=review_app;doc=upload(app,c)
    assert c.get('/workbench/knowledge',headers=READER).json()['documents']==[]
    assert c.get('/workbench/knowledge',headers=ADMIN).json()['documents'][0]['document_id']==doc
    assert c.get(f'/documents/{doc}/access',headers=ADMIN).json()['can_view_history']
    v=build(app,c,doc)
    assert hits(c,doc)==[] and hits(c,doc,OWNER)==[] and hits(c,doc,ADMIN)==[]
    for url in [f'/documents/{doc}/chunks',f'/documents/{doc}/index']:
        assert c.post(url,headers=OWNER,json={}).status_code==409
    assert c.put(f'/documents/{doc}/permissions',headers=OWNER,json={'user_ids':['u_employee_001']}).status_code==409
    assert c.get(f'/documents/{doc}/versions/{v}',headers=ADMIN).status_code==200
    review=submit(c,doc,v)
    assert submit(c,doc,v)['review_id']==review['review_id']
    assert c.post(f'/documents/{doc}/versions/{v}/publish',headers=OWNER,json={'expected_revision':1}).status_code==409
    assert c.post('/document-reviews/'+review['review_id']+'/approve',headers=OWNER,json={}).status_code==403
    assert c.get('/document-reviews',headers=READER).json()['items']==[]
    r=approve(c,review);assert r.status_code==200,r.text
    assert hits(c,doc) and hits(c,doc,ADMIN)
    assert approve(c,review).json()['reused'] is True
    assert c.get(f'/documents/{doc}/versions',headers=READER).status_code==404
    assert c.get(f'/documents/{doc}/versions',headers=ADMIN).status_code==200

def test_rejection_resubmission_and_revocation(review_app):
    app,c=review_app;doc=upload(app,c);v=build(app,c,doc);review=submit(c,doc,v)
    url='/document-reviews/'+review['review_id']+'/reject'
    assert c.post(url,headers=ADMIN,json={}).status_code==422
    assert c.post(url,headers=ADMIN,json={'comment':'补充来源'}).status_code==200
    assert hits(c,doc)==[]
    assert approve(c,review).status_code==409
    new=submit(c,doc,v);assert new['review_id']!=review['review_id']
    assert approve(c,new).status_code==200
    assert c.put(f'/documents/{doc}/permissions',headers=OWNER,json={'user_ids':['u_engineer_001']}).status_code==200
    assert hits(c,doc)==[]
    assert c.get(f'/documents/{doc}/chunks',headers=READER).status_code==404

def test_new_version_keeps_published_old_and_invalidates_pending_on_change(review_app):
    app,c=review_app;doc=upload(app,c);v=build(app,c,doc);assert approve(c,submit(c,doc,v)).status_code==200
    old=c.get(f'/documents/{doc}/chunks',headers=READER).json()
    r=c.post(f'/documents/{doc}/versions',headers=OWNER,files={'file':('policy.md',b'# New policy\nNew VPN technical support policy.\n')},data={'version':'v2','note':'更新','expected_revision':2})
    assert r.status_code==201,r.text
    v2=r.json()['version_id'];build(app,c,doc,v2);review=submit(c,doc,v2,2)
    assert c.get(f'/documents/{doc}/chunks',headers=READER).json()==old
    # Rebuilding changes the staged artifact and invalidates the exact approval request.
    build(app,c,doc,v2)
    assert approve(c,review).status_code==409
    assert c.get(f'/documents/{doc}/chunks',headers=READER).json()==old
    new=submit(c,doc,v2,2);assert approve(c,new).status_code==200
    assert c.get(f'/documents/{doc}/chunks',headers=READER).json()['chunk_set_id']!=old['chunk_set_id']
    assert c.put(f'/documents/{doc}',headers=OWNER,files={'file':('policy.md',b'# Bypass\n')},data={'version':'v3','expected_version':'v2'}).status_code==409

def test_configuration_and_cross_tenant_review_are_fenced(review_app,monkeypatch):
    app,c=review_app;doc=upload(app,c);v=build(app,c,doc);review=submit(c,doc,v)
    from app.core.identity import LOCAL_IDENTITIES
    monkeypatch.setitem(LOCAL_IDENTITIES,'foreign_admin',Identity('foreign_admin','other',frozenset(),platform_capabilities=frozenset({'manage_platform'})))
    assert c.get('/workbench/knowledge',headers={'X-Local-User-Id':'foreign_admin'}).json()['documents']==[]
    assert c.post('/document-reviews/'+review['review_id']+'/approve',headers={'X-Local-User-Id':'foreign_admin'},json={}).status_code==404
    original=app.state.vector_repository.settings
    app.state.vector_repository.settings=replace(original,collection='different')
    assert approve(c,review).status_code==409
    assert hits(c,doc)==[]
    app.state.vector_repository.settings=original
    assert approve(c,review).status_code==200

def test_account_mode_approves_dynamic_reader_and_ignores_spoofed_role(review_app):
    app,c=review_app
    from app.core.identity import LOCAL_IDENTITIES
    app.state.settings=replace(app.state.settings,auth_mode='accounts',auth_cookie_secure=False,public_origin='http://testserver')
    password='Offline-review-password-2026'
    for username,user in [('admin','u_admin_001'),('owner','u_engineer_001')]:
        app.state.accounts.create(username,password,username,LOCAL_IDENTITIES[user])
    reader=Identity('new_reader','tenant_clearwater',frozenset({'all-employees'}))
    app.state.accounts.create('reader',password,'新读者',reader)
    def login(username):
        r=c.post('/auth/login',headers={'X-Requested-With':'KnowledgeOps'},json={'username':username,'password':password})
        assert r.status_code==200,r.text
        return {'X-CSRF-Token':c.get('/auth/me').json()['csrf_token']}
    owner=login('owner')
    r=c.post('/documents',headers=owner,files={'file':('account-review.md',b'# Account workflow\nReviewed content for a newly created reader.\n')})
    assert r.status_code==202;doc=r.json()['document_id'];app.state.ingestion_worker.process_next()
    job=c.post('/index-jobs',headers=owner,json={'document_id':doc});assert job.status_code==202,job.text
    asyncio.run(app.state.index_worker.process_next())
    version=c.get('/index-jobs/'+job.json()['job_id']).json()['version_id']
    r=c.post(f'/documents/{doc}/versions/{version}/review',headers=owner,json={'expected_revision':1,'user_ids':['new_reader']})
    assert r.status_code==202,r.text
    review=r.json()
    assert c.post('/document-reviews/'+review['review_id']+'/approve',headers={**owner,**ADMIN},json={}).status_code==403
    admin=login('admin')
    assert c.get(f'/documents/{doc}/versions').status_code==200
    assert c.post('/document-reviews/'+review['review_id']+'/approve',headers=admin,json={}).status_code==200
    login('reader')
    assert c.get(f'/documents/{doc}/access').json()['can_read']
    assert c.get(f'/documents/{doc}/versions').status_code==404
    assert c.get(f'/documents/{doc}/chunks').status_code==200


def test_approval_is_idempotent_under_concurrent_requests(review_app):
    from concurrent.futures import ThreadPoolExecutor
    app,c=review_app;doc=upload(app,c);version=build(app,c,doc);review=submit(c,doc,version)
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(lambda _:approve(c,review),range(3)))
    assert all(r.status_code==200 for r in results),[r.text for r in results]
    assert sum(not r.json().get('reused',False) for r in results)==1
    with app.state.ingestion_repository.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM document_lifecycle_events WHERE document_id=%s AND kind='review_approved'",(doc,)).fetchone()['n']==1


def test_schema_upgrade_preserves_published_heads_and_private_unindexed_uploads(review_app):
    app,c=review_app;doc=upload(app,c);v=build(app,c,doc);assert approve(c,submit(c,doc,v)).status_code==200
    r=c.post('/documents',headers=OWNER,files={'file':('unindexed.md',b'# Unindexed upload\nPrivate pending source.\n')})
    private=r.json()['document_id'];app.state.ingestion_worker.process_next()
    with app.state.ingestion_repository.connection() as db:db.execute('ALTER TABLE documents DROP COLUMN publication_state')
    app.state.ingestion_repository.initialize()
    with app.state.ingestion_repository.connection() as db:
        assert db.execute('SELECT publication_state FROM documents WHERE document_id=%s',(doc,)).fetchone()['publication_state']=='published'
        assert db.execute('SELECT publication_state FROM documents WHERE document_id=%s',(private,)).fetchone()['publication_state']=='draft'

def test_admin_can_inspect_and_retry_failed_private_ingestion(review_app):
    app,c=review_app
    r=c.post('/documents',headers=OWNER,files={'file':('invalid.txt',b'\xff')});assert r.status_code==202
    app.state.ingestion_worker.process_next();path='/ingestion/tasks/'+r.json()['task_id']
    assert c.get(path,headers=ADMIN).json()['status']=='failed'
    assert c.get(path,headers=READER).status_code==404
    assert c.post(path+'/retry',headers=ADMIN).status_code==202


def test_pending_review_is_invalidated_by_scope_revocation(review_app):
    app,c=review_app;doc=upload(app,c);v=build(app,c,doc);assert approve(c,submit(c,doc,v)).status_code==200
    r=c.post(f'/documents/{doc}/versions',headers=OWNER,files={'file':('v2.md',b'# VPN updated policy\nKeep older content until reviewed.\n')},data={'version':'v2','note':'更新','expected_revision':2})
    v2=r.json()['version_id'];build(app,c,doc,v2);review=submit(c,doc,v2,2)
    assert c.put(f'/documents/{doc}/permissions',headers=OWNER,json={'user_ids':['u_engineer_001']}).status_code==200
    assert approve(c,review).status_code==409
    assert hits(c,doc)==[] and hits(c,doc,OWNER)


def test_pending_review_survives_core_snapshot_restore(review_app,tmp_path):
    from app.db.snapshots import evaluation_snapshot
    from app.db.document_reviews import DocumentReviews
    from app.core.identity import LOCAL_IDENTITIES
    from psycopg import sql
    app,c=review_app;doc=upload(app,c);v=build(app,c,doc);submit(c,doc,v)
    owner=LOCAL_IDENTITIES['u_engineer_001']
    before=DocumentReviews(app.state.ingestion_repository).list(owner)
    snapshot,schema=evaluation_snapshot(app.state.settings.database_url,tmp_path)
    try:assert DocumentReviews(snapshot).list(owner)==before
    finally:
        with snapshot.connection() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

def test_admin_retrieval_and_citations_do_not_depend_on_explicit_acl_rows(review_app):
    app,c=review_app;doc=upload(app,c);v=build(app,c,doc);assert approve(c,submit(c,doc,v)).status_code==200
    assert c.put(f'/documents/{doc}/permissions',headers=OWNER,json={'user_ids':[]}).status_code==200
    assert hits(c,doc,OWNER)==[]
    found=hits(c,doc,ADMIN);assert found
    assert c.get(found[0]['citation']['url'],headers=ADMIN).status_code==200
    app.state.conversations.check_evidence(found,__import__('app.core.identity',fromlist=['LOCAL_IDENTITIES']).LOCAL_IDENTITIES['u_admin_001'])
