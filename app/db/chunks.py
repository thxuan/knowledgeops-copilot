"""Persist complete chunk sets, then atomically switch the active pointer."""

import hashlib
import json
from dataclasses import asdict

from app.core.errors import AppError
from app.core.identity import Identity
from app.db.ingestion import IngestionRepository
from app.ingestion.chunker import ChunkConfig, chunk_document, text_revision


class ChunkRepository:
    def __init__(self, ingestion: IngestionRepository):
        self.ingestion = ingestion

    def initialize(self):
        with self.ingestion.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(52004001)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS chunk_sets (
                    chunk_set_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(document_id),
                    config_id TEXT NOT NULL, text_hash TEXT NOT NULL, config TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY, chunk_set_id TEXT NOT NULL REFERENCES chunk_sets(chunk_set_id),
                    ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(chunk_set_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS document_chunk_heads (
                    document_id TEXT PRIMARY KEY REFERENCES documents(document_id), chunk_set_id TEXT NOT NULL REFERENCES chunk_sets(chunk_set_id)
                );
            """)

    @staticmethod
    def _document(db, document_id: str, identity: Identity, lock=False):
        row = db.execute("""
            SELECT d.*, j.status FROM documents d JOIN ingestion_jobs j USING(document_id)
            JOIN document_access a USING(document_id)
            WHERE d.document_id = %s AND d.tenant_id = %s AND a.user_id = %s AND d.active=1
        """ + (" FOR UPDATE OF d FOR SHARE OF a" if lock else ""), (document_id, identity.tenant_id, identity.user_id)).fetchone()
        if row is None:
            raise AppError("NOT_FOUND", "Document not found", 404)
        if row["status"] != "succeeded":
            raise AppError("DOCUMENT_NOT_READY", "Parsing must succeed before chunking", 409)
        return dict(row)

    def build(self, document_id: str, identity: Identity, config: ChunkConfig) -> dict:
        with self.ingestion.connection() as db:
            document = self._document(db, document_id, identity)
        chunks = chunk_document(
            document_id, document["text"], title=document["title"], source=document["source"],
            version=document["version"], parser_version=document["parser_version"], config=config,
            markdown=document["filename"].lower().endswith(".md"), generation=document["revision"],
        )
        revision = text_revision(document["text"])
        key = json.dumps([document_id, config.config_id, revision, document["parser_version"], document["revision"]])
        set_id = hashlib.sha256(key.encode()).hexdigest()
        with self.ingestion.connection() as db:
            db.execute("BEGIN")
            current = self._document(db, document_id, identity, lock=True)
            if current["revision"] != document["revision"]:
                raise AppError("DOCUMENT_CHANGED", "Document changed; rebuild chunks", 409)
            db.execute("INSERT INTO chunk_sets VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                       (set_id, document_id, config.config_id, revision, json.dumps(asdict(config))))
            db.cursor().executemany("INSERT INTO chunks VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                           [(c.chunk_id, set_id, c.ordinal, json.dumps(asdict(c), ensure_ascii=False))
                            for c in chunks])
            db.execute("""INSERT INTO document_chunk_heads VALUES (%s, %s)
                       ON CONFLICT(document_id) DO UPDATE SET chunk_set_id = excluded.chunk_set_id""",
                       (document_id, set_id))
            return self._read_set(db, set_id)

    @staticmethod
    def _read_set(db, set_id: str) -> dict:
        row = dict(db.execute("SELECT * FROM chunk_sets WHERE chunk_set_id = %s", (set_id,)).fetchone())
        chunks = [json.loads(c["payload"]) for c in db.execute(
            "SELECT payload FROM chunks WHERE chunk_set_id = %s ORDER BY ordinal", (set_id,))]
        return {**row, "config": json.loads(row["config"]), "length_unit": "character",
                "coordinate_system": "cleaned_body:zero_based_half_open", "count": len(chunks), "chunks": chunks}

    def get(self, document_id: str, identity: Identity) -> dict:
        with self.ingestion.connection() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            self._document(db, document_id, identity)
            row = db.execute("SELECT chunk_set_id FROM document_chunk_heads WHERE document_id = %s",
                             (document_id,)).fetchone()
            if row is None:
                raise AppError("CHUNKS_NOT_BUILT", "Build chunks first", 409)
            return self._read_set(db, row["chunk_set_id"])
