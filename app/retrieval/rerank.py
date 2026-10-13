"""Qwen-compatible /reranks adapter."""
from dataclasses import dataclass
import math
from typing import Protocol
import httpx


@dataclass(frozen=True)
class RerankSettings:
    provider: str = "none"
    model_id: str = "qwen3-rerank"
    endpoint: str = "https://api.example.invalid/v1/reranks"
    api_key: str | None = None
    timeout_seconds: float = 10
    cost_per_million_tokens: float | None = None

    def validate(self):
        if self.provider not in {"none", "compatible"}:
            raise ValueError("Unsupported rerank provider")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Invalid rerank timeout")
        if self.cost_per_million_tokens is not None and (
            not math.isfinite(self.cost_per_million_tokens) or self.cost_per_million_tokens < 0
        ):
            raise ValueError("Invalid rerank price")
        if self.provider == "compatible" and (not self.api_key or not self.model_id or
                not self.endpoint.startswith("https://") or "example.invalid" in self.endpoint):
            raise ValueError("Rerank needs a key, model and complete HTTPS endpoint")

    @property
    def public_config(self):
        return {"provider": self.provider, "model_id": self.model_id if self.provider == "compatible"
                else None,
                "endpoint": self.endpoint if self.provider == "compatible" else "local",
                "timeout_seconds": self.timeout_seconds,
                "cost_per_million_tokens": self.cost_per_million_tokens}


@dataclass(frozen=True)
class RerankResult:
    results: list[tuple[int, float]]
    total_tokens: int | None = None
    model_id: str | None = None
    request_id: str | None = None


class RerankClient(Protocol):
    async def rerank(self, query: str, documents: list[str]) -> RerankResult: ...


class RerankError(Exception):
    """No provider body or credentials are included in public errors."""


def validate_result(result, count, bounded_scores=False):
    if not isinstance(result, RerankResult) or len(result.results) != count:
        raise RerankError("Invalid rerank result count")
    indices = []
    for index, score in result.results:
        if type(index) is not int or not 0 <= index < count:
            raise RerankError("Invalid rerank index")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise RerankError("Invalid rerank score")
        if bounded_scores and not 0 <= score <= 1:
            raise RerankError("Rerank score outside documented range")
        indices.append(index)
    if len(set(indices)) != count:
        raise RerankError("Duplicate rerank index")
    if result.total_tokens is not None and (type(result.total_tokens) is not int or result.total_tokens < 0):
        raise RerankError("Invalid rerank usage")


class CompatibleRerankClient:
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport

    async def rerank(self, query, documents):
        try:
            async with httpx.AsyncClient(timeout=self.settings.timeout_seconds, transport=self.transport) as client:
                response = await client.post(self.settings.endpoint,
                    headers={"Authorization": "Bearer " + self.settings.api_key},
                    json={"model": self.settings.model_id, "query": query,
                          "documents": documents, "top_n": len(documents)})
            if response.status_code != 200:
                raise RerankError("Rerank provider rejected request")
            body = response.json()
            result = RerankResult([(r["index"], r["relevance_score"]) for r in body["results"]],
                body.get("usage", {}).get("total_tokens"), body.get("model"), body.get("id"))
            validate_result(result, len(documents), bounded_scores=True)
            if result.model_id is not None and result.model_id != self.settings.model_id:
                raise RerankError("Rerank model mismatch")
            return result
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise RerankError("Rerank transport or response failure") from exc


def make_rerank_client(settings):
    settings.validate()
    if settings.provider == "none":
        return None
    return CompatibleRerankClient(settings)
