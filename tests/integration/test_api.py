from conftest import pg_dsn
import pytest
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(app_env="test", database_url=pg_dsn("intake.db"),
                        ingestion_worker_enabled=False)
    with TestClient(create_app(settings)) as client:
        yield client


def test_health_has_request_id(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]


def test_chat_requires_server_mapped_identity(client) -> None:
    response = client.post("/chat", json={"message": "计算 2 + 3"})
    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "UNAUTHENTICATED",
        "message": "Valid local test identity is required",
        "request_id": response.headers["X-Request-ID"],
    }


def test_fake_model_requests_tool_and_server_executes_it(client) -> None:
    response = client.post(
        "/agent/demo", json={"message": "计算 2 + 3"}, headers={"X-Local-User-Id": "u_employee_001"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "answer": "计算结果：5", "model_provider": "fake",
        "tool_calls": [{"call_id": "fake-call-1", "name": "calculator", "result": "5"}],
    }


def test_document_receipt_and_file_validation(client) -> None:
    headers = {"X-Local-User-Id": "u_engineer_001"}
    accepted = client.post("/documents", files={"file": ("note.md", b"# Test\n")}, headers=headers)
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "queued"
    assert accepted.json()["size_bytes"] == 7
    assert accepted.json()["document_id"]
    assert accepted.json()["task_id"]
    assert not accepted.json()["duplicate"]
    rejected = client.post("/documents", files={"file": ("note.pdf", b"PDF")}, headers=headers)
    assert rejected.status_code == 415
    assert rejected.json()["error"]["code"] == "UNSUPPORTED_FILE"


def test_request_validation_uses_common_error_shape(client) -> None:
    response = client.post(
        "/chat", json={"message": ""}, headers={"X-Local-User-Id": "u_employee_001"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_unknown_route_uses_common_error_shape(client) -> None:
    response = client.get("/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]

