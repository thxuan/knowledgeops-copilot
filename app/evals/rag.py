"""Deterministic aggregation; semantic judgments are explicit reviewed labels."""
import copy
import hashlib
import json
import math
import time
from app.core.errors import AppError


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


class RecordingGeneration:
    """Only instantiated by the fixture evaluation CLI; app logs stay unchanged."""
    def __init__(self, client):
        self.client = client
        self.calls = []

    async def generate(self, messages, settings):
        call = {"messages": copy.deepcopy(messages), "raw_output": None,
                "metadata": {}, "error_code": None}
        started = time.perf_counter()
        self.calls.append(call)
        try:
            raw = await self.client.generate(messages, settings)
            call["raw_output"] = str(raw)
            call["metadata"] = getattr(raw, "metadata", {})
            return raw
        except AppError as exc:
            call["error_code"] = exc.code
            raise
        except BaseException as exc:
            call["error_code"] = type(exc).__name__
            raise
        finally:
            call["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)


def generation_cost(calls, provider, input_price, output_price):
    usages = [c["metadata"].get("usage", {}) for c in calls]
    complete = bool(usages) and all(
        type(u.get(key)) is int for u in usages for key in ("prompt_tokens", "completion_tokens"))
    if not complete:
        return {"prompt_tokens": None, "completion_tokens": None, "estimated_cost": None}
    inputs = sum(u["prompt_tokens"] for u in usages)
    outputs = sum(u["completion_tokens"] for u in usages)
    cost = ((inputs * input_price + outputs * output_price) / 1_000_000
            if input_price is not None and output_price is not None else None)
    return {"prompt_tokens": inputs, "completion_tokens": outputs, "estimated_cost": cost}


def summarize(rows):
    answerable = [r for r in rows if r["answerable"]]
    unknown = [r for r in rows if not r["answerable"]]
    answered = [r for r in rows if r["status"] == "answered"]
    cross = [r for r in answerable if r["question_type"] == "cross_document"]
    latency = sorted(r["duration_ms"] for r in rows)
    def percentile(p):
        return latency[max(0, math.ceil(p * len(latency)) - 1)] if latency else None
    citations = [valid for r in answered for valid in r["citation_checks"]]
    return {
        "sample_count": len(rows), "answerable_count": len(answerable),
        "unanswerable_count": len(unknown),
        "status_counts": {s: sum(r["status"] == s for r in rows)
                          for s in ("answered", "refused", "rejected", "error")},
        "mean_source_recall_at_k": mean(r["retrieval"].get("source_recall_at_k") for r in answerable),
        "mrr_at_k": mean(r["retrieval"].get("reciprocal_rank") for r in answerable),
        "mean_section_char_coverage_at_k": mean(r["retrieval"].get("section_char_coverage_at_k") for r in answerable),
        "cross_document_all_sources_rate": mean(r["retrieval"].get("all_required_sources_recalled") for r in cross),
        "cross_document_count": len(cross),
        "input_evidence_source_recall": mean(r.get("input_evidence_metrics", {}).get("source_recall_at_k") for r in answerable),
        "input_evidence_section_coverage": mean(r.get("input_evidence_metrics", {}).get("section_char_coverage_at_k") for r in answerable),
        "input_evidence_cross_document_all_sources_rate": mean(r.get("input_evidence_metrics", {}).get("all_required_sources_recalled") for r in cross),
        "input_evidence_metric_count": sum(bool(r.get("input_evidence_metrics")) for r in answerable),
        "retrieval_metric_count": sum(bool(r["retrieval"]) for r in answerable),
        "retrieval_metric_denominator": "answerable samples with completed retrieval; errors separately counted",
        "latency_sample_count": len(latency),
        "unanswerable_refusal_rate": mean(r["status"] == "refused" for r in unknown),
        "answerable_false_refusal_rate": mean(r["status"] == "refused" for r in answerable),
        "citation_validity_rate": mean(citations), "citation_count": len(citations),
        "filter_checked_hits": sum(len(r["filter_checks"]) for r in rows),
        "filter_correctness_rate": mean(v for r in rows for v in r["filter_checks"]),
        "p50_ms": percentile(.5), "p95_ms": percentile(.95),
        "latency_scope": "sequential, retrieval plus generation and commit; not a load test",
        "semantic_correctness": None, "citation_support": None, "faithfulness": None,
        "semantic_status": "pending reviewed labels",
    }


def review_summary(rows, reviews, run_id):
    by_id = {r["question_id"]: r for r in rows}
    seen = set()
    for review in reviews:
        qid = review["question_id"]
        if qid in seen or qid not in by_id:
            raise ValueError("Duplicate or unknown reviewed question")
        seen.add(qid)
        if review.get("run_id") != run_id or review.get("sample_sha256") != digest(by_id[qid]):
            raise ValueError("Review belongs to a different run/output")
        for key in ("correctness", "citation_support", "faithfulness"):
            if review.get(key) not in ("pass", "fail", "not_applicable"):
                raise ValueError("Review labels must be pass/fail/not_applicable")
        if not review.get("reviewer") or not review.get("notes"):
            raise ValueError("Review needs reviewer and rationale")
    result = {"reviewed_count": len(reviews), "meets_minimum_ten": len(reviews) >= 10}
    for key in ("correctness", "citation_support", "faithfulness"):
        labels = [r[key] for r in reviews if r[key] != "not_applicable"]
        result[key] = {"passed": labels.count("pass"), "denominator": len(labels),
                       "rate": mean(v == "pass" for v in labels)}
    return result
