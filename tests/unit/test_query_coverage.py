"""Multi-query authorization, planner degradation and evidence selection regressions."""
import asyncio
import json
import pytest
from app.core.errors import AppError
from app.core.identity import LOCAL_IDENTITIES
from app.generation.provider import GenerationSettings
from app.retrieval.coverage import plan_queries, select_evidence
from app.retrieval.planning import QueryPlanner
from app.retrieval.pipeline import RetrievalService, RetrievalSettings


def hit(cid, text, doc="A", score=.9):
    return {"chunk_id": cid, "document_id": doc, "chunk_set_id": "set", "source": doc,
        "version": "v1", "text": text, "title": "", "heading_paths": [], "score": score,
        "text_hash": cid, "parser_version": "p", "start_char": 0, "end_char": len(text), "start_line": 1, "end_line": 1}


def normalized(cid, text, doc="A"):
    from app.retrieval.pipeline import normalize
    return normalize(hit(cid, text, doc))


def test_complementary_rule_survives_repeated_chunks_and_heading_only_results():
    a = normalized("a", "Network connection requires managed devices.")
    hs = [a, a, normalized("header", "# Network guide"),
          normalized("again", "Network connection requires managed devices."),
          normalized("b", "Windows 12 is the supported platform.", "B")]
    chosen = select_evidence("Network connection on Windows", hs, 3)
    assert {h["chunk_id"] for h in chosen} == {"a", "again", "b"}
    assert next(h for h in chosen if h["chunk_id"] == "b")["citation"] == hs[-1]["citation"]
    assert len({h["chunk_id"] for h in chosen}) == len(chosen)


def test_selector_preserves_original_direct_answer_core_when_subqueries_drift():
    hs = [normalized("new", "general introductory advice", "B"),
          normalized("original", "Disable retries after the first failure."),
          normalized("rule", "Only an administrator may resume retries.")]
    result = select_evidence("Should retries resume?", hs, 2, ["unrelated introduction"], ["original", "rule"])
    assert result[0]["chunk_id"] == "original"


def test_clause_fallback_is_bounded_and_does_not_truncate_history_corrections():
    query = "Network fails; Windows version is old; firewall is disabled; account is locked; device is lost"
    result = plan_queries(query)
    assert result[0] == query and len(result) <= 4
    assert plan_queries("它怎么办？\n前一轮问题：Windows version corrected") == ["它怎么办？\n前一轮问题：Windows version corrected"]


class Model:
    def __init__(self, raw): self.raw, self.calls = raw, 0
    async def generate(self, messages, settings):
        self.calls += 1
        assert settings.output_limit == 384
        assert "evidence" not in json.loads(messages[1]["content"])
        return self.raw


def test_planner_accepts_spacing_normalization_without_inventing_numbers():
    model = Model('{"queries":["30MB L3 external sharing restrictions","30 MB attachment size policy"]}')
    planner = QueryPlanner(model, GenerationSettings(provider="compatible", api_key="mock"))
    queries, diagnostic = asyncio.run(planner.plan("Share a 30 MB L3 document externally"))
    assert diagnostic["status"] == "completed" and len(queries) == 3
    assert model.calls == 1


@pytest.mark.parametrize("raw", ["private-model-marker", '{"queries":["Windows 99 requirements"]}',
    '{"queries":["x","y","z","four"]}', '{"queries":["   "]}', '{"queries":[],"extra":"private-model-marker"}'])
def test_bad_planning_falls_back_to_exact_original_and_excludes_raw_output(raw):
    planner = QueryPlanner(Model(raw), GenerationSettings(provider="compatible", api_key="mock"))
    queries, diagnostic = asyncio.run(planner.plan("Windows 12 requirements"))
    assert queries == ["Windows 12 requirements"] and diagnostic["status"] == "failed_fallback"
    assert "private-model-marker" not in json.dumps(diagnostic)


def test_fake_planner_never_claims_model_semantic_planning():
    model = Model("must not be called")
    _, diagnostic = asyncio.run(QueryPlanner(model, GenerationSettings()).plan("Network fails; account is locked"))
    assert diagnostic == {"status": "rules", "model_calls": 0} and model.calls == 0


class Vectors:
    def __init__(self):
        self.vectors = self
        self.allowed = [{"document_id": d, "chunk_set_id": "set"} for d in ("A", "B")]
        self.calls = []
        self.fail_extra = False
    def allowed_sets(self, identity, filters=None):
        return self.allowed if filters.get("source") != "missing" else []
    def lexical_candidates(self, allowed, tenant):
        ids = {a["document_id"] for a in allowed}
        return [h for h in [hit("network", "Network retries require approval."), hit("windows", "Windows version 12 is supported.", "B")] if h["document_id"] in ids]
    async def search(self, query, identity, top_k, mode, ef, filters):
        self.calls.append((query, identity, dict(filters)))
        if self.fail_extra and len(self.calls) > 1:
            raise AppError("EMBEDDING_FAILED", "private-provider-marker", 502)
        hs = [hit("network", "Network retries require approval."), hit("windows", "Windows version 12 is supported.", "B")]
        return {"collection": "c", "model_id": "fake", "dimensions": 2, "mode": mode,
            "run_id": str(len(self.calls)), "duration_ms": 1, "hits": hs[:top_k]}


class Planner:
    def __init__(self, vectors, revoke=False): self.vectors, self.revoke = vectors, revoke
    async def plan(self, query):
        if self.revoke: self.vectors.allowed = []
        return [query, "Windows version"], {"status": "completed", "model_calls": 1}


def test_subqueries_reuse_identity_filters_and_expose_actual_authorized_pool():
    v = Vectors(); identity = LOCAL_IDENTITIES["u_employee_001"]
    service = RetrievalService(v, RetrievalSettings(strategy="hybrid", candidate_k=2), planner=Planner(v))
    result = asyncio.run(service.retrieve("Network on Windows", identity, 2, filters={"source": "visible"}, retrieval_profile="multi_query"))
    assert len(v.calls) == 2
    assert all(i is identity and f == {"source": "visible"} for _, i, f in v.calls)
    assert len(result["hits"]) <= 2
    assert result["diagnostics"]["original_candidate_ids"]


def test_revocation_during_planning_removes_all_old_diagnostic_and_final_text():
    v = Vectors(); identity = LOCAL_IDENTITIES["u_employee_001"]
    service = RetrievalService(v, RetrievalSettings(strategy="hybrid", candidate_k=2), planner=Planner(v, True))
    result = asyncio.run(service.retrieve("Network on Windows", identity, 2, filters={}, retrieval_profile="multi_query"))
    assert result["hits"] == result["diagnostics"]["candidates"] == []
    assert result["diagnostics"]["original_candidate_ids"] == []
    assert "retries require approval" not in json.dumps(result)


def test_supplemental_failure_keeps_original_evidence_and_records_degradation():
    v = Vectors(); v.fail_extra = True
    service = RetrievalService(v, RetrievalSettings(strategy="hybrid", candidate_k=2), planner=Planner(v))
    result = asyncio.run(service.retrieve("Network on Windows", LOCAL_IDENTITIES["u_employee_001"], 2, filters={}, retrieval_profile="multi_query"))
    assert result["hits"] and result["diagnostics"]["query_runs"][-1]["status"] == "failed_fallback"
    assert "private-provider-marker" not in json.dumps(result)
