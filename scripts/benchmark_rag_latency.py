"""Independent local API timings for four profiles; fresh conversation per request."""
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time
from urllib.parse import urlparse
from uuid import uuid4
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_rag_api import request, write

PROFILES = [("ranked@8", "ranked", 8), ("coverage@8", "coverage", 8),
            ("multi_query@8", "multi_query", 8), ("ranked@20", "ranked", 20)]


def latency(values):
    if not values:
        return {"count": 0, "mean_ms": None, "median_ms": None, "p95_ms": None}
    values = sorted(values)
    return {"count": len(values), "mean_ms": statistics.mean(values),
            "median_ms": statistics.median(values), "p95_ms": values[math.ceil(.95 * len(values)) - 1]}


def summarize(rows):
    result = {}
    for label, _, _ in PROFILES:
        group = [r for r in rows if r["profile"] == label]
        ok = [r for r in group if r["http_status"] == "ok"]
        answered = [r for r in ok if r["status"] == "answered"]
        total_usage = {k: sum(c.get("usage", {}).get(k, 0) for r in ok for c in r["response"]["context"]["model_usage"])
                       for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        planning_usage = {k: sum(r["response"]["context"]["retrieval_diagnostics"].get("planning", {}).get("usage", {}).get(k, 0) for r in ok)
                          for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        result[label] = {"samples": len(group), "http_errors": len(group)-len(ok),
            "status_counts": {s: sum(r["status"] == s for r in group) for s in ("answered", "refused", "rejected", "error")},
            "end_to_end": latency([r["client_duration_ms"] for r in ok]),
            "all_attempts_end_to_end": latency([r["client_duration_ms"] for r in group]),
            "answered_end_to_end": latency([r["client_duration_ms"] for r in answered]),
            "retrieval": latency([r["response"]["context"]["timings_ms"]["retrieval"] for r in ok]),
            "generation": latency([r["response"]["context"]["timings_ms"]["generation"] for r in ok]),
            "generation_calls": sum(len(r["response"]["context"]["model_usage"]) for r in ok),
            "generation_usage": total_usage, "planning_usage": planning_usage,
            "usage_incomplete_calls": sum(not c.get("usage") for r in ok for c in r["response"]["context"]["model_usage"]),
            "planning_calls": sum(r["response"]["context"]["retrieval_diagnostics"].get("planning", {}).get("model_calls", 0) for r in ok),
            "mean_input_estimate": statistics.mean(r["response"]["context"]["input_token_estimate"] for r in ok) if ok else None}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:18080")
    parser.add_argument("--question-ids", nargs="+")
    args = parser.parse_args()
    url = urlparse(args.api_url)
    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1"} or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        parser.error("Use local HTTP API without URL credentials")
    base = args.api_url.rstrip("/")
    schema = request(base, "/openapi.json")
    if "timings_ms" not in schema["components"]["schemas"]["ChatContextResponse"]["properties"]:
        parser.error("Updated server timings required")
    questions = [json.loads(l) for l in (ROOT / "eval_data/questions.jsonl").read_text(encoding="utf-8").splitlines()]
    questions = [q for q in questions if q["split"] == "dev" and (not args.question_ids or q["question_id"] in args.question_ids)]
    if not questions or args.question_ids and set(args.question_ids) != {q["question_id"] for q in questions}:
        parser.error("Use existing DEV questions only")
    out = ROOT / "docs/experiments/latency" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    write(out / "manifest.json", {"scope": "Independent fresh /chat request for each question and profile; one repeat, sequential requests, rotated profile order",
        "api_url": base, "profiles": PROFILES, "question_ids": [q["question_id"] for q in questions],
        "no_history": True, "semantic_quality_judged": False,
        "code_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob("app/**/*.py")}})
    rows = []
    print("output", out, flush=True)
    for index, q in enumerate(questions):
        offset = index % len(PROFILES)
        order = PROFILES[offset:] + PROFILES[:offset]
        for label, profile, top_k in order:
            started = time.perf_counter()
            row = {"question_id": q["question_id"], "question_type": q["question_type"], "answerable": q["answerable"],
                   "profile": label, "status": "error", "http_status": "error"}
            try:
                response = request(base, "/chat", q["allowed_user_ids"][0],
                    {"message": q["question"], "top_k": top_k, "retrieval_profile": profile})
                row["client_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
                if response["model_provider"] != "compatible" or response["context"]["answer_mode"] != "generated":
                    raise ValueError("Require real model with natural language answers")
                row.update(http_status="ok", status=response["status"], response=response)
            except Exception as exc:
                row["client_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
                row["error_type"] = type(exc).__name__
            rows.append(row)
            write(out / "results.json", rows)
            write(out / "summary.json", summarize(rows))
            print(f"{len(rows)}/{len(questions)*4} {q['question_id']} {label} {row['status']} {row['client_duration_ms']/1000:.2f}s", flush=True)
    print(json.dumps({"output": str(out), "summary": summarize(rows)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
