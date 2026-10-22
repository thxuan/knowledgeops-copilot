"""Bounded batch embedding and all-or-nothing vector publication."""
import asyncio
import json
import logging
import time
from uuid import uuid4
from app.core.embedding import EmbeddingError, EmbeddingTransientError, chunk_input, validate_result
from app.core.errors import AppError
logger = logging.getLogger("knowledgeops")

class VectorService:
    def __init__(self, chunks, vectors, client, settings):
        self.chunks, self.vectors, self.client, self.settings = chunks, vectors, client, settings
        self.semaphore = asyncio.Semaphore(settings.concurrency)

    async def _embed(self, texts, run_id, batch_number, document_id, purpose):
        for attempt in range(1, self.settings.max_attempts + 1):
            async with self.semaphore:
                started = time.perf_counter()
                event = {"batch_id": str(uuid4()), "run_id": run_id, "batch_number": batch_number,
                         "document_id": document_id, "attempt": attempt, "purpose": purpose,
                         "input_count": len(texts), "input_chars": sum(map(len, texts)),
                         "status": "failed", "total_tokens": None, "estimated_cost": None, "error_code": None}
                failure = None
                try:
                    result = await asyncio.wait_for(self.client.embed(texts), self.settings.timeout_seconds)
                    validate_result(result, self.settings, len(texts))
                    event.update(status="succeeded", total_tokens=result.total_tokens)
                    if result.total_tokens is not None and self.settings.cost_per_million_tokens is not None:
                        event["estimated_cost"] = result.total_tokens / 1_000_000 * self.settings.cost_per_million_tokens
                except TimeoutError:
                    failure = EmbeddingTransientError("Embedding timed out")
                except EmbeddingError as exc:
                    failure = exc
                except Exception:
                    failure = EmbeddingError("Unexpected embedding failure")
                event["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
                event["error_code"] = failure.code if failure else None
                await asyncio.to_thread(self.vectors.record_batch, event)
                logger.info(json.dumps({"event": "embedding.batch", **event}))
                if failure is None:
                    return result.vectors
            if not isinstance(failure, EmbeddingTransientError) or attempt == self.settings.max_attempts:
                raise AppError(failure.code, "Embedding failed; vectors were not published", 502)
            await asyncio.sleep(min(0.1 * 2 ** (attempt - 1), 1))

    async def index_document(self, document_id, identity):
        await asyncio.to_thread(self.chunks.require_published, document_id, identity)
        started = time.perf_counter()
        snapshot = await asyncio.to_thread(self.chunks.get, document_id, identity)
        if not snapshot["count"]:
            raise AppError("EMPTY_DOCUMENT", "No chunks to embed", 409)
        run_id = str(uuid4())
        reused = await asyncio.to_thread(self.vectors.ready, document_id, snapshot["chunk_set_id"], snapshot["count"])
        if not reused:
            texts = [chunk_input(c) for c in snapshot["chunks"]]
            batches = [(n, texts[start:start + self.settings.batch_size]) for n, start in
                       enumerate(range(0, len(texts), self.settings.batch_size))]
            pending, results = iter(batches), [None] * len(batches)
            async def worker():
                for number, batch in pending:
                    results[number] = await self._embed(batch, run_id, number, document_id, "index")
            workers = [asyncio.create_task(worker()) for _ in range(min(self.settings.concurrency, len(batches)))]
            outcomes = await asyncio.gather(*workers, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            current = await asyncio.to_thread(self.chunks.get, document_id, identity)
            if current["chunk_set_id"] != snapshot["chunk_set_id"]:
                raise AppError("CHUNKS_CHANGED", "Chunk set changed during embedding; retry", 409)
            await asyncio.to_thread(self.vectors.publish, snapshot, identity.tenant_id,
                                    [v for result in results for v in result])
        events = await asyncio.to_thread(self.vectors.batch_events, run_id)
        return {"document_id": document_id, "chunk_set_id": snapshot["chunk_set_id"],
                "collection": self.settings.collection, "model_id": self.settings.model_id,
                "dimensions": self.settings.dimensions, "count": snapshot["count"], "reused": reused,
                "run_id": run_id, "duration_ms": round((time.perf_counter() - started) * 1000, 3), "batches": events}

    def allowed_sets(self, identity, filters=None):
        filters = filters or {}
        conditions = ["d.tenant_id=%s", "(EXISTS(SELECT 1 FROM document_access a WHERE a.document_id=d.document_id AND a.user_id=%s) OR %s)", "j.status='succeeded'", "d.active=1", "d.publication_state='published'", "d.withdrawn=0"]
        params = [identity.tenant_id, identity.user_id, 'manage_platform' in identity.platform_capabilities]
        if filters.get("document_ids") is not None:
            ids = filters["document_ids"]
            if not ids:
                return []
            conditions.append("d.document_id IN (" + ",".join("%s" for _ in ids) + ")")
            params.extend(ids)
        for key, operator in [("updated_after", ">="), ("updated_before", "<=")]:
            if filters.get(key) is not None:
                conditions.append("d.updated_at " + operator + " %s")
                params.append(filters[key])
        if filters.get("source") is not None:
            conditions.append("d.source=%s")
            params.append(filters["source"])
        with self.chunks.ingestion.connection() as db:
            return [dict(row) for row in db.execute("""SELECT d.document_id,h.chunk_set_id
                FROM documents d
                JOIN document_chunk_heads h USING(document_id) JOIN ingestion_jobs j USING(document_id)
                WHERE """ + " AND ".join(conditions), params)]

    async def search(self, query, identity, top_k=5, mode="exact", ef_search=40, filters=None):
        started, run_id = time.perf_counter(), str(uuid4())
        auth_started = time.perf_counter()
        allowed = await asyncio.to_thread(self.allowed_sets, identity, filters)
        timings = {"vector_authorization": (time.perf_counter() - auth_started) * 1000, "embedding": 0.0, "vector_search": 0.0}
        hits = []
        if allowed:
            embedding_started = time.perf_counter()
            vector = (await self._embed([query], run_id, 0, None, "query"))[0]
            timings["embedding"] = (time.perf_counter() - embedding_started) * 1000
            search_started = time.perf_counter()
            hits = await asyncio.to_thread(self.vectors.search, vector, allowed, identity.tenant_id, top_k, mode, ef_search)
            timings["vector_search"] = (time.perf_counter() - search_started) * 1000
            auth_started = time.perf_counter()
            current = {(a["document_id"], a["chunk_set_id"]) for a in
                       await asyncio.to_thread(self.allowed_sets, identity, filters)}
            timings["vector_authorization"] += (time.perf_counter() - auth_started) * 1000
            old = {a["document_id"]: a["chunk_set_id"] for a in allowed}
            hits = [h for h in hits if (h["document_id"], old[h["document_id"]]) in current]
            for hit in hits:
                hit["chunk_set_id"] = old[hit["document_id"]]
        return {"collection": self.settings.collection, "model_id": self.settings.model_id,
                "dimensions": self.settings.dimensions, "mode": mode, "run_id": run_id,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3), "hits": hits,
                "timings_ms": {k: round(v, 3) for k, v in timings.items()}}

    async def retrieve(self, query, identity, top_k=5, filters=None, mode="exact", ef_search=40):
        from dataclasses import asdict
        from app.retrieval.objects import RetrievalHit, make_citation
        result = await self.search(query, identity, top_k, mode, ef_search, filters)
        result["hits"] = [asdict(RetrievalHit(chunk_id=h["chunk_id"], document_id=h["document_id"],
            text=h["text"], score=h["score"], source=h["source"], version=h["version"],
            citation=make_citation(h, h["chunk_set_id"]))) for h in result["hits"]]
        result["score_type"] = "cosine_similarity"
        return result
