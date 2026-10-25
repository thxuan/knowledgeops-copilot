"""Business policy middleware for LangChain create_agent's standard agent loop."""
import asyncio
import hashlib
import json
import random
import time
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelResponse, hook_config
from langchain_core.messages import ToolMessage, convert_to_messages, convert_to_openai_messages
from langgraph.types import interrupt
from app.agent.contracts import SPECS, TOOLS, FINAL_REPAIR_PROMPT, parse_final, dump, event, redact, suspicious
from app.agent.provider import Retryable
from app.core.errors import AppError


class AgentPolicy(AgentMiddleware):
    def __init__(self, service, run_id, identity, token, started, prior):
        self.service, self.repo = service, service.repo
        self.settings, self.retrieval = service.settings, service.retrieval
        self.run_id, self.identity, self.token = run_id, identity, token
        self.started, self.prior = started, prior
        self.resume_interrupt = False
        self.state = None
        self.lock = asyncio.Lock()

    async def load(self):
        self.state = await asyncio.to_thread(self.repo.get, self.run_id, self.identity)
        return self.state

    async def save(self, state):
        state["elapsed_seconds"] = self.prior + time.monotonic() - self.started
        state["executor"] = "create-agent-v1"
        await asyncio.to_thread(self.repo.checkpoint, state, self.identity, self.token)

    @staticmethod
    def reply(message, digest):
        reply = convert_to_messages([message])[0]
        reply.id = "ko-model-" + digest
        return ModelResponse(result=[reply])

    async def record_model(self, state, digest, message):
        # A durable business receipt prevents repeating a successful model call
        # if the process exits before create_agent saves the model-node output.
        state["_agent_model_receipt"] = {"input_digest": digest, "message": message}
        state.pop("_graph_receipt", None)
        await self.save(state)
        return self.reply(message, digest)

    @hook_config(can_jump_to=["tools"])
    async def abefore_agent(self, state, runtime):
        domain = await self.load()
        if domain["phase"] == "tools":
            # Adopt legacy pending calls through the framework's tools node.
            self.validate_retrieval_batch(domain)
            return {"jump_to": "tools"}

    @hook_config(can_jump_to=["model", "end"])
    async def aafter_model(self, state, runtime):
        domain = await self.load()
        if domain["phase"] == "tools":
            self.validate_retrieval_batch(domain)
        if domain["phase"] == "final_repair":
            return {"jump_to": "model"}
        if domain["status"] == "completed":
            return {"jump_to": "end"}

    async def awrap_model_call(self, request, handler):
        state = await self.load()
        digest = hashlib.sha256(dump(convert_to_openai_messages(request.messages)).encode()).hexdigest()
        cached = state.get("_agent_model_receipt", {})
        if cached.get("input_digest") == digest:
            return self.reply(cached["message"], digest)
        if state["status"] != "running":
            raise AppError("INVALID_STATE", "Run is not executable", 409)
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
            await self.save(state)
            try:
                response = await handler(request.override(
                    messages=convert_to_messages(state["messages"]),
                    tools=request.tools if active_tools else []))
                turn = response.result[-1].additional_kwargs["_ko_turn"]
                break
            except Retryable as exc:
                event(state, "retry", component="model", attempt=attempt+1)
                if attempt == 2:
                    raise AppError("MODEL_UNAVAILABLE", "Model retries exhausted", 502)
                await self.save(state)
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
            return await self.record_model(state, digest, message)
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
                return await self.record_model(state, digest, {"role": "assistant", "content": ""})
            answer = redact(final["answer"])
            state["messages"].append({"role": "assistant", "content": dump({**final, "answer": answer})})
            state.update(status="completed", phase="done", outcome=final["status"], answer=answer)
            # Text events are chunks of the validated answer, NOT provider token streaming.
            for offset in range(0, len(answer), 120):
                event(state, "text_delta", text=answer[offset:offset+120])
            event(state, "final", status=state["status"], outcome=state["outcome"], answer=answer)
            return await self.record_model(state, digest, state["messages"][-1])

    def validate_retrieval_batch(self, state):
        # Tool nodes run concurrently. Check the whole outstanding batch before
        # dispatch so an unpriced/over-budget search cannot race another tool.
        # This copy only checks affordability; each tool reserves durably under
        # the lock before I/O. Receipted calls are already charged on replay.
        budget = dict(state)
        recorded = {entry["call_id"] for entry in state["trace"]}
        calls = next((message.get("tool_calls", []) for message in reversed(state["messages"])
                      if message["role"] == "assistant"), [])
        for call in calls:
            if call["id"] in recorded or call["function"]["name"] != "search_knowledge_base":
                continue
            arguments = TOOLS["search_knowledge_base"][0].model_validate(
                json.loads(call["function"]["arguments"])).model_dump()
            self.reserve_retrieval(budget, arguments)

    def reserve_retrieval(self, state, arguments):
        if not hasattr(self.retrieval, "vectors"):
            return
        embedding = self.retrieval.vectors.settings
        if embedding.provider == "compatible":
            if embedding.cost_per_million_tokens is None:
                raise AppError("UNPRICED_TOOL", "请填写 Embedding 单价并应用后，重新发起任务。", 422)
            tokens = (len(arguments["query"].encode()) + 256) * embedding.max_attempts * 3
            cost = tokens * embedding.cost_per_million_tokens / 1e6
            if state["reserved_cost"] + cost > self.settings.max_cost:
                raise AppError("COST_LIMIT", "Retrieval cost reservation exceeds budget", 422)
            if state["reserved_tokens"] + tokens > self.settings.max_tokens:
                raise AppError("TOKEN_LIMIT", "Retrieval token reservation exceeds budget", 422)
            state["reserved_tokens"] += tokens
            state["reserved_cost"] += cost
        if self.retrieval.settings.strategy == "hybrid_rerank" and self.retrieval.settings.rerank.provider == "compatible":
            raise AppError("UNBOUNDED_RERANK_COST", "Use frozen hybrid profile for budgeted Agent retrieval", 422)

    async def ticket_call(self, request, handler):
        state = await self.load()
        call_id = request.tool_call["id"]
        committed = next((t for t in state["trace"] if t["name"] == "create_ticket"
                          and t["call_id"] == call_id and t["status"] == "committed"), None)
        if not committed and state["status"] == "running":
            await asyncio.to_thread(self.repo.propose, state, self.identity, self.token,
                                    call_id, request.tool_call["args"], release=False)
        if self.resume_interrupt or not committed:
            interrupt({"run_id": self.run_id, "kind": "ticket_submission"})
            self.resume_interrupt = False
        state = await self.load()
        committed = next((t for t in state["trace"] if t["name"] == "create_ticket"
                          and t["call_id"] == call_id and t["status"] == "committed"), None)
        if state["status"] != "running" or not committed:
            raise AppError("INVALID_STATE", "Resume requires a committed server approval", 409)
        approved = {**request.tool_call, "args": committed["arguments"]}
        return await handler(request.override(tool_call=approved))

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        name, call_id = call["name"], call["id"]
        if name not in TOOLS:
            raise AppError("UNKNOWN_TOOL", "Tool is not allowed", 422)
        arguments = TOOLS[name][0].model_validate(call["args"]).model_dump()
        request = request.override(tool_call={**call, "args": arguments})
        if name == "create_ticket":
            return await self.ticket_call(request, handler)
        async with self.lock:
            state = await self.load()
            cached = next((t for t in state["trace"] if t["call_id"] == call_id), None)
            if cached:
                if cached["status"] != "succeeded":
                    raise AppError(cached["result"]["error"], "Previous tool failed", 502)
                return ToolMessage(content=dump(cached["result"]), tool_call_id=call_id, name=name)
            if name == "search_knowledge_base":
                self.reserve_retrieval(state, arguments)
            await self.save(state)
        started, attempts = time.monotonic(), 0
        result, status = None, "succeeded"
        try:
            for attempt in range(3):
                attempts += 1
                try:
                    async with asyncio.timeout(min(10, self.settings.max_seconds)):
                        output = await handler(request)
                        if not isinstance(output, ToolMessage) or output.status == "error":
                            raise AppError("TOOL_FAILED", "Tool did not return a successful result", 502)
                        result = json.loads(output.content)
                    break
                except (TimeoutError, Retryable) as exc:
                    if attempt == 2:
                        raise AppError("TOOL_TIMEOUT", "Tool retries exhausted", 504)
                    await asyncio.sleep(max(getattr(exc, "retry_after", 0), .1 * 2**attempt) + random.uniform(0, .05))
            encoded = redact(dump(result))
            if len(encoded.encode()) > 18000:
                raise AppError("TOOL_OUTPUT_LIMIT", "Tool result too large", 422)
            result = json.loads(encoded)
        except Exception as exc:
            status, result = "failed", {"error": exc.code if isinstance(exc, AppError) else "TOOL_FAILED"}
        async with self.lock:
            state = await self.load()
            trace = {"name": name, "call_id": call_id, "arguments": arguments, "result": result,
                     "status": status, "attempts": attempts, "duration_ms": (time.monotonic() - started) * 1000}
            state["trace"].append(trace)
            state["messages"].append({"role": "tool", "tool_call_id": call_id, "content": dump(result)})
            state["phase"] = "model"
            event(state, "tool_end", **trace)
            if suspicious(dump(result)):
                event(state, "security_warning", code="UNTRUSTED_INSTRUCTION", call_id=call_id)
            await self.save(state)
        if status != "succeeded":
            raise AppError(result["error"], "Tool failed; no unsupported success answer", 502)
        return ToolMessage(content=dump(result), tool_call_id=call_id, name=name)
