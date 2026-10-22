"""Background queue survives request boundaries, cancellation and stale leases."""
import asyncio
from dataclasses import replace
from app.core.identity import LOCAL_IDENTITIES
from tests.integration.test_workbench_api import workbench_app,HEADERS
from tests.integration.test_document_versions import draft,chunks

def enqueue(client,doc,**extra):
    r=client.post('/index-jobs',headers=HEADERS,json={'document_id':doc,**extra})
    assert r.status_code==202,r.text
    return r.json()

def test_queue_is_durable_deduplicated_owner_scoped_and_completes(workbench_app):
    app,client,receipts=workbench_app;doc=receipts[0]['document_id']
    job=enqueue(client,doc,force=True,size=80)
    assert enqueue(client,doc,force=True,size=80)['job_id']==job['job_id']
    assert client.get('/index-jobs/'+job['job_id'],headers={'X-Local-User-Id':'u_employee_001'}).status_code==404
    assert client.get('/index-jobs',headers=HEADERS).json()['items'][0]['status']=='queued'
    app.state.ingestion_repository.initialize()  # Simulate an idempotent startup before any processing.
    asyncio.run(app.state.index_worker.process_next())
    read=client.get('/index-jobs/'+job['job_id'],headers=HEADERS).json()
    assert read['status']=='succeeded' and read['completed']==read['total']>0

def test_cancelled_draft_cannot_publish_and_retry_succeeds(workbench_app):
    app,client,receipts=workbench_app;doc=receipts[0]['document_id'];v=draft(client,doc)
    job=enqueue(client,doc,version_id=v['version_id'])
    assert client.post('/index-jobs/'+job['job_id']+'/cancel',headers=HEADERS).json()['status']=='cancelled'
    assert asyncio.run(app.state.index_worker.process_next()) is False
    assert client.post(f"/documents/{doc}/versions/{v['version_id']}/publish",headers=HEADERS,json={'expected_revision':1}).status_code==409
    assert client.post('/index-jobs/'+job['job_id']+'/retry',headers=HEADERS).json()['status']=='queued'
    asyncio.run(app.state.index_worker.process_next())
    assert client.get('/index-jobs/'+job['job_id'],headers=HEADERS).json()['status']=='succeeded'

def test_expired_lease_reclaimed_stale_callback_cannot_settle(workbench_app):
    app,client,receipts=workbench_app;job=enqueue(client,receipts[0]['document_id'],force=True)
    first=app.state.index_jobs.claim()
    with app.state.ingestion_repository.connection() as db:db.execute('UPDATE ko_index_jobs SET lease_until=0 WHERE job_id=%s',(job['job_id'],))
    asyncio.run(app.state.index_worker.process_next())
    app.state.index_jobs.settle(first,'failed','STALE_CALLBACK')
    read=client.get('/index-jobs/'+job['job_id'],headers=HEADERS).json()
    assert read['status']=='succeeded' and read['attempts']==2 and read['error_code'] is None

def test_configuration_mismatch_blocks_and_explicit_retry_rebinds(workbench_app):
    app,client,receipts=workbench_app;job=enqueue(client,receipts[0]['document_id'])
    saved=app.state.vector_service.settings
    app.state.vector_service.settings=replace(saved,collection='another-collection')
    asyncio.run(app.state.index_worker.process_next())
    assert client.get('/index-jobs/'+job['job_id'],headers=HEADERS).json()['status']=='blocked'
    app.state.vector_service.settings=saved
    client.post('/index-jobs/'+job['job_id']+'/retry',headers=HEADERS)
    asyncio.run(app.state.index_worker.process_next())
    assert client.get('/index-jobs/'+job['job_id'],headers=HEADERS).json()['status']=='succeeded'

def test_cancel_running_embed_fences_publication_and_worker_remains_usable(workbench_app):
    app,client,receipts=workbench_app;doc=receipts[0]['document_id'];before=chunks(client,doc)
    job=enqueue(client,doc,force=True,size=80)
    original=app.state.vector_service.client
    class CancelDuringEmbedding:
        async def embed(self,texts):
            app.state.index_jobs.control(job['job_id'],LOCAL_IDENTITIES['u_engineer_001'],'cancel',app.state.settings.embedding)
            return await original.embed(texts)
    app.state.vector_service.client=CancelDuringEmbedding()
    asyncio.run(app.state.index_worker.process_next())
    assert client.get('/index-jobs/'+job['job_id'],headers=HEADERS).json()['status']=='cancelled'
    assert chunks(client,doc)==before
    app.state.vector_service.client=original
    client.post('/index-jobs/'+job['job_id']+'/retry',headers=HEADERS)
    asyncio.run(app.state.index_worker.process_next())
    assert client.get('/index-jobs/'+job['job_id'],headers=HEADERS).json()['status']=='succeeded'


def test_cross_schema_vector_configuration_cannot_publish(workbench_app):
    from psycopg.conninfo import make_conninfo
    app,client,receipts=workbench_app
    app.state.settings=replace(app.state.settings,vector_database_url=make_conninfo(app.state.settings.database_url,options='-c search_path=public'))
    response=client.post('/index-jobs',headers=HEADERS,json={'document_id':receipts[0]['document_id']})
    assert response.status_code==409 and response.json()['error']['code']=='ATOMIC_PUBLICATION_UNAVAILABLE'
