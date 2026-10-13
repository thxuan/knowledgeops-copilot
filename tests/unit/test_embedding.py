from tests.doubles import HashEmbeddingDouble
import asyncio
from dataclasses import replace
import math
from types import SimpleNamespace
import pytest
from app.core.embedding import (EmbeddingSettings, EmbeddingResult, EmbeddingError,
    EmbeddingTransientError, CompatibleEmbeddingClient, validate_result)
from app.core.errors import AppError
from app.retrieval.service import VectorService

@pytest.mark.parametrize("result", [EmbeddingResult("wrong", [[1,0]]), EmbeddingResult("text-embedding-v4", [[1]]),
    EmbeddingResult("text-embedding-v4", [[0,0]]), EmbeddingResult("text-embedding-v4", [[math.nan,1]]),
    EmbeddingResult("text-embedding-v4", [[True,1]]), EmbeddingResult("text-embedding-v4", []),
    EmbeddingResult("text-embedding-v4", [[1,0]], -1)])
def test_invalid_vectors(result):
    with pytest.raises(EmbeddingError):
        validate_result(result, EmbeddingSettings(dimensions=2), 1)

def test_fake_stable_normalized_batch():
    settings = EmbeddingSettings()
    client = HashEmbeddingDouble(settings)
    result = asyncio.run(client.embed(["设备合规 TL-403", "备用码"]))
    validate_result(result, settings, 2)
    assert result == asyncio.run(client.embed(["设备合规 TL-403", "备用码"]))
    assert result.vectors[0] != result.vectors[1]
    assert math.hypot(*result.vectors[0]) == pytest.approx(1)

class RecordingRepository:
    def __init__(self):
        self.events, self.published = [], []
    def record_batch(self, event): self.events.append(event)
    def ready(self, *args): return False
    def publish(self, *args): self.published.append(args)
    def batch_events(self, run_id): return self.events

class FlakyClient:
    def __init__(self, settings, invalid=False, hang=False):
        self.settings, self.invalid, self.hang = settings, invalid, hang
        self.calls = self.active = self.peak = 0
    async def embed(self, texts):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(.05 if self.hang else .002)
            if self.invalid:
                return EmbeddingResult("wrong", [[1,0]] * len(texts))
            if self.calls == 1:
                raise EmbeddingTransientError("test")
            return await HashEmbeddingDouble(self.settings).embed(texts)
        finally:
            self.active -= 1

def test_retry_and_failure_logging():
    settings = EmbeddingSettings(dimensions=2, max_attempts=2)
    repo = RecordingRepository()
    client = FlakyClient(settings)
    service = VectorService(None, repo, client, settings)
    result = asyncio.run(service._embed(["VPN"], "run", 0, "doc", "index"))
    assert len(result) == 1
    assert [e["status"] for e in repo.events] == ["failed", "succeeded"]
    assert [e["attempt"] for e in repo.events] == [1, 2]
    assert repo.events[0]["total_tokens"] is None
    assert repo.events[1]["estimated_cost"] is None

@pytest.mark.parametrize("invalid,hang,code,calls", [(True,False,"EMBEDDING_INVALID_RESPONSE",1),
    (False,True,"EMBEDDING_TEMPORARY_FAILURE",2)])
def test_invalid_no_retry_and_timeout_bounded(invalid, hang, code, calls):
    settings = EmbeddingSettings(dimensions=2, max_attempts=2, timeout_seconds=.01 if hang else 1)
    repo, client = RecordingRepository(), FlakyClient(settings, invalid, hang)
    service = VectorService(None, repo, client, settings)
    with pytest.raises(AppError) as error:
        asyncio.run(service._embed(["VPN"], "run", 0, "doc", "index"))
    assert error.value.code == code and client.calls == calls
    assert all(e["status"] == "failed" for e in repo.events)
    assert not repo.published

def test_batch_limit_concurrency_and_atomic_failure():
    settings = EmbeddingSettings(batch_size=2, concurrency=2)
    snapshot = {"count": 7, "chunk_set_id": "set", "chunks": [
        {"title": "VPN", "heading_paths": [["TL-403"]], "text": "设备合规"} for _ in range(7)]}
    chunks = SimpleNamespace(get=lambda *args: snapshot)
    identity = SimpleNamespace(tenant_id="tenant")
    for invalid in [False, True]:
        repo, client = RecordingRepository(), FlakyClient(settings, invalid=invalid)
        service = VectorService(chunks, repo, client, settings)
        if invalid:
            with pytest.raises(AppError): asyncio.run(service.index_document("doc", identity))
            assert not repo.published
        else:
            result = asyncio.run(service.index_document("doc", identity))
            assert result["count"] == 7 and len(repo.published) == 1
            assert sorted(e["input_count"] for e in repo.events) == [1,2,2,2]
        assert client.peak <= 2

def test_compatible_reorders_and_validates_indices(monkeypatch):
    import httpx
    settings = EmbeddingSettings(provider="compatible", model_id="demo", dimensions=2, api_key="test")
    calls = []
    class MockClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            calls.append(kwargs["json"])
            return httpx.Response(200, json={"model":"demo", "data":[
                {"index":1,"embedding":[0,1]}, {"index":0,"embedding":[1,0]}], "usage":{"total_tokens":9}})
    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    result = asyncio.run(CompatibleEmbeddingClient(settings).embed(["a","b"]))
    assert result.vectors == [[1,0],[0,1]] and result.total_tokens == 9
    assert calls[0]["input"] == ["a","b"]
