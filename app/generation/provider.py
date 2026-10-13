"""Real generation adapter with an explicit unconfigured state."""
import json
import re
from dataclasses import dataclass
from typing import Protocol
import httpx
from app.core.errors import AppError

@dataclass(frozen=True)
class GenerationSettings:
    provider: str = "unconfigured"
    model_id: str = "qwen-plus"
    input_limit: int = 32000
    output_limit: int = 2048
    history_budget: int = 2048
    summary_budget: int = 2048
    timeout_seconds: float = 60
    base_url: str = "https://api.example.invalid/v1"
    api_key: str | None = None
    temperature: float = 0.0
    top_p: float | None = None
    coverage_review: bool = True
    answer_mode: str = "generated"

    def validate(self):
        import math
        if self.answer_mode not in {"grounded", "generated"}:
            raise ValueError("answer_mode must be grounded or generated")
        if type(self.coverage_review) is not bool:
            raise ValueError("coverage_review must be boolean")
        if self.provider not in {"unconfigured", "compatible"}:
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

class CompatibleGenerationClient:
    def __init__(self, transport=None):
        self.transport = transport

    async def generate(self, messages, settings):
        payload = {"model": settings.model_id, "messages": messages,
                   "temperature": settings.temperature, "max_tokens": settings.output_limit,
                   "response_format": {"type": "json_object"}}
        if settings.top_p is not None:
            payload["top_p"] = settings.top_p
        finish_reason = None
        try:
            async with httpx.AsyncClient(timeout=settings.timeout_seconds, transport=self.transport) as client:
                response = await client.post(settings.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + settings.api_key},
                    json=payload)
            response.raise_for_status()
            envelope = response.json()
            body = envelope["choices"][0]
            finish_reason = body.get("finish_reason")
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
            # Fixed diagnostic fields only; never expose provider bodies or headers.
            detail = ""
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f" (HTTP {exc.response.status_code})"
            elif finish_reason in {"length", "content_filter", "tool_calls"}:
                detail = f" (finish_reason={finish_reason})"
            elif isinstance(exc, httpx.TimeoutException):
                detail = " (timeout)"
            raise AppError("GENERATION_FAILED", "Provider returned no complete answer" + detail, 502) from exc

def make_generation_client(settings):
    settings.validate()
    return UnconfiguredGenerationClient() if settings.provider == "unconfigured" else CompatibleGenerationClient()

class UnconfiguredGenerationClient:
    async def generate(self, messages, settings):
        raise AppError('MODEL_NOT_CONFIGURED', '请先配置真实生成模型。', 422)
