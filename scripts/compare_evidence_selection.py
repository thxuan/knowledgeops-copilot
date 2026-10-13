"""Fresh authorized retrieval; reuse pools for fixed-K selection ablations. No answer calls."""
import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_rag_api import request, write
from scripts.compare_chunks import load_corpus
from app.retrieval.coverage import select_evidence, SELECTOR_VERSION
from app.generation.context import build_context
from app.generation.provider import GenerationSettings
from app.evals.retrieval import measure
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:18080")
    parser.add_argument("--question-ids", nargs="+")
    parser.add_argument("--profile", choices=["coverage", "multi_query"], default="multi_query")
    args = parser.parse_args()
    url = urlparse(args.api_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost"} or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
        parser.error("Use local HTTP API without URL credentials")
    questions = [json.loads(l) for l in (ROOT / "eval_data/questions.jsonl").read_text(encoding="utf-8").splitlines()]
    questions = [q for q in questions if q["split"] == "dev" and (not args.question_ids or q["question_id"] in args.question_ids)]
    if not questions or args.question_ids and set(args.question_ids) != {q["question_id"] for q in questions}:
        parser.error("Use existing DEV questions only")
    corpus = load_corpus()
    source_ids = {"sample_data/knowledge_base/" + d["filename"]: key for key, d in corpus.items()}
    anchors = [json.loads(l) for l in (ROOT / "eval_data/day4-source-spans.jsonl").read_text(encoding="utf-8").splitlines()]
    out = ROOT / "docs/experiments/evidence-selection" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    settings = GenerationSettings()
    write(out / "manifest.json", {"scope": "DEV retrieval/context ablation; one repeat; no generated answers or semantic citation score", "profile": args.profile,
        "api_url": args.api_url, "candidate_k_per_query": 20, "selector_version": SELECTOR_VERSION,
        "settings": asdict(settings), "code_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob("app/**/*.py")}})
    rows = []
    for q in questions:
        row = {"question_id": q["question_id"], "question_type": q["question_type"], "answerable": q["answerable"], "status": "error"}
        try:
            result = request(args.api_url.rstrip("/"), "/retrieve", q["allowed_user_ids"][0], {"query": q["question"], "top_k": 20,
                "candidate_k": 20, "retrieval_profile": args.profile})
            d = result["diagnostics"]
            pool = d["candidates"]
            by_id = {h["chunk_id"]: h for h in pool}
            original = [by_id[cid] for cid in d["original_candidate_ids"]]
            row.update(status="ok", retrieval=result, variants={})
            for k in (5, 8, 10, 20):
                variants = {"ranked": original[:k], "coverage": select_evidence(q["question"], original, k),
                            "multi_query": select_evidence(q["question"], pool, k, d["queries"], d["original_candidate_ids"])}
                for profile, hits in variants.items():
                    ctx = build_context(q["question"], [], {}, hits, settings, preserve_order=profile != "ranked")
                    row["variants"][f"{profile}@{k}"] = {"metrics": measure(q, ctx.evidence, anchors, source_ids),
                        "input_estimate": ctx.input_estimate, "evidence_ids": [h["chunk_id"] for h in ctx.evidence],
                        "source_count": len({h["source"] for h in ctx.evidence})}
        except Exception as exc:
            row["error_type"] = type(exc).__name__
        rows.append(row)
        write(out / "results.json", rows)
        print(q["question_id"], row["status"], flush=True)
    summary = {"samples": len(rows), "errors": sum(r["status"] != "ok" for r in rows), "variants": {},
        "semantic_correctness": None, "embedding_usage_and_cost": None,
        "planner_calls": sum(r.get("retrieval", {}).get("diagnostics", {}).get("planning", {}).get("model_calls", 0) for r in rows)}
    for key in (f"{p}@{k}" for k in (5, 8, 10, 20) for p in ("ranked", "coverage", "multi_query")):
        valid = [r["variants"][key] for r in rows if r["status"] == "ok" and r["answerable"]]
        cross = [r["variants"][key] for r in rows if r["status"] == "ok" and r["question_type"] == "cross_document"]
        summary["variants"][key] = {"answerable_samples": len(valid), "cross_samples": len(cross),
            "source_recall": sum(v["metrics"]["source_recall_at_k"] for v in valid)/len(valid) if valid else None,
            "section_coverage": sum(v["metrics"]["section_char_coverage_at_k"] for v in valid)/len(valid) if valid else None,
            "cross_all_sources": sum(v["metrics"]["all_required_sources_recalled"] for v in cross),
            "mean_input_estimate": sum(v["input_estimate"] for v in valid)/len(valid) if valid else None}
    write(out / "summary.json", summary)
    print(json.dumps({"output": str(out), **summary}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
