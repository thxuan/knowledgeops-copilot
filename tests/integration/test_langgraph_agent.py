"""Critical stage-3 checks only; no model provider or production workspace."""
import asyncio
import time
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from app.agent.contracts import AgentSettings, ProposalRevision, dump
from app.agent.middleware import AgentPolicy
from app.agent.chat_model import AgentChatModel
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import convert_to_messages
from app.agent.service import AgentService
from app.core.errors import AppError
from app.core.identity import Identity
from app.db.agent import AgentRepository
from app.db.agent_checkpoints import AgentCheckpointSaver
from tests.agent_scenarios import approval_for, ticket_count
from tests.doubles import ScriptedAgentDouble, tool_message
from conftest import pg_dsn


@pytest.fixture
def domain():
    repo = AgentRepository(AgentSettings(database_url=pg_dsn("graph")))
    repo.initialize()
    return repo, Identity("graph-user", "graph-tenant", frozenset())


def test_revised_proposal_interrupt_survives_new_service_and_commits_once(domain):
    repo, identity = domain
    state = repo.create(identity, "创建工单", uuid4().hex, "graph-test")
    run_id = state["run_id"]
    asyncio.run(AgentService(repo, ScriptedAgentDouble(), repo.settings).execute(run_id, identity))
    pending = repo.get(run_id, identity)
    assert pending["status"] == "waiting_approval"
    saver = AgentCheckpointSaver(repo, run_id, identity, None)
    stored = saver.get_tuple({"configurable": {"thread_id": run_id}})
    assert any(channel == "__interrupt__" for _, channel, _ in stored.pending_writes)
    assert ticket_count(repo, run_id) == 0
    p = pending["pending"]
    revised = repo.revise_proposal(run_id, identity, ProposalRevision(
        action_id=p["action_id"], version=p["version"], digest=p["digest"],
        arguments={"title": "Updated VPN request", "description": "Please check the managed VPN client."}))
    approval = approval_for(revised)
    repo.approve(run_id, identity, approval)
    restored = AgentRepository(repo.settings)

    class AfterApproval:
        async def complete(self, messages, tools):
            request = next(m for m in messages if m.get("tool_calls"))
            assert "Updated VPN request" in request["tool_calls"][0]["function"]["arguments"]
            assert messages[-1]["role"] == "tool"
            return {"message": {"role": "assistant", "content": dump({
                "status": "completed", "answer": "Submitted; waiting for support."})}, "usage": {}, "model": "offline"}

    asyncio.run(AgentService(restored, AfterApproval(), restored.settings).execute(run_id, identity))
    assert restored.get(run_id, identity)["status"] == "completed"
    restored.approve(run_id, identity, approval)
    assert ticket_count(restored, run_id) == 1


def test_domain_receipt_prevents_repeating_a_completed_model_node(domain):
    repo, identity = domain
    state = repo.create(identity, "计算 2 + 3", uuid4().hex, "receipt-test")
    _, token = repo.claim(state["run_id"], identity)

    class CountingModel(ScriptedAgentDouble):
        calls = 0
        async def complete(self, *args):
            self.calls += 1
            return await super().complete(*args)

    model = CountingModel()
    service = AgentService(repo, model, repo.settings)
    request = ModelRequest(model=AgentChatModel(backend=model),
                           messages=convert_to_messages(state["messages"]), tools=[])
    async def handler(request):
        reply = await request.model.bind_tools(request.tools).ainvoke(request.messages)
        return ModelResponse(result=[reply])
    first = AgentPolicy(service, state["run_id"], identity, token, time.monotonic(), 0)
    outcome = asyncio.run(first.awrap_model_call(request, handler))
    # Simulate a crash after the domain commit, before graph output persistence.
    replay = AgentPolicy(service, state["run_id"], identity, token, time.monotonic(), 0)
    assert asyncio.run(replay.awrap_model_call(request, handler)) == outcome
    assert model.calls == 1
    repo.release_lease(state["run_id"], identity, token, 0)


def test_cancel_fences_framework_writes_and_hides_internal_receipt(domain):
    from app.api.agent_routes import public
    repo, identity = domain
    state = repo.create(identity, "计算 2 + 3", uuid4().hex, "cancel-test")
    _, token = repo.claim(state["run_id"], identity)
    saver = AgentCheckpointSaver(repo, state["run_id"], identity, token)
    config = {"configurable": {"thread_id": state["run_id"], "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    config = saver.put(config, checkpoint, {"source": "input", "step": -1}, {})
    repo.cancel(state["run_id"], identity)
    for write in (lambda: saver.put(config, empty_checkpoint(), {}, {}),
                  lambda: saver.put_writes(config, [("next", "model")], "old-task")):
        with pytest.raises(AppError) as error:
            write()
        assert error.value.code == "LEASE_LOST"
    outsider = AgentCheckpointSaver(repo, state["run_id"], Identity("other", identity.tenant_id, frozenset()), token)
    with pytest.raises(AppError) as error:
        outsider.get_tuple(config)
    assert error.value.status_code == 404
    assert public({"_graph_receipt": {}, "_agent_model_receipt": {}, "status": "cancelled"}) == {"status": "cancelled"}


def test_legacy_pending_read_tool_adopts_graph_without_repeating_model(domain):
    repo, identity = domain
    state = repo.create(identity, "计算 2 + 3", uuid4().hex, "legacy-test")
    state, token = repo.claim(state["run_id"], identity)
    state["messages"].append(tool_message("calculator", {"left": 2.0, "operator": "+", "right": 3.0}))
    state["phase"] = "tools"
    state["steps"] = 1
    # A checkpoint from the replaced explicit graph must not be read as a
    # create_agent checkpoint. Both runtimes can use the same business run_id.
    old_saver = AgentCheckpointSaver(repo, state["run_id"], identity, token, namespace_prefix="")
    old = empty_checkpoint()
    old["channel_values"] = {"sequence": 2, "next": "tools"}
    old_saver.put({"configurable": {"thread_id": state["run_id"], "checkpoint_ns": ""}}, old, {}, {})
    repo.checkpoint(state, identity, token, True)

    class Finish:
        async def complete(self, messages, tools):
            assert messages[-1]["role"] == "tool" and '"5"' in messages[-1]["content"]
            return {"message": {"role": "assistant", "content": dump({"status": "completed", "answer": "5"})},
                    "usage": {}, "model": "offline"}

    asyncio.run(AgentService(repo, Finish(), repo.settings).execute(state["run_id"], identity))
    done = repo.get(state["run_id"], identity)
    assert done["status"] == "completed" and done["steps"] == 2
    assert len(done["trace"]) == 1


def test_create_agent_runs_real_langchain_adapter_with_offline_http(domain):
    import json
    import httpx
    from dataclasses import replace
    from app.core.langchain_models import LangChainAgentModel
    repo, identity = domain
    repo.settings = replace(repo.settings, provider="compatible", model_id="wire-test", api_key="offline-key",
                            input_price=1.0, output_price=1.0, max_cost=2.0)
    requests = []

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["max_tokens"] == repo.settings.output_limit
        assert {t["function"]["name"] for t in body["tools"]} == {
            "calculator", "query_business_data", "search_knowledge_base", "create_ticket"}
        assert all("runtime" not in t["function"]["parameters"]["properties"] for t in body["tools"])
        if body["messages"][-1]["role"] == "tool":
            assert json.loads(body["messages"][-1]["content"])["value"] == "5"
            message = {"role": "assistant", "content": dump({"status": "completed", "answer": "5"})}
            finish = "stop"
        else:
            message = tool_message("calculator", {"left": 2.0, "operator": "+", "right": 3.0})
            finish = "tool_calls"
        return httpx.Response(200, json={"model": "wire-test", "choices": [{
            "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}})

    state = repo.create(identity, "offline wire task", uuid4().hex, "wire-test")
    backend = LangChainAgentModel(repo.settings, httpx.MockTransport(provider))
    asyncio.run(AgentService(repo, backend, repo.settings).execute(state["run_id"], identity))
    done = repo.get(state["run_id"], identity)
    assert done["status"] == "completed" and done["answer"] == "5"
    assert len(requests) == 2 and done["actual_model_tokens"] == 50
    checkpoint = AgentCheckpointSaver(repo, state["run_id"], identity, None).get_tuple(
        {"configurable": {"thread_id": state["run_id"]}})
    assert [m.type for m in checkpoint.checkpoint["channel_values"]["messages"]] == [
        "system", "human", "ai", "tool", "ai"]
