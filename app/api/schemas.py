from typing import Literal
from pydantic import BaseModel, Field, model_validator


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class DocumentResponse(BaseModel):
    document_id: str
    filename: str
    size_bytes: int
    task_id: str
    file_hash: str
    source: str
    version: str | None
    parser_version: str
    status: Literal["queued", "processing", "succeeded", "failed"]
    duplicate: bool


class TaskEventResponse(BaseModel):
    status: Literal["queued", "processing", "succeeded", "failed"]
    stage: Literal["validated", "parsing", "cleaning", "completed"]
    attempt: int
    error_code: str | None
    created_at: float


class TaskResponse(BaseModel):
    task_id: str
    document_id: str
    filename: str
    file_hash: str
    source: str
    version: str | None
    parser_version: str
    status: Literal["queued", "processing", "succeeded", "failed"]
    stage: Literal["validated", "parsing", "cleaning", "completed"]
    attempts: int
    error_code: str | None
    error_message: str | None
    created_at: float
    updated_at: float
    title: str | None
    text_length: int | None
    events: list[TaskEventResponse]


class ToolCallResponse(BaseModel):
    call_id: str
    name: str
    result: str


class ChunkBuildRequest(BaseModel):
    strategy: Literal["fixed", "structure"] = "structure"
    size: int = Field(default=400, ge=32, le=8192, strict=True)
    overlap: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_overlap(self):
        if self.overlap >= self.size:
            raise ValueError("Overlap must be smaller than size")
        return self


class ChunkResponse(BaseModel):
    document_id: str
    chunk_id: str
    ordinal: int
    text: str
    title: str
    heading_paths: list[list[str]]
    start_char: int
    end_char: int
    start_line: int
    end_line: int
    source: str
    version: str
    parser_version: str
    text_hash: str
    config_id: str
    split_reason: str
    previous_chunk_id: str | None
    next_chunk_id: str | None


class ChunkSetResponse(BaseModel):
    document_id: str
    chunk_set_id: str
    config_id: str
    text_hash: str
    config: ChunkBuildRequest
    length_unit: Literal["character"]
    coordinate_system: str
    count: int
    chunks: list[ChunkResponse]


class EmbeddingBatchResponse(BaseModel):
    batch_number: int
    attempt: int
    purpose: str
    status: str
    input_count: int
    input_chars: int
    duration_ms: float
    total_tokens: int | None
    estimated_cost: float | None
    error_code: str | None

class IndexResponse(BaseModel):
    document_id: str
    chunk_set_id: str
    collection: str
    model_id: str
    dimensions: int
    count: int
    reused: bool
    run_id: str
    duration_ms: float
    batches: list[EmbeddingBatchResponse]

class VectorSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4096)
    top_k: int = Field(default=5, ge=1, le=50, strict=True)
    mode: Literal["exact", "hnsw"] = "exact"
    ef_search: int = Field(default=40, ge=1, le=1000, strict=True)

class VectorHitResponse(ChunkResponse):
    distance: float
    score: float

class VectorSearchResponse(BaseModel):
    collection: str
    model_id: str
    dimensions: int
    mode: Literal["exact", "hnsw"]
    run_id: str
    duration_ms: float
    hits: list[VectorHitResponse]


class RetrievalFilters(BaseModel):
    model_config = {"extra": "forbid", "allow_inf_nan": False}
    document_ids: list[str] | None = Field(default=None, max_length=100)
    source: str | None = Field(default=None, min_length=1, max_length=512)
    updated_after: float | None = Field(default=None, ge=0)
    updated_before: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_range(self):
        if self.updated_after is not None and self.updated_before is not None and self.updated_after > self.updated_before:
            raise ValueError("Invalid update time range")
        return self


class RetrievalRequest(VectorSearchRequest):
    retrieval_profile: Literal["ranked", "coverage", "multi_query"] = "ranked"
    model_config = {"extra": "forbid"}
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)
    strategy: Literal["vector", "hybrid", "hybrid_rerank"] | None = None
    candidate_k: int | None = Field(default=None, ge=1, le=50, strict=True)

    @model_validator(mode="after")
    def nonblank_query(self):
        if not self.query.strip():
            raise ValueError("Query must contain non-whitespace text")
        if self.candidate_k is not None and self.candidate_k < self.top_k:
            raise ValueError("candidate_k must be at least top_k")
        return self


class LocationResponse(BaseModel):
    start_char: int
    end_char: int
    start_line: int
    end_line: int
    coordinate_system: str


class CitationResponse(BaseModel):
    chunk_id: str
    document_id: str
    chunk_set_id: str
    source: str
    version: str
    title: str
    heading_paths: list[list[str]]
    text_hash: str
    parser_version: str
    location: LocationResponse
    page: int | None
    url: str


class CitationReadResponse(BaseModel):
    text: str
    citation: CitationResponse


class RetrievalHitResponse(BaseModel):
    chunk_id: str
    document_id: str
    text: str
    score: float
    source: str
    version: str
    citation: CitationResponse
    ranks: dict[str, int] | None = None
    component_scores: dict[str, float] | None = None


class RetrievalResponse(BaseModel):
    collection: str
    model_id: str
    dimensions: int
    mode: Literal["exact", "hnsw"]
    score_type: Literal["cosine_similarity", "rrf", "rerank_relevance", "bm25"]
    run_id: str
    duration_ms: float
    hits: list[RetrievalHitResponse]
    diagnostics: dict | None = None


class PermissionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    user_ids: list[str] = Field(max_length=100)


class PermissionResponse(PermissionRequest):
    document_id: str


class DocumentUpdateResponse(BaseModel):
    document_id: str
    version: str
    revision: int
    active: bool
    index_required: bool

class ChatRequest(BaseModel):
    retrieval_profile: Literal["ranked", "coverage", "multi_query"] = "ranked"
    model_config = {"extra": "forbid"}
    message: str = Field(min_length=1, max_length=4096)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=64)
    top_k: int = Field(default=20, ge=1, le=20, strict=True)
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)

    @model_validator(mode="after")
    def nonblank_message(self):
        if not self.message.strip():
            raise ValueError("Blank message")
        return self


class ChatContextResponse(BaseModel):
    input_token_estimate: int
    estimate_method: str
    output_token_reserve: int
    input_limit: int
    history_message_ids: list[int]
    summary_message_ids: list[int]
    omitted_user_messages: int
    evidence_count: int
    retrieval_hit_count: int
    generation_attempts: int
    retrieval_query: str
    retrieval_profile: str = "ranked"
    retrieval_diagnostics: dict = Field(default_factory=dict)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    model_usage: list[dict] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    omitted_evidence_ids: list[str] = Field(default_factory=list)
    coverage_review: Literal["not_needed", "disabled", "completed", "skipped_budget", "failed"] = "not_needed"
    validation_failures: list[dict[str, str | int]] = Field(default_factory=list)
    answer_mode: Literal["grounded", "generated"] = "generated"
    effective_config: dict | None = None


from app.generation.models import Claim


class ChatResponse(BaseModel):
    conversation_id: str
    answer: str
    citations: list[CitationResponse]
    claims: list[Claim]
    insufficient_context: bool
    status: Literal["answered", "refused", "rejected"]
    reason: str | None
    model_provider: str
    model_id: str
    context: ChatContextResponse
