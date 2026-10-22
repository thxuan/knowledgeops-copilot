from tests.doubles import HashEmbeddingDouble
from tests.doubles import ExtractiveGenerationDouble
import asyncio
import json
from dataclasses import replace
import httpx
import pytest
from app.core.errors import AppError
from app.generation.context import build_context, estimate_tokens
from app.generation.models import GeneratedAnswer, parse_generation
from app.generation.provider import GenerationSettings, CompatibleGenerationClient

def test_whole_records_and_evidence_budget():
    settings = GenerationSettings(output_limit=1200, history_budget=800)
    user = {"message_id": 7, "role": "user", "content": "不是 TL-403，是 MAIL-552；上限 25 MB，禁止外发。"}
    small = {"chunk_id": "small", "source": "s", "version": "v1", "text": "MAIL-552 上限 25 MB，禁止外发。"}
    required = build_context("这个限制是什么？", [user], {}, [small], settings).input_estimate
    # Context reserves at most a third of spare input space for user history.
    settings = replace(settings, input_limit=required + settings.output_limit + settings.history_budget * 3)
    context = build_context("这个限制是什么？", [user], {}, [
        {"chunk_id": "huge", "source": "s", "version": "v1", "text": "长" * 5000},
        small], settings)
    payload = json.loads(context.messages[-1]["content"])
    assert payload["history"][0]["text"] == user["content"]
    assert [h["chunk_id"] for h in context.evidence] == ["small"]
    assert context.input_estimate + settings.output_limit <= settings.input_limit
    assert estimate_tokens(context.messages) == context.input_estimate

def test_current_question_is_not_silently_truncated():
    with pytest.raises(AppError) as error:
        build_context("很长" * 1000, [], {}, [], GenerationSettings(input_limit=4000))
    assert error.value.code == "CONTEXT_BUDGET_EXCEEDED"

@pytest.mark.parametrize("change", [
    {"insufficient_context": "false"}, {"citations": []}, {"answer": "额外断言"},
    {"claims": []}, {"unknown": True},
])
def test_invalid_structured_outputs(change):
    value = {"answer": "禁止外发。", "citations": ["c"], "insufficient_context": False,
             "claims": [{"text": "禁止外发。", "citations": ["c"]}]}
    with pytest.raises(ValueError):
        GeneratedAnswer.model_validate({**value, **change})

@pytest.mark.parametrize("finish,status", [("stop", 200), ("length", 200), ("stop", 429)])
def test_compatible_json_adapter(finish, status):
    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"][0]["role"] == "system"
        assert body["max_tokens"] == 2048
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(status, json={"choices": [{"finish_reason": finish,
            "message": {"role": "assistant", "content": "{}"}}]})
    settings = GenerationSettings(provider="compatible", model_id="fixture", api_key="test-key")
    call = CompatibleGenerationClient(httpx.MockTransport(handler)).generate(
        [{"role": "system", "content": "JSON"}], settings)
    if status == 200 and finish == "stop":
        assert asyncio.run(call) == "{}"
    else:
        with pytest.raises(AppError):
            asyncio.run(call)

def test_real_sample_correction_keeps_mail_evidence_in_top5():
    """Regress the user failure with all authorized samples, actual chunks and Fake vectors."""
    from dataclasses import asdict
    from app.core.embedding import EmbeddingSettings, chunk_input
    from app.generation.context import retrieval_query
    from tests.doubles import ExtractiveGenerationDouble
    from app.ingestion.chunker import ChunkConfig, chunk_document
    from scripts.compare_chunks import load_corpus

    history = [
        {"message_id": 1, "role": "user", "content": "TL-403 设备修复后合规状态多久刷新？"},
        {"message_id": 3, "role": "user", "content": "它修复后合规状态多久刷新？"},
        {"message_id": 5, "role": "user", "content": "不是 TL-403，我问的是 MAIL-552：邮件总大小限制是多少？"},
    ]
    question = "它超过大小限制后应该怎么处理？"
    query = retrieval_query(question, history)
    # A direct question with its own identifier should also avoid stale history.
    assert retrieval_query("MAIL-552 它应该怎么处理？", history) == "MAIL-552 它应该怎么处理？"
    chunks = []
    for doc_id, doc in load_corpus().items():
        if "u_engineer_001" not in doc["allowed_user_ids"]:
            continue
        chunks.extend(asdict(c) for c in chunk_document(doc_id, doc["text"],
            title=doc["title"], source="sample_data/knowledge_base/" + doc["filename"],
            version=doc["version"], parser_version=doc["parser_version"],
            config=ChunkConfig(), markdown=True))
    async def run():
        embedding = HashEmbeddingDouble(EmbeddingSettings())
        vectors = (await embedding.embed([chunk_input(c) for c in chunks])).vectors
        qv = (await embedding.embed([query])).vectors[0]
        ranked = sorted(zip(chunks, vectors), key=lambda item: sum(a*b for a,b in zip(qv,item[1])), reverse=True)
        hits = [item[0] for item in ranked[:5]]
        assert any("MAIL-552" in h["text"] for h in hits)
        context = build_context(question, history, {}, hits, GenerationSettings())
        answer = parse_generation(
            await ExtractiveGenerationDouble().generate(context.messages, GenerationSettings()))
        assert not answer.insufficient_context
        assert "TeamDrive" in answer.answer and "外发" in answer.answer
        assert "ShieldDesk" not in answer.answer
    asyncio.run(run())
