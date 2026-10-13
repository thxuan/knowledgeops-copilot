"""Authorized vector/BM25 -> RRF -> optional rerank -> final Top K."""
import asyncio
from dataclasses import asdict, dataclass
import time
from uuid import uuid4
from app.core.errors import AppError
from app.retrieval.lexical import TOKENIZER_VERSION, bm25, evidence_text, rrf
from app.retrieval.objects import RetrievalHit, make_citation
from app.retrieval.rerank import RerankSettings, make_rerank_client, validate_result

STRATEGIES = ("vector", "bm25", "hybrid", "vector_rerank", "bm25_rerank", "hybrid_rerank")


@dataclass(frozen=True)
class RetrievalSettings:
    strategy: str = "vector"
    candidate_k: int = 20
    rrf_constant: int = 60
    rerank: RerankSettings = RerankSettings()
    vector_k: int | None = None
    keyword_k: int | None = None
    fusion_k: int | None = None

    def validate(self):
        self.rerank.validate()
        if self.strategy not in STRATEGIES or not 1 <= self.candidate_k <= 50 or self.rrf_constant < 1:
            raise ValueError("Invalid retrieval configuration")
        if any(v is not None and not 1 <= v <= 50 for v in (self.vector_k, self.keyword_k, self.fusion_k)):
            raise ValueError("Invalid candidate window")
        if self.strategy.endswith("_rerank") and self.rerank.provider == "none":
            raise ValueError("hybrid_rerank requires a rerank provider")

    @property
    def public_config(self):
        return {"strategy": self.strategy, "candidate_k": self.candidate_k,
                "rrf_constant": self.rrf_constant, "tokenizer": TOKENIZER_VERSION,
                "bm25": {"k1": 1.2, "b": .75}, "rerank": self.rerank.public_config,
                "vector_k": self.vector_k, "keyword_k": self.keyword_k, "fusion_k": self.fusion_k}


def normalize(hit):
    return asdict(RetrievalHit(chunk_id=hit["chunk_id"], document_id=hit["document_id"],
        text=hit["text"], score=hit["score"], source=hit["source"], version=hit["version"],
        citation=make_citation(hit, hit["chunk_set_id"])))


class RetrievalService:
    def __init__(self, vectors, settings=None, reranker=None, planner=None):
        self.vectors = vectors
        self.planner = planner
        self.settings = settings or RetrievalSettings()
        self.settings.validate()
        self.reranker = reranker or make_rerank_client(self.settings.rerank)

    async def reauthorize(self, hits, identity, filters):
        current = {(a["document_id"], a["chunk_set_id"]) for a in
                   await asyncio.to_thread(self.vectors.allowed_sets, identity, filters)}
        return [h for h in hits if (h["document_id"], h["citation"]["chunk_set_id"]) in current]

    async def prepare(self, query, identity, candidate_k, filters=None, mode="exact", ef_search=40,
                      include_lexical=True, include_vector=True):
        started = time.perf_counter()
        vector_k = self.settings.vector_k or candidate_k
        keyword_k = self.settings.keyword_k or candidate_k
        fusion_k = self.settings.fusion_k or candidate_k
        timings = {}
        if include_vector:
            result = await self.vectors.search(query, identity, vector_k, mode, ef_search, filters)
            timings.update(result.get("timings_ms", {"vector_total": result["duration_ms"]}))
        else:
            config = self.vectors.settings
            result = {"collection": config.collection, "model_id": config.model_id,
                      "dimensions": config.dimensions, "mode": mode, "run_id": str(uuid4()),
                      "duration_ms": 0.0, "hits": []}
        vector_hits = [normalize(h) for h in result["hits"]]
        keyword_hits = []
        lexical_started = time.perf_counter()
        if include_lexical:
            auth_started = time.perf_counter()
            allowed = await asyncio.to_thread(self.vectors.allowed_sets, identity, filters)
            corpus = await asyncio.to_thread(self.vectors.vectors.lexical_candidates, allowed, identity.tenant_id)
            timings["keyword_corpus"] = (time.perf_counter() - auth_started) * 1000
            bm25_started = time.perf_counter()
            keyword_hits = [normalize(h) for h in await asyncio.to_thread(bm25, query, corpus, keyword_k)]
            timings["bm25"] = (time.perf_counter() - bm25_started) * 1000
        auth_started = time.perf_counter()
        vector_hits = await self.reauthorize(vector_hits, identity, filters)
        keyword_hits = await self.reauthorize(keyword_hits, identity, filters)
        timings["candidate_authorization"] = (time.perf_counter() - auth_started) * 1000
        fusion_started = time.perf_counter()
        # Only two enabled channels need fusion. Single-channel scores retain their meaning.
        fused = rrf({"vector": vector_hits, "keyword": keyword_hits}, self.settings.rrf_constant) if (
            include_vector and include_lexical) else vector_hits if include_vector else keyword_hits
        timings["rrf"] = (time.perf_counter() - fusion_started) * 1000 if include_vector and include_lexical else 0.0
        return {"query": query, "result": result, "vector_hits": vector_hits,
                "keyword_hits": keyword_hits, "fused": fused[:fusion_k],
                "fusion_union_count": len(fused), "candidate_k": candidate_k,
                "timings_ms": timings, "lexical_ms": (time.perf_counter() - lexical_started) * 1000,
                "prepare_ms": (time.perf_counter() - started) * 1000}

    async def finish(self, prepared, identity, top_k, strategy, filters=None):
        started = time.perf_counter()
        base_strategy = strategy.removesuffix("_rerank")
        hits = (prepared["vector_hits"] if base_strategy == "vector" else
                prepared["keyword_hits"] if base_strategy == "bm25" else prepared["fused"])
        auth_started = time.perf_counter()
        hits = await self.reauthorize(hits, identity, filters)
        authorization_ms = (time.perf_counter() - auth_started) * 1000
        candidates = [dict(h) for h in hits]
        rerank = {"status": "disabled", "provider": self.settings.rerank.provider,
                  "duration_ms": 0.0, "input_count": 0, "total_tokens": 0, "estimated_cost": 0.0}
        score_type = {"vector": "cosine_similarity", "bm25": "bm25", "hybrid": "rrf"}[base_strategy]
        if strategy.endswith("_rerank"):
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
                    cost = (result.total_tokens / 1_000_000 * price
                            if result.total_tokens is not None and price is not None else None)
                    rerank.update(status="succeeded", total_tokens=result.total_tokens, estimated_cost=cost,
                                  model_id=result.model_id, request_id=result.request_id)
                    score_type = "rerank_relevance"
                except Exception:
                    rerank.update(status="failed_fallback", total_tokens=None, estimated_cost=None,
                                  error_code="RERANK_FAILED")
                rerank["duration_ms"] = round((time.perf_counter() - rerank_started) * 1000, 3)
        # Recheck both final evidence and diagnostic payloads after external work.
        auth_started = time.perf_counter()
        stages = {"vector": prepared["vector_hits"], "keyword": prepared["keyword_hits"],
                  "fusion": prepared["fused"] if base_strategy == "hybrid" else [], "rerank": hits if strategy.endswith("_rerank") and rerank["status"] == "succeeded" else []}
        authorized = {h["chunk_id"] for h in await self.reauthorize(
            hits + candidates + [h for stage in stages.values() for h in stage], identity, filters)}
        authorization_ms += (time.perf_counter() - auth_started) * 1000
        stages = {name: [h for h in stage if h["chunk_id"] in authorized] for name, stage in stages.items()}
        candidates = [h for h in candidates if h["chunk_id"] in authorized]
        hits = [h for h in hits if h["chunk_id"] in authorized][:top_k]
        base = prepared["result"]
        stages["final"] = hits
        timings = {**prepared["timings_ms"], "final_authorization": authorization_ms,
                   "rerank": rerank["duration_ms"]}
        duration = prepared["prepare_ms"]
        duration += (time.perf_counter() - started) * 1000
        return {**{k: base[k] for k in ("collection", "model_id", "dimensions", "mode", "run_id")},
                "score_type": score_type, "duration_ms": round(duration, 3), "hits": hits,
                "diagnostics": {"strategy": strategy, "original_query": prepared["query"],
                    "query_rewritten": False, "candidate_k": prepared["candidate_k"], "top_k": top_k,
                    "vector_count": len(prepared["vector_hits"]), "keyword_count": len(prepared["keyword_hits"]),
                    "fusion_union_count": prepared["fusion_union_count"], "candidate_count": len(candidates),
                    "final_count": len(hits), "candidates": candidates, "rerank": rerank,
                    "stages": stages, "timings_ms": {k: round(v, 3) for k, v in timings.items()},
                    "vector_ms": base["duration_ms"], "lexical_and_fusion_ms": round(prepared["lexical_ms"], 3),
                    "config": self.settings.public_config}}

    async def retrieve(self, query, identity, top_k=5, filters=None, mode="exact", ef_search=40,
                       strategy=None, candidate_k=None, retrieval_profile="ranked"):
        if retrieval_profile not in {"ranked", "coverage", "multi_query"}:
            raise ValueError("Invalid retrieval profile")
        if retrieval_profile != "ranked":
            return await self.retrieve_coverage(query, identity, top_k, filters, mode, ef_search,
                                                strategy, candidate_k, retrieval_profile == "multi_query")
        strategy = strategy or self.settings.strategy
        candidate_k = max(self.settings.candidate_k, top_k) if candidate_k is None else candidate_k
        if strategy not in STRATEGIES or not 1 <= top_k <= candidate_k <= 50:
            raise ValueError("Need a valid strategy and 1 <= top_k <= candidate_k <= 50")
        if strategy.endswith("_rerank") and self.reranker is None:
            raise AppError("RERANK_NOT_CONFIGURED", "Configure a rerank provider first", 503)
        base_strategy = strategy.removesuffix("_rerank")
        prepared = await self.prepare(query, identity, top_k if strategy == "vector" and self.settings.vector_k is None else candidate_k,
                                      filters, mode, ef_search, include_lexical=base_strategy != "vector",
                                      include_vector=base_strategy != "bm25")
        return await self.finish(prepared, identity, top_k, strategy, filters)


    async def retrieve_coverage(self, query, identity, top_k=5, filters=None, mode="exact", ef_search=40,
                                strategy=None, candidate_k=None, decompose=True):
        from app.retrieval.coverage import plan_queries, select_evidence, PLANNER_VERSION, SELECTOR_VERSION
        started = time.perf_counter()
        strategy = strategy or self.settings.strategy
        count = max(self.settings.candidate_k, top_k) if candidate_k is None else candidate_k
        if strategy not in STRATEGIES or not 1 <= top_k <= count <= 50:
            raise ValueError("Need a valid strategy and 1 <= top_k <= candidate_k <= 50")
        # Original retrieval must succeed. Supplemental queries are a bounded aid.
        original = await self.retrieve(query, identity, count, filters, mode, ef_search, strategy, count)
        queries, planning = [query], {"status": "disabled", "model_calls": 0}
        if decompose and not original["hits"]:
            planning = {"status": "skipped_empty", "model_calls": 0}
        elif decompose:
            if self.planner:
                queries, planning = await self.planner.plan(query)
            else:
                queries, planning = plan_queries(query), {"status": "rules", "model_calls": 0}
        rankings = {"query_0": original["hits"]}
        runs = [original]
        query_runs = [{"query": query, "status": "succeeded", "run_id": original["run_id"]}]
        # At most four retrievals, sequential to respect existing embedding admission.
        for i, subquery in enumerate(queries[1:], 1):
            try:
                result = await self.retrieve(subquery, identity, count, filters, mode, ef_search, strategy, count)
                rankings[f"query_{i}"] = result["hits"]
                runs.append(result)
                query_runs.append({"query": subquery, "status": "succeeded", "run_id": result["run_id"]})
            except (AppError, TimeoutError):
                query_runs.append({"query": subquery, "status": "failed_fallback"})
        # Revoke stale evidence from every ranking and diagnostic before exposing it.
        all_hits = [h for hs in rankings.values() for h in hs]
        all_hits += [h for run in runs for stage in run["diagnostics"]["stages"].values() for h in stage]
        authorized = {(h["document_id"], h["citation"]["chunk_set_id"], h["chunk_id"])
                      for h in await self.reauthorize(all_hits, identity, filters)}
        rankings = {name: [h for h in hs if (h["document_id"], h["citation"]["chunk_set_id"],
                                            h["chunk_id"]) in authorized] for name, hs in rankings.items()}
        selection_started = time.perf_counter()
        merged = rrf(rankings, self.settings.rrf_constant) if len(rankings) > 1 else rankings["query_0"]
        hits = select_evidence(query, merged, top_k, queries,
                               anchor_ids=[h["chunk_id"] for h in rankings["query_0"]])
        # Diagnostic candidates represent the actual selection pool, never an old
        # pre-authorization result. Original ranks allow identical-budget ablations.
        diagnostics = {"strategy": strategy, "original_query": query, "query_rewritten": False,
            "retrieval_profile": "multi_query" if decompose else "coverage", "candidate_k": count,
            "top_k": top_k, "candidate_count": len(merged), "final_count": len(hits),
            "queries": queries, "query_runs": query_runs, "planning": planning,
            "planner_version": PLANNER_VERSION, "selector_version": SELECTOR_VERSION,
            "candidates": merged, "original_candidate_ids": [h["chunk_id"] for h in rankings["query_0"]],
            "stages": {**{name: [h for run in runs for h in run["diagnostics"]["stages"].get(name, [])
                if (h["document_id"], h["citation"]["chunk_set_id"], h["chunk_id"]) in authorized]
                for name in ("vector", "keyword", "fusion", "rerank")}, "selection_pool": merged, "final": hits},
            "timings_ms": {**{name: round(sum(run["diagnostics"]["timings_ms"].get(name, 0) for run in runs), 3)
                for name in {name for run in runs for name in run["diagnostics"]["timings_ms"]}},
                "planning": planning.get("duration_ms", 0),
                "evidence_selection": round((time.perf_counter() - selection_started) * 1000, 3)},
            "rerank": {"status": "failed_fallback" if any(run["diagnostics"]["rerank"]["status"] == "failed_fallback" for run in runs)
                       else "succeeded" if any(run["diagnostics"]["rerank"]["status"] == "succeeded" for run in runs) else "disabled",
                "duration_ms": sum(run["diagnostics"]["rerank"]["duration_ms"] for run in runs),
                "estimated_cost": None if any(run["diagnostics"]["rerank"]["estimated_cost"] is None for run in runs) else
                    sum(run["diagnostics"]["rerank"]["estimated_cost"] for run in runs)},
            "config": self.settings.public_config}
        return {**{k: original[k] for k in ("collection", "model_id", "dimensions", "mode", "run_id")},
                "score_type": "rrf" if len(rankings) > 1 else original["score_type"],
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "hits": hits, "diagnostics": diagnostics}
