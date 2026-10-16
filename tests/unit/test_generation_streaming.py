"""Offline streaming checks: synthetic SSE only, no external models or database."""
import asyncio
import json
from types import SimpleNamespace
import httpx
import pytest
from app.generation.streaming import claims_preview
from app.generation.provider import CompatibleGenerationClient, GenerationSettings
from app.api.streaming import stream_response
from app.core.errors import AppError
from app.core.lifecycle import ChatAdmission


def test_preview_only_exposes_claim_text_and_handles_split_escapes():
    assert claims_preview('{"claims":[{"text":"正在生成') == "正在生成"
    assert claims_preview('{"claims":[{"citations":["secret-id"],"text":"第一条"},'
                          '{"text":"第二条\\n下一行\\u4f60\\u597d') == "第一条\n第二条\n下一行你好"
    assert claims_preview('{"claims":[{"text":"a\\uD83D') == "a"
    assert claims_preview('{"claims":[{"text":"a\\uD83D\\uDE00') == "a😀"
    assert claims_preview('{"claims":[{"text":"撤回"}],"insufficient_context":true}') == ""
    assert claims_preview('{"answer":"不要展示","reasoning":"不要展示"}') == ""
    assert claims_preview('{"claims":[{"text":"quoted \\"word\\" \\\\ slash') == 'quoted "word" \\ slash'


def test_preview_accepts_every_prefix_without_leaking_metadata():
    wire = json.dumps({"insufficient_context": False, "claims": [
        {"text": "中文😀\n第一条", "citations": ["reference-SECRET"]},
        {"text": "第二条", "citations": ["reference-SECRET"]}]}, ensure_ascii=True)
    for end in range(len(wire) + 1):
        preview = claims_preview(wire[:end])
        assert "SECRET" not in preview and "citations" not in preview
    assert claims_preview(wire) == "中文😀\n第一条\n第二条"


class Bytes(httpx.AsyncByteStream):
    def __init__(self, seen, finish="stop"):
        self.seen, self.finish = seen, finish

    async def __aiter__(self):
        yield ('data: '+json.dumps({"model": "fixture", "choices": [
            {"index": 0, "delta": {"content": '{"claims":[{"text":"中文'}, "finish_reason": None}]},
            ensure_ascii=False)+'\n\n').encode()
        assert self.seen == ['{"claims":[{"text":"中文']  # callback runs before upstream completion
        yield ('data: '+json.dumps({"choices": [{"index": 0, "delta": {"content": '","citations":["E1"]}],"insufficient_context":false}'},
                                                     "finish_reason": self.finish}]})+'\n\n').encode()
        yield b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20}}\n\n'
        yield b'data: [DONE]\n\n'


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_provider_stream_delivers_real_deltas_and_requires_complete_stop(finish):
    async def run():
        seen = []
        async def delta(text):
            seen.append(text)
        def handle(request):
            payload = json.loads(request.content)
            assert payload["stream"] is True
            assert payload["stream_options"]["include_usage"] is True
            return httpx.Response(200, stream=Bytes(seen, finish))
        client = CompatibleGenerationClient(httpx.MockTransport(handle))
        settings = GenerationSettings(provider="compatible", api_key="test-key", model_id="fixture")
        if finish == "stop":
            result = await client.generate_stream([], settings, delta)
            assert str(result) == "".join(seen)
            assert result.metadata["usage"]["completion_tokens"] == 20
        else:
            with pytest.raises(AppError) as error:
                await client.generate_stream([], settings, delta)
            assert error.value.code == "GENERATION_FAILED"
    asyncio.run(run())


def request():
    return SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(
        state=SimpleNamespace(management_active_requests=0)))


def test_transport_preview_precedes_final_result_and_failure_does_not_finalize():
    async def run(fail):
        req = request()
        async def operation():
            await req.state.chat_events("preview", {"text": "draft"})
            if fail:
                raise AppError("GENERATION_FAILED", "failed", 502)
            return {"answer": "validated"}
        response = stream_response(req, operation)
        frames = [frame async for frame in response.body_iterator]
        encoded = "".join(frames)
        assert encoded.index("event: preview") < encoded.index("event: error" if fail else "event: result")
        assert ("event: result" not in encoded) if fail else ('"validated"' in encoded)
        assert req.app.state.management_active_requests == 0
    asyncio.run(run(False))
    asyncio.run(run(True))


def test_disconnect_cancels_upstream_and_releases_configuration_lock():
    async def run():
        req = request()
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def operation():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        response = stream_response(req, operation)
        iterator = response.body_iterator
        await anext(iterator)
        await started.wait()
        assert req.app.state.management_active_requests == 1
        await iterator.aclose()
        assert cancelled.is_set()
        assert req.app.state.management_active_requests == 0
    asyncio.run(run())


def test_stream_admission_keeps_capacity_and_propagates_cancellation():
    async def run():
        admission = ChatAdmission(1, 20)
        req = request()
        req.state.streaming_chat = True
        started, cancelled = asyncio.Event(), asyncio.Event()
        identity = SimpleNamespace(tenant_id="t", user_id="u")
        async def operation():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        job = asyncio.create_task(admission.run(req, identity, operation))
        await started.wait()
        with pytest.raises(AppError) as error:
            await admission.run(req, identity, operation)
        assert error.value.code == "RATE_LIMIT"
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert cancelled.is_set() and admission.active == 0
    asyncio.run(run())


@pytest.mark.parametrize("coverage", [False, True])
def test_streamed_chat_finalizes_only_validated_claims(coverage):
    from app.generation.service import ChatService
    from app.generation.provider import GenerationOutput
    from app.api.schemas import ChatRequest
    events, commits, calls = [], [], []
    hit = {"chunk_id": "canonical", "source": "guide", "version": "v1",
           "text": "Known source passage", "citation": {"chunk_id": "canonical"}}

    class Conversations:
        def load(self, conversation_id, identity):
            return {"conversation_id": "test", "messages": [], "summary": {}}
        def check_evidence(self, evidence, identity):
            pass
        def commit(self, snapshot, identity, message, response, evidence, summary_budget):
            commits.append(response["answer"])

    class Retrieval:
        async def retrieve(self, query, identity, **kwargs):
            return {"hits": [hit], "duration_ms": 0}

    class Client:
        async def generate_stream(self, messages, settings, on_delta):
            n = len(calls)
            calls.append(messages)
            # With no review, reject a fabricated ID then repair. With review,
            # first valid draft is superseded by the coverage review.
            text = ("Draft" if n == 0 else "Final")
            citation = "bad-id" if n == 0 and not coverage else "E1"
            raw = json.dumps({"insufficient_context": False, "claims": [
                {"text": text, "citations": [citation]}]})
            await on_delta(raw)
            assert commits == []
            return GenerationOutput(raw, {"usage": {}})

    async def run():
        async def notify(kind, data):
            events.append((kind, data))
        settings = GenerationSettings(provider="compatible", api_key="test-key", coverage_review=coverage)
        response = await ChatService(Conversations(), Retrieval(), Client(), settings).run(
            ChatRequest(message="Question"), SimpleNamespace(), on_event=notify)
        assert response["answer"] == "Final"
        assert response["claims"][0]["citations"] == ["canonical"]
        assert commits == ["Final"]
        assert [data["phase"] for kind, data in events if kind == "reset"] == (
            ["draft", "review"] if coverage else ["draft", "draft"])
        assert [data["text"] for kind, data in events if kind == "preview"] == ["Draft", "Final"]
        assert len(calls) == 2
    asyncio.run(run())
