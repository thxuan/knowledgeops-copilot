"""Import and preview datasets without any generation calls."""
import json
from fastapi import APIRouter, Depends, Request
from app.core.identity import Identity, get_identity
from app.evals.imported import DatasetUpload, parse_dataset, store_dataset, preview, runtime_fingerprint
from app.evals.workbench import fixture
router = APIRouter()

@router.post("/workbench/datasets")
async def upload_dataset(body: DatasetUpload, request: Request, identity: Identity = Depends(get_identity)):
    dataset = parse_dataset(body, identity)
    store_dataset(request.app, dataset)
    return preview(dataset) | {"runtime_fingerprint":runtime_fingerprint(request.app)}

@router.get("/workbench/datasets/example")
async def example(identity: Identity = Depends(get_identity)):
    questions, anchors, *_ = fixture()
    values = [{**q,"source_spans":[a for a in anchors if a["question_id"]==q["question_id"]]}
              for q in questions if identity.user_id in q["allowed_user_ids"]]
    return {"filename":"knowledgeops-dev-example.jsonl", "content":"\n".join(json.dumps(q,ensure_ascii=False) for q in values)+"\n"}

@router.get("/workbench/datasets/runtime")
async def runtime(request: Request, identity: Identity = Depends(get_identity)):
    return {"fingerprint":runtime_fingerprint(request.app)}
