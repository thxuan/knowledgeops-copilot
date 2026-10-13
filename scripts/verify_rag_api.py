"""Evaluate the configured local API; keys stay in the server process.

Default DEV run. Selected old test questions must be labeled regression, never a
fresh held-out result. Rubrics are stored locally for review and never sent to API.
"""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlparse
import urllib.request
import urllib.error
from uuid import uuid4
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evals.retrieval import measure
from scripts.compare_chunks import load_corpus


def request(base, route, user=None, body=None):
    headers = {"Content-Type": "application/json"}
    if user:
        headers["X-Local-User-Id"] = user
    req = urllib.request.Request(base + route, headers=headers,
          data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
    with urllib.request.urlopen(req, timeout=200) as response:
        return json.loads(response.read())


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:18080")
    parser.add_argument("--scope", choices=("dev", "regression"), default="dev")
    parser.add_argument("--question-ids", nargs="+")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--retrieval-profile", choices=("ranked", "coverage", "multi_query"), default="ranked")
    args = parser.parse_args()
    url = urlparse(args.api_url)
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost"}
            or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
        parser.error("Use the local HTTP API without credentials in its URL")
    if not 1 <= args.top_k <= 20:
        parser.error("top-k must be 1..20")
    base = args.api_url.rstrip("/")
    schema = request(base, "/openapi.json")
    if "coverage_review" not in schema["components"]["schemas"]["ChatContextResponse"]["properties"]:
        parser.error("API does not expose the new evidence/review diagnostics; use the updated app")
    questions = [json.loads(x) for x in (ROOT / "eval_data/questions.jsonl").read_text(encoding="utf-8").splitlines()]
    if args.question_ids:
        selected = [q for q in questions if q["question_id"] in args.question_ids]
        if {q["question_id"] for q in selected} != set(args.question_ids):
            parser.error("Unknown question ID")
    else:
        selected = [q for q in questions if q["split"] == "dev"]
    if args.scope == "dev" and any(q["split"] != "dev" for q in selected):
        parser.error("Previously seen test questions require --scope regression")
    if args.scope == "regression" and not args.question_ids:
        parser.error("Specify known cases for a regression run")
    corpus = load_corpus()
    source_ids = {"sample_data/knowledge_base/" + d["filename"]: key for key, d in corpus.items()}
    anchors = [json.loads(x) for x in (ROOT / "eval_data/day4-source-spans.jsonl").read_text(encoding="utf-8").splitlines()]
    run_id = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    out = ROOT / "docs/experiments/cross-document" / run_id
    out.mkdir(parents=True, exist_ok=False)
    write(out / "manifest.json", {"run_id": run_id, "scope": args.scope, "api_url": base,
         "top_k": args.top_k, "retrieval_profile": args.retrieval_profile, "question_ids": [q["question_id"] for q in selected],
         "source": "Fresh requests to local configured API; review labels are not sent to model",
         "repeat": 1, "usage_scope": "API does not expose provider token usage or billed costs",
         "code_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted(ROOT.glob("app/**/*.py"))}})
    rows = []
    for q in selected:
        row = {"question_id": q["question_id"], "question": q["question"], "answerable": q["answerable"],
               "question_type": q["question_type"], "expected_answer_points": q["expected_answer_points"],
               "status": "error", "retrieval_metrics": {}, "input_evidence_metrics": {}}
        user = q["allowed_user_ids"][0]
        try:
            retrieval = request(base, "/retrieve", user, {"query": q["question"], "top_k": args.top_k, "retrieval_profile": args.retrieval_profile})
            started = time.perf_counter()
            response = request(base, "/chat", user, {"message": q["question"], "top_k": args.top_k, "retrieval_profile": args.retrieval_profile})
            row["chat_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
            if response["model_provider"] != "compatible":
                raise ValueError("Real model validation requires compatible generation")
            row.update(status=response["status"], response=response, retrieval=retrieval)
            row["retrieval_metrics"] = measure(q, retrieval["hits"], anchors, source_ids)
            by_id = {h["chunk_id"]: h for h in retrieval.get("diagnostics", {}).get("candidates", retrieval["hits"])}
            ids = response["context"]["evidence_chunk_ids"]
            if not set(ids) <= set(by_id):
                raise ValueError("Cannot correlate retrieved candidates with actual context; index may have changed")
            evidence = [by_id[cid] for cid in ids]
            row["input_evidence"] = evidence
            row["input_evidence_metrics"] = measure(q, evidence, anchors, source_ids)
            row["citation_checks"] = [c["chunk_id"] in ids for c in response["citations"]]
        except urllib.error.HTTPError as exc:
            row["http_status"] = exc.code
            try:row["error"] = json.loads(exc.read())
            except ValueError:row["error"] = {"code": "NON_JSON_ERROR"}
        except (ValueError, TimeoutError, urllib.error.URLError) as exc:
            row["status"] = "error"
            row["error"] = {"code": type(exc).__name__}
        rows.append(row)
        write(out / "results.json", rows)
        print(f"{len(rows)}/{len(selected)} {q['question_id']} {row['status']}", flush=True)
    valid = [r for r in rows if r["answerable"] and r["input_evidence_metrics"]]
    cross = [r for r in valid if r["question_type"] == "cross_document"]
    summary = {"scope": args.scope, "samples": len(rows),
        "status_counts": {s: sum(r["status"] == s for r in rows) for s in ("answered", "refused", "rejected", "error")},
        "input_evidence_source_recall": sum(r["input_evidence_metrics"]["source_recall_at_k"] for r in valid)/len(valid) if valid else None,
        "input_evidence_section_coverage": sum(r["input_evidence_metrics"]["section_char_coverage_at_k"] for r in valid)/len(valid) if valid else None,
        "input_evidence_metric_samples": len(valid),
        "cross_all_sources": sum(r["input_evidence_metrics"]["all_required_sources_recalled"] for r in cross),
        "cross_samples": len(cross), "semantic_correctness": None, "token_usage": None, "cost": None,
        "review_completed": sum(r.get("response", {}).get("context", {}).get("coverage_review") == "completed" for r in rows)}
    write(out / "summary.json", summary)
    write(out / "review-template.json", [{"question_id": r["question_id"],
          "sample_sha256": hashlib.sha256(json.dumps(r, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
          "expected_answer_points": r["expected_answer_points"], "correctness": None, "citation_support": None,
          "reviewer": None, "notes": None} for r in rows])
    print(json.dumps({"output": str(out), **summary}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
