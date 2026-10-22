from tests.doubles import seed_published_legacy
from conftest import pg_dsn
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.core.identity import Identity, LOCAL_IDENTITIES
from app.db.ingestion import IngestionRepository
from app.db.chunks import ChunkRepository
from app.main import create_app
import pytest
import psycopg
from app.core.errors import AppError
from app.ingestion.chunker import ChunkConfig

HEADERS = {"X-Local-User-Id": "u_engineer_001"}


def test_build_rebuild_access_and_persistence(tmp_path):
    path = pg_dsn("chunks.db")
    app = create_app(Settings(app_env="test", database_url=path, ingestion_worker_enabled=False))
    with TestClient(app) as client:
        receipt = client.post("/documents", headers=HEADERS,
                              files={"file": ("vpn.md", ("# VPN\n\n## TL-403\n" + "不得绕过检查。" * 90).encode())}).json()
        url = f'/documents/{receipt["document_id"]}/chunks'
        assert client.get(url, headers=HEADERS).status_code == 409
        assert client.post(url, headers=HEADERS, json={}).status_code == 409
        app.state.ingestion_worker.process_next()
        seed_published_legacy(app,receipt['document_id'])
        assert client.get(url, headers=HEADERS).json()["error"]["code"] == "CHUNKS_NOT_BUILT"
        first_response = client.post(url, headers=HEADERS, json={})
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()
        assert first["count"] > 1
        assert first == client.post(url, headers=HEADERS, json={}).json()
        assert first == client.get(url, headers=HEADERS).json()
        assert client.get(url).status_code == 401
        other = {"X-Local-User-Id": "u_employee_001"}
        assert client.get(url, headers=other).status_code == 404
        assert client.post(url, headers=other, json={}).status_code == 404
        for body in [{"size": 100, "overlap": 100}, {"strategy": "invalid"}, {"size": True}]:
            assert client.post(url, headers=HEADERS, json=body).status_code == 422
        second = client.post(url, headers=HEADERS, json={"strategy": "fixed", "size": 200, "overlap": 40}).json()
        assert second["chunk_set_id"] != first["chunk_set_id"]
        assert second["text_hash"] == first["text_hash"]
        assert client.get(url, headers=HEADERS).json() == second
        with app.state.ingestion_repository.connection() as db:
            assert db.execute("SELECT count(*) FROM chunk_sets").fetchone()["count"] == 2
        # A database write failure must roll back the pointer and the incomplete set.
        with app.state.ingestion_repository.connection() as db:
            db.execute("""CREATE FUNCTION reject_chunk_insert() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE check_violation USING MESSAGE = 'test'; END $$;
                CREATE TRIGGER reject_chunks BEFORE INSERT ON chunks
                FOR EACH ROW EXECUTE FUNCTION reject_chunk_insert()""")
        with pytest.raises(psycopg.IntegrityError):
            app.state.chunk_repository.build(receipt["document_id"], LOCAL_IDENTITIES["u_engineer_001"],
                                            ChunkConfig("fixed", 100, 0))
        assert client.get(url, headers=HEADERS).json() == second
        with app.state.ingestion_repository.connection() as db:
            assert db.execute("SELECT count(*) FROM chunk_sets").fetchone()["count"] == 2
            db.execute("DROP TRIGGER reject_chunks ON chunks")
    with TestClient(create_app(Settings(app_env="test", database_url=path,
                                       ingestion_worker_enabled=False))) as client:
        assert client.get(url, headers=HEADERS).json() == second
    repository = ChunkRepository(IngestionRepository(path))
    engineer = LOCAL_IDENTITIES["u_engineer_001"]
    with pytest.raises(AppError) as error:
        repository.get(receipt["document_id"], Identity(engineer.user_id, "other-tenant", engineer.audiences))
    assert error.value.status_code == 404
