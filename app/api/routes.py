import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, Response, Query
from app.api.schemas import ChatRequest, ChatResponse, DocumentResponse, HealthResponse, TaskResponse
from app.core.identity import Identity, get_identity
from app.api.schemas import ChunkBuildRequest, ChunkSetResponse
from app.ingestion.chunker import ChunkConfig


router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@router.post("/documents", response_model=DocumentResponse, status_code=202)
async def upload_document(
    request: Request, file: UploadFile = File(...), identity: Identity = Depends(get_identity),
    source: Annotated[str | None, Form(min_length=1, max_length=512)] = None,
    version: Annotated[str | None, Form(min_length=1, max_length=64)] = None,
) -> DocumentResponse:
    try:
        document = await request.app.state.documents.receive(file, identity, source, version)
    finally:
        await file.close()
    return DocumentResponse(**document)


@router.get("/ingestion/tasks/{task_id}", response_model=TaskResponse)
async def ingestion_status(
    task_id: str, request: Request, identity: Identity = Depends(get_identity)
) -> TaskResponse:
    result = await asyncio.to_thread(request.app.state.ingestion_repository.get_task, task_id, identity)
    return TaskResponse(**result)


@router.post("/ingestion/tasks/{task_id}/retry", response_model=TaskResponse, status_code=202)
async def retry_ingestion(
    task_id: str, request: Request, identity: Identity = Depends(get_identity)
) -> TaskResponse:
    result = await asyncio.to_thread(
        request.app.state.ingestion_repository.retry, task_id, identity,
        request.app.state.settings.ingestion_max_attempts,
    )
    return TaskResponse(**result)


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request, identity: Identity = Depends(get_identity)):
    return await request.app.state.chat_admission.run(request, identity,
        lambda: request.app.state.chat.run(body, identity))


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: str, request: Request, after: int = Query(default=0, ge=0),
                       limit: int = Query(default=50, ge=1, le=100),
                       identity: Identity = Depends(get_identity)):
    return await asyncio.to_thread(request.app.state.conversations.read,
                                   conversation_id, identity, after, limit)




@router.post("/documents/{document_id}/chunks", response_model=ChunkSetResponse)
async def build_chunks(document_id: str, body: ChunkBuildRequest, request: Request,
                       identity: Identity = Depends(get_identity)) -> ChunkSetResponse:
    result = await asyncio.to_thread(request.app.state.chunk_repository.build,
                                    document_id, identity, ChunkConfig(**body.model_dump()))
    return ChunkSetResponse(**result)


@router.get("/documents/{document_id}/chunks", response_model=ChunkSetResponse)
async def get_chunks(document_id: str, request: Request,
                     identity: Identity = Depends(get_identity)) -> ChunkSetResponse:
    result = await asyncio.to_thread(request.app.state.chunk_repository.get, document_id, identity)
    return ChunkSetResponse(**result)


from app.api.schemas import IndexResponse, VectorSearchRequest, VectorSearchResponse
from app.core.errors import AppError


def get_vector_service(request: Request):
    service = request.app.state.vector_service
    if service is None:
        raise AppError("VECTOR_NOT_CONFIGURED", "Set KNOWLEDGEOPS_VECTOR_DATABASE_URL first", 503)
    return service


@router.post("/documents/{document_id}/index", response_model=IndexResponse)
async def index_document(document_id: str, request: Request,
                         identity: Identity = Depends(get_identity)) -> IndexResponse:
    return IndexResponse(**await get_vector_service(request).index_document(document_id, identity))


@router.post("/search/vector", response_model=VectorSearchResponse)
async def search_vector(body: VectorSearchRequest, request: Request,
                        identity: Identity = Depends(get_identity)) -> VectorSearchResponse:
    return VectorSearchResponse(**await get_vector_service(request).search(identity=identity, **body.model_dump()))


from dataclasses import asdict
from app.retrieval.objects import make_citation
from app.api.schemas import (RetrievalRequest, RetrievalResponse, CitationReadResponse,
                             PermissionRequest, PermissionResponse, DocumentUpdateResponse)


@router.post("/retrieve", response_model=RetrievalResponse)
async def retrieve(body: RetrievalRequest, request: Request, identity: Identity = Depends(get_identity)):
    get_vector_service(request)
    response = RetrievalResponse(**await request.app.state.retrieval_service.retrieve(identity=identity, **body.model_dump()))
    return JSONResponse(response.model_dump(), headers={"Cache-Control": "no-store"})


from fastapi.responses import JSONResponse


@router.get("/citations/{chunk_id}", response_model=CitationReadResponse)
async def read_citation(chunk_id: str, request: Request, chunk_set_id: str = Query(min_length=1),
                        identity: Identity = Depends(get_identity)):
    chunk = await asyncio.to_thread(request.app.state.document_repository.citation, chunk_id, chunk_set_id, identity)
    result = CitationReadResponse(text=chunk["text"], citation=asdict(make_citation(chunk, chunk_set_id)))
    return JSONResponse(result.model_dump(), headers={"Cache-Control": "no-store",
                        "Content-Type": "application/json; charset=utf-8"})


@router.put("/documents/{document_id}", response_model=DocumentUpdateResponse)
async def update_document(document_id: str, request: Request, file: UploadFile = File(...),
                          version: str = Form(min_length=1, max_length=64),
                          expected_version: str = Form(min_length=1, max_length=64),
                          identity: Identity = Depends(get_identity)):
    repository = request.app.state.document_repository
    try:
        await asyncio.to_thread(repository.check_owner, document_id, identity)
        filename, _, content = await request.app.state.documents.read_upload(file)
        return await asyncio.to_thread(repository.update, document_id, identity, filename, content, version, expected_version)
    finally:
        await file.close()


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: str, request: Request, identity: Identity = Depends(get_identity)):
    await asyncio.to_thread(request.app.state.document_repository.delete, document_id, identity)
    return Response(status_code=204)


@router.put("/documents/{document_id}/permissions", response_model=PermissionResponse)
async def set_permissions(document_id: str, body: PermissionRequest, request: Request,
                          identity: Identity = Depends(get_identity)):
    return await asyncio.to_thread(request.app.state.document_repository.permissions, document_id, identity, body.user_ids)
