import asyncio
from fastapi import APIRouter, Depends, Request, Query
from pydantic import BaseModel, Field
from app.core.identity import Identity, get_identity, require_platform
from app.db.document_reviews import DocumentReviews
from app.api.index_jobs import configured

router = APIRouter()

def reviews(request):
    state=request.app.state
    return DocumentReviews(state.ingestion_repository,state.accounts if state.settings.auth_mode=='accounts' else None)

class SubmitReview(BaseModel):
    expected_revision: int = Field(ge=1)
    user_ids: list[str] = Field(min_length=1,max_length=1000)

class Decision(BaseModel):
    comment: str = Field(default='',max_length=1000)

@router.get('/document-reviews')
async def inbox(request:Request,limit:int=Query(100,ge=1,le=500),identity:Identity=Depends(get_identity)):
    return await asyncio.to_thread(reviews(request).list,identity,limit)

@router.post('/documents/{document_id}/versions/{version_id}/review',status_code=202)
async def submit(document_id:str,version_id:str,body:SubmitReview,request:Request,identity:Identity=Depends(get_identity)):
    return await asyncio.to_thread(reviews(request).submit,document_id,version_id,identity,body.expected_revision,body.user_ids)

@router.post('/document-reviews/{review_id}/approve')
async def approve(review_id:str,body:Decision,request:Request,identity:Identity=Depends(require_platform)):
    configured(request.app)
    return await asyncio.to_thread(reviews(request).decide,review_id,identity,'approve',body.comment,request.app.state.vector_repository)

@router.post('/document-reviews/{review_id}/reject')
async def reject(review_id:str,body:Decision,request:Request,identity:Identity=Depends(require_platform)):
    return await asyncio.to_thread(reviews(request).decide,review_id,identity,'reject',body.comment,None)
