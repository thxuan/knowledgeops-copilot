"""Import isolation, real stored evidence and preserved business/approval boundaries."""
import asyncio
from dataclasses import asdict
import json
from uuid import uuid4
from psycopg import sql
from psycopg.conninfo import make_conninfo
from conftest import pg_dsn
from app.agent.contracts import AgentSettings
from app.agent.service import AgentService
from app.core.identity import LOCAL_IDENTITIES
from app.db.agent import AgentRepository
from app.db.chunks import ChunkRepository
from app.db.ingestion import IngestionRepository
from app.db.postgres import connection
from app.retrieval.lexical import bm25
from app.retrieval.objects import make_citation
from scripts.enterprise_corpus import import_corpus
from tests.unit.test_enterprise_corpus import fixture_corpus
from tests.agent_scenarios import ScenarioModel, approval_for, ticket_count


def test_enterprise_import_isolated_idempotent_and_uses_stored_evidence(tmp_path):
    base = pg_dsn()
    schema = "ko_enterprise_test_" + uuid4().hex
    fixture_corpus(tmp_path)
    owner = LOCAL_IDENTITIES["u_engineer_001"]
    try:
        first = import_corpus(tmp_path, base, schema)
        assert first["document_count"] == 15 and first["embedding_performed"] is False
        scoped = make_conninfo(base, options=f"-c search_path={schema},public")
        reader = make_conninfo(scoped, user="knowledgeops_agent_reader", password="local-agent-read-only")
        intake = IngestionRepository(scoped)
        with intake.connection() as db:
            db.execute("UPDATE ko_agent_business SET payload=%s WHERE user_id=%s AND query='device_status'",
                       ('{"compliant":true,"reason":"test-owned-state"}', owner.user_id))
            db.execute("UPDATE ko_agent_permissions SET can_write=false WHERE user_id=%s", (owner.user_id,))
            public_id = db.execute("SELECT document_id FROM documents WHERE source LIKE 'https://help.gnome.org/%'").fetchone()["document_id"]
            db.execute("DELETE FROM document_access WHERE document_id=%s AND user_id=%s",
                       (public_id, "u_support_001"))
        second = import_corpus(tmp_path, base, schema)
        assert second["document_count"] == 15
        settings = AgentSettings(database_url=scoped, business_database_url=reader, requests_per_minute=200)
        repo = AgentRepository(settings)
        assert repo.business(owner, "device_status")["data"]["reason"] == "test-owned-state"
        with intake.connection() as db:
            assert not db.execute("SELECT can_write FROM ko_agent_permissions WHERE user_id=%s", (owner.user_id,)).fetchone()["can_write"]
            assert db.execute("SELECT count(*) AS n FROM ko_embeddings").fetchone()["n"] == 0
            assert db.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 15
            assert db.execute("SELECT count(*) AS n FROM document_access WHERE document_id=%s AND user_id=%s",
                              (public_id, "u_support_001")).fetchone()["n"] == 0
            db.execute("UPDATE ko_agent_permissions SET can_write=true WHERE user_id=%s", (owner.user_id,))

        # The importer now stages private files. Explicitly seed an already-reviewed baseline
        # for the independent Agent/evidence checks below; importing itself never approves.
        manifest=json.loads((tmp_path/'manifest.json').read_text(encoding='utf-8'))
        with intake.connection() as db:
            assert db.execute("SELECT count(*) AS n FROM documents WHERE publication_state='draft'").fetchone()['n']==15
            for item in manifest['documents']:
                d=db.execute('SELECT document_id FROM documents WHERE source=%s',(item['source'],)).fetchone()
                did=d['document_id']
                db.execute("UPDATE documents SET publication_state='published' WHERE document_id=%s",(did,))
                db.execute('INSERT INTO document_chunk_heads SELECT document_id,chunk_set_id FROM chunk_sets WHERE document_id=%s ON CONFLICT(document_id) DO NOTHING',(did,))
                for user in item['user_ids']:
                    if not (did==public_id and user=='u_support_001'):
                        db.execute('INSERT INTO document_access VALUES (%s,%s) ON CONFLICT DO NOTHING',(did,user))

        class StoredRetrieval:
            async def retrieve(self, query, identity, top_k=3):
                with intake.connection() as db:
                    ids = [r["document_id"] for r in db.execute(
                        "SELECT document_id FROM document_access WHERE user_id=%s", (identity.user_id,))]
                hits = []
                for did in ids:
                    stored = ChunkRepository(intake).get(did, identity)
                    for chunk in stored["chunks"]:
                        hits.append({**chunk, "citation": asdict(make_citation(chunk, stored["chunk_set_id"]))})
                return {"hits": bm25(query, hits, top_k), "duration_ms": 0}

        retrieval = StoredRetrieval()
        state = repo.create(owner, "查找 TL-403 并查询本人设备状态", uuid4().hex, "enterprise-source-test")
        asyncio.run(AgentService(repo, ScenarioModel("A001"), settings, retrieval).execute(state["run_id"], owner))
        completed = repo.get(state["run_id"], owner)
        assert completed["status"] == "completed"
        evidence = next(t["result"]["untrusted_evidence"] for t in completed["trace"] if t["name"] == "search_knowledge_base")
        assert any("TL-403" in hit["text"] and hit["source"].startswith("sample_data/") for hit in evidence)
        assert "test-owned-state" in completed["answer"]

        state = repo.create(owner, "创建模拟工单", uuid4().hex, "enterprise-approval-test")
        asyncio.run(AgentService(repo, ScenarioModel("A008"), settings).execute(state["run_id"], owner))
        pending = repo.get(state["run_id"], owner)
        assert pending["status"] == "waiting_approval" and ticket_count(repo, state["run_id"]) == 0
        repo.approve(state["run_id"], owner, approval_for(pending, False))
        assert ticket_count(repo, state["run_id"]) == 0
    finally:
        with connection(base) as db:
            db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
