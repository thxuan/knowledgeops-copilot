from conftest import pg_dsn
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from app.core.errors import AppError
from app.core.identity import Identity, LOCAL_IDENTITIES
from app.db.ingestion import IngestionRepository
from app.ingestion.parser import ParsedDocument, ParseError
from app.ingestion.worker import IngestionWorker

ENGINEER = LOCAL_IDENTITIES["u_engineer_001"]


@pytest.fixture
def repository(tmp_path):
    repository = IngestionRepository(pg_dsn())
    repository.initialize()
    return repository


def submit(repository, content=b"# Test\n", identity=ENGINEER, parser_version="utf8-structure-v1:md"):
    return repository.submit(identity=identity, filename="note.md", content=content,
                             file_hash=hashlib.sha256(content).hexdigest(),
                             parser_version=parser_version, source="test-source", version=None)


def test_concurrent_uploads_create_one_document_and_one_job(repository):
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: submit(repository), range(16)))
    assert len({result["document_id"] for result in results}) == 1
    assert len({result["task_id"] for result in results}) == 1
    assert sum(not result["duplicate"] for result in results) == 1
    with repository.connection() as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()["count"] == 1
        assert db.execute("SELECT count(*) FROM ingestion_jobs").fetchone()["count"] == 1


def test_dedup_is_scoped_to_tenant_and_parser_configuration(repository):
    original = submit(repository)
    other_tenant = Identity(ENGINEER.user_id, "other-tenant", ENGINEER.audiences)
    other = submit(repository, identity=other_tenant)
    config = submit(repository, parser_version="utf8-structure-v2:md")
    changed = submit(repository, content=b"# Changed\n")
    assert len({r["document_id"] for r in [original, other, config, changed]}) == 4
    assert submit(repository)["duplicate"]


def test_unsubmitted_users_cannot_read_task(repository):
    receipt = submit(repository)
    employee = LOCAL_IDENTITIES["u_employee_001"]
    with pytest.raises(AppError) as error:
        repository.get_task(receipt["task_id"], employee)
    assert error.value.status_code == 404
    with pytest.raises(AppError):
        submit(repository, identity=employee)
    with repository.connection() as db:
        assert db.execute("SELECT count(*) FROM document_access").fetchone()["count"] == 1


def test_reopen_recovers_expired_claim_and_fences_old_worker(repository):
    receipt = submit(repository)
    # Simulate a worker that obtained a claim and crashed without completing.
    old_claim = repository.claim(lease_seconds=1, max_attempts=3, now=time.time() - 2)
    reopened = IngestionRepository(repository.database_url)
    reopened.initialize()
    assert IngestionWorker(reopened).process_next()
    task = reopened.get_task(receipt["task_id"], ENGINEER)
    assert task["status"] == "succeeded"
    assert task["attempts"] == 2
    assert not repository.complete(old_claim, ParsedDocument("stale", "stale", {}))
    assert not repository.advance(old_claim, "cleaning")
    repository.fail(old_claim, "STALE", "stale")
    with repository.connection() as db:
        assert db.execute("SELECT text FROM documents").fetchone()["text"] == "# Test\n"
    assert reopened.get_task(receipt["task_id"], ENGINEER)["status"] == "succeeded"


def test_active_lease_cannot_be_claimed_twice(repository):
    submit(repository)
    assert repository.claim(30, 3)
    assert repository.claim(30, 3) is None


def test_transient_failure_can_retry_then_succeed(repository, monkeypatch):
    receipt = submit(repository)
    worker = IngestionWorker(repository)
    import app.ingestion.worker as worker_module
    real_parser = worker_module.parse_document

    def failing_parser(*args):
        raise ParseError("TEMPORARY_PARSE_FAILURE", "Temporary parser failure")

    monkeypatch.setattr(worker_module, "parse_document", failing_parser)
    worker.process_next()
    failed = repository.get_task(receipt["task_id"], ENGINEER)
    assert failed["status"] == "failed"
    assert failed["stage"] == "parsing"
    assert failed["error_code"] == "TEMPORARY_PARSE_FAILURE"
    repository.retry(receipt["task_id"], ENGINEER, 3)
    monkeypatch.setattr(worker_module, "parse_document", real_parser)
    worker.process_next()
    final = repository.get_task(receipt["task_id"], ENGINEER)
    assert final["status"] == "succeeded"
    assert final["attempts"] == 2
    assert any(event["error_code"] == "TEMPORARY_PARSE_FAILURE" for event in final["events"])


def test_recovery_has_an_attempt_limit(repository):
    receipt = submit(repository)
    now = time.time() - 10
    repository.claim(1, 2, now=now)
    repository.claim(1, 2, now=now + 2)
    assert repository.claim(1, 2, now=now + 4) is None
    final = repository.get_task(receipt["task_id"], ENGINEER)
    assert final["status"] == "failed"
    assert final["error_code"] == "ATTEMPT_LIMIT"
    with pytest.raises(AppError):
        repository.retry(receipt["task_id"], ENGINEER, 2)



def test_cleaning_failure_records_correct_stage(repository):
    receipt = submit(repository, content=b" \n\t")
    IngestionWorker(repository).process_next()
    task = repository.get_task(receipt["task_id"], ENGINEER)
    assert task["status"] == "failed"
    assert task["stage"] == "cleaning"
    assert task["error_code"] == "EMPTY_TEXT"


def test_unexpected_parser_error_does_not_expose_document_content(repository, monkeypatch):
    receipt = submit(repository)
    import app.ingestion.worker as worker_module

    def failing_parser(*args):
        raise RuntimeError("sensitive document content")

    monkeypatch.setattr(worker_module, "parse_document", failing_parser)
    IngestionWorker(repository).process_next()
    task = repository.get_task(receipt["task_id"], ENGINEER)
    assert task["error_code"] == "PROCESSING_ERROR"
    assert "sensitive" not in str(task)


def test_parallel_workers_claim_distinct_tasks(repository):
    for index in range(12):
        submit(repository, content=f"# Task {index}".encode())
    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(lambda _: repository.claim(30, 3), range(16)))
    claims = [claim for claim in claims if claim is not None]
    assert len(claims) == 12
    assert len({claim['task_id'] for claim in claims}) == 12
    assert len({claim['claim_token'] for claim in claims}) == 12
