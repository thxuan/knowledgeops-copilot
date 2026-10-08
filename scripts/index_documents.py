"""Repeatable local indexing; trusted sample seeding uses a PostgreSQL database."""
import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.config import Settings
from app.core.embedding import make_embedding_client
from app.core.identity import LOCAL_IDENTITIES
from app.db.ingestion import IngestionRepository
from app.db.snapshots import database_label
from app.db.chunks import ChunkRepository
from app.db.vectors import VectorRepository
from app.ingestion.chunker import ChunkConfig
from app.ingestion.parser import PARSER_VERSION
from app.ingestion.worker import IngestionWorker
from app.retrieval.service import VectorService
from scripts.compare_chunks import load_corpus

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--user", choices=LOCAL_IDENTITIES, default="u_engineer_001")
    parser.add_argument("--document-id", action="append")
    parser.add_argument("--seed-samples", action="store_true")
    parser.add_argument("--hnsw", action="store_true")
    parser.add_argument("--report-name", default="day5-index")
    args = parser.parse_args()
    settings = Settings.from_env()
    if not settings.vector_database_url:
        raise SystemExit("Set KNOWLEDGEOPS_DATABASE_URL first")
    intake = IngestionRepository(args.database_url or settings.database_url)
    intake.initialize()
    chunks = ChunkRepository(intake)
    chunks.initialize()
    if args.seed_samples:
        corpus = load_corpus()
        sources = ["sample_data/knowledge_base/" + doc["filename"] for doc in corpus.values()]
        with intake.connection() as db:
            if db.execute("SELECT EXISTS (SELECT 1 FROM documents WHERE source <> ALL(%s)) AS present",
                          (sources,)).fetchone()["present"]:
                raise SystemExit("Seed samples only into an empty or existing sample schema")
        for doc in corpus.values():
            content = (ROOT / "sample_data/knowledge_base" / doc["filename"]).read_bytes()
            # Only this reviewed local fixture manifest grants access; uploads cannot.
            owner = LOCAL_IDENTITIES[doc["allowed_user_ids"][0]]
            receipt = intake.submit(identity=owner, filename=doc["filename"], content=content,
                file_hash=hashlib.sha256(content).hexdigest(), parser_version=PARSER_VERSION + ":md",
                source="sample_data/knowledge_base/" + doc["filename"], version=doc["version"])
            from app.db.documents import DocumentRepository
            with intake.connection() as db:
                current_acl = sorted(row["user_id"] for row in db.execute(
                    "SELECT user_id FROM document_access WHERE document_id=%s", (receipt["document_id"],)))
            if current_acl != sorted(doc["allowed_user_ids"]):
                DocumentRepository(intake).permissions(receipt["document_id"], owner, doc["allowed_user_ids"])
        worker = IngestionWorker(intake)
        while worker.process_next():
            pass
        with intake.connection() as db:
            rows = db.execute("SELECT d.document_id,min(a.user_id) AS user_id FROM documents d JOIN document_access a USING(document_id) GROUP BY d.document_id").fetchall()
        for row in rows:
            chunks.build(row["document_id"], LOCAL_IDENTITIES[row["user_id"]], ChunkConfig())
    vectors = VectorRepository(args.database_url or settings.database_url, settings.embedding)
    vectors.initialize()
    service = VectorService(chunks, vectors, make_embedding_client(settings.embedding), settings.embedding)
    identity = LOCAL_IDENTITIES[args.user]
    document_ids = args.document_id or [a["document_id"] for a in service.allowed_sets(identity)]
    if args.seed_samples and not args.document_id:
        # Seed all 14 trusted fixtures using a permitted actor for each document.
        results = [await service.index_document(row["document_id"], LOCAL_IDENTITIES[row["user_id"]]) for row in rows]
    else:
        results = [await service.index_document(doc, identity) for doc in document_ids]
    if args.hnsw:
        vectors.create_hnsw()
    report = {"provider": settings.embedding.provider, "model_id": settings.embedding.model_id,
              "dimensions": settings.embedding.dimensions, "collection": settings.embedding.collection,
              "database": database_label(args.database_url or settings.database_url), "hnsw_created": args.hnsw, "documents": results}
    directory = ROOT / "docs/experiments"
    directory.mkdir(exist_ok=True)
    if Path(args.report_name).name != args.report_name:
        raise SystemExit("Report name must be a filename stem")
    (directory / (args.report_name + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"document_count": len(results), "chunk_count": sum(r["count"] for r in results),
                      "reused_documents": sum(r["reused"] for r in results), "database": report["database"]}, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
