from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = PROJECT_ROOT / "sample_data" / "knowledge_base"
QUESTIONS_PATH = PROJECT_ROOT / "eval_data" / "questions.jsonl"

USER_GROUPS = {
    "u_employee_001": {"all-employees"},
    "u_engineer_001": {"all-employees", "engineering-cn"},
    "u_support_001": {"all-employees", "it-support"},
}


def parse_front_matter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(?P<header>.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise ValueError(f"{path.name}: missing front matter")

    metadata: dict[str, object] = {}
    for line in match.group("header").splitlines():
        key, separator, raw_value = line.partition(":")
        if not separator:
            raise ValueError(f"{path.name}: invalid metadata line: {line}")
        value = raw_value.strip()
        if value.startswith("[") and value.endswith("]"):
            metadata[key] = [
                item.strip() for item in value[1:-1].split(",") if item.strip()
            ]
        else:
            metadata[key] = value
    return metadata


def load_documents() -> dict[str, dict[str, object]]:
    documents: dict[str, dict[str, object]] = {}
    required = {
        "document_id",
        "title",
        "version",
        "effective_date",
        "status",
        "classification",
        "audience",
        "owner",
    }
    for path in sorted(CORPUS_DIR.glob("*.md")):
        metadata = parse_front_matter(path)
        missing = required - metadata.keys()
        if missing:
            raise ValueError(f"{path.name}: missing metadata {sorted(missing)}")
        document_id = str(metadata["document_id"])
        if document_id in documents:
            raise ValueError(f"duplicate document_id: {document_id}")
        if metadata["status"] != "active":
            raise ValueError(f"{document_id}: Day 1 corpus must be active")
        metadata["path"] = path
        documents[document_id] = metadata
    return documents


def load_questions() -> list[dict[str, object]]:
    questions: list[dict[str, object]] = []
    for line_number, line in enumerate(
        QUESTIONS_PATH.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            questions.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"questions.jsonl:{line_number}: {exc}") from exc
    return questions


def validate() -> None:
    documents = load_documents()
    questions = load_questions()
    errors: list[str] = []

    if len(documents) != 14:
        errors.append(f"expected 14 documents, got {len(documents)}")
    if len(questions) != 30:
        errors.append(f"expected 30 questions, got {len(questions)}")

    ids = [str(question.get("question_id")) for question in questions]
    duplicate_ids = [item for item, count in Counter(ids).items() if count > 1]
    if duplicate_ids:
        errors.append(f"duplicate question IDs: {duplicate_ids}")

    type_counts = Counter(str(question.get("question_type")) for question in questions)
    split_counts = Counter(str(question.get("split")) for question in questions)
    if type_counts != {"fact": 20, "cross_document": 5, "unanswerable": 5}:
        errors.append(f"unexpected type counts: {dict(type_counts)}")
    if split_counts != {"dev": 20, "test": 10}:
        errors.append(f"unexpected split counts: {dict(split_counts)}")

    for question in questions:
        question_id = str(question.get("question_id"))
        sources = question.get("expected_sources", [])
        allowed_users = question.get("allowed_user_ids", [])
        versions = question.get("document_versions", {})
        answerable = question.get("answerable")
        question_type = question.get("question_type")

        if not isinstance(sources, list) or not isinstance(allowed_users, list):
            errors.append(f"{question_id}: sources/users must be lists")
            continue
        if not isinstance(versions, dict):
            errors.append(f"{question_id}: document_versions must be an object")
            continue
        if answerable and not sources:
            errors.append(f"{question_id}: answerable question has no source")
        if not answerable and sources:
            errors.append(f"{question_id}: unanswerable question has sources")
        if question_type == "cross_document" and len(sources) < 2:
            errors.append(f"{question_id}: cross-document question needs 2+ sources")

        for user_id in allowed_users:
            if user_id not in USER_GROUPS:
                errors.append(f"{question_id}: unknown user {user_id}")

        for source in sources:
            document_id = source.get("document_id")
            if document_id not in documents:
                errors.append(f"{question_id}: unknown source {document_id}")
                continue
            expected_version = str(documents[document_id]["version"])
            if str(source.get("version")) != expected_version:
                errors.append(
                    f"{question_id}: {document_id} source version "
                    f"{source.get('version')} != {expected_version}"
                )
            if str(versions.get(document_id)) != expected_version:
                errors.append(
                    f"{question_id}: {document_id} missing/mismatched version map"
                )
            audience = set(documents[document_id]["audience"])
            for user_id in allowed_users:
                if not USER_GROUPS[user_id].intersection(audience):
                    errors.append(
                        f"{question_id}: {user_id} cannot access {document_id}"
                    )

    if errors:
        raise SystemExit("\n".join(f"ERROR: {error}" for error in errors))

    print(f"Validated {len(documents)} documents and {len(questions)} questions.")
    print(f"Question types: {dict(type_counts)}")
    print(f"Splits: {dict(split_counts)}")
    print("Source IDs, versions, and declared audience access are consistent.")


if __name__ == "__main__":
    validate()
