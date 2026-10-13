"""Configuration read only from environment variables."""

import os
from dataclasses import dataclass, replace
from app.db.postgres import DEFAULT_DATABASE_URL, database_identity
from app.agent.contracts import AgentSettings
from app.core.embedding import EmbeddingSettings
from app.generation.provider import GenerationSettings
from app.retrieval.pipeline import RetrievalSettings
from app.retrieval.rerank import RerankSettings


@dataclass(frozen=True)
class Settings:
    agent: AgentSettings = AgentSettings()
    app_env: str = "local"
    max_document_bytes: int = 1_048_576
    database_url: str = DEFAULT_DATABASE_URL
    ingestion_worker_enabled: bool = True
    ingestion_lease_seconds: float = 30.0
    ingestion_max_attempts: int = 3
    ingestion_poll_seconds: float = 0.2

    vector_database_url: str | None = None
    embedding: EmbeddingSettings = EmbeddingSettings()
    generation: GenerationSettings = GenerationSettings()
    retrieval: RetrievalSettings = RetrievalSettings()

    @classmethod
    def from_env(cls) -> "Settings":
        review = os.getenv("KNOWLEDGEOPS_GENERATION_COVERAGE_REVIEW", "true").lower()
        if review not in {"true", "false"}:
            raise ValueError("KNOWLEDGEOPS_GENERATION_COVERAGE_REVIEW must be true or false")
        database_url = (os.getenv("KNOWLEDGEOPS_DATABASE_URL") or
                        os.getenv("KNOWLEDGEOPS_VECTOR_DATABASE_URL") or DEFAULT_DATABASE_URL)
        agent = AgentSettings.from_env()
        agent = replace(agent, database_url=agent.database_url or database_url)
        settings = cls(
            agent=agent,
            retrieval=RetrievalSettings(
                strategy=os.getenv("KNOWLEDGEOPS_RETRIEVAL_STRATEGY", "vector"),
                candidate_k=int(os.getenv("KNOWLEDGEOPS_RETRIEVAL_CANDIDATE_K", "20")),
                rrf_constant=int(os.getenv("KNOWLEDGEOPS_RETRIEVAL_RRF_CONSTANT", "60")),
                rerank=RerankSettings(
                    provider=os.getenv("KNOWLEDGEOPS_RERANK_PROVIDER", "none"),
                    model_id=os.getenv("KNOWLEDGEOPS_RERANK_MODEL_ID", "qwen3-rerank"),
                    endpoint=os.getenv("KNOWLEDGEOPS_RERANK_ENDPOINT", "https://api.example.invalid/v1/reranks"),
                    api_key=os.getenv("KNOWLEDGEOPS_RERANK_API_KEY") or None,
                    timeout_seconds=float(os.getenv("KNOWLEDGEOPS_RERANK_TIMEOUT_SECONDS", "10")),
                    cost_per_million_tokens=(float(os.environ["KNOWLEDGEOPS_RERANK_COST_PER_MILLION_TOKENS"])
                        if os.getenv("KNOWLEDGEOPS_RERANK_COST_PER_MILLION_TOKENS") else None))),
            generation=GenerationSettings(
                provider=os.getenv("KNOWLEDGEOPS_GENERATION_PROVIDER", "unconfigured"),
                model_id=os.getenv("KNOWLEDGEOPS_GENERATION_MODEL_ID", "qwen-plus"),
                input_limit=int(os.getenv("KNOWLEDGEOPS_GENERATION_INPUT_LIMIT", "32000")),
                output_limit=int(os.getenv("KNOWLEDGEOPS_GENERATION_OUTPUT_LIMIT", "2048")),
                history_budget=int(os.getenv("KNOWLEDGEOPS_GENERATION_HISTORY_BUDGET", "2048")),
                summary_budget=int(os.getenv("KNOWLEDGEOPS_GENERATION_SUMMARY_BUDGET", "2048")),
                timeout_seconds=float(os.getenv("KNOWLEDGEOPS_GENERATION_TIMEOUT_SECONDS", "60")),
                base_url=os.getenv("KNOWLEDGEOPS_GENERATION_BASE_URL", "https://api.example.invalid/v1"),
                api_key=os.getenv("KNOWLEDGEOPS_GENERATION_API_KEY") or None,
                temperature=float(os.getenv("KNOWLEDGEOPS_GENERATION_TEMPERATURE", "0")),
                top_p=(float(os.environ["KNOWLEDGEOPS_GENERATION_TOP_P"])
                    if os.getenv("KNOWLEDGEOPS_GENERATION_TOP_P") else None),
                coverage_review=review == "true",
                answer_mode=os.getenv("KNOWLEDGEOPS_GENERATION_ANSWER_MODE", "generated"),
            ),
            vector_database_url=database_url,
            embedding=EmbeddingSettings(
                provider=os.getenv("KNOWLEDGEOPS_EMBEDDING_PROVIDER", "unconfigured"),
                model_id=os.getenv("KNOWLEDGEOPS_EMBEDDING_MODEL_ID", "text-embedding-v4"),
                dimensions=int(os.getenv("KNOWLEDGEOPS_EMBEDDING_DIMENSIONS", "128")),
                collection=os.getenv("KNOWLEDGEOPS_EMBEDDING_COLLECTION", "knowledgeops-v1"),
                base_url=os.getenv("KNOWLEDGEOPS_EMBEDDING_BASE_URL", "https://api.example.invalid/v1"),
                api_key=os.getenv("KNOWLEDGEOPS_EMBEDDING_API_KEY") or None,
                batch_size=int(os.getenv("KNOWLEDGEOPS_EMBEDDING_BATCH_SIZE", "16")),
                concurrency=int(os.getenv("KNOWLEDGEOPS_EMBEDDING_CONCURRENCY", "2")),
                timeout_seconds=float(os.getenv("KNOWLEDGEOPS_EMBEDDING_TIMEOUT_SECONDS", "30")),
                max_attempts=int(os.getenv("KNOWLEDGEOPS_EMBEDDING_MAX_ATTEMPTS", "3")),
                cost_per_million_tokens=(float(os.environ["KNOWLEDGEOPS_EMBEDDING_COST_PER_MILLION_TOKENS"])
                    if os.getenv("KNOWLEDGEOPS_EMBEDDING_COST_PER_MILLION_TOKENS") else None),
            ),
            app_env=os.getenv("KNOWLEDGEOPS_APP_ENV", "local"),
            max_document_bytes=int(os.getenv("KNOWLEDGEOPS_MAX_DOCUMENT_BYTES", "1048576")),
            database_url=database_url,
            ingestion_worker_enabled=os.getenv("KNOWLEDGEOPS_INGESTION_WORKER_ENABLED", "true").lower() == "true",
            ingestion_lease_seconds=float(os.getenv("KNOWLEDGEOPS_INGESTION_LEASE_SECONDS", "30")),
            ingestion_max_attempts=int(os.getenv("KNOWLEDGEOPS_INGESTION_MAX_ATTEMPTS", "3")),
            ingestion_poll_seconds=float(os.getenv("KNOWLEDGEOPS_INGESTION_POLL_SECONDS", "0.2")),

        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.database_url:
            raise ValueError("Configure KNOWLEDGEOPS_DATABASE_URL")
        if os.getenv("KNOWLEDGEOPS_INGESTION_DB_PATH"):
            raise ValueError("SQLite configuration is retired; migrate the file and use KNOWLEDGEOPS_DATABASE_URL")
        for url in (self.vector_database_url, self.agent.database_url, self.agent.business_database_url):
            if url and database_identity(url) != database_identity(self.database_url):
                raise ValueError("All repositories must use the same PostgreSQL server and database")
        self.agent.validate()
        self.retrieval.validate()
        self.embedding.validate()
        self.generation.validate()
        if self.app_env not in {"local", "test"}:
            raise ValueError("Local test authentication cannot be used in production")
        if self.max_document_bytes < 1:
            raise ValueError("Size and token limits must be positive")

        if self.ingestion_lease_seconds <= 0 or self.ingestion_poll_seconds <= 0:
            raise ValueError("Ingestion lease and poll intervals must be positive")
        if self.ingestion_max_attempts < 1:
            raise ValueError("Ingestion attempt limit must be positive")
