"""Retrieve -> budget -> generate -> validate -> atomic final commit."""
import asyncio
import json
import logging
import time
from pydantic import ValidationError
from app.core.errors import AppError
from app.generation.context import build_context, retrieval_query, estimate_tokens, continuation_context
from app.generation.models import GeneratedAnswer, REFUSAL, VALIDATION_FAILURE, parse_generation, resolve_citations
from app.generation.review import review_messages
from app.generation.applicability import ApplicabilityValidationError, parse_assessed_generation, validate_clarification_scope

logger = logging.getLogger("knowledgeops.generation")

REPAIR_HINTS = {
    "answer_claims_mismatch": "answer 必须逐字等于所有 claims.text 用换行符连接的结果，标点和空格也必须一致。",
    "citation_union_mismatch": "顶层 citations 必须是所有 claims.citations 的去重合集，不能多引、漏引或重复。",
    "citation_outside_evidence": "所有引用必须逐字复制本轮 evidence 中的 chunk_id，不能引用历史或编造 ID。",
    "refusal_has_claims": "拒答时 insufficient_context=true、claims=[]、follow_up=null，applicability 必须说明 evidence_status=insufficient；缺用户条件则应追问。",
    "missing_cited_claims": "回答时 insufficient_context=false，有事实回答时必须有非空 claims 且每条结论都有引用；只有追问时允许 claims=[] 并提供 follow_up。",
    "blank_answer": "有答案时每条 claims.text 必须是非空文本；拒答时 insufficient_context=true、claims=[]。不要输出 answer 或顶层 citations。",
    "schema_invalid": "只输出 insufficient_context、claims、follow_up 和 applicability；字段类型严格匹配，不增加 answer、顶层 citations 或 Markdown。控制结论总长度与引用数量在限制内。",
    "clarification_scope": "澄清轮 claims 最多两条且合计不超过400字符，只保留通用或仅依赖已确认条件的必要简短说明，也可为空；不要枚举原因与专用操作。同步更新 claim_scopes。",
    "missing_applicability": "必须填写 applicability.evidence_status、conditions 和逐条对应 claims 的 claim_scopes，拒答也必须说明证据是否适用。",
    "applicability_mismatch": "检查条件名称唯一、引用条件存在，claim_scopes 与 claims 一一对应。general 的 conditions 为空；conditional 必须列出所需条件。missing 条件必须给出 question。",
    "unconfirmed_conditions": "从 claims 删除条件未确认或不匹配的结论，同步删除对应 claim_scopes；缺失条件保留为 missing 并填写 question，用于追问。不要把专用结论改标 general。",
    "unconfirmed_user_quote": "confirmed 的 user_quote 必须逐字摘录单条入模用户消息，不拼接、补写或同义改写原话；value 可规范化同义表达，不要求逐字出现在原话中，但不能增加或强化原话的含义。不能引用资料或助手文本确认条件。缺失的细节保留 missing 并追问。",
    "invalid_condition_value": "已确认条件的 value 必须是非空、忠实于 user_quote 的条件值，允许同义规范化，禁止补充原话未提供的细节。",
    "evidence_decision_mismatch": "区分缺用户条件与缺适用证据。有相关资料但缺系统等条件时，evidence_status=relevant，保留 missing 条件及 question，insufficient_context=false；只保留条件已确认的结论。只有已知条件下无适用资料时才 insufficient，且无 claims 或 missing 条件。追问必须对应 missing 条件。",
    "invalid_output": "只输出完整 JSON 对象，并控制在输出长度限制内。",
}


def validation_code(exc):
    # Map only fixed validator messages; never log raw outputs or arbitrary fields.
    known = {
        "Answer must consist of the validated claims": "answer_claims_mismatch",
        "Citation list must match claim citations": "citation_union_mismatch",
        "Refusal must not assert evidence-backed claims": "refusal_has_claims",
        "Every answer needs cited claims": "missing_cited_claims",
        "Blank answer": "blank_answer",
        "Citation outside provided context": "citation_outside_evidence",
        "Clarification exceeds scope": "clarification_scope",
        "Missing applicability assessment": "missing_applicability",
        "Claim applicability mismatch": "applicability_mismatch",
        "Unconfirmed claim conditions": "unconfirmed_conditions",
        "Condition not confirmed by user": "unconfirmed_user_quote",
        "Invalid confirmed condition value": "invalid_condition_value",
        "Evidence decision mismatch": "evidence_decision_mismatch",
    }
    if isinstance(exc, ValidationError):
        for error in exc.errors(include_input=False, include_url=False):
            if error["loc"] == ("answer",) and error["type"] == "string_too_short":
                return "blank_answer"
            code = known.get(str(error.get("ctx", {}).get("error", "")))
            if code:
                return code
        return "schema_invalid"
    return known.get(str(exc), "invalid_output")


def schema_issues(exc):
    if not isinstance(exc, ValidationError):
        return []
    # Allowlist paths/types; unknown extra field names can contain private text.
    fields = {"answer", "citations", "insufficient_context", "claims", "text", "follow_up", "applicability", "conditions", "claim_scopes", "scope", "name", "value", "status", "user_quote", "question", "evidence_status"}
    types = {"missing", "extra_forbidden", "json_invalid", "json_type", "string_type",
             "bool_type", "list_type", "model_type", "string_too_short", "string_too_long",
             "too_short", "too_long", "value_error"}
    result = []
    for error in exc.errors(include_input=False, include_context=False, include_url=False)[:3]:
        parts = [str(part) if part in fields else "[]" if isinstance(part, int) else "<extra>"
                 for part in error["loc"]]
        result.append({"field": ".".join(parts) or "<root>",
                       "type": error["type"] if error["type"] in types else "other"})
    return result


class ChatService:
    def __init__(self, conversations, vectors, client, settings):
        self.conversations, self.vectors = conversations, vectors
        self.client, self.settings = client, settings

    async def run(self, body, identity, *, include_trace=False, retrieval_options=None, effective_config=None, on_event=None, history_snapshot=None, commit_turn=True):
        started = time.perf_counter()
        async def notify(kind, data):
            if on_event:
                await on_event(kind, data)
        await notify("phase", {"stage": "retrieval"})
        if self.vectors is None:
            raise AppError("VECTOR_NOT_CONFIGURED", "Set KNOWLEDGEOPS_VECTOR_DATABASE_URL first", 503)
        history_started = time.perf_counter()
        snapshot = history_snapshot if history_snapshot is not None else await asyncio.to_thread(self.conversations.load, body.conversation_id, identity)
        history_ms = (time.perf_counter() - history_started) * 1000
        users = [m for m in snapshot["messages"] if m["role"] == "user"]
        pending = continuation_context(body.message, snapshot.get("pending_follow_up"))
        if pending and hasattr(self.conversations, "check_follow_up"):
            pending = await asyncio.to_thread(self.conversations.check_follow_up, pending, identity)
        query = retrieval_query(body.message, users, pending)
        kwargs = {"top_k": body.top_k, "filters": body.filters.model_dump()}
        if hasattr(self.vectors, "retrieve_coverage"):
            kwargs["retrieval_profile"] = body.retrieval_profile
        kwargs.update(retrieval_options or {})
        retrieval_started = time.perf_counter()
        result = await self.vectors.retrieve(query, identity, **kwargs)
        retrieval_ms = (time.perf_counter() - retrieval_started) * 1000
        context_started = time.perf_counter()
        context = build_context(body.message, snapshot["messages"], snapshot["summary"], result["hits"], self.settings,
                                preserve_order=result.get("diagnostics", {}).get("retrieval_profile") in {"coverage", "multi_query"},
                                pending_follow_up=pending)
        await asyncio.to_thread(self.conversations.check_evidence, context.evidence, identity)
        status, reason, attempts = "refused", "no_evidence_in_budget", 0
        answer = GeneratedAnswer(answer=REFUSAL, citations=[], insufficient_context=True, claims=[])
        ids = {hit["chunk_id"] for hit in context.evidence}
        if users and not context.history_ids and any(word in body.message for word in ("它", "这个", "那个", "上述", "刚才", "多久", "然后")):
            ids = set()
            reason = "reference_history_exceeds_budget"
        context_ms = (time.perf_counter() - context_started) * 1000
        generation_started = time.perf_counter()
        model_usage = []
        output_validation_ms = 0.0
        validation_failures = []
        review_status = "disabled" if not self.settings.coverage_review else "not_needed"
        can_ask = (reason == "no_evidence_in_budget" and self.settings.provider == "compatible"
                   and body.filters.document_ids != [])
        if ids or can_ask:
            phase, repaired = "draft", False
            # One draft + one coverage review, sharing a single format repair.
            while attempts < 3:
                attempts += 1
                try:
                    if pending and hasattr(self.conversations, "check_follow_up"):
                        if not await asyncio.to_thread(self.conversations.check_follow_up, pending, identity):
                            raise AppError("EVIDENCE_CHANGED", "原追问的依据已失效或无权访问，请重新提问。", 409)
                    call_started = time.perf_counter()
                    await notify("reset", {"phase": phase})
                    await notify("phase", {"stage": "review" if phase == "review" else "generating"})
                    if on_event and hasattr(self.client, "generate_stream"):
                        # Do not expose claim text before applicability and citation validation.
                        partial_bytes = 0

                        async def delta(text):
                            nonlocal partial_bytes
                            partial_bytes += len(text.encode("utf-8"))
                            if partial_bytes > self.settings.output_limit * 16:
                                raise ValueError("Invalid or excessive model output")

                        raw = await asyncio.wait_for(
                            self.client.generate_stream(context.messages, self.settings, delta),
                            self.settings.timeout_seconds)
                    else:
                        raw = await asyncio.wait_for(self.client.generate(context.messages, self.settings),
                                                     self.settings.timeout_seconds)
                    await notify("phase", {"stage": "validating"})
                    metadata = getattr(raw, "metadata", {})
                    usage = metadata.get("usage", {}) if isinstance(metadata, dict) else {}
                    model_usage.append({"phase": phase, "attempt": attempts,
                        "duration_ms": round((time.perf_counter() - call_started) * 1000, 3),
                        "usage": {k: v for k, v in usage.items()
                                  if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                                  and type(v) is int and v >= 0} if isinstance(usage, dict) else {}})
                    validation_started = time.perf_counter()
                    try:
                        if not isinstance(raw, str) or len(raw.encode("utf-8")) > self.settings.output_limit * 16:
                            raise ValueError("Invalid or excessive model output")
                        parsed = (parse_assessed_generation(raw, context.messages)
                                  if self.settings.provider == "compatible" else parse_generation(raw))
                        parsed = validate_clarification_scope(resolve_citations(parsed, context.citation_map))
                        if not set(parsed.citations) <= ids:
                            raise ValueError("Citation outside provided context")
                    finally:
                        output_validation_ms += (time.perf_counter() - validation_started) * 1000
                    answer = (GeneratedAnswer(answer=REFUSAL, citations=[], insufficient_context=True, claims=[])
                              if parsed.insufficient_context else parsed)
                    status = "refused" if parsed.insufficient_context else "needs_clarification" if parsed.follow_up else "answered"
                    reason = "insufficient_context" if parsed.insufficient_context else None
                    if parsed.follow_up:
                        review_status = "skipped_clarification"
                        break
                    if phase == "review":
                        review_status = "completed"
                        break
                    if (self.settings.provider == "compatible" and self.settings.coverage_review
                            and not parsed.insufficient_context):
                        review = review_messages(context.messages, parsed, context.citation_map)
                        if estimate_tokens(review) + self.settings.output_limit > self.settings.input_limit:
                            review_status = "skipped_budget"
                            break
                        # Do not resend revoked evidence to a second model call.
                        await asyncio.to_thread(self.conversations.check_evidence, context.evidence, identity)
                        context.messages = review
                        phase = "review"
                        continue
                    break
                except (ValidationError, ValueError, TypeError) as exc:
                    status, reason = "rejected", "invalid_generation"
                    answer = GeneratedAnswer(answer=VALIDATION_FAILURE, citations=[], insufficient_context=True, claims=[])
                    if phase == "review":
                        review_status = "failed"
                    code = validation_code(exc)
                    failure = {"phase": phase, "attempt": attempts, "code": code}
                    if isinstance(exc, ApplicabilityValidationError):
                        failure.update(field=exc.field, detail=exc.detail)
                    validation_failures.append(failure)
                    event = {"event": "generation.validation_failed",
                        "conversation_id": snapshot["conversation_id"], "attempt": attempts,
                        "phase": phase, "validation_code": code}
                    if isinstance(exc, ApplicabilityValidationError):
                        event["validation_details"] = {"field": exc.field, "detail": exc.detail}
                    if code == "schema_invalid":
                        event["schema_issues"] = schema_issues(exc)
                    logger.info(json.dumps(event, ensure_ascii=False))
                    if repaired:
                        break
                    repaired = True
                    # Fixed hints only; never replay invalid model text.
                    field_hint = ("需修正字段：" + exc.field + "。" if isinstance(exc, ApplicabilityValidationError) else "")
                    repair = {"role": "system", "content": context.messages[0]["content"] +
                              "\n上次输出未通过校验。" + field_hint + REPAIR_HINTS[code] +
                              "重新生成符合 JSON Schema 且只使用本轮引用的完整输出。"}
                    messages = [repair, context.messages[1]]
                    if estimate_tokens(messages) + self.settings.output_limit > self.settings.input_limit:
                        break
                    context.messages = messages
                except TimeoutError as exc:
                    raise AppError("GENERATION_TIMEOUT", "Generation timed out; no answer committed", 504) from exc
        # Whole-source expansion could reintroduce unassessed branches. Render only
        # the claims that passed this request's applicability and scope checks.
        generation_ms = (time.perf_counter() - generation_started) * 1000
        by_id = {hit["chunk_id"]: hit["citation"] for hit in context.evidence}
        response = {"conversation_id": snapshot["conversation_id"], "answer": answer.answer,
            "citations": [by_id[cid] for cid in answer.citations],
            "claims": [c.model_dump() for c in answer.claims], "follow_up": answer.follow_up,
            "insufficient_context": answer.insufficient_context, "status": status, "reason": reason,
            "model_provider": self.settings.provider, "model_id": self.settings.model_id,
            "context": {"input_token_estimate": estimate_tokens(context.messages),
                "estimate_method": "utf8_bytes_plus_framing_v1", "output_token_reserve": self.settings.output_limit,
                "input_limit": self.settings.input_limit, "history_message_ids": context.history_ids,
                "summary_message_ids": context.summary_ids,
                "omitted_user_messages": context.omitted_history, "evidence_count": len(context.evidence),
                "retrieval_hit_count": len(result["hits"]), "generation_attempts": attempts,
                "retrieval_query": query,
                "follow_up_origin": (pending["original_question"] if pending else body.message) if answer.follow_up else None,
                "timings_ms": {"retrieval": round(retrieval_ms, 3),
                    "context_and_validation": round(context_ms, 3), "generation": round(generation_ms, 3)},
                "model_usage": model_usage,
                "retrieval_profile": body.retrieval_profile if hasattr(self.vectors, "retrieve_coverage") else "ranked",
                "retrieval_diagnostics": {k: v for k, v in result.get("diagnostics", {}).items()
                    if k in {"queries", "planning", "candidate_count", "query_runs", "selector_version", "planner_version"}},
                "evidence_chunk_ids": [h["chunk_id"] for h in context.evidence],
                "omitted_evidence_ids": context.omitted_evidence_ids,
                "coverage_review": review_status,
                "validation_failures": validation_failures, "answer_mode": "generated"}}
        if effective_config is not None:
            response["context"]["effective_config"] = effective_config
        await notify("phase", {"stage": "saving"})
        commit_started = time.perf_counter()
        if commit_turn:
            await asyncio.to_thread(self.conversations.commit, snapshot, identity, body.message, response,
                                    context.evidence, self.settings.summary_budget,
                                    **({"pending_follow_up": pending} if pending else {}))
        # Final response timings are measured after persistence; phase timings and
        # usage above are persisted, commit/total timings describe this live response.
        response["context"]["timings_ms"].update(commit=round((time.perf_counter() - commit_started) * 1000, 3),
                                               total=round((time.perf_counter() - started) * 1000, 3))
        if include_trace:
            response["context"]["timings_ms"].update(history=round(history_ms, 3),
                model_calls=round(sum(c["duration_ms"] for c in model_usage), 3),
                output_validation=round(output_validation_ms, 3))
            # Trace text is response-only, never persisted in conversation history.
            # Recheck every candidate after model calls, not just selected evidence.
            diagnostics = result.get("diagnostics", {})
            stages = diagnostics.get("stages", {"final": result["hits"]})
            stages = {**stages, "evidence": context.evidence}
            all_hits = [h for stage in stages.values() for h in stage]
            allowed = {h["chunk_id"] for h in await self.vectors.reauthorize(all_hits, identity, body.filters.model_dump())}
            stages = {name: [h for h in stage if h["chunk_id"] in allowed] for name, stage in stages.items()}
            response["retrieval_trace"] = {"stages": stages,
                "timings_ms": diagnostics.get("timings_ms", {}),
                "rerank": diagnostics.get("rerank", {}),
                "queries": diagnostics.get("queries", [query]),
                "planning": diagnostics.get("planning", {}),
                "score_type": result.get("score_type"),
                "scope": "multi-query stage lists concatenate per-query results; final evidence is deduplicated"}
            response["context"]["timings_ms"]["total"] = round((time.perf_counter() - started) * 1000, 3)
        return response
