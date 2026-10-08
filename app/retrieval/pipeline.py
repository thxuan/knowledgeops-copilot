"""Authorized vector/BM25 -> RRF -> optional rerank -> final Top K."""
import asyncio
from dataclasses import asdict, dataclass
import time
from app.core.errors import AppError
from app.retrieval.lexical import TOKENIZER_VERSION, bm25, evidence_text, rrf
from app.retrieval.objects import RetrievalHit, make_citation
from app.retrieval.rerank import RerankSettings, make_rerank_client, validate_result

STRATEGIES = ("vector", "hybrid", "hybrid_rerank")


@dataclass(frozen=True)
class RetrievalSettings:
    strategy: str = "vector"
    candidate_k: int = 20
    rrf_constant: int = 60
    rerank: RerankSettings = RerankSettings()

    def validate(self):
        self.rerank.validate()
        if self.strategy not in STRATEGIES or not 1 <= self.candidate_k <= 50 or self.rrf_constant < 1:
            raise ValueError("Invalid retrieval configuration")
        if self.strategy == "hybrid_rerank" and self.rerank.provider == "none":
            raise ValueError("hybrid_rerank requires a rerank provider")

    @property
    def public_config(self):
        return {"strategy": self.strategy, "candidate_k": self.candidate_k,
                "rrf_constant": self.rrf_constant, "tokenizer": TOKENIZER_VERSION,
                "bm25": {"k1": 1.2, "b": .75}, "rerank": self.rerank.public_config}


def normalize(hit):
    return asdict(RetrievalHit(chunk_id=hit["chunk_id"], document_id=hit["document_id"],
        text=hit["text"], score=hit["score"], source=hit["source"], version=hit["version"],
        citation=make_citation(hit, hit["chunk_set_id"])))


class RetrievalService:
    def __init__(self, vectors, settings=None, reranker=None):
        self.vectors = vectors
        self.settings = settings or RetrievalSettings()
        self.settings.validate()
        self.reranker = reranker or make_rerank_client(self.settings.rerank)

    async def reauthorize(self, hits, identity, filters):
        current = {(a["document_id"], a["chunk_set_id"]) for a in
                   await asyncio.to_thread(self.vectors.allowed_sets, identity, filters)}
        return [h for h in hits if (h["document_id"], h["citation"]["chunk_set_id"]) in current]

    async def prepare(self, query, identity, candidate_k, filters=None, mode="exact", ef_search=40,
                      include_lexical=True):
        started = time.perf_counter()
        result = await self.vectors.search(query, identity, candidate_k, mode, ef_search, filters)
        vector_hits = [normalize(h) for h in result["hits"]]
        lexical_started = time.perf_counter()
        keyword_hits = []
        if include_lexical:
            allowed = await asyncio.to_thread(self.vectors.allowed_sets, identity, filters)
            corpus = await asyncio.to_thread(self.vectors.vectors.lexical_candidates, allowed, identity.tenant_id)
            keyword_hits = [normalize(h) for h in await asyncio.to_thread(bm25, query, corpus, candidate_k)]
        vector_hits = await self.reauthorize(vector_hits, identity, filters)
        keyword_hits = await self.reauthorize(keyword_hits, identity, filters)
        fused = rrf({"vector": vector_hits, "keyword": keyword_hits}, self.settings.rrf_constant)
        return {"query": query, "result": result, "vector_hits": vector_hits,
                "keyword_hits": keyword_hits, "fused": fused[:candidate_k],
                "fusion_union_count": len(fused), "candidate_k": candidate_k,
                "lexical_ms": (time.perf_counter() - lexical_started) * 1000,
                "prepare_ms": (time.perf_counter() - started) * 1000}

    async def finish(self, prepared, identity, top_k, strategy, filters=None):
        started = time.perf_counter()
        hits = prepared["vector_hits"] if strategy == "vector" else prepared["fused"]
        hits = await self.reauthorize(hits, identity, filters)
        candidates = [dict(h) for h in hits]
        rerank = {"status": "disabled", "provider": self.settings.rerank.provider,
                  "duration_ms": 0.0, "input_count": 0, "total_tokens": 0, "estimated_cost": 0.0}
        score_type = "cosine_similarity" if strategy == "vector" else "rrf"
        if strategy == "hybrid_rerank":
            if self.reranker is None:
                raise AppError("RERANK_NOT_CONFIGURED", "Configure a rerank provider first", 503)
            rerank.update(status="empty", input_count=len(hits))
            if hits:
                rerank_started = time.perf_counter()
                try:
                    result = await asyncio.wait_for(self.reranker.rerank(prepared["query"],
                        [evidence_text(h) for h in hits]), self.settings.rerank.timeout_seconds)
                    validate_result(result, len(hits), self.settings.rerank.provider == "compatible")
                    hits = [{**hits[i], "score": score} for i, score in sorted(
                        result.results, key=lambda r: (-r[1], r[0]))]
                    price = self.settings.rerank.cost_per_million_tokens
                    cost = (0.0 if self.settings.rerank.provider == "fake" else
                            result.total_tokens / 1_000_000 * price
                            if result.total_tokens is not None and price is not None else None)
                    rerank.update(status="succeeded", total_tokens=result.total_tokens, estimated_cost=cost,
                                  model_id=result.model_id, request_id=result.request_id)
                    score_type = "fake_bm25" if self.settings.rerank.provider == "fake" else "rerank_relevance"
                except Exception:
                    rerank.update(status="failed_fallback", total_tokens=None, estimated_cost=None,
                                  error_code="RERANK_FAILED")
                rerank["duration_ms"] = round((time.perf_counter() - rerank_started) * 1000, 3)
        # Recheck both final evidence and diagnostic payloads after external work.
        authorized = {h["chunk_id"] for h in await self.reauthorize(hits + candidates, identity, filters)}
        candidates = [h for h in candidates if h["chunk_id"] in authorized]
        hits = [h for h in hits if h["chunk_id"] in authorized][:top_k]
        base = prepared["result"]
        duration = (base["duration_ms"] if strategy == "vector" else prepared["prepare_ms"])
        duration += (time.perf_counter() - started) * 1000
        return {**{k: base[k] for k in ("collection", "model_id", "dimensions", "mode", "run_id")},
                "score_type": score_type, "duration_ms": round(duration, 3), "hits": hits,
                "diagnostics": {"strategy": strategy, "original_query": prepared["query"],
                    "query_rewritten": False, "candidate_k": prepared["candidate_k"], "top_k": top_k,
                    "vector_count": len(prepared["vector_hits"]), "keyword_count": len(prepared["keyword_hits"]),
                    "fusion_union_count": prepared["fusion_union_count"], "candidate_count": len(candidates),
                    "final_count": len(hits), "candidates": candidates, "rerank": rerank,
                    "vector_ms": base["duration_ms"], "lexical_and_fusion_ms": round(prepared["lexical_ms"], 3),
                    "config": self.settings.public_config}}

    async def retrieve(self, query, identity, top_k=5, filters=None, mode="exact", ef_search=40,
                       strategy=None, candidate_k=None):
        strategy = strategy or self.settings.strategy
        candidate_k = max(self.settings.candidate_k, top_k) if candidate_k is None else candidate_k
        if strategy not in STRATEGIES or not 1 <= top_k <= candidate_k <= 50:
            raise ValueError("Need a valid strategy and 1 <= top_k <= candidate_k <= 50")
        if strategy == "hybrid_rerank" and self.reranker is None:
            raise AppError("RERANK_NOT_CONFIGURED", "Configure a rerank provider first", 503)
        prepared = await self.prepare(query, identity, top_k if strategy == "vector" else candidate_k,
                                      filters, mode, ef_search, include_lexical=strategy != "vector")
        return await self.finish(prepared, identity, top_k, strategy, filters)
