"""Pricing configuration and budget gates without databases or provider calls."""
import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError

from app.agent.contracts import AgentSettings
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


def run_tools(price, *, knowledge=True, max_cost=0.1):
    settings = AgentSettings(max_cost=max_cost)
    calls = [{"id": "business", "function": {"name": "query_business_data", "arguments": '{"query":"device_status"}'}}]
    if knowledge:
        calls.append({"id": "search", "function": {"name": "search_knowledge_base", "arguments": '{"query":"设备合规"}'}})
    state = {"status": "running", "phase": "tools", "steps": settings.max_steps,
             "elapsed_seconds": 0, "reserved_tokens": 0, "reserved_cost": 0,
             "messages": [{"role": "assistant", "tool_calls": calls}], "events": [], "trace": []}
    snapshots, invoked = [], []

    class Repo:
        def claim(self, *args):
            return state, "lease"
        def checkpoint(self, current, *args):
            snapshots.append(copy.deepcopy(current))

    retrieval = NS(vectors=NS(settings=EmbeddingSettings(provider="compatible", api_key="test-only",
                    cost_per_million_tokens=price)), settings=NS(strategy="hybrid"))
    service = AgentService(Repo(), None, settings, retrieval)

    async def read(call, identity):
        invoked.append(call["id"])
        return {"call_id": call["id"], "name": call["function"]["name"], "status": "succeeded", "result": {}}
    service.read_tool = read
    asyncio.run(service.execute("offline-test", None))
    return state, snapshots, invoked


def test_missing_price_stops_entire_batch_before_any_tool():
    state, snapshots, invoked = run_tools(None)
    assert state["outcome"] == "UNPRICED_TOOL"
    assert invoked == [] and state["trace"] == []
    assert snapshots[-1]["status"] == "failed"


@pytest.mark.parametrize("price", [0, 0.25])
def test_explicit_price_reserves_cost_and_allows_tools(price):
    state, _, invoked = run_tools(price)
    assert invoked == ["business", "search"]
    expected_tokens = (len("设备合规".encode()) + 256) * 3 * 3
    assert state["reserved_tokens"] == expected_tokens
    assert state["reserved_cost"] == expected_tokens * price / 1e6
    assert state["outcome"] == "STEP_LIMIT"


def test_price_does_not_bypass_cost_limit():
    state, _, invoked = run_tools(10, max_cost=0.000001)
    assert state["outcome"] == "COST_LIMIT"
    assert invoked == []


def test_business_only_does_not_require_embedding_price():
    state, _, invoked = run_tools(None, knowledge=False)
    assert invoked == ["business"]
    assert state["reserved_cost"] == 0
