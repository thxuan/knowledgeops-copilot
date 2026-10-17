"""Bounded request-local controls. Credentials and provider URLs stay server owned."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.api.schemas import ChatRequest


class GenerationControls(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    model: Literal["configured"] = "configured"
    temperature: float | None = Field(default=None, ge=0, lt=2, strict=True)
    top_p: float | None = Field(default=None, gt=0, le=1, strict=True)
    output_limit: int | None = Field(default=None, ge=128, le=8192, strict=True)
    coverage_review: bool | None = Field(default=None, strict=True)


class RetrievalControls(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    semantic: bool = True
    keyword: bool = True
    rrf: bool = True
    rerank: bool = False
    semantic_k: int = Field(default=20, ge=1, le=50)
    keyword_k: int = Field(default=20, ge=1, le=50)
    fusion_k: int = Field(default=20, ge=1, le=50)
    rrf_constant: int = Field(default=60, ge=1, le=200)
    mode: Literal["exact", "hnsw"] = "exact"
    ef_search: int = Field(default=40, ge=1, le=1000)

    @model_validator(mode="after")
    def valid_pipeline(self):
        if not self.semantic and not self.keyword:
            raise ValueError("Enable at least one retrieval channel")
        if self.semantic and self.keyword and not self.rrf:
            raise ValueError("Two retrieval channels require RRF fusion")
        return self


class WorkbenchChatRequest(ChatRequest):
    replay_context_id: str | None = Field(default=None, min_length=1, max_length=64)
    dataset_id: str | None = Field(default=None, max_length=64)
    runtime_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    generation: GenerationControls = Field(default_factory=GenerationControls)
    retrieval: RetrievalControls = Field(default_factory=RetrievalControls)
    question_id: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def valid_window(self):
        if self.replay_context_id and (self.conversation_id or self.dataset_id or self.question_id):
            raise ValueError("Replay uses a fixed free-question context, without a live conversation or dataset")
        r = self.retrieval
        available = r.fusion_k if r.semantic and r.keyword else r.semantic_k if r.semantic else r.keyword_k
        if self.top_k > available:
            raise ValueError("Final evidence count exceeds upstream candidate count")
        return self


class ModelConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    base_url: str = Field(min_length=8, max_length=500)
    model_id: str = Field(min_length=1, max_length=120)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    input_price: float | None = Field(default=None, ge=0)
    output_price: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_endpoint(self):
        from urllib.parse import urlsplit
        u = urlsplit(self.base_url)
        if u.scheme not in {"https", "http"} or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("Use an absolute API base URL without credentials, query or fragment")
        if u.scheme == "http" and u.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote API endpoints require HTTPS")
        if (self.input_price is None) != (self.output_price is None):
            raise ValueError("Provide both input and output prices")
        self.base_url = self.base_url.rstrip("/")
        self.model_id = self.model_id.strip()
        if not self.model_id or self.model_id.lower().startswith("fake"):
            raise ValueError("Configure a real model")
        return self


class WorkbenchMessage(WorkbenchChatRequest):
    intent: Literal["auto", "question"] = "auto"
    request_key: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
