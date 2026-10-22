"""Portable core snapshots and isolated PostgreSQL evaluation copies."""
import base64
import json
from uuid import uuid4
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg.conninfo import make_conninfo, conninfo_to_dict
from app.db.ingestion import IngestionRepository
from app.db.chunks import ChunkRepository
from app.db.conversations import ConversationRepository

TABLES = ("documents", "document_access", "ingestion_jobs", "ingestion_events",
          "chunk_sets", "chunks", "document_chunk_heads", "conversations", "conversation_messages",
          "document_versions", "document_version_heads", "document_version_builds", "document_lifecycle_events", "ko_index_jobs", "document_reviews")

def database_label(url):
    config = conninfo_to_dict(url)
    return {key: config.get(key) for key in ("host", "port", "dbname", "options")}

def initialize_core(repository):
    repository.initialize()
    ChunkRepository(repository).initialize()
    ConversationRepository(repository).initialize()

def restore_rows(db, tables):
    counts = {}
    for table in TABLES:
        rows = tables.get(table, [])
        counts[table] = len(rows)
        if not rows:
            continue
        columns = list(rows[0])
        statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join(sql.Placeholder() for _ in columns))
        db.cursor().executemany(statement, [tuple(Jsonb(row[c]) if isinstance(row[c],(dict,list)) else row[c] for c in columns) for row in rows])
    for table, column in (("ingestion_events", "event_id"), ("conversation_messages", "message_id"), ("document_lifecycle_events", "event_id")):
        maximum = db.execute(sql.SQL("SELECT max({}) AS value FROM {}").format(
            sql.Identifier(column), sql.Identifier(table))).fetchone()["value"]
        db.execute("SELECT setval(pg_get_serial_sequence(%s, %s), %s, %s)",
                   (table, column, maximum or 1, maximum is not None))
    return counts

def evaluation_snapshot(database_url, output):
    source = IngestionRepository(database_url)
    initialize_core(source)
    with source.connection() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        tables = {table: [dict(row) for row in db.execute(sql.SQL("SELECT * FROM {}").format(
            sql.Identifier(table)))] for table in TABLES}
    def encode(value):
        if isinstance(value, bytes):
            return {"bytea_base64": base64.b64encode(value).decode()}
        raise TypeError(type(value).__name__)
    (output / "snapshot.json").write_text(json.dumps({"format": "knowledgeops-core-v1", "tables": tables},
        default=encode, ensure_ascii=False, indent=2), encoding="utf-8")
    schema = "ko_eval_" + uuid4().hex
    with source.connection() as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    options = conninfo_to_dict(database_url).get("options", "")
    snapshot_url = make_conninfo(database_url, options=f"{options} -c search_path={schema},public")
    snapshot = IngestionRepository(snapshot_url)
    try:
        initialize_core(snapshot)
        with snapshot.connection() as db:
            restore_rows(db, tables)
    except Exception:
        with source.connection() as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        raise
    return snapshot, schema
