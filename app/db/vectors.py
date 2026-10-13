"""PostgreSQL snapshots, atomic publication and isolated cosine vector spaces."""
from contextlib import contextmanager, nullcontext
import hashlib
import json
import psycopg
from app.db.postgres import connection_options
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from app.core.embedding import EmbeddingSettings, EmbeddingResult, validate_result
from app.core.errors import AppError

class VectorRepository:
    def __init__(self, database_url: str, settings: EmbeddingSettings):
        settings.validate()
        self.database_url, self.settings = database_url, settings
        self.index_name = "ko_hnsw_" + hashlib.sha256(settings.collection.encode()).hexdigest()[:20]

    @contextmanager
    def connection(self):
        with psycopg.connect(self.database_url, connect_timeout=5, row_factory=dict_row,
                             options=connection_options(self.database_url)) as db:
            yield db

    def initialize(self):
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(52005001)")
            db.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            db.execute("""
                CREATE TABLE IF NOT EXISTS ko_collections (
                    collection TEXT PRIMARY KEY, signature JSONB NOT NULL,
                    dimensions INTEGER NOT NULL, UNIQUE(collection, dimensions)
                );
                CREATE TABLE IF NOT EXISTS ko_documents (
                    document_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    source TEXT NOT NULL, version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ko_chunks (
                    chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES ko_documents,
                    chunk_set_id TEXT NOT NULL, payload JSONB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ko_embeddings (
                    collection TEXT NOT NULL, chunk_id TEXT NOT NULL REFERENCES ko_chunks,
                    dimensions INTEGER NOT NULL, embedding vector NOT NULL,
                    PRIMARY KEY(collection, chunk_id),
                    FOREIGN KEY(collection, dimensions) REFERENCES ko_collections(collection, dimensions),
                    CHECK(vector_dims(embedding) = dimensions), CHECK(vector_norm(embedding) > 0)
                );
                CREATE TABLE IF NOT EXISTS ko_vector_heads (
                    collection TEXT NOT NULL REFERENCES ko_collections,
                    document_id TEXT NOT NULL REFERENCES ko_documents,
                    chunk_set_id TEXT NOT NULL, chunk_count INTEGER NOT NULL,
                    PRIMARY KEY(collection, document_id)
                );
                CREATE TABLE IF NOT EXISTS ko_embedding_batches (
                    batch_id TEXT PRIMARY KEY, collection TEXT NOT NULL REFERENCES ko_collections,
                    run_id TEXT NOT NULL, document_id TEXT, batch_number INTEGER NOT NULL,
                    attempt INTEGER NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL,
                    input_count INTEGER NOT NULL, input_chars INTEGER NOT NULL, duration_ms DOUBLE PRECISION NOT NULL,
                    total_tokens BIGINT, estimated_cost DOUBLE PRECISION, error_code TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
            db.execute("INSERT INTO ko_collections VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                       (self.settings.collection, Jsonb(self.settings.signature), self.settings.dimensions))
            self._check(db)

    def _check(self, db):
        row = db.execute("SELECT signature FROM ko_collections WHERE collection = %s",
                         (self.settings.collection,)).fetchone()
        if row is None or row["signature"] != self.settings.signature:
            raise AppError("EMBEDDING_CONFIG_MISMATCH", "Collection configuration differs; use a new collection", 409)

    def ready(self, document_id, set_id, count):
        with self.connection() as db:
            self._check(db)
            row = db.execute("SELECT chunk_set_id, chunk_count FROM ko_vector_heads WHERE collection=%s AND document_id=%s",
                             (self.settings.collection, document_id)).fetchone()
            return bool(row and row["chunk_set_id"] == set_id and row["chunk_count"] == count)

    def publish(self, snapshot, tenant_id, vectors, *, transaction=None):
        validate_result(EmbeddingResult(self.settings.model_id, vectors), self.settings, snapshot["count"])
        chunks = snapshot["chunks"]
        with (nullcontext(transaction) if transaction is not None else self.connection()) as db:
            self._check(db)
            db.execute("""INSERT INTO ko_documents VALUES (%s,%s,%s,%s)
                ON CONFLICT(document_id) DO UPDATE SET source=excluded.source, version=excluded.version
                WHERE ko_documents.tenant_id=excluded.tenant_id""",
                (snapshot["document_id"], tenant_id, chunks[0]["source"], chunks[0]["version"]))
            doc = db.execute("SELECT tenant_id FROM ko_documents WHERE document_id=%s",
                             (snapshot["document_id"],)).fetchone()
            if doc["tenant_id"] != tenant_id:
                raise AppError("NOT_FOUND", "Document not found", 404)
            with db.cursor() as cursor:
                cursor.executemany("INSERT INTO ko_chunks VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    [(c["chunk_id"], c["document_id"], snapshot["chunk_set_id"], Jsonb(c)) for c in chunks])
                cursor.executemany("""INSERT INTO ko_embeddings VALUES (%s,%s,%s,%s::vector)
                    ON CONFLICT(collection, chunk_id) DO NOTHING""",
                    [(self.settings.collection, c["chunk_id"], self.settings.dimensions, json.dumps(v))
                     for c, v in zip(chunks, vectors, strict=True)])
            db.execute("""INSERT INTO ko_vector_heads VALUES (%s,%s,%s,%s)
                ON CONFLICT(collection,document_id) DO UPDATE
                SET chunk_set_id=excluded.chunk_set_id,chunk_count=excluded.chunk_count""",
                (self.settings.collection, snapshot["document_id"], snapshot["chunk_set_id"], snapshot["count"]))

    def record_batch(self, event):
        with self.connection() as db:
            db.execute("""INSERT INTO ko_embedding_batches
                (batch_id,collection,run_id,document_id,batch_number,attempt,purpose,status,
                 input_count,input_chars,duration_ms,total_tokens,estimated_cost,error_code)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (event["batch_id"], self.settings.collection, event["run_id"], event["document_id"],
                 event["batch_number"], event["attempt"], event["purpose"], event["status"],
                 event["input_count"], event["input_chars"], event["duration_ms"], event["total_tokens"],
                 event["estimated_cost"], event["error_code"]))

    def create_hnsw(self):
        with self.connection() as db:
            self._check(db)
            db.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON ko_embeddings USING hnsw ((embedding::vector({})) vector_cosine_ops) WHERE collection = {}")
                .format(sql.Identifier(self.index_name), sql.Literal(self.settings.dimensions), sql.Literal(self.settings.collection)))
            db.execute("ANALYZE ko_embeddings")

    def search(self, vector, allowed, tenant_id, top_k, mode="exact", ef_search=40, explain=False):
        validate_result(EmbeddingResult(self.settings.model_id, [vector]), self.settings, 1)
        if mode not in {"exact", "hnsw"} or not 1 <= top_k <= 50 or not 1 <= ef_search <= 1000:
            raise ValueError("Invalid search configuration")
        # OFFSET 0 preserves a parameterized LATERAL filter. The outer vector scan
        # keeps distance order while validating each candidate against active/allowed sets.
        query = sql.SQL("""SELECT c.payload, (e.embedding::vector({}) <=> %s::vector) AS distance
            FROM ko_embeddings e
            JOIN LATERAL (
                SELECT c.payload FROM ko_chunks c JOIN ko_documents d USING(document_id)
                JOIN ko_vector_heads h ON h.document_id=c.document_id
                    AND h.collection=e.collection AND h.chunk_set_id=c.chunk_set_id
                JOIN jsonb_to_recordset(%s::jsonb) AS a(document_id text, chunk_set_id text)
                    ON a.document_id=c.document_id AND a.chunk_set_id=c.chunk_set_id
                WHERE c.chunk_id=e.chunk_id AND d.tenant_id=%s
                OFFSET 0
            ) c ON true
            WHERE e.collection={}
            ORDER BY e.embedding::vector({}) <=> %s::vector LIMIT %s""").format(
                sql.Literal(self.settings.dimensions), sql.Literal(self.settings.collection), sql.Literal(self.settings.dimensions))
        params = (json.dumps(vector), Jsonb(allowed), tenant_id, json.dumps(vector), top_k)
        with self.connection() as db:
            self._check(db)
            if mode == "exact":
                db.execute("SET LOCAL enable_indexscan = off")
                db.execute("SET LOCAL enable_bitmapscan = off")
            else:
                if db.execute("SELECT to_regclass(%s) AS name", (self.index_name,)).fetchone()["name"] is None:
                    raise AppError("HNSW_NOT_BUILT", "Build the HNSW index first", 409)
                db.execute("SET LOCAL enable_seqscan = off")
                db.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(ef_search),))
                db.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            if explain:
                return db.execute(sql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ") + query, params).fetchone()["QUERY PLAN"]
            rows = db.execute(query, params).fetchall()
        return [{**r["payload"], "distance": r["distance"], "score": 1 - r["distance"]} for r in rows]

    def lexical_candidates(self, allowed, tenant_id):
        """Published embeddings in current, authorized chunk sets only."""
        if not allowed:
            return []
        with self.connection() as db:
            self._check(db)
            rows = db.execute("""SELECT c.payload, c.chunk_set_id FROM ko_chunks c
                JOIN ko_documents d USING(document_id)
                JOIN ko_embeddings e ON e.chunk_id=c.chunk_id AND e.collection=%s
                JOIN ko_vector_heads h ON h.document_id=c.document_id AND h.collection=e.collection
                    AND h.chunk_set_id=c.chunk_set_id
                JOIN jsonb_to_recordset(%s::jsonb) AS a(document_id text, chunk_set_id text)
                    ON a.document_id=c.document_id AND a.chunk_set_id=c.chunk_set_id
                WHERE d.tenant_id=%s ORDER BY c.chunk_id""",
                (self.settings.collection, Jsonb(allowed), tenant_id)).fetchall()
        return [{**r["payload"], "chunk_set_id": r["chunk_set_id"]} for r in rows]

    def batch_events(self, run_id):
        with self.connection() as db:
            return db.execute("SELECT batch_number,attempt,purpose,status,input_count,input_chars,duration_ms,total_tokens,estimated_cost,error_code FROM ko_embedding_batches WHERE run_id=%s ORDER BY batch_number,attempt",
                              (run_id,)).fetchall()
