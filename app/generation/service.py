"""Retrieve -> budget -> generate -> validate -> atomic final commit."""
import asyncio
import json
import logging
import time
from pydantic import ValidationError
from app.core.errors import AppError
from app.generation.context import build_context, retrieval_query, estimate_tokens
from app.generation.models import GeneratedAnswer, REFUSAL, parse_generation, resolve_citations, grounded_answer
from app.generation.review import review_messages

logger = logging.getLogger("knowledgeops.generation")

REPAIR_HINTS = {
    "answer_claims_mismatch": "answer 必须逐字等于所有 claims.text 用换行符连接的结果，标点和空格也必须一致。",
    "citation_union_mismatch": "顶层 citations 必须是所有 claims.citations 的去重合集，不能多引、漏引或重复。",
    "citation_outside_evidence": "所有引用必须逐字复制本轮 evidence 中的 chunk_id，不能引用历史或编造 ID。",
    "refusal_has_claims": "拒答时 insufficient_context=true，claims=[]；只输出 insufficient_context 和 claims。",
    "missing_cited_claims": "回答时 insufficient_context=false，必须有非空 claims，且每条结论都有引用。",
    "blank_answer": "有答案时每条 claims.text 必须是非空文本；拒答时 insufficient_context=true、claims=[]。不要输出 answer 或顶层 citations。",
    "schema_invalid": "只输出 insufficient_context 和 claims；字段类型严格匹配，不增加 answer、顶层 citations 或 Markdown。控制结论总长度与引用数量在限制内。",
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
    fields = {"answer", "citations", "insufficient_context", "claims", "text"}
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

    async def run(self, body, identity, *, include_trace=False, retrieval_options=None, effective_config=None):
        started = time.perf_counter()
        if self.vectors is None:
            raise AppError("VECTOR_NOT_CONFIGURED", "Set KNOWLEDGEOPS_VECTOR_DATABASE_URL first", 503)
        history_started = time.perf_counter()
        snapshot = await asyncio.to_thread(self.conversations.load, body.conversation_id, identity)
        history_ms = (time.perf_counter() - history_started) * 1000
        users = [m for m in snapshot["messages"] if m["role"] == "user"]
        query = retrieval_query(body.message, users)
        kwargs = {"top_k": body.top_k, "filters": body.filters.model_dump()}
        if hasattr(self.vectors, "retrieve_coverage"):
            kwargs["retrieval_profile"] = body.retrieval_profile
        kwargs.update(retrieval_options or {})
        retrieval_started = time.perf_counter()
        result = await self.vectors.retrieve(query, identity, **kwargs)
        retrieval_ms = (time.perf_counter() - retrieval_started) * 1000
        context_started = time.perf_counter()
        context = build_context(body.message, snapshot["messages"], snapshot["summary"], result["hits"], self.settings,
                                preserve_order=result.get("diagnostics", {}).get("retrieval_profile") in {"coverage", "multi_query"})
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
        if ids:
            phase, repaired = "draft", False
            # One draft + one coverage review, sharing a single format repair.
            while attempts < 3:
                attempts += 1
                try:
                    call_started = time.perf_counter()
                    raw = await asyncio.wait_for(self.client.generate(context.messages, self.settings),
                                                 self.settings.timeout_seconds)
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
                        parsed = resolve_citations(parse_generation(raw), context.citation_map)
                        if not set(parsed.citations) <= ids:
                            raise ValueError("Citation outside provided context")
                    finally:
                        output_validation_ms += (time.perf_counter() - validation_started) * 1000
                    answer = (GeneratedAnswer(answer=REFUSAL, citations=[], insufficient_context=True, claims=[])
                              if parsed.insufficient_context else parsed)
                    status = "refused" if parsed.insufficient_context else "answered"
                    reason = "insufficient_context" if parsed.insufficient_context else None
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
                    answer = GeneratedAnswer(answer=REFUSAL, citations=[], insufficient_context=True, claims=[])
                    if phase == "review":
                        review_status = "failed"
                    code = validation_code(exc)
                    validation_failures.append({"phase": phase, "attempt": attempts, "code": code})
                    event = {"event": "generation.validation_failed",
                        "conversation_id": snapshot["conversation_id"], "attempt": attempts,
                        "phase": phase, "validation_code": code}
                    if code == "schema_invalid":
                        event["schema_issues"] = schema_issues(exc)
                    logger.info(json.dumps(event, ensure_ascii=False))
                    if repaired:
                        break
                    repaired = True
                    # Fixed hints only; never replay invalid model text.
                    repair = {"role": "system", "content": context.messages[0]["content"] +
                              "\n上次输出未通过校验。" + REPAIR_HINTS[code] +
                              "重新生成符合 JSON Schema 且只使用本轮引用的完整输出。"}
                    messages = [repair, context.messages[1]]
                    if estimate_tokens(messages) + self.settings.output_limit > self.settings.input_limit:
                        break
                    context.messages = messages
                except TimeoutError as exc:
                    raise AppError("GENERATION_TIMEOUT", "Generation timed out; no answer committed", 504) from exc
        answer_mode = "generated"
        if self.settings.provider == "compatible" and self.settings.answer_mode == "grounded" and status == "answered":
            try:
                # Use whole selected source passages, never model-authored factual edits.
                answer = grounded_answer(answer, context.evidence, body.message)
                answer_mode = "grounded"
            except (ValidationError, ValueError):
                status, reason = "rejected", "evidence_output_limit"
                answer = GeneratedAnswer(answer=REFUSAL, citations=[], insufficient_context=True, claims=[])
        generation_ms = (time.perf_counter() - generation_started) * 1000
        by_id = {hit["chunk_id"]: hit["citation"] for hit in context.evidence}
        response = {"conversation_id": snapshot["conversation_id"], "answer": answer.answer,
            "citations": [by_id[cid] for cid in answer.citations],
            "claims": [c.model_dump() for c in answer.claims],
            "insufficient_context": answer.insufficient_context, "status": status, "reason": reason,
            "model_provider": self.settings.provider, "model_id": self.settings.model_id,
            "context": {"input_token_estimate": estimate_tokens(context.messages),
                "estimate_method": "utf8_bytes_plus_framing_v1", "output_token_reserve": self.settings.output_limit,
                "input_limit": self.settings.input_limit, "history_message_ids": context.history_ids,
                "summary_message_ids": context.summary_ids,
                "omitted_user_messages": context.omitted_history, "evidence_count": len(context.evidence),
                "retrieval_hit_count": len(result["hits"]), "generation_attempts": attempts,
                "retrieval_query": query,
                "timings_ms": {"retrieval": round(retrieval_ms, 3),
                    "context_and_validation": round(context_ms, 3), "generation": round(generation_ms, 3)},
                "model_usage": model_usage,
                "retrieval_profile": body.retrieval_profile if hasattr(self.vectors, "retrieve_coverage") else "ranked",
                "retrieval_diagnostics": {k: v for k, v in result.get("diagnostics", {}).items()
                    if k in {"queries", "planning", "candidate_count", "query_runs", "selector_version", "planner_version"}},
                "evidence_chunk_ids": [h["chunk_id"] for h in context.evidence],
                "omitted_evidence_ids": context.omitted_evidence_ids,
                "coverage_review": review_status,
                "validation_failures": validation_failures, "answer_mode": answer_mode}}
        if effective_config is not None:
            response["context"]["effective_config"] = effective_config
        commit_started = time.perf_counter()
        await asyncio.to_thread(self.conversations.commit, snapshot, identity, body.message, response,
                                context.evidence, self.settings.summary_budget)
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
