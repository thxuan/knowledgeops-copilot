from conftest import pg_dsn
import time
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.main import create_app

HEADERS = {"X-Local-User-Id": "u_engineer_001"}


def test_upload_dedup_status_and_retry(tmp_path):
    settings = Settings(app_env="test", database_url=pg_dsn("intake.db"),
                        ingestion_worker_enabled=False, ingestion_max_attempts=2)
    app = create_app(settings)
    with TestClient(app) as client:
        receipt = client.post("/documents", headers=HEADERS,
                              files={"file": ("bad.txt", b"\xff")}).json()
        task_url = f'/ingestion/tasks/{receipt["task_id"]}'
        duplicate = client.post("/documents", headers=HEADERS,
                                files={"file": ("renamed.txt", b"\xff")}).json()
        assert duplicate["duplicate"]
        assert duplicate["task_id"] == receipt["task_id"]
        assert client.get(task_url).status_code == 401
        assert client.get(task_url, headers={"X-Local-User-Id": "u_employee_001"}).status_code == 404
        assert client.get("/ingestion/tasks/missing", headers=HEADERS).status_code == 404
        assert client.post(task_url + "/retry", headers=HEADERS).status_code == 409
        app.state.ingestion_worker.process_next()
        failed = client.get(task_url, headers=HEADERS).json()
        assert failed["status"] == "failed"
        assert failed["error_code"] == "INVALID_ENCODING"
        retry = client.post(task_url + "/retry", headers=HEADERS)
        assert retry.status_code == 202
        assert retry.json()["status"] == "queued"
        app.state.ingestion_worker.process_next()
        assert client.get(task_url, headers=HEADERS).json()["attempts"] == 2
        assert client.post(task_url + "/retry", headers=HEADERS).status_code == 409


def test_lifespan_worker_processes_persisted_queue_after_app_restart(tmp_path):
    path = pg_dsn("intake.db")
    with TestClient(create_app(Settings(app_env="test", database_url=path,
                                       ingestion_worker_enabled=False))) as client:
        result = client.post("/documents", headers=HEADERS,
                             data={"source": "manual/vpn", "version": "2.0"},
                             files={"file": ("vpn.md", "# VPN\n使用 MFA。\n".encode())})
        assert result.status_code == 202
        receipt = result.json()
    with TestClient(create_app(Settings(app_env="test", database_url=path,
                                       ingestion_poll_seconds=0.01))) as client:
        deadline = time.monotonic() + 5
        while True:
            task = client.get(f'/ingestion/tasks/{receipt["task_id"]}', headers=HEADERS).json()
            if task["status"] in {"succeeded", "failed"}:
                break
            assert time.monotonic() < deadline, task
            time.sleep(0.01)
        assert task["status"] == "succeeded"
        assert task["source"] == "manual/vpn"
        assert task["version"] == "2.0"
        assert task["title"] == "VPN"
        assert task["text_length"] == len("# VPN\n使用 MFA。\n")
        assert [event["stage"] for event in task["events"]] == [
            "validated", "parsing", "cleaning", "completed"
        ]


def test_bounded_upload_validation(tmp_path):
    with TestClient(create_app(Settings(app_env="test", database_url=pg_dsn("small.db"),
                                       max_document_bytes=4, ingestion_worker_enabled=False))) as client:
        for content, code, status in [(b"", "EMPTY_DOCUMENT", 422),
                                      (b"12345", "DOCUMENT_TOO_LARGE", 413)]:
            result = client.post("/documents", headers=HEADERS, files={"file": ("tiny.txt", content)})
            assert result.status_code == status
            assert result.json()["error"]["code"] == code
        accepted = client.post("/documents", headers=HEADERS,
                               files={"file": ("C:\\temp\\tiny.TXT", b"1234")})
        assert accepted.status_code == 202
        assert accepted.json()["filename"] == "tiny.TXT"



def test_restarted_lifespan_worker_recovers_inflight_task(tmp_path):
    path = pg_dsn("interrupted.db")
    first_app = create_app(Settings(app_env="test", database_url=path,
                                    ingestion_worker_enabled=False))
    with TestClient(first_app) as client:
        receipt = client.post("/documents", headers=HEADERS,
                              files={"file": ("recover.md", b"# Recover\n")}).json()
        abandoned = first_app.state.ingestion_repository.claim(1, 3, now=time.time() - 2)
        assert client.get(f'/ingestion/tasks/{receipt["task_id"]}', headers=HEADERS).json()["status"] == "processing"
    restarted_app = create_app(Settings(app_env="test", database_url=path,
                                        ingestion_poll_seconds=0.01))
    with TestClient(restarted_app) as client:
        deadline = time.monotonic() + 5
        while True:
            task = client.get(f'/ingestion/tasks/{receipt["task_id"]}', headers=HEADERS).json()
            if task["status"] == "succeeded":
                break
            assert time.monotonic() < deadline, task
            time.sleep(0.01)
        assert task["attempts"] == 2
        assert task["document_id"] == abandoned["document_id"]
