"""Online display of existing frozen dev annotations; never grades arbitrary prompts."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from app.evals.retrieval import measure, validate_annotations
from app.ingestion.parser import PARSER_VERSION, clean_document, parse_document

ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def fixture():
    corpus = {}
    for file in sorted((ROOT / "sample_data/knowledge_base").glob("*.md")):
        parsed = clean_document(parse_document(file.name, file.read_bytes()))
        corpus[parsed.metadata["document_id"]] = {"text": parsed.text, "filename": file.name,
            "version": parsed.metadata["version"], "parser_version": PARSER_VERSION + ":md"}
    qpath, apath = ROOT / "eval_data/questions.jsonl", ROOT / "eval_data/day4-source-spans.jsonl"
    questions = [json.loads(line) for line in qpath.read_text(encoding="utf-8").splitlines() if line.strip()]
    questions = [q for q in questions if q["split"] == "dev"]
    anchors = [json.loads(line) for line in apath.read_text(encoding="utf-8").splitlines() if line.strip()]
    validate_annotations(questions, anchors, corpus)
    source_ids = {"sample_data/knowledge_base/" + doc["filename"]: key for key, doc in corpus.items()}
    digest = hashlib.sha256(qpath.read_bytes() + apath.read_bytes() +
        json.dumps(corpus, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return questions, anchors, corpus, source_ids, digest


def available_questions(identity):
    questions, _, _, _, digest = fixture()
    return {"dataset": "dev", "fingerprint": digest, "questions": [
        {k: q[k] for k in ("question_id", "question", "question_type", "answerable")}
        for q in questions if identity.user_id in q["allowed_user_ids"]]}


def selected_question(question_id, message, identity, conversation_id):
    if not question_id:
        return None, "自由提问没有标准来源标注。"
    questions, *_ = fixture()
    q = next((q for q in questions if q["question_id"] == question_id), None)
    if q is None or identity.user_id not in q["allowed_user_ids"]:
        return None, "当前身份没有可用的该题标注。"
    if message.strip() != q["question"].strip() or conversation_id is not None:
        return None, "题目已修改或包含会话历史，不能沿用单题标准标注。"
    return q, None


def evaluate_trace(question, trace, identity, document_repository):
    pending = {"answer_correctness": None, "citation_support": None,
               "semantic_status": "pending_review", "sample_count": 1}
    if question is None:
        return {"status": "unavailable", "stages": {}, **pending}
    _, anchors, corpus, source_ids, digest = fixture()
    # Check the live expected sources against frozen coordinates and current ACL.
    # Source IDs alone are insufficient when documents have been edited or revoked.
    with document_repository.ingestion.connection() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for source in question["expected_sources"]:
            logical_id = source["document_id"]
            doc = corpus[logical_id]
            source_name = "sample_data/knowledge_base/" + doc["filename"]
            rows = db.execute("""SELECT d.version,d.text,d.parser_version FROM documents d
                JOIN document_access a USING(document_id)
                JOIN document_chunk_heads h USING(document_id)
                JOIN ingestion_jobs j USING(document_id)
                WHERE d.tenant_id=%s AND a.user_id=%s AND d.source=%s AND d.active=1 AND j.status='succeeded'""",
                (identity.tenant_id, identity.user_id, source_name)).fetchall()
            if len(rows) != 1 or any(rows[0][key] != doc[key] for key in ("version", "text", "parser_version")):
                return {"status": "stale_fixture", "reason": "标准文档缺失、已更新或当前身份无权访问。", "stages": {}, **pending}
    stages = {}
    try:
        for name, hits in trace["stages"].items():
            unique = list({h["chunk_id"]: h for h in reversed(hits)}.values())[::-1]
            value = measure(question, unique, anchors, source_ids)
            relevant = sum(bool(measure(question, [h], anchors, source_ids)["matched_sources"]) for h in unique)
            value.update(precision_at_k=relevant / len(unique) if unique and question["answerable"] else None,
                         relevant_count=relevant,
                         expected_source_count=len(question["expected_sources"]))
            stages[name] = value
    except ValueError:
        return {"status": "stale_fixture", "reason": "返回来源与冻结标注的版本或坐标不一致。", "stages": {}, **pending}
    return {"status": "measured", "question_id": question["question_id"], "question_type": question["question_type"],
        "answerable": question["answerable"], "dataset": "dev", "dataset_fingerprint": digest,
        "stages": stages, "reference_points": question["expected_answer_points"],
        "precision_scope": "切块与已标注必要章节有坐标交集；不是逐句语义相关性评审。",
        "cross_document_scope": "仅 cross_document 题统计所有必要来源全部召回。", **pending}


def corpus_snapshot(document_repository, identity):
    with document_repository.ingestion.connection() as db:
        rows = db.execute("""SELECT d.document_id,d.source,d.version,d.parser_version,h.chunk_set_id,s.text_hash,char_length(d.text) AS text_length
            FROM documents d JOIN document_access a USING(document_id)
            JOIN document_chunk_heads h USING(document_id) JOIN chunk_sets s ON s.chunk_set_id=h.chunk_set_id
            JOIN ingestion_jobs j ON j.document_id=d.document_id
            WHERE d.tenant_id=%s AND a.user_id=%s AND d.active=1 AND j.status='succeeded'
            ORDER BY d.document_id""", (identity.tenant_id, identity.user_id)).fetchall()
    docs = [dict(row) for row in rows]
    return {"documents": docs, "fingerprint": hashlib.sha256(
        json.dumps(docs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
