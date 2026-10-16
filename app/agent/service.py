"""Bounded, checkpointed execution; only repository approval can write tickets."""
import asyncio
import json
import logging
import random
import time
from decimal import Decimal
from pydantic import ValidationError
from app.agent.contracts import SPECS, TOOLS, FINAL_REPAIR_PROMPT, parse_final, dump, event, redact, suspicious
from app.agent.calculator import CalculatorArguments, calculate
from app.agent.provider import Retryable
from app.core.errors import AppError

class AgentService:
    def __init__(self, repository, model, settings, retrieval=None):
        self.repo, self.model, self.settings, self.retrieval = repository, model, settings, retrieval
        self.jobs = {}

    def launch(self, run_id, identity):
        if run_id not in self.jobs:
            if len(self.jobs) >= self.settings.concurrency:
                return  # Durable queued run; POST resume retries admission without creating a run.
            task = asyncio.create_task(self.execute(run_id, identity))
            self.jobs[run_id] = task
            def finished(task):
                self.jobs.pop(run_id, None)
                if not task.cancelled() and task.exception():
                    logging.getLogger("knowledgeops").error(dump({"event": "agent.worker_failed", "run_id": run_id,
                        "error_type": type(task.exception()).__name__}))
            task.add_done_callback(finished)

    async def shutdown(self):
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def execute(self, run_id, identity):
        try:
            claimed = await asyncio.to_thread(self.repo.claim, run_id, identity)
        except AppError as exc:
            if exc.code in {"RUN_BUSY", "CONCURRENCY_LIMIT"}:
                return
            raise
        if not claimed:
            return
        state, token = claimed
        started, prior = time.monotonic(), state["elapsed_seconds"]

        async def save(release=False):
            state["elapsed_seconds"] = prior + time.monotonic()-started
            await asyncio.to_thread(self.repo.checkpoint, state, identity, token, release)

        try:
            if state.get("policy", self.settings.policy) != self.settings.policy:
                raise AppError("POLICY_CHANGED", "Restore the original run model/budget configuration before execution", 409)
            async with asyncio.timeout(max(.001, self.settings.max_seconds-prior)):
                while state["status"] == "running":
                    # An assistant request is durable before tool execution. Replays only read-only work.
                    if state["phase"] == "tools":
                        request = next(m for m in reversed(state["messages"]) if m["role"] == "assistant")
                        resolved = {m["tool_call_id"] for m in state["messages"] if m["role"] == "tool"}
                        pending = [c for c in request["tool_calls"] if c["id"] not in resolved]
                        if pending and pending[0]["function"]["name"] == "create_ticket":
                            call = pending[0]
                            state["elapsed_seconds"] = prior + time.monotonic()-started
                            await asyncio.to_thread(self.repo.propose, state, identity, token, call["id"],
                                                    json.loads(call["function"]["arguments"]))
                            return
                        # Calls in one model turn must be independent; results are not visible until next turn.
                        # Reserve paid retrieval before any external query, including its nested retries.
                        for call in pending:
                            if call["function"]["name"] == "search_knowledge_base" and hasattr(self.retrieval, "vectors"):
                                embedding = self.retrieval.vectors.settings
                                if embedding.provider == "compatible":
                                    if embedding.cost_per_million_tokens is None:
                                        raise AppError("UNPRICED_TOOL", "请在连接设置中填写 Embedding 单价并应用后，重新发起任务；本轮工具尚未执行。", 422)
                                    query = json.loads(call["function"]["arguments"])["query"]
                                    tokens = (len(query.encode())+256)*embedding.max_attempts*3
                                    cost = tokens*embedding.cost_per_million_tokens/1e6
                                    if state["reserved_cost"]+cost > self.settings.max_cost:
                                        raise AppError("COST_LIMIT", "Retrieval cost reservation exceeds budget", 422)
                                    if state["reserved_tokens"]+tokens > self.settings.max_tokens:
                                        raise AppError("TOKEN_LIMIT", "Retrieval token reservation exceeds budget", 422)
                                    state["reserved_tokens"] += tokens
                                    state["reserved_cost"] += cost
                                if self.retrieval.settings.strategy == "hybrid_rerank" and self.retrieval.settings.rerank.provider == "compatible":
                                    raise AppError("UNBOUNDED_RERANK_COST", "Use frozen hybrid profile for budgeted Agent retrieval", 422)
                        await save()
                        results = await asyncio.gather(*(self.read_tool(c, identity) for c in pending))
                        failed = None
                        for call, result in zip(pending, results):
                            state["trace"].append(result)
                            state["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": dump(result["result"])})
                            event(state, "tool_end", **result)
                            if suspicious(dump(result["result"])):
                                event(state, "security_warning", code="UNTRUSTED_INSTRUCTION", call_id=call["id"])
                            if result["status"] != "succeeded":
                                failed = result["result"]["error"]
                        state["phase"] = "model"
                        await save()
                        if failed:
                            raise AppError(failed, "Tool failed; no unsupported success answer", 502)
                    if state["steps"] >= self.settings.max_steps:
                        raise AppError("STEP_LIMIT", "Maximum agent steps reached", 422)
                    state["steps"] += 1
                    if suspicious(state["messages"][1]["content"]) and state["steps"] == 1:
                        event(state, "security_warning", code="UNTRUSTED_INSTRUCTION")
                    active_tools = [] if state["phase"] == "final_repair" else SPECS
                    model_started = time.monotonic()
                    for attempt in range(3):
                        # Conservative UTF-8 byte reservation includes tools/framing and stays charged on unknown failures.
                        estimate = len(dump({"messages": state["messages"], "tools": active_tools}).encode()) + 256
                        total = estimate + self.settings.output_limit
                        cost = (estimate*(self.settings.input_price or 0) + self.settings.output_limit*(self.settings.output_price or 0))/1e6
                        if total > self.settings.context_limit or state["reserved_tokens"]+total > self.settings.max_tokens:
                            raise AppError("TOKEN_LIMIT", "Token budget exhausted", 422)
                        if state["reserved_cost"]+cost > self.settings.max_cost:
                            raise AppError("COST_LIMIT", "Cost budget exhausted", 422)
                        state["reserved_tokens"] += total
                        state["reserved_cost"] += cost
                        state["phase"] = "final_repair" if not active_tools else "model"
                        await save()
                        try:
                            turn = await self.model.complete(state["messages"], active_tools)
                            break
                        except Retryable as exc:
                            event(state, "retry", component="model", attempt=attempt+1)
                            if attempt == 2:
                                raise AppError("MODEL_UNAVAILABLE", "Model retries exhausted", 502)
                            await save()
                            await asyncio.sleep(max(exc.retry_after, .1*2**attempt)+random.uniform(0,.05))
                    duration = (time.monotonic()-model_started)*1000
                    state["usage"].append({"model": turn["model"], "provider_usage": turn["usage"], "duration_ms": duration})
                    usage = turn["usage"]
                    if self.settings.provider == "compatible":
                        if all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens")):
                            state["actual_model_tokens"] += usage["prompt_tokens"]+usage["completion_tokens"]
                            if state["estimated_model_cost"] is not None:
                                state["estimated_model_cost"] += (usage["prompt_tokens"]*self.settings.input_price + usage["completion_tokens"]*self.settings.output_price)/1e6
                            if state["actual_model_tokens"] > self.settings.max_tokens or (state["estimated_model_cost"] or 0) > self.settings.max_cost:
                                raise AppError("PROVIDER_BUDGET_EXCEEDED", "Reported usage exceeded reserved budget; stop execution", 422)
                        else:
                            state["estimated_model_cost"] = None
                    message = turn["message"]
                    if len(dump(message)) > self.settings.output_limit*16:
                        raise AppError("OUTPUT_LIMIT", "Model output too large", 502)
                    calls = message.get("tool_calls") or []
                    if calls:
                        if not active_tools:
                            raise AppError("INVALID_MODEL_OUTPUT", "Final format repair cannot request tools", 502)
                        if len(calls) > 4 or (any(c["function"]["name"] == "create_ticket" for c in calls) and len(calls) != 1):
                            raise AppError("INVALID_TOOL_BATCH", "Write proposal must be the only call", 502)
                        ids = {c["id"] for m in state["messages"] for c in m.get("tool_calls", [])}
                        for call in calls:
                            if not isinstance(call["id"], str) or not call["id"] or call["id"] in ids or call.get("type") != "function":
                                raise AppError("INVALID_CALL_ID", "Tool call ID missing or reused", 502)
                            ids.add(call["id"])
                            name = call["function"]["name"]
                            if name not in TOOLS:
                                raise AppError("UNKNOWN_TOOL", "Tool is not allowed", 422)
                            args = TOOLS[name][0].model_validate(json.loads(call["function"]["arguments"]))
                            normalized = json.loads(redact(dump(args.model_dump())))
                            signature = dump([name, normalized])
                            if signature in state["seen"]:
                                event(state, "repeat_call", name=name)
                                raise AppError("NO_PROGRESS", "Repeated tool request stopped", 422)
                            state["seen"].append(signature)
                            call["function"]["arguments"] = dump(normalized)
                            event(state, "tool_start", name=name, call_id=call["id"], arguments=normalized)
                        message["content"] = redact(message.get("content") or "")
                        state["messages"].append(message)
                        state["phase"] = "tools"
                        await save()
                    else:
                        final, reason = parse_final(message.get("content"))
                        if reason:
                            event(state, "model_validation_failed", stage="final_answer", reason=reason,
                                  model=turn["model"], step=state["steps"])
                            if state.get("final_repair_attempts", 0) >= 1:
                                raise AppError("INVALID_MODEL_OUTPUT", "Final answer format repair exhausted", 502)
                            state["final_repair_attempts"] = 1
                            state["messages"].append({"role": "user", "content": FINAL_REPAIR_PROMPT})
                            state["phase"] = "final_repair"
                            event(state, "format_repair", attempt=1, reason=reason)
                            await save()
                            continue
                        answer = redact(final["answer"])
                        state["messages"].append({"role": "assistant", "content": dump({**final, "answer": answer})})
                        state.update(status="completed", phase="done", outcome=final["status"], answer=answer)
                        # Text events are chunks of the validated answer, NOT provider token streaming.
                        for offset in range(0, len(answer), 120):
                            event(state, "text_delta", text=answer[offset:offset+120])
                        event(state, "final", status=state["status"], outcome=state["outcome"], answer=answer)
                        await save(True)
        except asyncio.CancelledError:
            # Graceful shutdown releases the lease; explicit cancellation already fenced DB state.
            try:
                await save(True)
            except AppError:
                pass
            raise
        except Exception as exc:
            code = ("TIME_LIMIT" if isinstance(exc, TimeoutError) else
                    "INVALID_TOOL_ARGUMENTS" if isinstance(exc, ValidationError) else
                    exc.code if isinstance(exc, AppError) else "INVALID_MODEL_OUTPUT")
            state.update(status="failed", phase="done", outcome=code)
            event(state, "error", code=code)
            try:
                await save(True)
            except AppError as error:
                if error.code != "LEASE_LOST":
                    raise

    async def read_tool(self, call, identity):
        name = call["function"]["name"]
        arguments = json.loads(call["function"]["arguments"])
        started = time.monotonic()
        attempts = 0
        result, status = None, "succeeded"
        try:
            for attempt in range(3):
                attempts += 1
                try:
                    async with asyncio.timeout(min(10, self.settings.max_seconds)):
                        if name == "calculator":
                            result = {"value": calculate(CalculatorArguments(left=Decimal(str(arguments["left"])),
                                right=Decimal(str(arguments["right"])), operator=arguments["operator"]))}
                        elif name == "query_business_data":
                            result = await asyncio.to_thread(self.repo.business, identity, arguments["query"], **({"ticket_id": arguments["ticket_id"]} if arguments.get("ticket_id") else {}))
                        elif name == "search_knowledge_base":
                            if self.retrieval is None:
                                raise AppError("RETRIEVAL_NOT_CONFIGURED", "Retrieval unavailable", 503)
                            found = await self.retrieval.retrieve(arguments["query"], identity, top_k=arguments.get("top_k", 3))
                            result = {"untrusted_evidence": found["hits"], "duration_ms": found["duration_ms"],
                                      "top_k": arguments.get("top_k", 3)}
                        else:
                            raise AppError("UNKNOWN_TOOL", "Read tool unavailable", 422)
                    break
                except (TimeoutError, Retryable) as exc:
                    if attempt == 2:
                        raise AppError("TOOL_TIMEOUT", "Tool retries exhausted", 504)
                    await asyncio.sleep(max(getattr(exc, "retry_after", 0), .1*2**attempt)+random.uniform(0,.05))
            encoded = redact(dump(result))
            if len(encoded.encode()) > 18000:
                raise AppError("TOOL_OUTPUT_LIMIT", "Tool result too large", 422)
            result = json.loads(encoded)
        except Exception as exc:
            status, result = "failed", {"error": exc.code if isinstance(exc, AppError) else "TOOL_FAILED"}
        return {"name": name, "call_id": call["id"], "arguments": arguments, "result": result,
                "status": status, "attempts": attempts, "duration_ms": (time.monotonic()-started)*1000}
