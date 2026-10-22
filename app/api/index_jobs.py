import asyncio
from psycopg.conninfo import conninfo_to_dict
from app.db.postgres import database_identity
from pathlib import Path
from fastapi import APIRouter,Depends,Request,Query
from fastapi.responses import FileResponse
from pydantic import Field
from app.core.errors import AppError
from app.core.identity import Identity,get_identity
from app.api.schemas import ChunkBuildRequest
router=APIRouter()
class Submit(ChunkBuildRequest):
    document_id:str=Field(min_length=1,max_length=128)
    version_id:str|None=Field(default=None,max_length=128)
    force:bool=False
class Batch(ChunkBuildRequest):
    document_ids:list[str]=Field(min_length=1,max_length=100)
    force:bool=False

def configured(app):
    if not app.state.vector_service or app.state.settings.embedding.provider!='compatible':
        raise AppError('EMBEDDING_NOT_CONFIGURED','请由平台管理员配置真实 Embedding。',422)
    a,b=app.state.settings.database_url,app.state.settings.vector_database_url
    if database_identity(a)!=database_identity(b) or conninfo_to_dict(a).get('options','')!=conninfo_to_dict(b).get('options',''):
        raise AppError('ATOMIC_PUBLICATION_UNAVAILABLE','文档与向量必须位于同一数据库空间。',409)

@router.get('/workbench/index-jobs',include_in_schema=False)
async def page():return FileResponse(Path(__file__).resolve().parents[1]/'web'/'index-jobs.html')

@router.get('/index-jobs')
async def jobs(request:Request,limit:int=Query(50,ge=1,le=100),identity:Identity=Depends(get_identity)):
    return await asyncio.to_thread(request.app.state.index_jobs.list,identity,limit)

@router.get('/index-jobs/{job_id}')
async def read(job_id:str,request:Request,identity:Identity=Depends(get_identity)):
    return await asyncio.to_thread(request.app.state.index_jobs.read,job_id,identity)

@router.post('/index-jobs',status_code=202)
async def submit(body:Submit,request:Request,identity:Identity=Depends(get_identity)):
    configured(request.app)
    return await asyncio.to_thread(request.app.state.index_jobs.enqueue,body.document_id,body.version_id,identity,
        body.model_dump(exclude={'document_id','version_id','force'}),request.app.state.settings.embedding,body.force)

@router.post('/index-jobs/batch',status_code=202)
async def batch(body:Batch,request:Request,identity:Identity=Depends(get_identity)):
    configured(request.app)
    items=[];errors=[]
    for doc in dict.fromkeys(body.document_ids):
        try:
            items.append(await asyncio.to_thread(request.app.state.index_jobs.enqueue,doc,None,identity,
                body.model_dump(exclude={'document_ids','force'}),request.app.state.settings.embedding,body.force))
        except AppError as exc:
            errors.append({'document_id':doc,'code':exc.code,'message':exc.message})
    return {'items':items,'errors':errors}

@router.post('/index-jobs/{job_id}/{action}')
async def control(job_id:str,action:str,request:Request,identity:Identity=Depends(get_identity)):
    if action not in {'cancel','retry'}:raise AppError('NOT_FOUND','Unknown action',404)
    if action=='retry':configured(request.app)
    return await asyncio.to_thread(request.app.state.index_jobs.control,job_id,identity,action,request.app.state.settings.embedding)
