"""Use a fresh PostgreSQL schema per test; never clear application tables."""
import os
from uuid import uuid4
import pytest
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

_schemas = {}
_test_url = None

def pg_dsn(name="default"):
    if not _test_url:
        pytest.skip("Set KNOWLEDGEOPS_TEST_DATABASE_URL to run database regressions")
    if name not in _schemas:
        schema = "ko_test_" + uuid4().hex
        with psycopg.connect(_test_url) as db:
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        _schemas[name] = schema
    return make_conninfo(_test_url, options=f"-c search_path={_schemas[name]},public")

@pytest.fixture(autouse=True)
def isolated_postgres(monkeypatch):
    global _test_url
    _test_url = os.getenv("KNOWLEDGEOPS_TEST_DATABASE_URL") or os.getenv("KNOWLEDGEOPS_TEST_VECTOR_DATABASE_URL")
    monkeypatch.delenv("KNOWLEDGEOPS_INGESTION_DB_PATH", raising=False)
    try:
        yield
    finally:
        if _test_url:
            with psycopg.connect(_test_url) as db:
                for schema in _schemas.values():
                    # Only names generated here, in the explicitly configured test database.
                    db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        _schemas.clear()
        _test_url = None


@pytest.fixture(autouse=True)
def offline_protocol_doubles(monkeypatch):
    import app.main as main
    from tests.doubles import ExtractiveGenerationDouble, HashEmbeddingDouble, ScriptedAgentDouble
    original_generation, original_embedding = main.make_generation_client, main.make_embedding_client
    monkeypatch.setattr(main, 'make_generation_client', lambda s: ExtractiveGenerationDouble() if s.provider == 'unconfigured' else original_generation(s))
    monkeypatch.setattr(main, 'make_embedding_client', lambda s: HashEmbeddingDouble(s) if s.provider == 'unconfigured' else original_embedding(s))
    monkeypatch.setattr(main, 'UnconfiguredAgentModel', ScriptedAgentDouble)
