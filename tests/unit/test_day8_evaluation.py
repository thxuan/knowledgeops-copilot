import asyncio
import json
import httpx
import pytest
from app.evals.rag import digest, generation_cost, summarize, review_summary, RecordingGeneration
from app.evals.retrieval import measure
from app.generation.provider import CompatibleGenerationClient, GenerationSettings
from scripts.evaluate_rag import filter_checks


def row(qid, answerable, status, recall=None):
    return {"question_id": qid, "answerable": answerable, "status": status,
            "question_type": "fact" if answerable else "unanswerable",
            "duration_ms": 10, "retrieval": {"source_recall_at_k": recall,
            "reciprocal_rank": recall, "section_char_coverage_at_k": recall},
            "citation_checks": [True] if status == "answered" else [], "filter_checks": [True]}


def test_refusal_denominators_do_not_reward_all_refusal_or_rejection():
    summary = summarize([row("a", True, "refused", .5), row("u", False, "refused"),
                         row("u2", False, "rejected")])
    assert summary["mean_source_recall_at_k"] == .5
    assert summary["unanswerable_refusal_rate"] == .5
    assert summary["answerable_false_refusal_rate"] == 1
    assert summary["citation_validity_rate"] is None
    assert summary["semantic_correctness"] is None
    assert summary["status_counts"]["rejected"] == 1


def test_mrr_and_cross_document_completeness_require_both_sources():
    question = {"question_id": "q", "answerable": True,
                "expected_sources": [{"document_id": "A", "version": "1"}, {"document_id": "B", "version": "1"}]}
    anchors = [{"question_id": "q", "document_id": d, "version": "1", "text_hash": "h",
                "parser_version": "p", "start_char": 10, "end_char": 20} for d in ("A", "B")]
    def hit(source, start, end):
        return {"source": source, "citation": {"version": "1", "text_hash": "h", "parser_version": "p",
                "location": {"start_char": start, "end_char": end}}}
    result = measure(question, [hit("a", 0, 10), hit("a", 10, 20)], anchors, {"a": "A", "b": "B"})
    assert result["reciprocal_rank"] == .5
    assert result["all_required_sources_recalled"] is False
    assert result["source_recall_at_k"] == .5


def test_review_cannot_attach_to_different_output_or_count_twice():
    rows = [row("a", True, "answered", 1)]
    review = {"run_id": "run", "question_id": "a", "sample_sha256": digest(rows[0]),
              "reviewer": "human", "notes": "Evidence supports both conditions",
              "correctness": "pass", "citation_support": "pass", "faithfulness": "fail"}
    assert review_summary(rows, [review], "run")["faithfulness"]["rate"] == 0
    with pytest.raises(ValueError):
        review_summary(rows, [review, review], "run")
    with pytest.raises(ValueError):
        review_summary(rows, [{**review, "sample_sha256": "other"}], "run")
    with pytest.raises(ValueError):
        review_summary(rows, [{**review, "correctness": ""}], "run")


def test_record_all_repair_usage_and_unknown_cost():
    calls = [{"metadata": {"usage": {"prompt_tokens": 100, "completion_tokens": 10}}},
             {"metadata": {"usage": {"prompt_tokens": 110, "completion_tokens": 20}}}]
    assert generation_cost(calls, "compatible", 2, 4)["estimated_cost"] == pytest.approx(.00054)
    assert generation_cost(calls + [{"metadata": {}}], "compatible", 2, 4)["estimated_cost"] is None
    assert generation_cost([], "fake", None, None)["prompt_tokens"] is None


def test_actual_sampling_payload_and_usage_are_recorded():
    def handler(request):
        body = json.loads(request.content)
        assert body["temperature"] == .7 and body["top_p"] == .4
        return httpx.Response(200, json={"model": "fixture-version", "usage": {"prompt_tokens": 25,
            "completion_tokens": 5, "total_tokens": 30}, "choices": [{"finish_reason": "stop",
            "message": {"role": "assistant", "content": "{}"}}]})
    client = RecordingGeneration(CompatibleGenerationClient(httpx.MockTransport(handler)))
    settings = GenerationSettings(provider="compatible", model_id="fixture", api_key="secret", temperature=.7, top_p=.4)
    assert asyncio.run(client.generate([{"role": "system", "content": "JSON"}], settings)) == "{}"
    assert client.calls[0]["metadata"]["usage"]["total_tokens"] == 30
    assert "secret" not in json.dumps(client.calls)
    assert client.calls[0]["metadata"]["effective_sampling"] == "not echoed by provider"


@pytest.mark.parametrize("kwargs", [{"temperature": float("nan")}, {"temperature": 2},
                                    {"top_p": 0}, {"top_p": float("inf")}])
def test_invalid_sampling_config(kwargs):
    with pytest.raises(ValueError):
        GenerationSettings(**kwargs).validate()


def test_filter_validation_uses_manifest_acl_and_requested_filters():
    from app.core.identity import LOCAL_IDENTITIES
    corpus = {"A": {"allowed_user_ids": ["u_employee_001"], "version": "1"},
              "B": {"allowed_user_ids": ["u_engineer_001"], "version": "1"}}
    hits = [{"source": "a", "version": "1", "document_id": "da"},
            {"source": "b", "version": "1", "document_id": "db"}]
    checks = filter_checks(hits, LOCAL_IDENTITIES["u_employee_001"], corpus, {"a": "A", "b": "B"}, {"source": "a"})
    assert checks == [True, False]


def test_sampling_reuses_identical_dev_inputs_and_one_variable(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from dataclasses import replace
    from app.core.config import Settings
    import scripts.evaluate_rag as evaluator
    observed = []
    class Client:
        async def generate(self, messages, settings):
            observed.append((digest(messages), settings.temperature, settings.top_p))
            return '{"claims":[],"insufficient_context":true}'
    monkeypatch.setattr(evaluator, "make_generation_client", lambda _: Client())
    settings = replace(Settings(), generation=GenerationSettings(
        provider="compatible", model_id="qwen-plus-fixture", api_key="secret"))
    messages = [{"role": "system", "content": "JSON"},
                {"role": "user", "content": json.dumps({"evidence": []})}]
    rows = [{"question_id": qid, "generation_calls": [{"messages": messages}]}
            for qid in ("F005", "X001", "U001")]
    asyncio.run(evaluator.sampling(rows, settings,
        SimpleNamespace(input_price=None, output_price=None), tmp_path))
    assert len(observed) == 45
    assert {o[0] for o in observed} == {digest(messages)}
    from collections import Counter
    assert Counter((t, p) for _, t, p in observed) == {
        (.7, .8): 9, (0, .8): 9, (1, .8): 9, (.7, .4): 9, (.7, 1): 9}
    results = evaluator.read_jsonl(tmp_path / "sampling-results.jsonl")
    assert len(results) == 45
    assert all(r["usage"]["estimated_cost"] is None for r in results)
    assert "secret" not in (tmp_path / "sampling-results.jsonl").read_text(encoding="utf-8")
