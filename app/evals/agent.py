"""Executable Agent scenario oracles. Scripted policy != real-model quality."""
import asyncio
import json
import time
from uuid import uuid4
from app.agent.contracts import ApprovalRequest, dump
from app.agent.provider import tool_message, Retryable
from app.agent.service import AgentService
from app.core.errors import AppError
from app.db.agent import AgentRepository

class FixtureRetrieval:
    async def retrieve(self, query, identity, top_k=3):
        return {"hits": [{"text": "TL-403：更新设备补丁后重试。", "source": "fixture/tl403.md", "score": 1,
                          "citation": {"chunk_id": "fixture-tl403"}}], "duration_ms": .1}

class TimeoutRetrieval:
    async def retrieve(self, *args, **kwargs):
        raise Retryable()

class ScenarioModel:
    def __init__(self, scenario):
        self.scenario = scenario

    async def complete(self, messages, tools):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        code = self.scenario
        if not results:
            if code == "A001":
                message = tool_message("search_knowledge_base", {"query": "TL-403", "top_k": 3})
                message["tool_calls"] += tool_message("query_business_data", {"query": "device_status"})["tool_calls"]
            elif code in {"A002", "A005"}:
                message = tool_message("query_business_data", {"query": "ticket_durations"})
            elif code == "A003":
                message = {"role": "assistant", "content": dump({"status": "needs_clarification", "answer": "请补充系统、所需权限和目标。"})}
            elif code == "A004":
                message = tool_message("search_knowledge_base", {"query": "TL-403"})
            else:
                message = tool_message("create_ticket", {"title": "模拟权限问题", "description": "申请检查本人的测试系统权限"})
        elif code == "A002" and len(results) == 1:
            data = results[0]["data"]
            message = tool_message("calculator", {"left": data["triage_minutes"], "operator": "+", "right": data["resolution_minutes"]})
        elif code == "A005":
            message = tool_message("query_business_data", {"query": "ticket_durations"})
        else:
            answer = (f'模拟总耗时 {results[-1]["value"]} 分钟，目标 {results[0]["data"]["target_minutes"]} 分钟，未超时。'
                      if code == "A002" else "本次工具证据与模拟状态：" + dump(results))
            message = {"role": "assistant", "content": dump({"status": "completed", "answer": answer})}
        return {"message": message, "usage": {}, "model": "scripted-oracle-policy-v1"}

def approval_for(state, approve=True):
    p = state["pending"]
    return ApprovalRequest(action_id=p["action_id"], version=p["version"], digest=p["digest"], approve=approve)

def ticket_count(repo, run_id):
    with repo.connection() as db:
        return db.execute("SELECT count(*) AS n FROM ko_agent_tickets t JOIN ko_agent_actions a USING(action_id) WHERE a.run_id=%s", (run_id,)).fetchone()["n"]

async def run_scenario(repo, identity, scenario):
    start = time.perf_counter()
    sid = scenario["scenario_id"]
    model = ScenarioModel(sid)
    service = AgentService(repo, model, repo.settings, TimeoutRetrieval() if sid == "A004" else FixtureRetrieval())
    state = repo.create(identity, scenario["task"], uuid4().hex, uuid4().hex)
    rid = state["run_id"]
    await service.execute(rid, identity)
    state = repo.get(rid, identity)
    checks = []
    observed = state["outcome"] or state["status"]
    if sid == "A006":
        state = repo.approve(rid, identity, approval_for(state, False))
        observed = state["outcome"]
    elif sid == "A007":
        restored = AgentRepository(repo.settings)
        checks.append(restored.get(rid, identity)["pending"] == state["pending"])
        await AgentService(restored, model, repo.settings).execute(rid, identity)
        state = restored.get(rid, identity)
        observed = state["status"]
    elif sid == "A008":
        approval = approval_for(state)
        repo.approve(rid, identity, approval)  # commit boundary; next executor is newly constructed
        restored = AgentRepository(repo.settings)
        restored.approve(rid, identity, approval)
        await AgentService(restored, model, repo.settings).execute(rid, identity)
        state = restored.get(rid, identity)
        observed = state["outcome"]
        checks.append(ticket_count(repo, rid) == 1)
    elif sid == "A009":
        approval = approval_for(state).model_copy(update={"digest": "0"*64})
        try:
            repo.approve(rid, identity, approval)
            checks.append(False)
        except AppError as exc:
            checks.append(exc.code == "APPROVAL_MISMATCH")
        state = repo.get(rid, identity)
        observed = state["status"]
    elif sid == "A010":
        with repo.connection() as db:
            db.execute("UPDATE ko_agent_permissions SET can_write=false WHERE tenant_id=%s AND user_id=%s", (identity.tenant_id, identity.user_id))
        try:
            repo.approve(rid, identity, approval_for(state))
            checks.append(False)
        except AppError as exc:
            observed = "permission_denied" if exc.code == "PERMISSION_DENIED" else exc.code
            checks.append(exc.code == "PERMISSION_DENIED")
        finally:
            with repo.connection() as db:
                db.execute("UPDATE ko_agent_permissions SET can_write=true WHERE tenant_id=%s AND user_id=%s", (identity.tenant_id, identity.user_id))
    if sid in {"A004", "A005"}:
        observed = state["status"]
        checks.append(state["outcome"] == ("TOOL_TIMEOUT" if sid == "A004" else "NO_PROGRESS"))
    if sid == "A001":
        checks.append("TL-403" in state["answer"] and "compliant" in state["answer"])
    if sid == "A002":
        checks.append("40" in state["answer"] and "60" in state["answer"])
        checks.append(state["trace"][1]["arguments"] == {"left": 12.0, "operator": "+", "right": 28.0})
    if sid != "A008":
        checks.append(ticket_count(repo, rid) == 0)
    aliases = {"knowledge_search": "search_knowledge_base", "business_query": "query_business_data"}
    used = {t["name"] for t in state["trace"]}
    checks.append(all(aliases.get(t, t) in used for t in scenario["required_tools"]))
    checks.append(observed == scenario["expected_terminal_state"])
    return {"scenario_id": sid, "run_id": rid, "passed": all(checks), "checks": checks,
            "expected": scenario["expected_terminal_state"], "observed": observed,
            "status": state["status"], "outcome": state["outcome"], "ticket_count": ticket_count(repo, rid),
            "steps": state["steps"], "duration_ms": (time.perf_counter()-start)*1000,
            "estimated_cost": 0.0, "cost_scope": "scripted local policy; no model API calls",
            "trace": state["trace"], "events": state["events"], "reserved_tokens": state["reserved_tokens"]}
