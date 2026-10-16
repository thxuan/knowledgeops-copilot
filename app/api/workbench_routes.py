"""Local workbench UI and scoped execution; no global configuration mutation."""
import asyncio
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from app.api.schemas import ChatRequest
from app.api.workbench_schemas import WorkbenchChatRequest
from app.core.errors import AppError
from app.core.identity import LOCAL_IDENTITIES, Identity, get_identity
from app.evals.imported import get_dataset, evaluate_imported, runtime_fingerprint
from app.evals.workbench import available_questions, evaluate_trace, selected_question, corpus_snapshot
from app.generation.provider import GenerationSettings, make_generation_client
from app.generation.service import ChatService
from app.retrieval.pipeline import RetrievalService
from app.retrieval.planning import QueryPlanner

router = APIRouter()
UI = Path(__file__).resolve().parents[1] / "web"


@router.get("/", include_in_schema=False)
async def workbench_page():
    return FileResponse(UI / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/workbench/tickets", include_in_schema=False)
@router.get("/workbench/tickets/{ticket_id}", include_in_schema=False)
async def tickets_page():
    return FileResponse(UI / "tickets.html", headers={"Cache-Control": "no-store"})


@router.get("/workbench/business", include_in_schema=False)
async def business_page():
    return FileResponse(UI / "business.html", headers={"Cache-Control": "no-store"})


@router.get("/workbench/config")
async def config(request: Request, identity: Identity = Depends(get_identity)):
    settings = request.app.state.settings
    gs, rs = settings.generation, settings.retrieval
    return {"version": "1", "local_auth": True,
        "identities": [{"id": i.user_id, "audiences": sorted(i.audiences)} for i in LOCAL_IDENTITIES.values()],
        "generation": {"provider": gs.provider if gs.provider == "compatible" else "unconfigured", "model_id": gs.model_id if gs.provider == "compatible" else "",
            "temperature": gs.temperature, "top_p": gs.top_p, "output_limit": gs.output_limit,
            "input_limit": gs.input_limit, "coverage_review": gs.coverage_review,
            "sampling_supported": gs.provider == "compatible", "sampling_top_k_supported": False,
            "ready": gs.provider == "compatible",
            "models": [{"id": "configured", "label": gs.model_id if gs.provider == "compatible" else "请配置真实模型"}]},
        "retrieval": {"strategy": rs.strategy, "candidate_k": rs.candidate_k,
            "rrf_constant": rs.rrf_constant, "rerank_available": rs.rerank.provider != "none",
            "rerank_provider": rs.rerank.provider,
            "embedding_model": settings.embedding.model_id if settings.embedding.provider == "compatible" else "未配置", "collection": settings.embedding.collection if settings.embedding.provider == "compatible" else "未选择"},
        "agent": {"available": request.app.state.persistent_agent is not None,
                  "model_id": settings.agent.model_id, "provider": settings.agent.provider,
                  "scope": "同一会话自动分配问答与任务；任务保留独立的执行预算。"},
        "questions": await asyncio.to_thread(available_questions, identity)}


def scope_services(app, body):
    configured = app.state.chat.settings
    gs = configured
    overrides = body.generation.model_dump(exclude={"model"}, exclude_unset=True)
    # Null top_p explicitly restores provider default; other omitted values inherit.
    overrides = {k: v for k, v in overrides.items() if v is not None or k == "top_p"}
    if overrides.get("output_limit", gs.output_limit) >= gs.input_limit:
        raise AppError("INVALID_CONFIG", "输出预算必须小于模型输入预算。", 422)
    gs = replace(gs, **overrides)
    gs.validate()
    controls = body.retrieval
    strategy = "hybrid" if controls.semantic and controls.keyword else "vector" if controls.semantic else "bm25"
    if controls.rerank:
        if app.state.settings.retrieval.rerank.provider == "none":
            raise AppError("RERANK_NOT_CONFIGURED", "服务端尚未配置重排模型。", 422)
        strategy += "_rerank"
    rs = replace(app.state.settings.retrieval, strategy=strategy,
        candidate_k=max(body.top_k, controls.fusion_k), rrf_constant=controls.rrf_constant,
        vector_k=controls.semantic_k, keyword_k=controls.keyword_k, fusion_k=controls.fusion_k)
    rs.validate()
    client = app.state.chat.client
    vectors = RetrievalService(app.state.vector_service, rs,
        planner=QueryPlanner(client, gs))
    actual = {"generation": {"provider": gs.provider, "model_id": gs.model_id,
        "temperature": gs.temperature, "top_p": gs.top_p, "output_limit": gs.output_limit,
        "input_limit": gs.input_limit, "coverage_review": gs.coverage_review,
        "sampling_effect": "sent_not_echoed_by_provider"},
        "retrieval": {**controls.model_dump(), "strategy": strategy, "rrf": controls.rrf and controls.semantic and controls.keyword,
            "embedding_model": app.state.settings.embedding.model_id,
            "collection": app.state.settings.embedding.collection},
        "top_k": body.top_k, "retrieval_profile": body.retrieval_profile,
        "filters": body.filters.model_dump()}
    return ChatService(app.state.conversations, vectors, client, gs), actual


@router.post("/workbench/chat/stream")
async def workbench_chat_stream(body: WorkbenchChatRequest, request: Request, identity: Identity = Depends(get_identity)):
    from app.api.streaming import stream_response
    return stream_response(request, lambda: workbench_chat(body, request, identity))


@router.post("/workbench/chat")
async def workbench_chat(body: WorkbenchChatRequest, request: Request, identity: Identity = Depends(get_identity)):
    if request.app.state.vector_service is None:
        raise AppError("VECTOR_NOT_CONFIGURED", "请先初始化知识库与向量索引。", 503)
    if request.app.state.chat.settings.provider != "compatible":
        raise AppError("MODEL_NOT_CONFIGURED", "请在模型设置中配置真实模型。", 422)
    if body.runtime_fingerprint and body.runtime_fingerprint != runtime_fingerprint(request.app):
        raise AppError("RUNTIME_CHANGED", "模型或索引配置发生变化，已停止本批评测。", 409)
    if body.retrieval.semantic and request.app.state.settings.embedding.provider != 'compatible':
        raise AppError('EMBEDDING_NOT_CONFIGURED', '请在连接设置中配置真实 Embedding，并选择匹配的索引集合。', 422)
    dataset = get_dataset(request.app, body.dataset_id, identity) if body.dataset_id else None
    if dataset:
        from app.api.workbench_management import readiness
        preflight = await asyncio.to_thread(readiness, request.app, identity, body.retrieval.semantic)
        if not preflight['ready']:
            raise AppError('KNOWLEDGE_NOT_READY', '；'.join(preflight['reasons']), 409)
        question = next((q for q in dataset["questions"] if q["question_id"] == body.question_id), None)
        if question is None or question["question"] != body.message or body.conversation_id is not None:
            raise AppError("DATASET_QUESTION_MISMATCH", "评测题目必须与导入文件一致，且使用独立上下文。", 422)
    scoped, actual = scope_services(request.app, body)
    chat_body = ChatRequest(**body.model_dump(exclude={"generation", "retrieval", "question_id", "dataset_id", "runtime_fingerprint"}))
    if not dataset:
        question, unavailable = selected_question(body.question_id, body.message, identity, body.conversation_id)
    else:
        unavailable = None

    async def execute():
        started = time.perf_counter()
        snapshot = await asyncio.to_thread(corpus_snapshot, request.app.state.document_repository, identity)
        result = await scoped.run(chat_body, identity, include_trace=True,
            retrieval_options={"mode": body.retrieval.mode, "ef_search": body.retrieval.ef_search},
            effective_config=actual,
            **({"on_event": request.state.chat_events} if getattr(request.state, "chat_events", None) else {}))
        final_snapshot = await asyncio.to_thread(corpus_snapshot, request.app.state.document_repository, identity)
        metrics = (evaluate_imported(dataset, question, result["retrieval_trace"], snapshot) if dataset else
            await asyncio.to_thread(evaluate_trace, question, result["retrieval_trace"], identity, request.app.state.document_repository))
        if snapshot["fingerprint"] != final_snapshot["fingerprint"]:
            metrics = {"status": "stale_fixture", "stages": {}, "reason": "运行期间知识库或权限发生变化。",
                       "answer_correctness": None, "citation_support": None}
        if unavailable:
            metrics["reason"] = unavailable
        core_ms = result["context"]["timings_ms"]["total"]
        result["context"]["timings_ms"].update(chat_core=core_ms,
            inspection_overhead=round((time.perf_counter() - started) * 1000 - core_ms, 3),
            total=round((time.perf_counter() - started) * 1000, 3))
        return {**result, "experiment_id": str(uuid4()), "identity": identity.user_id,
                "effective_config": actual, "evaluation": metrics,
                "corpus_snapshot": snapshot, "corpus_changed_during_run": snapshot["fingerprint"] != final_snapshot["fingerprint"],
                "metrics_scope": "单次独立评测；质量指标依据所选测试集的有效标注，答案正确性需人工评审。"}
    return await request.app.state.chat_admission.run(request, identity, execute)
