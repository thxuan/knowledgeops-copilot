"""Injectable generation adapter and explicitly limited extractive Fake."""
import json
import re
from dataclasses import dataclass
from typing import Protocol
import httpx
from app.core.errors import AppError
from app.generation.models import GeneratedClaims

@dataclass(frozen=True)
class GenerationSettings:
    provider: str = "fake"
    model_id: str = "fake-extractive-v1"
    input_limit: int = 16384
    output_limit: int = 2048
    history_budget: int = 2048
    summary_budget: int = 2048
    timeout_seconds: float = 30
    base_url: str = "https://api.example.invalid/v1"
    api_key: str | None = None
    temperature: float = 0.0
    top_p: float | None = None

    def validate(self):
        import math
        if self.provider not in {"fake", "compatible"}:
            raise ValueError("Unsupported generation provider")
        if not 1 <= self.output_limit < self.input_limit or min(self.history_budget, self.summary_budget) < 0:
            raise ValueError("Invalid generation context budget")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Invalid generation timeout")
        if not math.isfinite(self.temperature) or not 0 <= self.temperature < 2:
            raise ValueError("Invalid temperature")
        if self.top_p is not None and (not math.isfinite(self.top_p) or not 0 < self.top_p <= 1):
            raise ValueError("Invalid top_p")
        if not self.model_id or (self.provider == "compatible" and not self.api_key):
            raise ValueError("Compatible generation needs model ID and API key")
        if self.provider == "fake" and self.model_id != "fake-extractive-v1":
            raise ValueError("Fake generation model ID is fixed")

class GenerationClient(Protocol):
    async def generate(self, messages: list[dict], settings: GenerationSettings) -> str: ...

class GenerationOutput(str):
    """String-compatible result carrying provider usage for offline evaluation."""
    def __new__(cls, value, metadata):
        instance = super().__new__(cls, value)
        instance.metadata = metadata
        return instance

def terms(text):
    result = set(re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", text.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(run[i:i + 2] for i in range(len(run) - 1))
    return result - {"什么", "怎么", "如何", "可以", "需要", "这个", "那个", "多久", "然后", "问题"}

class FakeGenerationClient:
    async def generate(self, messages, settings):
        payload = json.loads(messages[-1]["content"])
        question = payload["question"]
        history = payload["history"] or payload["summary"]
        if any(word in question for word in ("它", "这个", "那个", "上述", "刚才", "多久", "然后")) and history:
            question = " ".join(h["text"] for h in history) + " " + question
        query_terms = terms(question)
        codes = re.findall(r"\b[a-z]+-\d+\b", question.lower())
        candidates = []
        for hit in payload["evidence"]:
            for line in hit["text"].splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if codes and codes[-1] not in hit["text"].lower():
                    continue
                overlap = len(query_terms & terms(line))
                if overlap >= 2:
                    candidates.append((overlap, line, hit["chunk_id"]))
        if not candidates:
            return GeneratedClaims(insufficient_context=True, claims=[]).model_dump_json()
        _, text, cid = max(candidates, key=lambda c: c[0])
        result = GeneratedClaims(insufficient_context=False,
                                 claims=[{"text": text, "citations": [cid]}]).model_dump_json()
        if len(result.encode("utf-8")) > settings.output_limit:
            return GeneratedClaims(insufficient_context=True, claims=[]).model_dump_json()
        return result

class CompatibleGenerationClient:
    def __init__(self, transport=None):
        self.transport = transport

    async def generate(self, messages, settings):
        payload = {"model": settings.model_id, "messages": messages,
                   "temperature": settings.temperature, "max_tokens": settings.output_limit,
                   "response_format": {"type": "json_object"}}
        if settings.top_p is not None:
            payload["top_p"] = settings.top_p
        try:
            async with httpx.AsyncClient(timeout=settings.timeout_seconds, transport=self.transport) as client:
                response = await client.post(settings.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + settings.api_key},
                    json=payload)
            response.raise_for_status()
            envelope = response.json()
            body = envelope["choices"][0]
            if body.get("finish_reason") != "stop" or not isinstance(body["message"]["content"], str):
                raise ValueError("Incomplete generation")
            usage = envelope.get("usage") or {}
            usage = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                     if type(usage.get(key)) is int and usage[key] >= 0}
            return GenerationOutput(body["message"]["content"],
                {"returned_model": envelope.get("model"), "usage": usage,
                 "sent_sampling": {"temperature": settings.temperature, "top_p": settings.top_p},
                 "effective_sampling": "not echoed by provider"})
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise AppError("GENERATION_FAILED", "Provider returned no complete answer", 502) from exc

def make_generation_client(settings):
    settings.validate()
    return FakeGenerationClient() if settings.provider == "fake" else CompatibleGenerationClient()
