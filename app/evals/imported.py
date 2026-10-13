"""Owner-scoped JSONL datasets. Gold is consumed only after generation."""
import hashlib
import json
from uuid import uuid4
from functools import lru_cache
from collections import Counter
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from app.core.errors import AppError
from app.evals.chunk_comparison import union_length

class Source(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    document_id: str | None = Field(default=None, min_length=1, max_length=300)
    source: str | None = Field(default=None, min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=100)
    @model_validator(mode="after")
    def identifier(self):
        if not self.document_id and not self.source:
            raise ValueError("source or document_id required")
        return self

class Span(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    question_id: str | None = Field(default=None, max_length=32)
    document_id: str | None = Field(default=None, max_length=300)
    source: str | None = Field(default=None, max_length=500)
    version: str = Field(min_length=1, max_length=100)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    text_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser_version: str = Field(min_length=1, max_length=100)
    @model_validator(mode="after")
    def coordinates(self):
        if self.end_char <= self.start_char or not (self.document_id or self.source):
            raise ValueError("Invalid source span")
        return self

class Question(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    question_id: str = Field(min_length=1, max_length=32)
    question: str = Field(min_length=1, max_length=4000)
    split: str = Field(default="imported", min_length=1, max_length=40)
    question_type: str = Field(default="fact", max_length=40)
    answerable: bool | None = None
    expected_sources: list[Source] = Field(default_factory=list, max_length=30)
    expected_answer_points: list[str] = Field(default_factory=list, max_length=100)
    allowed_user_ids: list[str] | None = Field(default=None, max_length=100)
    source_spans: list[Span] = Field(default_factory=list, max_length=100)
    @model_validator(mode="after")
    def consistent(self):
        if not self.question.strip():
            raise ValueError("Question cannot be blank")
        if self.answerable is False and (self.expected_sources or self.source_spans):
            raise ValueError("Unanswerable question cannot have necessary sources")
        if self.source_spans and not self.expected_sources:
            raise ValueError("Spans require expected_sources")
        if len(self.expected_sources) > 1:
            self.question_type = "cross_document"
        return self

class DatasetUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=2_000_000)
    spans_content: str = Field(default="", max_length=2_000_000)


def jsonl(content, label):
    if len(content.encode("utf-8")) > 2_000_000:
        raise AppError("DATASET_INVALID", label + "超过 2 MB。", 422)
    def invalid_constant(value):
        raise ValueError("Non-finite JSON number")
    result = []
    for number, line in enumerate(content.lstrip("\ufeff").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line, parse_constant=invalid_constant)
            if not isinstance(value, dict):
                raise ValueError()
        except (ValueError, RecursionError):
            raise AppError("DATASET_INVALID", f"{label}第 {number} 行不是合法的 JSON 对象。", 422)
        result.append((number, value))
    return result


def parse_dataset(body, identity):
    records = jsonl(body.content, "题目文件")
    if not 1 <= len(records) <= 200:
        raise AppError("DATASET_INVALID", "测试集需包含 1–200 道题。", 422)
    questions, ids = [], set()
    for number, record in records:
        record = {"question_id": f"Q{number:03d}", **record}
        try:
            q = Question.model_validate(record)
        except ValidationError as exc:
            fields = ", ".join(".".join(str(p) for p in e["loc"]) or "字段关系" for e in exc.errors(include_input=False)[:3])
            raise AppError("DATASET_INVALID", f"题目文件第 {number} 行字段无效：{fields}。", 422)
        if q.question_id in ids:
            raise AppError("DATASET_INVALID", f"题目文件第 {number} 行 question_id 重复。", 422)
        ids.add(q.question_id)
        questions.append(q)
    by_id = {q.question_id: q for q in questions}
    for number, row in jsonl(body.spans_content, "坐标文件"):
        try:
            span = Span.model_validate(row)
            if span.question_id not in by_id:
                continue  # A shared coordinate file may cover more than this dataset.
            by_id[span.question_id].source_spans.append(span)
        except ValidationError:
            raise AppError("DATASET_INVALID", f"坐标文件第 {number} 行字段或范围无效。", 422)
    for q in questions:
        expected = {(s.document_id or s.source, s.version) for s in q.expected_sources}
        spans = {(s.document_id or s.source, s.version) for s in q.source_spans}
        if len(q.source_spans) > 100 or (spans and spans != expected):
            raise AppError("DATASET_INVALID", f"题目 {q.question_id} 的坐标必须覆盖全部必要来源，且不超过 100 条。", 422)
    normalized = [q.model_dump() for q in questions]
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    permitted = [q for q in normalized if q["allowed_user_ids"] is None or identity.user_id in q["allowed_user_ids"]]
    if not permitted:
        raise AppError("DATASET_INVALID", "测试集没有适用于当前身份的题目。", 422)
    return {"id": str(uuid4()), "owner": (identity.tenant_id, identity.user_id), "name": body.filename,
            "fingerprint": digest, "questions": permitted, "total": len(questions), "excluded": len(questions)-len(permitted)}


def store_dataset(app, dataset):
    if not hasattr(app.state, "imported_datasets"):
        app.state.imported_datasets = {}
    store = app.state.imported_datasets
    owned = [key for key, value in store.items() if value["owner"] == dataset["owner"]]
    for key in owned[:-7]:
        del store[key]
    store[dataset["id"]] = dataset


def get_dataset(app, dataset_id, identity):
    result = getattr(app.state, "imported_datasets", {}).get(dataset_id)
    if result is None or result["owner"] != (identity.tenant_id, identity.user_id):
        raise AppError("DATASET_NOT_FOUND", "测试集不存在、服务已重启或不属于当前身份，请重新导入。", 404)
    return result


def preview(dataset):
    questions = dataset["questions"]
    return {k: dataset[k] for k in ("id", "name", "fingerprint", "total", "excluded")} | {
        "count": len(questions), "splits": dict(Counter(q["split"] for q in questions)),
        "source_labels": sum(bool(q["expected_sources"]) for q in questions),
        "span_labels": sum(bool(q["source_spans"]) for q in questions),
        "answer_labels": sum(bool(q["expected_answer_points"]) for q in questions),
        "questions": [{k:q[k] for k in ("question_id","question","split","question_type")} for q in questions]}


def runtime_fingerprint(app):
    # Pin provider/model/budgets and index configuration, never expose credentials.
    from dataclasses import asdict
    settings = app.state.settings
    from app.api.workbench_management import db_view
    config = {"database": db_view(settings.database_url), "generation": asdict(app.state.chat.settings), "retrieval": asdict(settings.retrieval), "embedding": asdict(settings.embedding)}
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k,v in value.items() if k not in {"api_key", "password", "password_present"}}
        return value
    return hashlib.sha256(json.dumps(clean(config), sort_keys=True).encode()).hexdigest()


def evaluate_imported(dataset, question, trace, snapshot):
    base = {"status":"unavailable", "stages":{}, "question_id":question["question_id"],
            "question_type":question["question_type"], "dataset":dataset["name"], "split":question["split"],
            "dataset_fingerprint":dataset["fingerprint"], "reference_points":question["expected_answer_points"],
            "answer_correctness":None, "citation_support":None, "semantic_status":"pending_review", "sample_count":1}
    if not question["expected_sources"]:
        return base | {"reason":"没有必要来源标注；答案正确性仍需评审。"}
    # Existing fixture logical IDs can map to their stable source paths. Never use question gold from that fixture.
    aliases = sample_aliases()
    docs = snapshot["documents"]
    def resolve(source):
        target = source.get("source") or source.get("document_id")
        matches = [d for d in docs if d["version"] == source["version"] and
                   (d["document_id"] == target or d["source"] == target or d["source"] == aliases.get(target))]
        return matches[0] if len(matches) == 1 else None
    required = {}
    for source in question["expected_sources"]:
        doc = resolve(source)
        if doc is None:
            return base | {"status":"stale_fixture", "reason":"必要来源无法匹配当前有权限的文档及版本，未计算质量指标。"}
        required[doc["document_id"]] = doc
    anchors = {}
    for span in question["source_spans"]:
        doc = resolve(span)
        if doc is None or span["end_char"] > doc["text_length"] or doc["text_hash"] != span["text_hash"] or doc["parser_version"] != span["parser_version"]:
            return base | {"status":"stale_fixture", "reason":"坐标标注与当前文档内容或解析版本不一致。"}
        anchors.setdefault(doc["document_id"], []).append((span["start_char"], span["end_char"]))
    if anchors and set(anchors) != set(required):
        return base | {"reason":"坐标标注未覆盖全部必要来源。"}
    metrics = {}
    for stage, hits in trace["stages"].items():
        hits = list({h["chunk_id"]:h for h in reversed(hits)}.values())[::-1]
        matched, intersections, relevant = set(), {}, 0
        for hit in hits:
            citation = hit["citation"]
            did = citation.get("document_id") or hit.get("document_id")
            doc = required.get(did)
            if not doc or citation["version"] != doc["version"] or citation.get("text_hash") != doc["text_hash"]:
                continue
            overlaps = []
            if anchors:
                location = citation["location"]
                overlaps = [(max(a,location["start_char"]),min(b,location["end_char"])) for a,b in anchors[did]]
                overlaps = [(a,b) for a,b in overlaps if a < b]
            if not anchors or overlaps:
                matched.add(did); relevant += 1
                intersections.setdefault(did,[]).extend(overlaps)
        total = sum(union_length(v) for v in anchors.values())
        metrics[stage] = {"source_recall_at_k":len(matched)/len(required),
            "all_required_sources_recalled":len(matched)==len(required),
            "precision_at_k":relevant/len(hits) if anchors and hits else None,
            "section_char_coverage_at_k":sum(union_length(v) for v in intersections.values())/total if total else None,
            "retrieved_count":len(hits),"expected_source_count":len(required),"relevant_count":relevant if anchors else None}
    return base | {"status":"measured","stages":metrics,
        "precision_scope":"章节坐标交集" if anchors else "未提供坐标，章节精确率未评测",
        "reason":"按文档、版本和章节坐标评测。" if anchors else "按文档和版本评测来源召回；未提供章节坐标。"}


@lru_cache(maxsize=1)
def sample_aliases():
    from pathlib import Path
    from app.ingestion.parser import parse_document
    root = Path(__file__).resolve().parents[2] / "sample_data/knowledge_base"
    aliases = {}
    for file in root.glob("*.md"):
        doc = parse_document(file.name, file.read_bytes())
        aliases[doc.metadata["document_id"]] = "sample_data/knowledge_base/" + file.name
    return aliases
