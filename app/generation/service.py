"""Retrieve -> budget -> generate -> validate -> atomic final commit."""
import asyncio
import json
import logging
from pydantic import ValidationError
from app.core.errors import AppError
from app.generation.context import build_context, retrieval_query, estimate_tokens
from app.generation.models import GeneratedAnswer, REFUSAL, parse_generation

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

    async def run(self, body, identity):
        if self.vectors is None:
            raise AppError("VECTOR_NOT_CONFIGURED", "Set KNOWLEDGEOPS_VECTOR_DATABASE_URL first", 503)
        snapshot = await asyncio.to_thread(self.conversations.load, body.conversation_id, identity)
        users = [m for m in snapshot["messages"] if m["role"] == "user"]
        query = retrieval_query(body.message, users)
        result = await self.vectors.retrieve(query, identity, top_k=body.top_k, filters=body.filters.model_dump())
        context = build_context(body.message, snapshot["messages"], snapshot["summary"], result["hits"], self.settings)
        await asyncio.to_thread(self.conversations.check_evidence, context.evidence, identity)
        status, reason, attempts = "refused", "no_evidence_in_budget", 0
        answer = GeneratedAnswer(answer=REFUSAL, citations=[], insufficient_context=True, claims=[])
        ids = {hit["chunk_id"] for hit in context.evidence}
        if users and not context.history_ids and any(word in body.message for word in ("它", "这个", "那个", "上述", "刚才", "多久", "然后")):
            ids = set()
            reason = "reference_history_exceeds_budget"
        if ids:
            for attempt in range(2):
                attempts += 1
                try:
                    raw = await asyncio.wait_for(self.client.generate(context.messages, self.settings),
                                                 self.settings.timeout_seconds)
                    if not isinstance(raw, str) or len(raw.encode("utf-8")) > self.settings.output_limit * 16:
                        raise ValueError("Invalid or excessive model output")
                    parsed = parse_generation(raw)
                    if not set(parsed.citations) <= ids:
                        raise ValueError("Citation outside provided context")
                    # Model-authored refusal text is never trusted as a vehicle for unsupported facts.
                    answer = answer if parsed.insufficient_context else parsed
                    status = "refused" if parsed.insufficient_context else "answered"
                    reason = "insufficient_context" if parsed.insufficient_context else None
                    break
                except (ValidationError, ValueError, TypeError) as exc:
                    status, reason = "rejected", "invalid_generation"
                    code = validation_code(exc)
                    event = {"event": "generation.validation_failed",
                        "conversation_id": snapshot["conversation_id"], "attempt": attempts,
                        "validation_code": code}
                    if code == "schema_invalid":
                        event["schema_issues"] = schema_issues(exc)
                    logger.info(json.dumps(event, ensure_ascii=False))
                    if attempt == 0:
                        # Same authorized evidence, bounded repair hint; no invalid raw text is replayed.
                        repair = {"role": "system", "content": context.messages[0]["content"] +
                                  "\n上次输出未通过校验。" + REPAIR_HINTS[code] +
                                  "重新生成符合 JSON Schema 且只使用本轮引用的完整输出。"}
                        repaired = [repair, context.messages[1]]
                        if estimate_tokens(repaired) + self.settings.output_limit > self.settings.input_limit:
                            break
                        context.messages = repaired
                except TimeoutError as exc:
                    raise AppError("GENERATION_TIMEOUT", "Generation timed out; no answer committed", 504) from exc
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
                "retrieval_query": query}}
        await asyncio.to_thread(self.conversations.commit, snapshot, identity, body.message, response,
                                context.evidence, self.settings.summary_budget)
        return response
