import hashlib
import sqlite3
import pytest
from conftest import pg_dsn
from app.db.ingestion import IngestionRepository
from app.db.chunks import ChunkRepository
from app.db.conversations import ConversationRepository
from app.db.snapshots import initialize_core
from app.core.identity import LOCAL_IDENTITIES
from app.ingestion.chunker import ChunkConfig
from app.ingestion.worker import IngestionWorker
from scripts.migrate_sqlite_to_postgres import migrate

def test_import_preserves_ids_content_acl_heads_history_and_sequences(tmp_path):
    source = tmp_path / "legacy.db"
    with sqlite3.connect(source) as db:
        db.executescript("""CREATE TABLE documents (
            document_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, file_hash TEXT NOT NULL, parser_version TEXT NOT NULL,
            source TEXT NOT NULL, version TEXT, uploaded_by TEXT NOT NULL, content BLOB NOT NULL,
            text TEXT, title TEXT, metadata TEXT, created_at REAL NOT NULL);
            INSERT INTO documents VALUES ('doc','tenant','a.md',1,'hash','parser','source','v1','owner',X'61','a','a','{}',123);
            CREATE TABLE chunk_sets (chunk_set_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,
                config_id TEXT NOT NULL,text_hash TEXT NOT NULL,config TEXT NOT NULL);
            INSERT INTO chunk_sets VALUES ('old','doc','cfg','hash','{}');
            CREATE TABLE document_chunk_heads(document_id TEXT PRIMARY KEY,chunk_set_id TEXT);
            INSERT INTO document_chunk_heads VALUES ('doc','old');
            CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY,tenant_id TEXT,user_id TEXT,
                revision INTEGER,summary TEXT,created_at REAL,updated_at REAL);
            INSERT INTO conversations VALUES ('conv','tenant','owner',1,'{}',123,123);
            CREATE TABLE conversation_messages(message_id INTEGER PRIMARY KEY,conversation_id TEXT,
                role TEXT,content TEXT,payload TEXT,created_at REAL);
            INSERT INTO conversation_messages VALUES (40,'conv','user','question','{}',123);""")
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    url = pg_dsn()
    result = migrate(source, url)
    assert result['counts']['documents'] == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    intake = IngestionRepository(url)
    initialize_core(intake)
    with intake.connection() as db:
        row = db.execute('SELECT * FROM documents').fetchone()
        assert row['content'] == b'a' and row['revision'] == 1 and row['updated_at'] == 123
        assert db.execute('SELECT count(*) FROM document_access').fetchone()['count'] == 0
        assert db.execute('SELECT chunk_set_id FROM document_chunk_heads').fetchone()['chunk_set_id'] == 'old'
        row = db.execute("INSERT INTO conversation_messages(conversation_id,role,content,payload,created_at) VALUES ('conv','assistant','a','{}',124) RETURNING message_id").fetchone()
        assert row['message_id'] == 41
    with pytest.raises(ValueError, match='empty'):
        migrate(source, url)

def test_failed_import_rolls_back_all_rows(tmp_path):
    source = tmp_path / 'broken.db'
    with sqlite3.connect(source) as db:
        db.executescript("""CREATE TABLE documents(document_id TEXT,tenant_id TEXT,filename TEXT,
            size_bytes INTEGER,file_hash TEXT,parser_version TEXT,source TEXT,version TEXT,
            uploaded_by TEXT,content BLOB,created_at REAL);
            INSERT INTO documents VALUES ('doc','t','a.md',1,'h','p','s','v','u',X'61',1);
            CREATE TABLE document_access(document_id TEXT,user_id TEXT);
            INSERT INTO document_access VALUES ('missing','u');""")
    url = pg_dsn()
    import psycopg
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrate(source, url)
    with IngestionRepository(url).connection() as db:
        assert db.execute('SELECT count(*) FROM documents').fetchone()['count'] == 0
