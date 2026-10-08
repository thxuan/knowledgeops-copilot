from app.evals.chunk_comparison import compare, resolve_anchors, terms, union_length
from app.ingestion.chunker import ChunkConfig


def test_keyword_identifiers_and_union_do_not_double_count_overlap():
    assert {"tl-403", "23h2", "合规"} <= terms("TL-403 Windows 23H2 不合规")
    assert union_length([(0, 10), (5, 15), (20, 25)]) == 20


def test_source_hits_require_position_overlap_and_ignore_holdout():
    corpus = {"doc": {"text": "# Guide\n\n## 1. Alpha\nneedle alpha\n\n## 2. Beta\nother rule\n",
                       "title": "Guide", "filename": "guide.md", "version": "1",
                       "parser_version": "test", "allowed_user_ids": ["user"]}}
    dev = {"question_id": "q", "split": "dev", "question": "needle", "answerable": True,
           "allowed_user_ids": ["user"],
           "expected_sources": [{"document_id": "doc", "version": "1", "section": "2. Beta"}]}
    holdout = {**dev, "question_id": "held", "split": "test"}
    anchors = resolve_anchors(corpus, [dev, holdout])
    assert {a["question_id"] for a in anchors} == {"q"}
    result = compare(corpus, [dev, holdout], anchors, ChunkConfig("structure", 100, 0), top_k=1)
    assert result["question_count"] == 1
    assert result["mean_source_recall_at_k"] == 0
    assert result["mean_section_char_coverage_at_k"] == 0
