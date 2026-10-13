"""Real embedding adapter with an explicit unconfigured state."""
from dataclasses import dataclass
import math
from typing import Protocol
import httpx

class EmbeddingError(Exception):
    code = "EMBEDDING_INVALID_RESPONSE"

class EmbeddingTransientError(EmbeddingError):
    code = "EMBEDDING_TEMPORARY_FAILURE"

@dataclass(frozen=True)
class EmbeddingSettings:
    provider: str = "unconfigured"
    model_id: str = "text-embedding-v4"
    dimensions: int = 128
    collection: str = "knowledgeops-v1"
    base_url: str = "https://api.example.invalid/v1"
    api_key: str | None = None
    batch_size: int = 16
    concurrency: int = 2
    timeout_seconds: float = 30
    max_attempts: int = 3
    cost_per_million_tokens: float | None = None

    def validate(self):
        if self.provider not in {"unconfigured", "compatible"}:
            raise ValueError("Unsupported embedding provider")
        if not self.model_id or not self.collection or not 1 <= self.dimensions <= 2000:
            raise ValueError("Invalid embedding model/collection/HNSW dimension")
        if not 1 <= self.batch_size <= 128 or not 1 <= self.concurrency <= 8:
            raise ValueError("Invalid batch size/concurrency")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0 or not 1 <= self.max_attempts <= 5:
            raise ValueError("Invalid timeout/retry limit")
        if self.cost_per_million_tokens is not None and (not math.isfinite(self.cost_per_million_tokens) or self.cost_per_million_tokens < 0):
            raise ValueError("Invalid embedding price")
        if self.provider == "compatible" and not self.api_key:
            raise ValueError("Compatible embedding adapter needs an API key")

    @property
    def signature(self):
        endpoint = self.base_url.rstrip("/") if self.provider == "compatible" else "local"
        return {"provider": self.provider, "model_id": self.model_id, "dimensions": self.dimensions,
                "endpoint": endpoint, "distance": "cosine", "input_format": "title-heading-text-v1"}

@dataclass
class EmbeddingResult:
    model_id: str
    vectors: list[list[float]]
    total_tokens: int | None = None

class EmbeddingClient(Protocol):
    async def embed(self, texts: list[str]) -> EmbeddingResult: ...

def validate_result(result, settings, count):
    if result.model_id != settings.model_id or len(result.vectors) != count:
        raise EmbeddingError("Model or vector count mismatch")
    for vector in result.vectors:
        if len(vector) != settings.dimensions:
            raise EmbeddingError("Vector dimension mismatch")
        if any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) for x in vector):
            raise EmbeddingError("Invalid vector values")
        if math.hypot(*vector) == 0:
            raise EmbeddingError("Cosine distance needs nonzero vectors")
    if result.total_tokens is not None and (type(result.total_tokens) is not int or result.total_tokens < 0):
        raise EmbeddingError("Invalid token usage")

class CompatibleEmbeddingClient:
    def __init__(self, settings):
        self.settings = settings

    async def embed(self, texts):
        try:
            async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
                response = await client.post(self.settings.base_url.rstrip("/") + "/embeddings",
                    headers={"Authorization": "Bearer " + self.settings.api_key},
                    json={"model": self.settings.model_id, "input": texts, "encoding_format": "float"})
            if response.status_code == 429 or response.status_code >= 500:
                raise EmbeddingTransientError("Provider unavailable")
            if response.status_code != 200:
                raise EmbeddingError("Provider rejected request")
            body = response.json()
            data = sorted(body["data"], key=lambda row: row["index"])
            if [row["index"] for row in data] != list(range(len(texts))):
                raise EmbeddingError("Invalid embedding response indices")
            return EmbeddingResult(body["model"], [row["embedding"] for row in data],
                                   body.get("usage", {}).get("total_tokens"))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise EmbeddingTransientError("Provider transport failure") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError("Malformed embedding response") from exc

def make_embedding_client(settings):
    settings.validate()
    return UnconfiguredEmbeddingClient() if settings.provider == "unconfigured" else CompatibleEmbeddingClient(settings)

def chunk_input(chunk):
    headings = "\n".join(" > ".join(path) for path in chunk["heading_paths"])
    return chunk["title"] + "\n" + headings + "\n" + chunk["text"]

class UnconfiguredEmbeddingClient:
    async def embed(self, texts):
        raise EmbeddingError('请先配置真实 Embedding 模型。')
