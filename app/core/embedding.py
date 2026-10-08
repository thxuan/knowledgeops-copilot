"""Embedding adapters; the deterministic Fake has no learned semantics."""
from dataclasses import dataclass
import hashlib
import math
import re
from typing import Protocol
import httpx

class EmbeddingError(Exception):
    code = "EMBEDDING_INVALID_RESPONSE"

class EmbeddingTransientError(EmbeddingError):
    code = "EMBEDDING_TEMPORARY_FAILURE"

@dataclass(frozen=True)
class EmbeddingSettings:
    provider: str = "fake"
    model_id: str = "fake-hash-v1"
    dimensions: int = 128
    collection: str = "day5-fake"
    base_url: str = "https://api.example.invalid/v1"
    api_key: str | None = None
    batch_size: int = 16
    concurrency: int = 2
    timeout_seconds: float = 30
    max_attempts: int = 3
    cost_per_million_tokens: float | None = None

    def validate(self):
        if self.provider not in {"fake", "compatible"}:
            raise ValueError("Unsupported embedding provider")
        if not self.model_id or not self.collection or not 1 <= self.dimensions <= 2000:
            raise ValueError("Invalid embedding model/collection/HNSW dimension")
        if not 1 <= self.batch_size <= 128 or not 1 <= self.concurrency <= 8:
            raise ValueError("Invalid batch size/concurrency")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0 or not 1 <= self.max_attempts <= 5:
            raise ValueError("Invalid timeout/retry limit")
        if self.cost_per_million_tokens is not None and (not math.isfinite(self.cost_per_million_tokens) or self.cost_per_million_tokens < 0):
            raise ValueError("Invalid embedding price")
        if self.provider == "fake" and self.model_id != "fake-hash-v1":
            raise ValueError("Fake model ID is fixed")
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

class FakeEmbeddingClient:
    def __init__(self, settings):
        self.settings = settings

    async def embed(self, texts):
        vectors = []
        for text in texts:
            tokens = re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", text.lower())
            for run in re.findall(r"[\u4e00-\u9fff]+", text):
                tokens.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
            vector = [0.0] * self.settings.dimensions
            for token in tokens or ["<empty>"]:
                digest = hashlib.sha256(token.encode()).digest()
                vector[int.from_bytes(digest[:4], "big") % len(vector)] += 1 if digest[4] % 2 else -1
            norm = math.hypot(*vector)
            if norm == 0:
                vector[0], norm = 1.0, 1.0
            vectors.append([x / norm for x in vector])
        return EmbeddingResult(self.settings.model_id, vectors, 0)

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
    return FakeEmbeddingClient(settings) if settings.provider == "fake" else CompatibleEmbeddingClient(settings)

def chunk_input(chunk):
    headings = "\n".join(" > ".join(path) for path in chunk["heading_paths"])
    return chunk["title"] + "\n" + headings + "\n" + chunk["text"]
