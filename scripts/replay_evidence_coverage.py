"""Replay saved DEV candidates through current context admission, without API/DB calls.

This is an offline context-window ablation, not a fresh retrieval or answer evaluation.
Gold labels are used only after selection to compute metrics. Old artifacts stay untouched.
"""
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
from app.evals.retrieval import measure
from app.generation.context import build_context, SYSTEM_PROMPT
from app.generation.provider import GenerationSettings
from scripts.compare_chunks import load_corpus


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.top_k <= 20:
        parser.error("top-k must be 1..20")
    corpus = load_corpus()
    source_ids = {"sample_data/knowledge_base/" + d["filename"]: key for key, d in corpus.items()}
    questions = {q["question_id"]: q for q in read(ROOT / "eval_data/questions.jsonl") if q["split"] == "dev"}
    anchors = read(ROOT / "eval_data/day4-source-spans.jsonl")
    before = {r["question_id"]: r for r in read(args.baseline)}
    candidates = {r["question_id"]: r for r in read(args.candidates)
                  if r["strategy"] == "hybrid" and r["repeat"] == 1 and r["status"] == "ok"}
    if set(questions) != set(before) or set(questions) != set(candidates):
        parser.error("Use complete matching DEV baseline and candidates")
    rows = []
    settings = GenerationSettings()
    for qid, q in questions.items():
        old = before[qid]["input_evidence"]
        context = build_context(q["question"], [], {}, candidates[qid]["result"]["diagnostics"]["candidates"][:args.top_k], settings)
        # Evaluation after selection; no expected sources/points are passed to build_context.
        rows.append({"question_id": qid, "answerable": q["answerable"], "question_type": q["question_type"],
                     "before": measure(q, old, anchors, source_ids),
                     "after": measure(q, context.evidence, anchors, source_ids),
                     "before_count": len(old), "after_count": len(context.evidence),
                     "input_token_estimate": context.input_estimate,
                     "evidence_chunk_ids": [h["chunk_id"] for h in context.evidence],
                     "omitted_evidence_ids": context.omitted_evidence_ids})
    def summary(field):
        valid = [r[field] for r in rows if r["answerable"]]
        cross = [r[field] for r in rows if r["question_type"] == "cross_document"]
        return {"answerable_samples": len(valid), "source_recall": sum(r["source_recall_at_k"] for r in valid)/len(valid),
                "section_char_coverage": sum(r["section_char_coverage_at_k"] for r in valid)/len(valid),
                "cross_all_sources": sum(r["all_required_sources_recalled"] for r in cross), "cross_samples": len(cross)}
    out = args.output or ROOT / "docs/experiments/cross-document" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    manifest = {"scope": "Offline replay of historical real DEV candidates; no new retrieval/model calls or answer correctness score",
                "baseline": str(args.baseline.resolve()), "candidates": str(args.candidates.resolve()),
                "baseline_sha256": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
                "candidates_sha256": hashlib.sha256(args.candidates.read_bytes()).hexdigest(),
                "selection": "document-round-robin-v1", "top_k": args.top_k,
                "settings": asdict(settings), "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()}
    result = {"before": summary("before"), "after": summary("after"),
              "cloud_calls": 0, "semantic_correctness": None,
              "max_input_estimate": max(r["input_token_estimate"] for r in rows),
              "cross_cases": [r for r in rows if r["question_type"] == "cross_document"]}
    for name, data in [("manifest.json", manifest), ("summary.json", result), ("results.json", rows)]:
        (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(out.resolve()), **result}, ensure_ascii=True))


if __name__ == "__main__":
    main()
