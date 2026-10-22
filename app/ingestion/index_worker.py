"""Request-independent worker. Only a live lease may prepare or publish an index."""
import asyncio
import time
from app.core.embedding import chunk_input,EmbeddingResult,validate_result
from app.core.errors import AppError
from app.core.identity import LOCAL_IDENTITIES
from app.db.document_versions import DocumentVersions
from app.ingestion.chunker import ChunkConfig

class IndexWorker:
    def __init__(self,app):self.app=app;self.active=False

    async def run(self,stop):
        while not stop.is_set():
            try:
                worked=await self.process_next()
            except asyncio.CancelledError:raise
            except Exception:
                worked=False
            if not worked:
                try:await asyncio.wait_for(stop.wait(),timeout=.5)
                except asyncio.TimeoutError:pass

    async def process_next(self):
        state=self.app.state;repo=state.index_jobs
        job=await asyncio.to_thread(repo.claim)
        if job is None:return False
        self.active=True;version_token=None;versions=DocumentVersions(state.ingestion_repository)
        work=None;heartbeat=None
        try:
            identity=await asyncio.to_thread(state.accounts.user,job['user_id']) if state.settings.auth_mode=='accounts' else LOCAL_IDENTITIES.get(job['user_id'])
            if not identity or identity.tenant_id!=job['tenant_id']:
                raise AppError('ACCOUNT_DISABLED','任务提交账号不可用。',403)
            from app.api.index_jobs import configured
            try:configured(self.app)
            except AppError as exc:
                await asyncio.to_thread(repo.settle,job,'blocked',exc.code);return True
            service=state.vector_service
            if not service or service.settings.provider!='compatible' or job['signature']!=service.settings.signature or job['collection']!=service.settings.collection:
                await asyncio.to_thread(repo.settle,job,'blocked','EMBEDDING_CONFIG_CHANGED');return True
            async def operation():
                nonlocal version_token
                config=ChunkConfig(**job['config']);doc=job['document_id']
                if job['version_id']:
                    version_token,snapshot=await asyncio.to_thread(versions.start_build,doc,job['version_id'],identity,config,takeover=True)
                else:
                    await asyncio.to_thread(state.document_repository.check_owner,doc,identity)
                    if not job['force']:
                        try:current=await asyncio.to_thread(state.chunk_repository.get,doc,identity)
                        except AppError as exc:
                            if exc.code!='CHUNKS_NOT_BUILT':raise
                        else:
                            if current['count'] and await asyncio.to_thread(state.vector_repository.ready,doc,current['chunk_set_id'],current['count']):
                                await asyncio.to_thread(repo.progress,job,current['count'],current['count'])
                                await asyncio.to_thread(repo.settle,job,'succeeded');return
                    snapshot=await asyncio.to_thread(state.chunk_repository.build,doc,identity,config,publish=False)
                vectors=[]
                await asyncio.to_thread(repo.progress,job,0,snapshot['count'])
                for number,start in enumerate(range(0,snapshot['count'],service.settings.batch_size)):
                    batch=snapshot['chunks'][start:start+service.settings.batch_size]
                    vectors.extend(await service._embed([chunk_input(c) for c in batch],job['job_id'],number,doc,'index'))
                    await asyncio.to_thread(repo.progress,job,len(vectors),snapshot['count'])
                validate_result(EmbeddingResult(service.settings.model_id,vectors),service.settings,snapshot['count'])
                def commit():
                    # Re-resolve account status before commit, so disabling an account fences its jobs.
                    if state.settings.auth_mode=='accounts':state.accounts.user(identity.user_id)
                    with state.ingestion_repository.connection() as db:
                        current=state.document_repository._owner(db,doc,identity)
                        repo.fenced(db,job)
                        if job['version_id']:
                            versions.finish_build(doc,job['version_id'],identity,version_token,
                                {'snapshot':snapshot,'vectors':vectors,'collection':service.settings.collection,'signature':service.settings.signature},transaction=db)
                        else:
                            state.chunk_repository._document(db,doc,identity,lock=True)
                            if current['revision']!=snapshot['document_revision']:
                                raise AppError('DOCUMENT_CHANGED','正文已变化，请提交新任务。',409)
                            state.vector_repository.publish(snapshot,identity.tenant_id,vectors,transaction=db)
                            db.execute('''INSERT INTO document_chunk_heads VALUES (%s,%s)
                                ON CONFLICT(document_id) DO UPDATE SET chunk_set_id=excluded.chunk_set_id''',(doc,snapshot['chunk_set_id']))
                        db.execute("UPDATE ko_index_jobs SET status='succeeded',stage='completed',completed=total,token=NULL,lease_until=NULL,error_code=NULL,updated_at=%s WHERE job_id=%s",(time.time(),job['job_id']))
                await asyncio.to_thread(commit)
            work=asyncio.create_task(operation())
            async def keep_alive():
                while True:
                    await asyncio.sleep(2)
                    try:
                        if state.settings.auth_mode=='accounts':await asyncio.to_thread(state.accounts.user,job['user_id'])
                        live=await asyncio.to_thread(repo.renew,job,version_token)
                    except Exception:live=False
                    if not live:work.cancel();return
            heartbeat=asyncio.create_task(keep_alive())
            await work
        except BaseException as exc:
            code=getattr(exc,'code','PROCESS_STOPPED' if isinstance(exc,asyncio.CancelledError) else 'INDEX_BUILD_FAILED')
            if job['version_id'] and version_token:
                try:await asyncio.shield(asyncio.to_thread(versions.finish_build,job['document_id'],job['version_id'],identity,version_token,error_code=code))
                except Exception:pass
            await asyncio.shield(asyncio.to_thread(repo.settle,job,'queued' if isinstance(exc,asyncio.CancelledError) else 'failed',code))
            if isinstance(exc,asyncio.CancelledError) and asyncio.current_task().cancelling():raise
        finally:
            if heartbeat:heartbeat.cancel();await asyncio.gather(heartbeat,return_exceptions=True)
            self.active=False
        return True
