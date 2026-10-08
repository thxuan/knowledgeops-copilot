"""Offline keyword diagnostic; source annotations never enter ranking."""

import math
import re
from collections import Counter

from app.ingestion.chunker import ChunkConfig, chunk_document, markdown_sections, text_revision


def terms(text: str) -> set[str]:
    result = set(re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", text.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            result.add(run)
    return result


def union_length(ranges: list[tuple[int, int]]) -> int:
    end, total = -1, 0
    for start, stop in sorted(ranges):
        if stop > start:
            total += max(0, stop - max(start, end))
            end = max(end, stop)
    return total


def resolve_anchors(corpus: dict, questions: list[dict], split: str = "dev") -> list[dict]:
    """Map Day 1 section labels once, independently of any chunk configuration."""
    if split not in {"dev", "test"}:
        raise ValueError("Annotation split must be dev or test")
    anchors = []
    for q in questions:
        if q["split"] != split:
            continue
        for source in q["expected_sources"]:
            doc = corpus[source["document_id"]]
            if doc["version"] != source["version"]:
                raise ValueError("Source version mismatch")
            for label in source["section"].split("；"):
                target = re.sub(r"^\d+\.\s*", "", label.split("/")[-1].strip())
                sections = [s for s in markdown_sections(doc["text"]) if s.heading_path]
                heading = lambda s: re.sub(r"^\d+\.\s*", "", s.heading_path[-1])
                # Prefer an exact section title over a document-title prefix match.
                exact = [s for s in sections if heading(s) == target]
                candidates = exact or [s for s in sections if heading(s).startswith(target)]
                if len(candidates) != 1:
                    raise ValueError(f"Ambiguous source section: {q['question_id']} / {label}")
                section = candidates[0]
                anchors.append({"question_id": q["question_id"], "document_id": source["document_id"],
                                "version": source["version"], "section": label,
                                "start_char": section.start, "end_char": section.subtree_end,
                                "text_hash": text_revision(doc["text"]), "parser_version": doc["parser_version"]})
    return anchors


def compare(corpus: dict, questions: list[dict], anchors: list[dict], config: ChunkConfig,
            top_k: int = 5) -> dict:
    questions = [q for q in questions if q["split"] == "dev"]
    for a in anchors:
        doc = corpus[a["document_id"]]
        if a["text_hash"] != text_revision(doc["text"]) or a["version"] != doc["version"]:
            raise ValueError("Frozen source coordinates no longer match the corpus")
        if a["parser_version"] != doc["parser_version"]:
            raise ValueError("Source parser configuration changed")
        if not 0 <= a["start_char"] < a["end_char"] <= len(doc["text"]):
            raise ValueError("Invalid source coordinates")
    chunks = [c for doc_id, doc in sorted(corpus.items()) for c in chunk_document(
        doc_id, doc["text"], title=doc["title"], source=doc["filename"], version=doc["version"],
        parser_version=doc["parser_version"], config=config)]
    tokens = {c.chunk_id: terms(c.title + " " + " ".join(" ".join(p) for p in c.heading_paths) + " " + c.text)
              for c in chunks}
    frequency = Counter(t for ts in tokens.values() for t in ts)
    details = []
    for q in questions:
        # This manifest is a trusted fixture, not an ACL extracted from uploaded content.
        user = q["allowed_user_ids"][0]
        allowed = [c for c in chunks if user in corpus[c.document_id]["allowed_user_ids"]]
        query_terms = terms(q["question"])
        def score(c):
            ts = tokens[c.chunk_id]
            return sum(math.log(1 + len(chunks) / (1 + frequency[t])) for t in query_terms & ts) / math.sqrt(max(1, len(ts)))
        ranked = sorted(((score(c), c) for c in allowed), key=lambda x: (-x[0], x[1].document_id, x[1].ordinal))
        selected = [(s, c) for s, c in ranked if s > 0][:top_k]
        qa = [a for a in anchors if a["question_id"] == q["question_id"]]
        expected = {s["document_id"] for s in q["expected_sources"]}
        if q["answerable"] and {a["document_id"] for a in qa} != expected:
            raise ValueError("Missing source-level annotations")
        covered, required, hit_sources = 0, 0, set()
        unique_context, context_evidence = 0, 0
        for doc_id in {c.document_id for _, c in selected} | expected:
            retrieved = [(c.start_char, c.end_char) for _, c in selected if c.document_id == doc_id]
            gold = [(a["start_char"], a["end_char"]) for a in qa if a["document_id"] == doc_id]
            intersections = [(max(a, x), min(b, y)) for a, b in retrieved for x, y in gold if max(a, x) < min(b, y)]
            matched = union_length(intersections)
            if matched:
                hit_sources.add(doc_id)
            covered += matched
            required += union_length(gold)
            unique_context += union_length(retrieved)
            context_evidence += matched
        chars = sum(len(c.text) for _, c in selected)
        details.append({
            "question_id": q["question_id"], "answerable": q["answerable"], "user_id": user,
            "source_recall_at_k": len(hit_sources) / len(expected) if expected else None,
            "section_char_coverage_at_k": covered / required if required else None,
            "context_chars": chars,
            "nonannotated_context_fraction": 1 - context_evidence / unique_context if required and unique_context else None,
            "duplicate_context_chars": chars - unique_context,
            "hits": [{"chunk_id": c.chunk_id, "document_id": c.document_id, "score": round(s, 6),
                      "start_char": c.start_char, "end_char": c.end_char, "text": c.text,
                      "heading_paths": c.heading_paths} for s, c in selected],
        })
    answered = [d for d in details if d["answerable"]]
    def mean(field):
        return sum(d[field] for d in answered) / len(answered)
    total_chars = sum(len(c.text) for c in chunks)
    body_chars = sum(len(d["text"]) for d in corpus.values())
    return {
        "config_id": config.config_id, "top_k": top_k, "split": "dev",
        "question_count": len(details), "answerable_count": len(answered),
        "unanswerable_count": len(details) - len(answered),
        "chunk_count": len(chunks), "stored_chars": total_chars,
        "storage_expansion": total_chars / body_chars,
        "hard_limit_chunks": sum(c.split_reason == "hard_limit" for c in chunks),
        "mean_source_recall_at_k": mean("source_recall_at_k"),
        "mean_section_char_coverage_at_k": mean("section_char_coverage_at_k"),
        "mean_context_chars": mean("context_chars"),
        "mean_nonannotated_context_fraction": mean("nonannotated_context_fraction"),
        "unanswerable_with_hits": sum(bool(d["hits"]) for d in details if not d["answerable"]),
        "details": details,
    }
