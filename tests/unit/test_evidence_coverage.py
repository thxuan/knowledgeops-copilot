"""Evidence admission and bounded reviewer regressions; no cloud model required."""
import asyncio
from dataclasses import replace
import json
import pytest
from app.api.schemas import ChatRequest
from app.core.errors import AppError
from app.generation.context import build_context
from app.generation.models import REFUSAL
from app.generation.provider import GenerationSettings
from app.generation.service import ChatService


def hit(key, source="procedure", text="Report the incident."):
    return {"chunk_id": key, "document_id": source, "source": source, "version": "v1",
            "text": text, "citation": {"chunk_id": key, "source": source, "version": "v1"}}


def output(*claims):
    return json.dumps({"insufficient_context": False,
                       "claims": [{"text": text, "citations": [cid]} for text, cid in claims]})


class Repository:
    def __init__(self, revoke=False):
        self.checks, self.commits, self.revoke = 0, [], revoke
    def load(self, *args):
        return {"conversation_id": "conversation", "messages": [], "summary": {}}
    def check_evidence(self, evidence, identity):
        self.checks += 1
        if self.revoke and self.checks == 2:
            raise AppError("EVIDENCE_CHANGED", "Evidence access revoked", 409)
    def commit(self, *args):
        self.commits.append(args)


class Retrieval:
    def __init__(self, hits):
        self.hits = hits
    async def retrieve(self, *args, **kwargs):
        return {"hits": self.hits[:kwargs["top_k"]]}


class Model:
    def __init__(self, outputs):
        self.outputs, self.calls = iter(outputs), []
    async def generate(self, messages, settings):
        self.calls.append(json.loads(json.dumps(messages)))
        return next(self.outputs)


def settings(**kwargs):
    return replace(GenerationSettings(provider="compatible", model_id="fixture", api_key="mock-only", answer_mode="generated"), **kwargs)


def run(model, repo=None, hits=None, config=None):
    repo = repo or Repository()
    hits = hits or [hit("report"), hit("authority", "policy", "Only security decides whether to erase.")]
    value = asyncio.run(ChatService(repo, Retrieval(hits), model, config or settings()).run(
        ChatRequest(message="What must I do after losing a device?"), object()))
    return value, repo


def test_budget_admission_retains_other_document_before_repeated_topic():
    hits = [hit("a1", "A", "x" * 400), hit("a2", "A", "x" * 400),
            hit("a3", "A", "x" * 400), hit("b", "B", "critical independent condition")]
    base = build_context("q", [], {}, hits[:1] + hits[-1:], GenerationSettings()).input_estimate
    config = GenerationSettings(input_limit=base + 128, output_limit=128)
    context = build_context("q", [], {}, hits, config)
    assert [h["chunk_id"] for h in context.evidence] == ["a1", "b"]
    assert context.omitted_evidence_ids == ["a2", "a3"]
    assert context.input_estimate + config.output_limit <= config.input_limit


def test_review_replaces_draft_and_uses_same_authorized_evidence_without_rubric():
    model = Model([output(("Report the incident.", "report")),
                   output(("Report the incident.", "report"), ("Only security decides whether to erase.", "authority"))])
    value, repo = run(model)
    assert value["answer"].endswith("Only security decides whether to erase.")
    assert value["context"]["coverage_review"] == "completed"
    assert value["context"]["generation_attempts"] == 2
    first = json.loads(model.calls[0][1]["content"])
    review = json.loads(model.calls[1][1]["content"])
    assert review["evidence"] == first["evidence"]
    assert review["question"] == first["question"]
    assert set(review) == set(first) | {"draft"}
    assert repo.checks == 2 and len(repo.commits) == 1


def test_bad_review_citations_get_one_repair_and_never_enter_final_answer():
    model = Model([output(("Report.", "report")), output(("Untrusted.", "outside")),
                   output(("Only security decides.", "authority"))])
    value, _ = run(model)
    assert value["status"] == "answered"
    assert value["answer"] == "Only security decides."
    assert value["context"]["generation_attempts"] == 3
    assert "Untrusted." not in json.dumps(model.calls[2])


def test_draft_and_review_share_one_repair_budget():
    model = Model(["not JSON", output(("Report.", "report")), output(("Untrusted.", "outside"))])
    value, _ = run(model)
    assert len(model.calls) == 3
    assert value["status"] == "rejected"
    assert value["answer"] == REFUSAL
    assert value["context"]["coverage_review"] == "failed"


def test_permission_revocation_prevents_review_and_commit():
    model = Model([output(("Report.", "report"))])
    repo = Repository(revoke=True)
    with pytest.raises(AppError) as error:
        run(model, repo)
    assert error.value.code == "EVIDENCE_CHANGED"
    assert len(model.calls) == 1 and not repo.commits


def test_review_does_not_evict_evidence_or_exceed_budget_to_fit_draft():
    hits = [hit("report")]
    config = settings()
    base = build_context("What must I do after losing a device?", [], {}, hits, config).input_estimate
    config = replace(config, input_limit=base + config.output_limit + 32)
    model = Model([output(("x" * 3500, "report"))])
    value, _ = run(model, hits=hits, config=config)
    assert value["context"]["coverage_review"] == "skipped_budget"
    assert len(model.calls) == 1
    assert value["context"]["evidence_chunk_ids"] == ["report"]
    assert value["context"]["input_token_estimate"] + config.output_limit <= config.input_limit


def test_explicit_review_disable_preserves_single_call():
    model = Model([output(("Report.", "report"))])
    value, _ = run(model, config=settings(coverage_review=False))
    assert value["context"]["coverage_review"] == "disabled" and len(model.calls) == 1


def test_review_refusal_discards_the_answered_draft():
    model = Model([output(("Report.", "report")), '{"insufficient_context":true,"claims":[]}'])
    value, _ = run(model)
    assert value["status"] == "refused" and value["answer"] == REFUSAL
    assert value["claims"] == value["citations"] == []


def test_short_references_resolve_to_canonical_citations_in_draft_and_review():
    actual = "f" * 64
    hits = [hit(actual)]
    model = Model([output(("Report.", "E1")), output(("Report.", "E1"))])
    value, _ = run(model, hits=hits)
    assert value["claims"][0]["citations"] == [actual]
    assert value["citations"][0]["chunk_id"] == actual
    for messages in model.calls:
        payload = json.loads(messages[1]["content"])
        assert payload["evidence"][0]["chunk_id"] == "E1"
        assert actual not in messages[1]["content"]
    assert json.loads(model.calls[1][1]["content"])["draft"]["claims"][0]["citations"] == ["E1"]


def test_unknown_short_reference_is_rejected_and_diagnostics_exclude_model_text():
    model = Model([output(("private-output-marker", "E999")), output(("private-output-marker", "E999"))])
    value, _ = run(model)
    assert value["status"] == "rejected"
    assert value["context"]["validation_failures"] == [
        {"phase": "draft", "attempt": 1, "code": "citation_outside_evidence"},
        {"phase": "draft", "attempt": 2, "code": "citation_outside_evidence"}]
    assert "private-output-marker" not in json.dumps(value)


def test_grounded_answer_keeps_authority_and_discards_model_factual_edits():
    source = "Report loss immediately. End active sessions. Only security decides remote erasure."
    model = Model([output(("Report loss.", "E1")), output(("Erase the device yourself.", "E1"))])
    value, _ = run(model, hits=[hit("actual", text=source)], config=settings(answer_mode="grounded"))
    assert value["answer"] == source
    assert value["context"]["answer_mode"] == "grounded"
    assert value["claims"] == [{"text": source, "citations": ["actual"]}]
    assert "Erase the device yourself" not in value["answer"]


def test_grounded_answer_deduplicates_repeated_passage_references():
    model = Model([output(("Report.", "E1")), output(("Report.", "E1"), ("Another wording.", "E1"))])
    value, _ = run(model, hits=[hit("actual")], config=settings(answer_mode="grounded"))
    assert value["answer"] == "Report the incident." and len(value["claims"]) == 1


def test_grounded_answer_never_truncates_a_large_rule_to_hide_its_condition():
    model = Model([output(("Claim.", "E1")), output(("Claim.", "E1"))])
    value, _ = run(model, hits=[hit("actual", text="x" * 4001)], config=settings(answer_mode="grounded"))
    assert value["status"] == "rejected" and value["reason"] == "evidence_output_limit"
    assert value["claims"] == []


def test_queried_section_gap_expands_only_selected_source_inside_actual_context():
    from app.generation.models import grounded_answer, parse_generation
    first = hit("emergency", "changes", "Emergency fixes require approval.")
    freeze = hit("freeze", "changes", "During freeze only emergency recovery is permitted.")
    freeze["citation"]["heading_paths"] = [["Change rules", "Freeze window"]]
    unrelated = hit("other", "another-source", "Freeze has unrelated restrictions.")
    unrelated["citation"]["heading_paths"] = [["Other rules", "Freeze window"]]
    result = grounded_answer(parse_generation(output(("Emergency fixes.", "emergency"))),
                             [first, freeze, unrelated], "Can I deploy during a freeze?")
    assert result.citations == ["emergency", "freeze"]
    assert result.claims[1].text == freeze["text"]
    assert "unrelated restrictions" not in result.answer
