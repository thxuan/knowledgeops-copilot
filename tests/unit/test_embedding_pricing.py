"""Pricing configuration and budget gates without databases or provider calls."""
import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.contracts import AgentSettings, dump
from app.agent.service import AgentService
from app.api.workbench_management import EmbeddingConnection, connections, embedding_candidate
from app.core.config import Settings
from app.core.embedding import EmbeddingSettings


def config_app(price=None):
    embedding = EmbeddingSettings(provider="compatible", base_url="https://embedding.test/v1",
                                  api_key="test-only", dimensions=16, cost_per_million_tokens=price)
    return NS(state=NS(settings=replace(Settings(), embedding=embedding)))


def body(**values):
    return EmbeddingConnection(base_url=values.pop("base_url", "https://embedding.test/v1"),
        model_id=values.pop("model_id", "text-embedding-v4"), dimensions=16,
        collection="knowledgeops-v1", **values)


def test_price_round_trip_preserves_signature_and_secret_boundary():
    app = config_app()
    before = app.state.settings.embedding
    after = embedding_candidate(app, body(cost_per_million_tokens=0.25))
    assert after.cost_per_million_tokens == 0.25
    assert after.signature == before.signature
    assert after.api_key == before.api_key
    app.state.settings = replace(app.state.settings, embedding=after)
    view = asyncio.run(connections(NS(app=app), None))
    assert view["embedding"]["cost_per_million_tokens"] == 0.25
    assert "api_key" not in view["embedding"]


def test_omitted_price_reuses_only_the_same_model_and_explicit_null_clears():
    app = config_app(0.25)
    assert embedding_candidate(app, body()).cost_per_million_tokens == 0.25
    assert embedding_candidate(app, body(model_id="another-model")).cost_per_million_tokens is None
    assert embedding_candidate(app, body(base_url="https://other.test/v1", api_key="other-key")).cost_per_million_tokens is None
    assert embedding_candidate(app, body(cost_per_million_tokens=None)).cost_per_million_tokens is None


@pytest.mark.parametrize("price", [-1, float("nan"), float("inf"), -float("inf")])
def test_rejects_invalid_price(price):
    with pytest.raises(ValidationError):
        body(cost_per_million_tokens=price)


def run_tools(monkeypatch, price, *, knowledge=True, max_cost=0.1, max_tokens=120000,
              legacy=True, business_first=True, searches=1, cached_search=False):
    settings = AgentSettings(max_cost=max_cost, max_tokens=max_tokens, max_steps=1)
    calls = [{"id": "business", "type": "function", "function": {
        "name": "query_business_data", "arguments": '{"query":"device_status"}'}}]
    calls.extend({"id": f"search-{i}", "type": "function", "function": {
        "name": "search_knowledge_base", "arguments": dump({"query": "设备合规" + str(i)})}}
        for i in range(searches if knowledge else 0))
    if not business_first:
        calls.reverse()
    message = {"role": "assistant", "content": "", "tool_calls": calls}
    state = {"status": "running", "phase": "tools" if legacy else "queued",
             "steps": settings.max_steps if legacy else 0,
             "elapsed_seconds": 0, "reserved_tokens": 0, "reserved_cost": 0,
             "messages": [{"role": "system", "content": "Use tools."},
                          {"role": "user", "content": "查询设备合规"}],
             "usage": [], "seen": [], "events": [], "trace": []}
    if legacy:
        state["messages"].append(message)
    if cached_search:
        # A partially completed batch already paid for its successful search.
        call = next(call for call in calls if call["function"]["name"] == "search_knowledge_base")
        query = "设备合规0"
        state["reserved_tokens"] = (len(query.encode()) + 256) * 3 * 3
        state["reserved_cost"] = state["reserved_tokens"] * price / 1e6
        state["trace"].append({"call_id": call["id"], "name": "search_knowledge_base",
                               "status": "succeeded", "result": {}})
        state["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": "{}"})
    snapshots, invoked = [], []

    class Repo:
        released = False
        def claim(self, *args):
            return self.get(), "lease"
        def get(self, *args):
            return copy.deepcopy(state)
        def checkpoint(self, current, *args):
            snapshots.append(copy.deepcopy(current))
            state.clear()
            state.update(copy.deepcopy(current))
        def release_lease(self, *args):
            self.released = True
        def business(self, *args, **kwargs):
            invoked.append("business")
            return {}

    async def retrieve(*args, **kwargs):
        invoked.append("search")
        return {"hits": [], "duration_ms": 0}

    class Model:
        async def complete(self, *args):
            assert not legacy, "Pending tools must resume without another model call"
            return {"message": copy.deepcopy(message), "usage": {}, "model": "offline"}

    retrieval = NS(vectors=NS(settings=EmbeddingSettings(provider="compatible", api_key="test-only",
                    cost_per_million_tokens=price)), settings=NS(strategy="hybrid"), retrieve=retrieve)
    repo = Repo()
    # Keep the real create_agent graph, middleware and tools; only persistence
    # and external I/O are replaced by offline doubles.
    monkeypatch.setattr("app.agent.service.AgentCheckpointSaver", lambda *args: InMemorySaver())
    service = AgentService(repo, Model(), settings, retrieval)
    asyncio.run(service.execute("offline-test", None))
    assert repo.released
    return state, snapshots, invoked


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("business_first", [True, False])
def test_missing_price_stops_entire_batch_before_any_tool(monkeypatch, legacy, business_first):
    state, snapshots, invoked = run_tools(monkeypatch, None, legacy=legacy, business_first=business_first)
    assert state["outcome"] == "UNPRICED_TOOL"
    assert invoked == [] and state["trace"] == []
    assert snapshots[-1]["status"] == "failed"


@pytest.mark.parametrize("price", [0, 0.25])
def test_explicit_price_reserves_cost_and_allows_tools(monkeypatch, price):
    state, _, invoked = run_tools(monkeypatch, price)
    assert sorted(invoked) == ["business", "search"]
    expected_tokens = (len("设备合规0".encode()) + 256) * 3 * 3
    assert state["reserved_tokens"] == expected_tokens
    assert state["reserved_cost"] == expected_tokens * price / 1e6
    assert state["outcome"] == "STEP_LIMIT"


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("business_first", [True, False])
def test_price_does_not_bypass_cost_limit(monkeypatch, legacy, business_first):
    state, _, invoked = run_tools(monkeypatch, 10, max_cost=0.000001,
                                  legacy=legacy, business_first=business_first)
    assert state["outcome"] == "COST_LIMIT"
    assert invoked == []


def test_business_only_does_not_require_embedding_price(monkeypatch):
    state, _, invoked = run_tools(monkeypatch, None, knowledge=False)
    assert invoked == ["business"]
    assert state["reserved_cost"] == 0


@pytest.mark.parametrize("legacy", [True, False])
def test_combined_retrieval_cost_stops_entire_batch(monkeypatch, legacy):
    # Each search fits by itself, but both reservations together exceed budget.
    state, _, invoked = run_tools(monkeypatch, 1, max_cost=0.0035, searches=2, legacy=legacy)
    assert state["outcome"] == "COST_LIMIT"
    assert invoked == [] and state["trace"] == []


def test_combined_retrieval_tokens_stop_entire_batch(monkeypatch):
    state, _, invoked = run_tools(monkeypatch, 0, max_tokens=3500, searches=2)
    assert state["outcome"] == "TOKEN_LIMIT"
    assert invoked == [] and state["trace"] == []


def test_multiple_searches_reserve_each_call_once(monkeypatch):
    state, _, invoked = run_tools(monkeypatch, 0.25, searches=2)
    assert sorted(invoked) == ["business", "search", "search"]
    expected_tokens = sum((len(("设备合规" + str(i)).encode()) + 256) * 3 * 3 for i in range(2))
    assert state["reserved_tokens"] == expected_tokens
    assert state["reserved_cost"] == expected_tokens * 0.25 / 1e6
    assert state["outcome"] == "STEP_LIMIT"


@pytest.mark.parametrize("price", [0, 0.25])
def test_new_model_batch_with_explicit_price_runs_all_tools(monkeypatch, price):
    state, _, invoked = run_tools(monkeypatch, price, legacy=False)
    assert sorted(invoked) == ["business", "search"]
    assert state["reserved_cost"] == (len("设备合规0".encode()) + 256) * 3 * 3 * price / 1e6
    assert state["outcome"] == "STEP_LIMIT"


def test_resumed_batch_does_not_charge_or_repeat_receipted_search(monkeypatch):
    state, _, invoked = run_tools(monkeypatch, 1, max_cost=0.0035, cached_search=True)
    assert invoked == ["business"]
    expected_tokens = (len("设备合规0".encode()) + 256) * 3 * 3
    assert state["reserved_tokens"] == expected_tokens
    assert state["reserved_cost"] == expected_tokens / 1e6
    assert len(state["trace"]) == 2
    assert state["outcome"] == "STEP_LIMIT"
