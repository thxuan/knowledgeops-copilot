"""Dev-only exact/HNSW benchmark with saved plans and per-query observations."""
import argparse
import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.config import Settings
from app.core.embedding import make_embedding_client
from app.core.identity import LOCAL_IDENTITIES
from app.db.ingestion import IngestionRepository
from app.db.snapshots import database_label
from app.db.chunks import ChunkRepository
from app.db.vectors import VectorRepository
from app.retrieval.service import VectorService

def percentile(values, quantile):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, __import__("math").ceil(quantile * len(ordered)) - 1))]

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        raise SystemExit("repeats must be positive")
    settings = Settings.from_env()
    if not settings.vector_database_url:
        raise SystemExit("Set KNOWLEDGEOPS_DATABASE_URL")
    repo = VectorRepository(args.database_url or settings.database_url, settings.embedding)
    repo.initialize()
    started = time.perf_counter()
    repo.create_hnsw()
    index_setup_ms = (time.perf_counter() - started) * 1000
    service = VectorService(ChunkRepository(IngestionRepository(args.database_url or settings.database_url)), repo,
                            make_embedding_client(settings.embedding), settings.embedding)
    qpath = ROOT / "eval_data/questions.jsonl"
    questions = [json.loads(line) for line in qpath.read_text(encoding="utf-8").splitlines() if line.strip()]
    questions = [q for q in questions if q["split"] == "dev"]
    details, plans = [], {}
    for q in questions:
        identity = LOCAL_IDENTITIES[q["allowed_user_ids"][0]]
        allowed = service.allowed_sets(identity)
        vector = (await service._embed([q["question"]], "benchmark-" + q["question_id"] + "-" + str(time.time_ns()), 0, None, "benchmark"))[0]
        baseline = repo.search(vector, allowed, identity.tenant_id, 5)
        gold = {h["chunk_id"] for h in baseline}
        item = {"question_id": q["question_id"], "user_id": identity.user_id,
                "exact_chunk_ids": sorted(gold), "runs": []}
        for repeat in range(args.repeats):
            # Alternate order to reduce systematic warm-cache bias.
            modes = [("exact", 40), ("hnsw", 40), ("hnsw", 100)]
            if repeat % 2:
                modes.reverse()
            for mode, ef in modes:
                started = time.perf_counter()
                hits = repo.search(vector, allowed, identity.tenant_id, 5, mode, ef)
                elapsed = (time.perf_counter() - started) * 1000
                found = {h["chunk_id"] for h in hits}
                item["runs"].append({"mode": mode, "ef_search": ef, "repeat": repeat,
                    "db_call_ms": elapsed, "returned": len(hits), "chunk_ids": [h["chunk_id"] for h in hits],
                    "index_recall_at_5": len(gold & found) / len(gold) if gold else None})
        if not plans:
            plans = {mode: repo.search(vector, allowed, identity.tenant_id, 5, mode, 40, explain=True)
                     for mode in ["exact", "hnsw"]}
        details.append(item)
    summaries = []
    for mode, ef in [("exact", 40), ("hnsw", 40), ("hnsw", 100)]:
        runs = [r for d in details for r in d["runs"] if r["mode"] == mode and r["ef_search"] == ef]
        times = [r["db_call_ms"] for r in runs]
        recalls = [r["index_recall_at_5"] for r in runs if r["index_recall_at_5"] is not None]
        summaries.append({"mode": mode, "ef_search": ef, "sample_count": len(runs),
                          "p50_db_call_ms": statistics.median(times), "p95_db_call_ms": percentile(times, .95),
                          "mean_index_recall_at_5": statistics.mean(recalls) if recalls else None})
    with repo.connection() as db:
        version = db.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()["extversion"]
        count = db.execute("SELECT count(*) AS n FROM ko_embeddings WHERE collection=%s", (settings.embedding.collection,)).fetchone()["n"]
        snapshots = db.execute("""SELECT e.chunk_id, e.embedding::text AS vector_text, c.chunk_set_id, c.payload
            FROM ko_embeddings e JOIN ko_chunks c USING(chunk_id) WHERE e.collection=%s ORDER BY e.chunk_id""",
            (settings.embedding.collection,)).fetchall()
    corpus = {row["payload"]["document_id"]: {"document_id":row["payload"]["document_id"],
              "version":row["payload"]["version"], "text_hash":row["payload"]["text_hash"],
              "config_id":row["payload"]["config_id"], "chunk_set_id":row["chunk_set_id"]} for row in snapshots}
    vector_hash = hashlib.sha256(json.dumps([(r["chunk_id"],r["vector_text"]) for r in snapshots]).encode()).hexdigest()
    report = {"run_date": datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
              "signature": settings.embedding.signature, "collection": settings.embedding.collection,
              "pgvector_version": version, "indexed_vectors": count, "question_count": len(questions), "split": "dev",
              "repeats": args.repeats, "index_setup_ms": index_setup_ms, "index_name": repo.index_name,
              "scope": "DB call includes connection and config checks; excludes query embedding. Recall compares ANN IDs to exact IDs, not answer evidence. Timing is local warm/small-corpus evidence only.",
              "corpus": list(corpus.values()), "vectors_sha256": vector_hash,
              "questions_sha256": hashlib.sha256(qpath.read_bytes()).hexdigest(),
              "code_sha256": {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                ["app/core/embedding.py", "app/db/vectors.py", "app/retrieval/service.py", "scripts/compare_vector_indexes.py"]},
              "summaries": summaries, "plans": plans, "details": details}
    directory = ROOT / "docs/experiments"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "day5-vector-indexes.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = ["# Day 5 向量索引实验（助手运行）", "", f"日期：{report['run_date']}。pgvector {version}；集合 {settings.embedding.collection}；{count} 个向量。",
            f"20 条开发题，每种配置重复 {args.repeats} 次，Top K=5。保留测试集未运行。", "",
            "| 搜索 | ef_search | 样本数 | DB 调用 P50 ms | P95 ms | ANN 对精确结果 Recall@5 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for s in summaries:
        rows.append(f"| {s['mode']} | {s['ef_search']} | {s['sample_count']} | {s['p50_db_call_ms']:.3f} | {s['p95_db_call_ms']:.3f} | {s['mean_index_recall_at_5']:.3f} |")
    plan_used = repo.index_name in json.dumps(plans["hnsw"])
    rows += ["", f"HNSW 查询计划中出现目标索引：{plan_used}。精确模式禁用 IndexScan/BitmapScan；HNSW 模式仅在该事务中禁用 SeqScan，便于小数据上验证索引路径。",
             "计时包含连接、配置校验及 SQL，排除查询 Embedding；不是 API 全链路延迟。",
             "Embedding 调用使用已配置的真实模型；本报告不统计其费用，不能据此认为费用为零。",
             "ANN Recall 的标注是精确搜索返回的 Chunk ID；不等于 Day 4 的来源 Recall，更不等于答案准确率。距离同分边界可能使 ID 重叠下降。",
             "小语料和本机计时不支持声称 HNSW 一定更快；选择默认精确搜索，HNSW 保留作对照。",
             "", "原始结果、代码指纹、查询计划：[day5-vector-indexes.json](day5-vector-indexes.json)。"]
    (directory / "day5-vector-indexes.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(json.dumps(summaries, indent=2))
    if not plan_used:
        raise SystemExit("HNSW plan did not use the target index; inspect the saved plan before claiming ANN results")

if __name__ == "__main__":
    asyncio.run(main())
