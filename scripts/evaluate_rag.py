"""Day 8 offline dataset evaluation using the same ChatService as /chat."""
import argparse
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
import hashlib
import json
import logging
import math
from pathlib import Path
import random
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.api.schemas import ChatRequest
from app.core.config import Settings
from app.core.embedding import make_embedding_client
from app.core.errors import AppError
from app.core.identity import LOCAL_IDENTITIES
from app.db.ingestion import IngestionRepository
from app.db.snapshots import evaluation_snapshot, database_label
from app.db.chunks import ChunkRepository
from app.db.conversations import ConversationRepository
from app.db.vectors import VectorRepository
from app.evals.rag import RecordingGeneration, digest, generation_cost, summarize, review_summary
from app.evals.retrieval import measure, validate_annotations
from app.generation.context import SYSTEM_PROMPT, build_context
from app.generation.models import GeneratedAnswer, GeneratedClaims, parse_generation
from app.generation.provider import make_generation_client
from app.generation.service import ChatService, validation_code
from app.retrieval.service import VectorService
from app.retrieval.pipeline import RetrievalService
from scripts.compare_chunks import load_corpus

PARAMETER_SOURCE = "https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions"


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def fingerprint():
    names = sorted([*ROOT.glob("app/**/*.py"), *ROOT.glob("scripts/*.py")])
    names += [ROOT / "eval_data/questions.jsonl", ROOT / "eval_data/day4-source-spans.jsonl"]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in names}


def safe_config(settings, args):
    generation = asdict(settings.generation)
    generation.pop("api_key")
    embedding = settings.embedding.signature
    return {"generation": generation, "embedding": embedding,
            "collection": settings.embedding.collection, "top_k": args.top_k, "mode": "exact",
            "retrieval": settings.retrieval.public_config,
            "prices_per_million": {"generation_input": args.input_price,
                                  "generation_output": args.output_price,
                                  "embedding": settings.embedding.cost_per_million_tokens},
            "currency": args.currency, "prompt_sha256": digest(SYSTEM_PROMPT),
            "schema_sha256": digest(GeneratedClaims.model_json_schema()),
            "response_schema_sha256": digest(GeneratedAnswer.model_json_schema()),
            "generation_contract": "claims-v2-server-rendered-answer",
            "code_and_data_sha256": fingerprint()}



def load_evaluation_data(split):
    corpus = load_corpus()
    questions = [q for q in read_jsonl(ROOT / "eval_data/questions.jsonl") if q["split"] == split]
    anchors = read_jsonl(ROOT / "eval_data/day4-source-spans.jsonl")
    validate_annotations(questions, anchors, corpus)
    return corpus, questions, anchors


def validate_frozen_config(frozen, config):
    if frozen.get("split") != "dev" or frozen.get("status") not in {"completed", "frozen_after_evaluation_repair"}:
        raise ValueError("Freeze reference must derive from a completed dev experiment")
    if frozen.get("config") != config:
        raise ValueError("Code, data or configuration differs from the frozen dev experiment")


class FixedRetrieval:
    def __init__(self, result):
        self.result = result
    async def retrieve(self, *args, **kwargs):
        return self.result


def validate_fixture(intake, chunks, vectors, corpus):
    source_ids = {"sample_data/knowledge_base/" + doc["filename"]: key for key, doc in corpus.items()}
    with intake.connection() as db:
        docs = [dict(r) for r in db.execute("""SELECT d.*,h.chunk_set_id FROM documents d
                JOIN document_chunk_heads h USING(document_id) WHERE d.active=1""")]
        acl = {d["document_id"]: sorted(r["user_id"] for r in db.execute(
            "SELECT user_id FROM document_access WHERE document_id=%s", (d["document_id"],))) for d in docs}
    if len(docs) != len(corpus) or {d["source"] for d in docs} != set(source_ids):
        raise ValueError("Use the complete isolated 14-document sample index")
    for d in docs:
        doc = corpus[source_ids[d["source"]]]
        if any(d[k] != doc[k] for k in ("version", "text", "parser_version")):
            raise ValueError("Sample corpus differs from frozen sources")
        if acl[d["document_id"]] != sorted(doc["allowed_user_ids"]):
            raise ValueError("Sample ACL differs from reviewed fixture manifest")
        snapshot = chunks.get(d["document_id"], LOCAL_IDENTITIES[doc["allowed_user_ids"][0]])
        if not vectors.ready(d["document_id"], d["chunk_set_id"], snapshot["count"]):
            raise ValueError("Sample vector index is incomplete or model signature differs")
    return source_ids, docs


def filter_checks(hits, identity, corpus, source_ids, filters=None):
    filters = filters or {}
    result = []
    for hit in hits:
        doc = corpus.get(source_ids.get(hit["source"]))
        valid = bool(doc and identity.user_id in doc["allowed_user_ids"] and hit["version"] == doc["version"])
        if filters.get("source") is not None:
            valid = valid and hit["source"] == filters["source"]
        if filters.get("document_ids") is not None:
            valid = valid and hit["document_id"] in filters["document_ids"]
        result.append(valid)
    return result


async def sampling(rows, settings, args, out):
    if settings.generation.provider != "compatible" or not settings.generation.model_id.startswith("qwen-plus"):
        write_json(out / "sampling.json", {"status": "skipped", "reason": "This experiment validates qwen-plus parameters only; Fake has no sampling behavior."})
        return
    ids = ["F005", "X001", "U001"]
    selected = [r for qid in ids for r in rows if r["question_id"] == qid and r["generation_calls"]]
    if len(selected) != 3:
        raise ValueError("Sampling requires all three dev questions with recorded generation inputs")
    write_json(out / "sampling-inputs.json", [
        {"question_id": r["question_id"], "messages": r["generation_calls"][0]["messages"],
         "messages_sha256": digest(r["generation_calls"][0]["messages"])} for r in selected])
    configs = [("baseline", .7, .8), ("temperature_0", 0, .8), ("temperature_1", 1, .8),
               ("top_p_04", .7, .4), ("top_p_1", .7, 1)]
    tasks = [(r, name, t, p, n) for r in selected for name, t, p in configs for n in range(1, 4)]
    random.Random(8).shuffle(tasks)
    results = []
    client = RecordingGeneration(make_generation_client(settings.generation))
    for row, name, temperature, top_p, repeat in tasks:
        gs = replace(settings.generation, temperature=temperature, top_p=top_p)
        client.calls = []
        messages = row["generation_calls"][0]["messages"]
        item = {"question_id": row["question_id"], "configuration": name, "repeat": repeat,
                "sent_temperature": temperature, "sent_top_p": top_p,
                "messages_sha256": digest(messages), "status": "error",
                "correctness": None, "citation_support": None, "faithfulness": None}
        started = time.perf_counter()
        try:
            raw = await asyncio.wait_for(client.generate(messages, gs), gs.timeout_seconds)
            parsed = parse_generation(raw)
            evidence = json.loads(messages[-1]["content"])["evidence"]
            if not set(parsed.citations) <= {h["chunk_id"] for h in evidence}:
                raise ValueError("Citation outside provided context")
            item.update(status="refused" if parsed.insufficient_context else "answered",
                        parsed_output=parsed.model_dump())
        except (ValueError, AppError, TimeoutError) as exc:
            item["error_code"] = exc.code if isinstance(exc, AppError) else validation_code(exc)
        item.update(duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    calls=client.calls, usage=generation_cost(client.calls, gs.provider, args.input_price, args.output_price))
        results.append(item)
        write_jsonl(out / "sampling-results.jsonl", results)
        print(f"sampling {len(results)}/{len(tasks)} {row['question_id']} {name}", flush=True)
    groups = []
    for qid in ids:
        for name, _, _ in configs:
            group = [r for r in results if r["question_id"] == qid and r["configuration"] == name]
            groups.append({"question_id": qid, "configuration": name, "attempts": len(group),
                "distinct_raw_outputs": len({c["raw_output"] for r in group for c in r["calls"] if c["raw_output"] is not None}),
                "error_count": sum(r["status"] == "error" for r in group)})
    write_json(out / "sampling.json", {"status": "completed", "parameter_source": PARAMETER_SOURCE,
        "parameter_check_date": "2026-10-03", "effective_parameters": "sent values recorded; provider does not echo effective values",
        "model_version_limit": "Use a dated model ID for pinned reproducibility; qwen-plus is an unpinned alias.",
        "scope": "dev only, fixed first-attempt inputs, one generation per attempt, no repair; semantic review pending",
        "attempt_count": len(results), "groups": groups})


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--sampling", action="store_true")
    parser.add_argument("--freeze-from", type=Path)
    parser.add_argument("--input-price", type=float)
    parser.add_argument("--output-price", type=float)
    parser.add_argument("--currency", default="unspecified")
    parser.add_argument("--review-run", type=Path)
    parser.add_argument("--reviews", type=Path)
    args = parser.parse_args()
    if args.review_run:
        if not args.reviews:
            parser.error("--review-run requires --reviews")
        run = json.loads((args.review_run / "manifest.json").read_text(encoding="utf-8"))
        summary = review_summary(read_jsonl(args.review_run / "results.jsonl"), read_jsonl(args.reviews), run["run_id"])
        write_json(args.review_run / "review-summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    if not 1 <= args.top_k <= 20:
        parser.error("Need a seeded PostgreSQL database and top-k in 1..20")
    if args.sampling and args.split != "dev":
        parser.error("Sampling must not use the held-out test split")
    for price in (args.input_price, args.output_price):
        if price is not None and (not math.isfinite(price) or price < 0):
            parser.error("Prices must be finite and nonnegative")
    if (args.input_price is not None or args.output_price is not None) and args.currency == "unspecified":
        parser.error("Specify --currency when recording prices")
    # Check every selected gold label before database access or model calls.
    try:
        corpus, questions, anchors = load_evaluation_data(args.split)
    except ValueError as exc:
        parser.error(str(exc))
    settings = Settings.from_env()
    if not settings.vector_database_url:
        parser.error("Set KNOWLEDGEOPS_DATABASE_URL")
    config = safe_config(settings, args)
    if args.split == "test":
        if not args.freeze_from:
            parser.error("Test requires --freeze-from pointing to the chosen dev manifest")
        frozen = json.loads(args.freeze_from.read_text(encoding="utf-8"))
        try:
            validate_frozen_config(frozen, config)
        except ValueError as exc:
            parser.error(str(exc))
    run_id = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    out = ROOT / "docs/experiments/day8" / run_id
    out.mkdir(parents=True, exist_ok=False)
    manifest = {"run_id": run_id, "split": args.split, "config": config, "source_db": database_label(args.database_url or settings.database_url),
                "status": "running", "git_commit": None,
                "git_note": "Workspace has no Git repository; full source file SHA256 recorded instead.",
                "scope": "Fake engineering baseline" if settings.generation.provider == "fake" else "real model RAG evaluation"}
    write_json(out / "manifest.json", manifest)
    try:
        await run_evaluation(args, settings, corpus, questions, anchors, out, manifest)
    except Exception as exc:
        manifest.update(status="failed", error_code=getattr(exc, "code", type(exc).__name__),
                        failure_note="Evaluation aborted; inspect completed rows before using any partial result.")
        write_json(out / "manifest.json", manifest)
        raise


async def run_evaluation(args, settings, corpus, questions, anchors, out, manifest):
    run_id = manifest["run_id"]
    # Copy core tables into an isolated PostgreSQL schema; preserve a portable JSON snapshot.
    intake, schema = evaluation_snapshot(args.database_url or settings.database_url, out)
    manifest["snapshot_schema"] = schema
    intake.initialize()
    chunks = ChunkRepository(intake)
    chunks.initialize()
    conversations = ConversationRepository(intake)
    conversations.initialize()
    vectors = VectorRepository(args.database_url or settings.database_url, settings.embedding)
    vectors.initialize()
    source_ids, docs = validate_fixture(intake, chunks, vectors, corpus)
    manifest["corpus"] = [{k: d[k] for k in ("document_id", "source", "version", "chunk_set_id")} for d in docs]
    write_json(out / "manifest.json", manifest)
    service = RetrievalService(VectorService(chunks, vectors, make_embedding_client(settings.embedding),
        settings.embedding), settings.retrieval)
    client = RecordingGeneration(make_generation_client(settings.generation))
    rows = []
    for q in questions:
        identity = LOCAL_IDENTITIES[q["allowed_user_ids"][0]]
        row = {"question_id": q["question_id"], "question": q["question"], "user_id": identity.user_id,
               "question_type": q["question_type"], "answerable": q["answerable"],
               "expected_answer_points": q["expected_answer_points"], "expected_behavior": q["expected_behavior"],
               "status": "error", "retrieval": {}, "hits": [], "citation_checks": [], "filter_checks": [],
               "generation_calls": [], "failure_category": None}
        client.calls = []
        started = time.perf_counter()
        try:
            result = await service.retrieve(q["question"], identity, top_k=args.top_k)
            row["hits"] = result["hits"]
            row["retrieval_duration_ms"] = result["duration_ms"]
            row["embedding_batches"] = vectors.batch_events(result["run_id"])
            row["retrieval"] = measure(q, result["hits"], anchors, source_ids)
            row["filter_checks"] = filter_checks(result["hits"], identity, corpus, source_ids)
            response = await ChatService(conversations, FixedRetrieval(result), client, settings.generation).run(
                ChatRequest(message=q["question"], top_k=args.top_k), identity)
            row["response"] = response
            row["status"] = response["status"]
            context = build_context(q["question"], [], {}, result["hits"], settings.generation)
            row["input_evidence"] = context.evidence
            ids = {h["chunk_id"] for h in context.evidence}
            row["citation_checks"] = [c["chunk_id"] in ids for c in response["citations"]]
            if row["status"] == "rejected":
                row["failure_category"] = "generation_validation"
            elif row["status"] == "refused" and q["answerable"]:
                row["failure_category"] = ("retrieval_or_evidence_missing" if row["retrieval"]["source_recall_at_k"] < 1
                                           else "context_or_generation_review_needed")
            elif row["status"] == "answered" and not q["answerable"]:
                row["failure_category"] = "unanswerable_answered_review_needed"
        except AppError as exc:
            row["error_code"] = exc.code
            row["failure_category"] = "runtime_error"
        row["generation_calls"] = client.calls
        row["generation_usage"] = generation_cost(client.calls, settings.generation.provider, args.input_price, args.output_price)
        row["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        rows.append(row)
        write_jsonl(out / "results.jsonl", rows)
        print(f"{len(rows)}/{len(questions)} {q['question_id']} {row['status']}", flush=True)
    summary = summarize(rows)
    usage_rows = [r["generation_usage"] for r in rows]
    summary["generation_usage"] = {
        key: sum(u[key] for u in usage_rows) if all(u[key] is not None for u in usage_rows) else None
        for key in ("prompt_tokens", "completion_tokens", "estimated_cost")}
    summary["generation_usage"]["complete_usage_samples"] = sum(u["prompt_tokens"] is not None for u in usage_rows)
    summary["currency"] = args.currency
    batch_rows = [r.get("embedding_batches", []) for r in rows]
    summary["embedding_usage"] = {
        "total_tokens": sum(e["total_tokens"] for events in batch_rows for e in events)
            if all(events and all(e["total_tokens"] is not None for e in events) for events in batch_rows) else None,
        "estimated_cost": sum(e["estimated_cost"] for events in batch_rows for e in events)
            if all(events and all(e["estimated_cost"] is not None for e in events) for events in batch_rows) else None,
        "scope": "query embedding only; prior corpus indexing cost excluded"}

    write_json(out / "summary.json", summary)
    write_jsonl(out / "review-template.jsonl", [
        {"run_id": run_id, "question_id": r["question_id"], "sample_sha256": digest(r),
         "reviewer": "", "correctness": "", "citation_support": "", "faithfulness": "",
         "failure_stage": "", "notes": ""} for r in rows])
    # Dedicated filters independent of gold labels: source restriction and inaccessible engineering document.
    employee = LOCAL_IDENTITIES["u_employee_001"]
    filters = [{"source": "sample_data/knowledge_base/" + corpus["KB-NET-001"]["filename"]},
               {"document_ids": [d["document_id"] for d in docs if source_ids[d["source"]] == "KB-ENG-001"]}]
    probes = []
    for f in filters:
        result = await service.retrieve("VPN access", employee, top_k=args.top_k, filters=f)
        probes.append({"user_id": employee.user_id, "filters": f, "hits": result["hits"],
                       "checks": filter_checks(result["hits"], employee, corpus, source_ids, f)})
    write_json(out / "filter-probes.json", probes)
    if args.sampling:
        await sampling(rows, settings, args, out)
    manifest.update(status="completed", sample_count=len(rows), semantic_review="pending")
    write_json(out / "manifest.json", manifest)
    print(json.dumps({"run_directory": str(out), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    logging.getLogger("knowledgeops").setLevel(logging.WARNING)
    asyncio.run(main())
