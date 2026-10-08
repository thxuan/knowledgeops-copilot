"""Business source recall: matching document/version AND frozen section coordinates."""
from app.evals.chunk_comparison import union_length


def measure(question, hits, anchors, source_ids):
    gold = [a for a in anchors if a["question_id"] == question["question_id"]]
    expected = {(s["document_id"], s["version"]) for s in question["expected_sources"]}
    if question["answerable"] and {(a["document_id"], a["version"]) for a in gold} != expected:
        raise ValueError("Missing source annotations")
    matched = set()
    first_rank = None
    intersections = {}
    for rank, hit in enumerate(hits, 1):
        citation = hit["citation"]
        logical_id = source_ids.get(hit["source"])
        location = citation["location"]
        for anchor in gold:
            if (logical_id, citation["version"]) != (anchor["document_id"], anchor["version"]):
                continue
            if citation["text_hash"] != anchor["text_hash"] or citation["parser_version"] != anchor["parser_version"]:
                raise ValueError("Frozen source coordinates changed")
            start = max(location["start_char"], anchor["start_char"])
            end = min(location["end_char"], anchor["end_char"])
            if start < end:
                key = (logical_id, citation["version"])
                matched.add(key)
                if first_rank is None:
                    first_rank = rank
                intersections.setdefault(key, []).append((start,end))
    required = {}
    for anchor in gold:
        required.setdefault((anchor["document_id"],anchor["version"]), []).append((anchor["start_char"],anchor["end_char"]))
    total = sum(union_length(ranges) for ranges in required.values())
    return {"source_recall_at_k": len(matched) / len(expected) if expected else None,
            "reciprocal_rank": (1 / first_rank if first_rank else 0) if expected else None,
            "all_required_sources_recalled": len(matched) == len(expected) if expected else None,
            "section_char_coverage_at_k": sum(union_length(ranges) for ranges in intersections.values()) / total if total else None,
            "matched_sources": sorted(matched), "retrieved_count": len(hits)}



def validate_annotations(questions, anchors, corpus):
    """Fail before paid calls when the selected split has incomplete/stale gold."""
    from app.ingestion.chunker import text_revision
    if not questions:
        raise ValueError("Selected evaluation split has no questions")
    ids = [q["question_id"] for q in questions]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate evaluation question IDs")
    for q in questions:
        qid = q["question_id"]
        gold = [a for a in anchors if a["question_id"] == qid]
        expected = {(s["document_id"], s["version"]) for s in q["expected_sources"]}
        if not q["answerable"]:
            if expected or gold:
                raise ValueError(f"Unanswerable question has gold sources: {qid}")
            continue
        if not expected or {(a["document_id"], a["version"]) for a in gold} != expected:
            raise ValueError(f"Missing source annotations: {qid}")
        expected_sections = {(s["document_id"], s["version"], label.strip())
            for s in q["expected_sources"] for label in s["section"].split("；")}
        actual_sections = {(a["document_id"], a["version"], a["section"].strip()) for a in gold}
        if actual_sections != expected_sections or len(gold) != len(actual_sections):
            raise ValueError(f"Missing or duplicate section annotations: {qid}")
        for a in gold:
            doc = corpus.get(a["document_id"])
            if doc is None or a["version"] != doc["version"] or a["text_hash"] != text_revision(doc["text"]):
                raise ValueError(f"Stale source annotations: {qid}")
            if a["parser_version"] != doc["parser_version"]:
                raise ValueError(f"Source parser changed: {qid}")
            start, end = a["start_char"], a["end_char"]
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(doc["text"]):
                raise ValueError(f"Invalid source coordinates: {qid}")
    # The same deterministic section mapping is required, not arbitrary in-range spans.
    from app.evals.chunk_comparison import resolve_anchors
    for split in {q["split"] for q in questions}:
        resolved = resolve_anchors(corpus, questions, split=split)
        relevant = [a for a in anchors if a["question_id"] in {q["question_id"] for q in questions if q["split"] == split}]
        key = lambda a: (a["question_id"], a["document_id"], a["section"])
        if sorted(resolved, key=key) != sorted(relevant, key=key):
            raise ValueError(f"Source section coordinates differ from frozen labels: {split}")
