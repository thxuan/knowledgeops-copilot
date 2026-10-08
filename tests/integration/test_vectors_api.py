from conftest import pg_dsn
import asyncio
from dataclasses import replace
import os
from uuid import uuid4
import pytest
import psycopg
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.core.embedding import EmbeddingSettings, EmbeddingError, EmbeddingResult
from app.core.errors import AppError
from app.core.identity import LOCAL_IDENTITIES
from app.db.vectors import VectorRepository
from app.main import create_app

HEADERS = {"X-Local-User-Id": "u_engineer_001"}

@pytest.fixture
def vector_app(tmp_path):
    url = os.getenv("KNOWLEDGEOPS_TEST_VECTOR_DATABASE_URL")
    if not url:
        pytest.skip("Set KNOWLEDGEOPS_TEST_VECTOR_DATABASE_URL for real pgvector tests")
    embedding = EmbeddingSettings(collection="test-" + uuid4().hex, dimensions=16, batch_size=2)
    app = create_app(Settings(app_env="test", database_url=pg_dsn("intake.db"),
        ingestion_worker_enabled=False, vector_database_url=url, embedding=embedding))
    with TestClient(app) as client:
        yield app, client
    repo = app.state.vector_repository
    with repo.connection() as db:
        docs = db.execute("SELECT document_id FROM ko_vector_heads WHERE collection=%s", (embedding.collection,)).fetchall()
        db.execute("DELETE FROM ko_embedding_batches WHERE collection=%s", (embedding.collection,))
        db.execute("DELETE FROM ko_vector_heads WHERE collection=%s", (embedding.collection,))
        db.execute("DELETE FROM ko_embeddings WHERE collection=%s", (embedding.collection,))
        db.execute("DELETE FROM ko_collections WHERE collection=%s", (embedding.collection,))
        for row in docs:
            db.execute("DELETE FROM ko_chunks WHERE document_id=%s", (row["document_id"],))
            db.execute("DELETE FROM ko_documents WHERE document_id=%s", (row["document_id"],))
        db.execute(psycopg.sql.SQL("DROP INDEX IF EXISTS {}").format(psycopg.sql.Identifier(repo.index_name)))

def upload(app, client):
    receipt = client.post("/documents", headers=HEADERS,
        files={"file": ("vpn.md", ("# VPN\n\n## TL-403\n设备修复后等待十分钟。\n\n## MFA\n备用码。" * 12).encode())},
        data={"source":"fixture/vpn.md", "version":"v1"}).json()
    app.state.ingestion_worker.process_next()
    doc_id = receipt["document_id"]
    response = client.post(f"/documents/{doc_id}/chunks", headers=HEADERS, json={})
    assert response.status_code == 200, response.text
    return doc_id, response.json()

def test_index_search_reuse_acl_and_chunk_invalidation(vector_app):
    app, client = vector_app
    doc_id, snapshot = upload(app, client)
    url = f"/documents/{doc_id}/index"
    first = client.post(url, headers=HEADERS)
    assert first.status_code == 200, first.text
    assert first.json()["count"] == snapshot["count"] and not first.json()["reused"]
    assert all(e["input_count"] <= 2 for e in first.json()["batches"])
    second = client.post(url, headers=HEADERS).json()
    assert second["reused"] and not second["batches"]
    other = {"X-Local-User-Id": "u_employee_001"}
    assert client.post(url, headers=other).status_code == 404
    body = {"query":"TL-403 设备修复", "top_k":3}
    exact = client.post("/search/vector", headers=HEADERS, json=body)
    assert exact.status_code == 200, exact.text
    assert len(exact.json()["hits"]) == 3
    assert client.post("/search/vector", headers=other, json=body).json()["hits"] == []
    assert client.post("/search/vector", json=body).status_code == 401
    assert client.post("/search/vector", headers=HEADERS, json={"query":"x","top_k":True}).status_code == 422
    repo = app.state.vector_repository
    repo.create_hnsw()
    ann = client.post("/search/vector", headers=HEADERS, json={**body,"mode":"hnsw"})
    assert ann.status_code == 200, ann.text
    # Repeated sections have tied distances; different IDs can be equally correct.
    assert [h["distance"] for h in ann.json()["hits"]] == pytest.approx([h["distance"] for h in exact.json()["hits"]])
    service = app.state.vector_service
    vector = asyncio.run(service.client.embed([body["query"]])).vectors[0]
    plan = repo.search(vector, service.allowed_sets(LOCAL_IDENTITIES["u_engineer_001"]), "tenant_clearwater", 3, "hnsw", explain=True)
    assert repo.index_name in str(plan)
    client.post(f"/documents/{doc_id}/chunks", headers=HEADERS, json={"strategy":"fixed", "size":80})
    assert client.post("/search/vector", headers=HEADERS, json=body).json()["hits"] == []
    assert client.post(url, headers=HEADERS).status_code == 200
    assert client.post("/search/vector", headers=HEADERS, json=body).json()["hits"]
    with app.state.ingestion_repository.connection() as db:
        db.execute("DELETE FROM document_access WHERE document_id=%s", (doc_id,))
    assert client.post("/search/vector", headers=HEADERS, json=body).json()["hits"] == []

def test_collection_signature_and_db_dimension_guard(vector_app):
    app, client = vector_app
    doc_id, snapshot = upload(app, client)
    repo = app.state.vector_repository
    for settings in [replace(repo.settings, dimensions=32),
        replace(repo.settings, provider="compatible", model_id="other", api_key="test")]:
        with pytest.raises(AppError) as error:
            VectorRepository(repo.database_url, settings).initialize()
        assert error.value.code == "EMBEDDING_CONFIG_MISMATCH"
    client.post(f"/documents/{doc_id}/index", headers=HEADERS)
    with pytest.raises(psycopg.errors.CheckViolation):
        with repo.connection() as db:
            db.execute("UPDATE ko_embeddings SET embedding='[1,0]'::vector WHERE collection=%s", (repo.settings.collection,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with repo.connection() as db:
            db.execute("UPDATE ko_embeddings SET dimensions=2, embedding='[1,0]'::vector WHERE collection=%s", (repo.settings.collection,))
    with pytest.raises(EmbeddingError):
        repo.search([1,0], [], "tenant_clearwater", 5)

def test_pg_publication_rollback(vector_app):
    app, client = vector_app
    doc_id, snapshot = upload(app, client)
    repo = app.state.vector_repository
    client.post(f"/documents/{doc_id}/index", headers=HEADERS)
    before = repo.ready(doc_id, snapshot["chunk_set_id"], snapshot["count"])
    broken = {**snapshot, "document_id": uuid4().hex, "chunk_set_id": "broken"}
    broken["chunks"] = [{**c, "chunk_id":uuid4().hex,"document_id":"nonexistent"} for c in snapshot["chunks"]]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        repo.publish(broken, "tenant_clearwater", [[1.] + [0.] * 15 for c in broken["chunks"]])
    assert before and repo.ready(doc_id, snapshot["chunk_set_id"], snapshot["count"])
    with repo.connection() as db:
        assert db.execute("SELECT 1 FROM ko_documents WHERE document_id=%s", (broken["document_id"],)).fetchone() is None

def test_optional_vector_not_configured(tmp_path):
    app = create_app(Settings(app_env="test", database_url=pg_dsn("local.db"), ingestion_worker_enabled=False))
    with TestClient(app) as client:
        response = client.post("/search/vector", headers=HEADERS, json={"query":"VPN"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "VECTOR_NOT_CONFIGURED"


EMPLOYEE = {"X-Local-User-Id": "u_employee_001"}
SUPPORT = {"X-Local-User-Id": "u_support_001"}


def retrieve(client, headers=HEADERS, **kwargs):
    response = client.post("/retrieve", headers=headers, json={"query":"TL-403 repair", **kwargs})
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


def test_retrieval_filters_citations_and_identity(vector_app):
    app, client = vector_app
    doc_id, snapshot = upload(app, client)
    client.post(f"/documents/{doc_id}/index", headers=HEADERS)
    result = retrieve(client, top_k=2, filters={"document_ids":[doc_id],"source":"fixture/vpn.md", "updated_after":0})
    assert len(result["hits"]) == 2
    citation = result["hits"][0]["citation"]
    assert citation["chunk_set_id"] == snapshot["chunk_set_id"] and citation["page"] is None
    read = client.get(citation["url"], headers=HEADERS)
    assert read.status_code == 200 and read.json()["text"] == result["hits"][0]["text"]
    assert read.headers["Cache-Control"] == "no-store"
    assert citation["location"]["end_char"] > citation["location"]["start_char"]
    for filters in [{"document_ids":[]}, {"document_ids":["missing"]}, {"source":"other"},
                    {"updated_after":9999999999}, {"updated_before":1}]:
        assert not retrieve(client, filters=filters)["hits"]
    assert not retrieve(client, headers=EMPLOYEE, filters={"document_ids":[doc_id]})["hits"]
    assert client.get(citation["url"], headers=EMPLOYEE).status_code == 404
    assert client.get(citation["url"]).status_code == 401
    for body in [{"query":"x","tenant_id":"other"}, {"query":"x","filters":{"tenant_id":"other"}},
                 {"query":"x","filters":{"updated_after":2,"updated_before":1}}, {"query":"  "}]:
        assert client.post("/retrieve", headers=HEADERS, json=body).status_code == 422
    from app.core.identity import Identity, get_identity
    app.dependency_overrides[get_identity] = lambda: Identity("u_engineer_001", "other", frozenset())
    assert not retrieve(client)["hits"]
    assert client.get(citation["url"], headers=HEADERS).status_code == 404
    app.dependency_overrides.clear()


def test_update_delete_and_old_citation_invalidation(vector_app):
    app, client = vector_app
    doc_id, _ = upload(app, client)
    index = f"/documents/{doc_id}/index"
    client.post(index, headers=HEADERS)
    old = retrieve(client)["hits"][0]["citation"]
    url = f"/documents/{doc_id}"
    with app.state.ingestion_repository.connection() as db:
        original = db.execute("SELECT content FROM documents WHERE document_id=%s", (doc_id,)).fetchone()["content"]
    update = client.put(url, headers=HEADERS, files={"file":("vpn.md", original)},
                        data={"version":"v2","expected_version":"v1"})
    assert update.status_code == 200, update.text
    assert update.json()["index_required"] and update.json()["revision"] == 2
    assert not retrieve(client)["hits"]
    assert client.get(old["url"], headers=HEADERS).status_code == 404
    assert client.post(index, headers=HEADERS).status_code == 409
    assert client.put(url, headers=HEADERS, files={"file":("vpn.md", b"# New")},
                      data={"version":"v3","expected_version":"v1"}).status_code == 409
    # Same body, new provenance must not reuse an old chunk/vector payload.
    client.post(url+"/chunks", headers=HEADERS, json={})
    assert client.post(index, headers=HEADERS).status_code == 200
    new = retrieve(client)["hits"][0]["citation"]
    assert new["version"] == "v2" and new["chunk_id"] != old["chunk_id"]
    assert client.get(old["url"], headers=HEADERS).status_code == 404
    assert client.get(new["url"], headers=HEADERS).status_code == 200
    # Returning to an earlier version label still creates a new generation.
    assert client.put(url, headers=HEADERS, files={"file":("vpn.md", original)},
                      data={"version":"v1","expected_version":"v2"}).status_code == 200
    client.post(url+"/chunks", headers=HEADERS, json={})
    client.post(index, headers=HEADERS)
    restored = retrieve(client)["hits"][0]["citation"]
    assert restored["chunk_id"] not in {old["chunk_id"],new["chunk_id"]}
    assert client.get(old["url"], headers=HEADERS).status_code == 404
    assert client.delete(url, headers=HEADERS).status_code == 204
    assert not retrieve(client)["hits"]
    assert not client.post("/search/vector", headers=HEADERS, json={"query":"x"}).json()["hits"]
    for path in [restored["url"], url+"/chunks"]:
        assert client.get(path, headers=HEADERS).status_code == 404
    assert client.post(index, headers=HEADERS).status_code == 404


def test_acl_revocation_rechecks_saved_links_and_duplicate_upload(vector_app):
    app, client = vector_app
    doc_id, _ = upload(app, client)
    client.post(f"/documents/{doc_id}/index", headers=HEADERS)
    permissions = f"/documents/{doc_id}/permissions"
    assert client.put(permissions, headers=HEADERS, json={"user_ids":["u_engineer_001","u_employee_001"]}).status_code == 200
    saved = retrieve(client, headers=EMPLOYEE)["hits"][0]["citation"]
    assert client.get(saved["url"], headers=EMPLOYEE).status_code == 200
    assert client.put(permissions, headers=EMPLOYEE, json={"user_ids":["u_employee_001"]}).status_code == 404
    assert client.put(permissions, headers=HEADERS, json={"user_ids":["unknown"]}).status_code == 422
    assert client.put(permissions, headers=HEADERS, json={"user_ids":["u_engineer_001"]}).status_code == 200
    assert not retrieve(client, headers=EMPLOYEE)["hits"]
    assert client.get(saved["url"], headers=EMPLOYEE).status_code == 404
    with app.state.ingestion_repository.connection() as db:
        content = db.execute("SELECT content FROM documents WHERE document_id=%s", (doc_id,)).fetchone()["content"]
    duplicate = client.post("/documents", headers=EMPLOYEE, files={"file":("vpn.md",content)})
    assert duplicate.status_code == 404
    assert client.get(saved["url"], headers=HEADERS).status_code == 200
    assert client.put(permissions, headers=HEADERS, json={"user_ids":[]}).status_code == 200
    assert not retrieve(client)["hits"]
    # Owner administration is separate from the reading ACL, so it can be restored.
    assert client.put(permissions, headers=HEADERS, json={"user_ids":["u_engineer_001"]}).status_code == 200
    assert retrieve(client)["hits"]


def test_authorization_change_during_vector_query(vector_app, monkeypatch):
    app, client = vector_app
    doc_id, _ = upload(app, client)
    client.post(f"/documents/{doc_id}/index", headers=HEADERS)
    repo = app.state.vector_repository
    original_search = repo.search
    def revoke_after_search(*args, **kwargs):
        hits = original_search(*args, **kwargs)
        app.state.document_repository.permissions(doc_id, LOCAL_IDENTITIES["u_engineer_001"], [])
        return hits
    monkeypatch.setattr(repo, "search", revoke_after_search)
    assert not retrieve(client)["hits"]


def test_filtered_hnsw_plan_and_fill(vector_app):
    app, client = vector_app
    doc_id, snapshot = upload(app, client)
    client.post(f"/documents/{doc_id}/index", headers=HEADERS)
    repo = app.state.vector_repository
    repo.create_hnsw()
    result = retrieve(client, top_k=3, mode="hnsw", filters={"document_ids":[doc_id]})
    assert len(result["hits"]) == min(3,snapshot["count"])
    service = app.state.vector_service
    identity = LOCAL_IDENTITIES["u_engineer_001"]
    vector = asyncio.run(service.client.embed(["TL-403 repair"])).vectors[0]
    plan = repo.search(vector, service.allowed_sets(identity,{"document_ids":[doc_id]}),
                       identity.tenant_id,3,"hnsw",explain=True)
    assert repo.index_name in str(plan)



def test_two_private_identities_cannot_read_each_others_content(vector_app):
    app,client=vector_app
    engineer_doc,_=upload(app,client)
    receipt=client.post("/documents",headers=SUPPORT,files={"file":("support.md",b"# Support private\nTL-403 incident escalation only.")},data={"version":"s1"}).json()
    app.state.ingestion_worker.process_next()
    support_doc=receipt["document_id"]
    client.post(f"/documents/{support_doc}/chunks",headers=SUPPORT,json={})
    for doc,headers in [(engineer_doc,HEADERS),(support_doc,SUPPORT)]:
        assert client.post(f"/documents/{doc}/index",headers=headers).status_code==200
    for own,other,headers,other_headers in [(engineer_doc,support_doc,HEADERS,SUPPORT),(support_doc,engineer_doc,SUPPORT,HEADERS)]:
        hits=retrieve(client,headers=headers)["hits"]
        assert hits and {h["document_id"] for h in hits}=={own}
        assert not retrieve(client,headers=headers,filters={"document_ids":[other]})["hits"]
        assert client.get(hits[0]["citation"]["url"],headers=other_headers).status_code==404
        assert client.delete(f"/documents/{own}",headers=other_headers).status_code==404


def test_invalid_update_preserves_old_active_version(vector_app):
    app,client=vector_app
    doc,_=upload(app,client)
    client.post(f"/documents/{doc}/index",headers=HEADERS)
    citation=retrieve(client)["hits"][0]["citation"]
    for filename,content,status in [("bad.txt",b"\xff",422),("blank.txt",b" ",422),("bad.pdf",b"x",415)]:
        assert client.put(f"/documents/{doc}",headers=HEADERS,files={"file":(filename,content)},data={"version":"v2","expected_version":"v1"}).status_code==status
        assert client.get(citation["url"],headers=HEADERS).status_code==200
    assert client.put(f"/documents/{doc}",headers=EMPLOYEE,files={"file":("valid.md",b"# Other")},data={"version":"v2","expected_version":"v1"}).status_code==404


def test_update_during_search_drops_old_hits(vector_app,monkeypatch):
    app,client=vector_app
    doc,_=upload(app,client)
    client.post(f"/documents/{doc}/index",headers=HEADERS)
    repo=app.state.vector_repository
    original=repo.search
    def change_after_query(*args,**kwargs):
        hits=original(*args,**kwargs)
        app.state.document_repository.update(doc,LOCAL_IDENTITIES["u_engineer_001"],"new.md",b"# New version\nnew policy", "v2","v1")
        return hits
    monkeypatch.setattr(repo,"search",change_after_query)
    assert not retrieve(client)["hits"]

def test_chat_real_retrieval_generation_and_citation(vector_app):
    app, client = vector_app
    doc_id, snapshot = upload(app, client)
    assert client.post(f"/documents/{doc_id}/index", headers=HEADERS).status_code == 200
    body = {"message":"TL-403 设备修复后等待多久？", "filters":{"document_ids":[doc_id]}}
    response = client.post("/chat", headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "answered" and "十分钟" in result["answer"]
    assert result["citations"]
    second = client.post("/chat", headers=HEADERS, json={**body,
        "message":"这个设备修复后等待多久？", "conversation_id":result["conversation_id"]})
    assert second.status_code == 200 and second.json()["status"] == "answered"
    citation = second.json()["citations"][0]
    assert client.get(citation["url"], headers=HEADERS).status_code == 200
    assert client.post("/chat", headers=EMPLOYEE, json={"message":body["message"]}).json()["status"] == "refused"


# Day 9 exercises the actual PostgreSQL candidate query and API, not only doubles.
@pytest.mark.parametrize('strategy', ['vector', 'hybrid', 'hybrid_rerank'])
def test_day9_modes_authorization_filters_versions_and_citations(vector_app, strategy):
    from app.retrieval.pipeline import RetrievalSettings
    from app.retrieval.rerank import RerankSettings, FakeRerankClient
    app, client = vector_app
    service = app.state.retrieval_service
    service.settings = RetrievalSettings(strategy=strategy, candidate_k=10,
                                         rerank=RerankSettings(provider='fake'))
    service.reranker = FakeRerankClient()
    doc, _ = upload(app, client)
    assert client.post(f'/documents/{doc}/index', headers=HEADERS).status_code == 200
    result = retrieve(client, strategy=strategy, candidate_k=10, top_k=3)
    assert result['diagnostics']['strategy'] == strategy and result['hits']
    assert result['diagnostics']['candidate_count'] >= len(result['hits'])
    if strategy != 'vector':
        assert result['diagnostics']['keyword_count'] > 0
        assert result['hits'][0]['ranks']
    if strategy == 'hybrid_rerank':
        assert result['diagnostics']['rerank']['status'] == 'succeeded'
    for h in result['hits']:
        assert client.get(h['citation']['url'], headers=HEADERS).status_code == 200
    assert not retrieve(client, headers=EMPLOYEE, strategy=strategy)['hits']
    assert not retrieve(client, strategy=strategy, filters={'document_ids': []})['hits']
    assert not retrieve(client, strategy=strategy, filters={'source': 'unknown'})['hits']
    # /chat uses the configured retrieval service.
    answer = client.post('/chat', headers=HEADERS, json={'message': 'TL-403 设备修复后等待多久？'})
    assert answer.status_code == 200 and answer.json()['status'] == 'answered'
    old = result['hits'][0]['citation']
    app.state.document_repository.update(doc, LOCAL_IDENTITIES['u_engineer_001'],
        'new.md', b'# New\nTL-403 new version', 'v2', 'v1')
    assert not retrieve(client, strategy=strategy)['hits']
    assert client.get(old['url'], headers=HEADERS).status_code == 404
    client.post(f'/documents/{doc}/chunks', headers=HEADERS, json={})
    assert client.post(f'/documents/{doc}/index', headers=HEADERS).status_code == 200
    assert all(h['version'] == 'v2' for h in retrieve(client, strategy=strategy)['hits'])
    app.state.document_repository.permissions(doc, LOCAL_IDENTITIES['u_engineer_001'], [])
    assert not retrieve(client, strategy=strategy)['hits']


def test_day9_lexical_revocation_during_query_does_not_leak_diagnostics(vector_app, monkeypatch):
    app, client = vector_app
    doc, _ = upload(app, client)
    client.post(f'/documents/{doc}/index', headers=HEADERS)
    repo = app.state.vector_repository
    original = repo.lexical_candidates
    def revoke(*args):
        hits = original(*args)
        app.state.document_repository.permissions(doc, LOCAL_IDENTITIES['u_engineer_001'], [])
        return hits
    monkeypatch.setattr(repo, 'lexical_candidates', revoke)
    result = retrieve(client, strategy='hybrid')
    assert result['hits'] == [] and result['diagnostics']['candidates'] == []


def test_day9_api_validation_and_missing_rerank(vector_app):
    app, client = vector_app
    for body in [dict(query='x', top_k=5, candidate_k=2), dict(query='x', candidate_k=True),
                 dict(query='x', strategy='unknown')]:
        assert client.post('/retrieve', headers=HEADERS, json=body).status_code == 422
    response = client.post('/retrieve', headers=HEADERS, json={'query':'x', 'strategy':'hybrid_rerank'})
    assert response.status_code == 503 and response.json()['error']['code'] == 'RERANK_NOT_CONFIGURED'


def test_pgvector_extension_shared_by_isolated_schemas():
    settings = EmbeddingSettings(collection="test-extension-" + uuid4().hex, dimensions=16)
    first = VectorRepository(pg_dsn("extension-first"), settings)
    second = VectorRepository(pg_dsn("extension-second"), settings)
    first.initialize()
    second.initialize()
    with second.connection() as db:
        row = db.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'").fetchone()
        assert row["nspname"] == "public"
        assert db.execute("SELECT vector_dims('[1,2]'::vector) AS dims").fetchone()["dims"] == 2
