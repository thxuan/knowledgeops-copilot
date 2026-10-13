"""Latency and usage reporting must distinguish failures from successful work."""
from scripts.benchmark_rag_latency import latency, summarize
from app.generation.service import ChatService
from app.generation.provider import GenerationOutput
from app.api.schemas import ChatRequest
from app.generation.provider import GenerationSettings
import asyncio


def row(status="answered", http="ok", ms=1000):
    return {"profile": "ranked@8", "http_status": http, "status": status, "client_duration_ms": ms,
        "response": {"context": {"timings_ms": {"retrieval": 100, "generation": 800},
          "model_usage": [{"usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}}],
          "retrieval_diagnostics": {}, "input_token_estimate": 100}}}


def test_timeout_never_makes_success_latency_look_faster_and_remains_visible():
    result = summarize([row(), row(status="error", http="error", ms=60000), row(status="refused", ms=300)])
    value = result["ranked@8"]
    assert value["http_errors"] == 1
    assert value["status_counts"] == {"answered": 1, "refused": 1, "rejected": 0, "error": 1}
    assert value["end_to_end"]["mean_ms"] == 650
    assert value["all_attempts_end_to_end"]["p95_ms"] == 60000
    assert value["answered_end_to_end"]["mean_ms"] == 1000
    assert value["generation_usage"]["total_tokens"] == 50


def test_percentile_uses_nearest_rank_and_empty_profile_stays_unknown():
    assert latency(list(range(1,21)))["p95_ms"] == 19
    assert latency([])["mean_ms"] is None
    assert summarize([])["ranked@20"]["end_to_end"]["count"] == 0


def test_provider_usage_and_phase_timing_are_sanitized_and_separate_from_planning():
    class Repository:
        def load(self, *args): return {"conversation_id": "c", "messages": [], "summary": {}}
        def check_evidence(self, *args): pass
        def commit(self, *args): pass
    class Retrieval:
        async def retrieve(self, *args, **kwargs):
            return {"hits": [{"chunk_id": "x", "document_id": "d", "source": "s", "version": "v",
                "text": "The answer is supported.", "citation": {"chunk_id": "x"}}]}
    class Model:
        async def generate(self, *args):
            return GenerationOutput('{"insufficient_context":false,"claims":[{"text":"Answer.","citations":["E1"]}]}',
                {"usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
                           "private-key": "private-marker", "secret-text": "private-marker"}})
    result = asyncio.run(ChatService(Repository(), Retrieval(), Model(), GenerationSettings()).run(ChatRequest(message="question"), object()))
    assert result["context"]["model_usage"][0]["usage"] == {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
    assert "private-marker" not in str(result)
    phases = result["context"]["timings_ms"]
    assert phases["total"] >= phases["retrieval"] + phases["generation"]
    assert set(phases) == {"total", "retrieval", "generation", "context_and_validation", "commit"}
