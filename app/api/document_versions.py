"""Owner-only document history; normal retrieval still reads only current published chunks."""
import asyncio
from psycopg.conninfo import conninfo_to_dict
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import BaseModel, Field
from app.api.schemas import ChunkBuildRequest
from app.core.errors import AppError
from app.core.identity import Identity, get_identity
from app.db.document_versions import DocumentVersions
from app.db.postgres import database_identity

router = APIRouter()

def repo(request):
    return DocumentVersions(request.app.state.ingestion_repository)

class Revision(BaseModel):
    expected_revision: int = Field(ge=1)

class Restore(Revision):
    version: str = Field(min_length=1, max_length=64, pattern=r'.*\S.*')
    note: str = Field(min_length=1, max_length=1000, pattern=r'.*\S.*')

class Withdraw(Revision):
    reason: str = Field(min_length=1, max_length=1000, pattern=r'.*\S.*')

@router.get('/documents/{document_id}/versions')
async def versions(document_id: str, request: Request, identity: Identity = Depends(get_identity)):
    return await asyncio.to_thread(repo(request).list, document_id, identity)

@router.get('/documents/{document_id}/access')
async def access(document_id: str, request: Request, identity: Identity = Depends(get_identity)):
    return await asyncio.to_thread(repo(request).access, document_id, identity)

@router.post('/documents/{document_id}/versions', status_code=201)
async def draft(document_id: str, request: Request, file: UploadFile = File(...),
                version: str = Form(min_length=1, max_length=64, pattern=r'.*\S.*'),
                note: str = Form(min_length=1, max_length=1000, pattern=r'.*\S.*'),
                expected_revision: int = Form(ge=1), identity: Identity = Depends(get_identity)):
    try:
        await asyncio.to_thread(request.app.state.document_repository.check_owner, document_id, identity)
        filename, _, content = await request.app.state.documents.read_upload(file)
        return await asyncio.to_thread(repo(request).create, document_id, identity, filename, content,
                                       version, note, expected_revision)
    finally:
        await file.close()

@router.get('/documents/{document_id}/versions/{version_id}')
async def read(document_id: str, version_id: str, request: Request,
               compare_to: str | None = Query(default=None, max_length=128), identity: Identity = Depends(get_identity)):
    return await asyncio.to_thread(repo(request).read, document_id, version_id, identity, compare_to)

@router.post('/documents/{document_id}/versions/{version_id}/restore', status_code=201)
async def restore(document_id: str, version_id: str, body: Restore, request: Request,
                  identity: Identity = Depends(get_identity)):
    return await asyncio.to_thread(repo(request).create, document_id, identity, None, None,
        body.version, body.note, body.expected_revision, version_id)

@router.post('/documents/{document_id}/versions/{version_id}/build',status_code=202)
async def build(document_id: str, version_id: str, body: ChunkBuildRequest, request: Request,
                identity: Identity = Depends(get_identity)):
    from app.api.index_jobs import configured
    configured(request.app)
    return await asyncio.to_thread(request.app.state.index_jobs.enqueue,document_id,version_id,identity,
        body.model_dump(),request.app.state.settings.embedding)

@router.post('/documents/{document_id}/versions/{version_id}/publish')
async def publish(document_id: str, version_id: str, body: Revision, request: Request,
                  identity: Identity = Depends(get_identity)):
    app = request.app
    await asyncio.to_thread(app.state.document_repository.check_owner, document_id, identity)
    if (not app.state.vector_repository
        or database_identity(app.state.settings.database_url) != database_identity(app.state.settings.vector_database_url)
        or conninfo_to_dict(app.state.settings.database_url).get('options', '') != conninfo_to_dict(app.state.settings.vector_database_url).get('options', '')):
        raise AppError('ATOMIC_PUBLICATION_UNAVAILABLE', '发布需要文档与向量位于同一数据库空间。', 409)
    return await asyncio.to_thread(repo(request).publish, document_id, version_id, identity,
                                  body.expected_revision, app.state.vector_repository)

@router.post('/documents/{document_id}/withdraw')
async def withdraw(document_id: str, body: Withdraw, request: Request, identity: Identity = Depends(get_identity)):
    return await asyncio.to_thread(repo(request).withdraw, document_id, identity, body.expected_revision, body.reason)
