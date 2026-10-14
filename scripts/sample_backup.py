"""Export reviewed samples; restore into a fresh schema using Compose PostgreSQL."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.db.postgres import DEFAULT_DATABASE_URL
from app.ingestion.parser import parse_document, clean_document
from app.ingestion.chunker import ChunkConfig, chunk_document
from scripts.compare_chunks import load_corpus

BACKUP = ROOT / 'sample_data/backups/ko_samples.dump'
MANIFEST = BACKUP.with_suffix('.json')
SCHEMA = 'ko_samples'
COLLECTION = 'bailian-v4-1024-validation-v1'
PLACEHOLDER = 'https://workspace.example.invalid/compatible-mode/v1'
TABLES = ['documents', 'document_access', 'ingestion_jobs', 'chunk_sets', 'chunks',
          'document_chunk_heads', 'ko_collections', 'ko_documents', 'ko_chunks',
          'ko_embeddings', 'ko_vector_heads']


def database_url(name):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', name):
        raise ValueError('Use a lowercase PostgreSQL database name')
    return make_conninfo(DEFAULT_DATABASE_URL, dbname=name)


def docker(tool, *args, content=None):
    result = subprocess.run(['docker', 'compose', 'exec', '-T', 'vectors', tool, *args],
                            cwd=ROOT, input=content, capture_output=True)
    if result.returncode:
        raise RuntimeError(f'{tool} failed: '+result.stderr.decode('utf-8', errors='replace')[:1200])
    return result.stdout


def connect(name, **kwargs):
    return psycopg.connect(database_url(name), row_factory=dict_row, **kwargs)


def reviewed_documents(db):
    corpus = {d['filename']: d for d in load_corpus().values()}
    rows = db.execute('SELECT * FROM ko_samples.documents ORDER BY filename').fetchall()
    if len(rows) != len(corpus) or len({d['filename'] for d in rows}) != len(corpus):
        raise ValueError('Export requires exactly the 14 reviewed sample documents')
    for d in rows:
        spec = corpus.get(d['filename'])
        if not spec:
            raise ValueError('Non-sample document found; export refused')
        content = (ROOT / 'sample_data/knowledge_base' / d['filename']).read_bytes()
        parsed = clean_document(parse_document(d['filename'], content))
        stored_content = bytes(d['content'])
        # Git on Windows may check out CRLF; compare sample text without that representation change.
        # The stored file hash must still verify the exact bytes from the database.
        if (stored_content.replace(b'\r\n', b'\n') != content.replace(b'\r\n', b'\n')
                or d['file_hash'] != hashlib.sha256(stored_content).hexdigest()
                or d['text'] != parsed.text or d['title'] != parsed.title
                or json.loads(d['metadata']) != parsed.metadata or d['version'] != spec['version']
                or d['source'] != 'sample_data/knowledge_base/'+d['filename']
                or not d['active'] or d['tenant_id'] != 'tenant_clearwater'
                or d['uploaded_by'] not in spec['allowed_user_ids']):
            raise ValueError('Sample content or metadata differs: '+d['filename'])
        access = [r['user_id'] for r in db.execute(
            'SELECT user_id FROM ko_samples.document_access WHERE document_id=%s ORDER BY user_id',
            (d['document_id'],))]
        if access != sorted(spec['allowed_user_ids']):
            raise ValueError('Sample permissions differ: '+d['filename'])
        head = db.execute('''SELECT s.* FROM ko_samples.chunk_sets s
            JOIN ko_samples.document_chunk_heads h USING(chunk_set_id)
            WHERE h.document_id=%s''', (d['document_id'],)).fetchone()
        if head is None:
            raise ValueError('Missing current chunks')
        expected = [asdict(c) for c in chunk_document(d['document_id'], parsed.text,
            title=parsed.title, source=d['source'], version=d['version'],
            parser_version=d['parser_version'], config=ChunkConfig(**json.loads(head['config'])),
            markdown=True, generation=d['revision'])]
        actual = [json.loads(c['payload']) for c in db.execute(
            'SELECT payload FROM ko_samples.chunks WHERE chunk_set_id=%s ORDER BY ordinal',
            (head['chunk_set_id'],))]
        if actual != json.loads(json.dumps(expected, ensure_ascii=False)):
            raise ValueError('Current chunks differ from reviewed sample text')
    return rows


def verify(db, *, endpoint=None):
    reviewed_documents(db)
    counts = {t: db.execute(sql.SQL('SELECT count(*) AS n FROM ko_samples.{}').format(
        sql.Identifier(t))).fetchone()['n'] for t in TABLES}
    if counts['documents'] != 14 or counts['ko_collections'] != 1:
        raise ValueError('Unexpected documents or collections')
    collection = db.execute('SELECT * FROM ko_samples.ko_collections').fetchone()
    signature = collection['signature']
    expected = {'provider': 'compatible', 'model_id': 'text-embedding-v4', 'dimensions': 1024,
                'distance': 'cosine', 'input_format': 'title-heading-text-v1',
                'endpoint': endpoint or signature['endpoint']}
    if collection['collection'] != COLLECTION or signature != expected or collection['dimensions'] != 1024:
        raise ValueError('Embedding signature does not match this backup')
    bad = db.execute('''SELECT count(*) AS n FROM ko_samples.document_chunk_heads h
        LEFT JOIN ko_samples.ko_vector_heads v ON v.document_id=h.document_id AND v.collection=%s
        WHERE v.chunk_set_id IS DISTINCT FROM h.chunk_set_id OR v.chunk_count <>
        (SELECT count(*) FROM ko_samples.chunks c WHERE c.chunk_set_id=h.chunk_set_id)''',
        (COLLECTION,)).fetchone()['n']
    if bad or counts['ko_vector_heads'] != 14 or counts['chunks'] != counts['ko_embeddings']:
        raise ValueError('Current vector heads or embedding counts are incomplete')
    bad = db.execute('''SELECT count(*) AS n FROM ko_samples.chunks c
        LEFT JOIN ko_samples.ko_chunks k USING(chunk_id)
        LEFT JOIN ko_samples.ko_embeddings e ON e.chunk_id=c.chunk_id AND e.collection=%s
        WHERE k.chunk_id IS NULL OR e.chunk_id IS NULL OR k.payload <> c.payload::jsonb
        OR e.dimensions<>1024 OR vector_dims(e.embedding)<>1024 OR vector_norm(e.embedding)<=0''',
        (COLLECTION,)).fetchone()['n']
    if bad or counts['ko_chunks'] != counts['chunks'] or counts['ko_documents'] != 14:
        raise ValueError('Stored vectors are incomplete or reference different text')
    if db.execute("SELECT count(*) AS n FROM ko_samples.ingestion_jobs WHERE status<>'succeeded' OR error_message IS NOT NULL").fetchone()['n']:
        raise ValueError('Unexpected ingestion state')
    return counts


def export_backup():
    temp = 'ko_export_'+uuid4().hex
    created = False
    try:
        with connect('knowledgeops', autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(temp)))
        created = True
        with connect(temp, autocommit=True) as target:
            target.execute('CREATE EXTENSION vector WITH SCHEMA public')
            target.execute('CREATE SCHEMA ko_samples')
        definitions = docker('pg_dump', '-U', 'knowledgeops', '-d', 'knowledgeops',
            '--schema-only', '--no-owner', '--no-acl',
            *[arg for t in TABLES for arg in ('--table', SCHEMA+'.'+t)])
        docker('psql', '-X', '-U', 'knowledgeops', '-d', temp, '-v', 'ON_ERROR_STOP=1', content=definitions)
        with connect('knowledgeops') as source, connect(temp) as target:
            source.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            reviewed_documents(source)
            original = source.execute('SELECT * FROM ko_samples.ko_collections WHERE collection=%s',
                                      (COLLECTION,)).fetchone()
            if original is None or original['signature']['provider'] != 'compatible':
                raise ValueError('Real sample embedding collection is absent')
            embedding_endpoint(original['signature']['endpoint'])
            queries = {
                'chunk_sets': 'SELECT s.* FROM ko_samples.chunk_sets s JOIN ko_samples.document_chunk_heads h USING(chunk_set_id)',
                'chunks': 'SELECT c.* FROM ko_samples.chunks c JOIN ko_samples.document_chunk_heads h USING(chunk_set_id)',
                'ko_collections': sql.SQL('SELECT * FROM ko_samples.ko_collections WHERE collection={}').format(sql.Literal(COLLECTION)),
                'ko_documents': 'SELECT d.* FROM ko_samples.ko_documents d JOIN ko_samples.documents s USING(document_id)',
                'ko_chunks': 'SELECT c.* FROM ko_samples.ko_chunks c JOIN ko_samples.document_chunk_heads h USING(chunk_set_id)',
                'ko_embeddings': sql.SQL('SELECT e.* FROM ko_samples.ko_embeddings e JOIN ko_samples.ko_chunks c USING(chunk_id) JOIN ko_samples.document_chunk_heads h USING(chunk_set_id) WHERE e.collection={}').format(sql.Literal(COLLECTION)),
                'ko_vector_heads': sql.SQL('SELECT * FROM ko_samples.ko_vector_heads WHERE collection={}').format(sql.Literal(COLLECTION)),
            }
            for table in TABLES:
                query = queries.get(table, sql.SQL('SELECT * FROM ko_samples.{}').format(sql.Identifier(table)))
                with source.cursor().copy(sql.SQL('COPY ({}) TO STDOUT').format(sql.SQL(query) if isinstance(query,str) else query)) as reader:
                    with target.cursor().copy(sql.SQL('COPY ko_samples.{} FROM STDIN').format(sql.Identifier(table))) as writer:
                        for block in reader:
                            writer.write(block)
            signature = {**original['signature'], 'endpoint': PLACEHOLDER}
            target.execute('UPDATE ko_samples.ko_collections SET signature=%s', (Jsonb(signature),))
            target.execute("UPDATE ko_samples.ingestion_jobs SET attempts=1,lease_until=NULL,claim_token=NULL,error_code=NULL,error_message=NULL")
            for index in target.execute("SELECT indexname FROM pg_indexes WHERE schemaname='ko_samples' AND indexdef LIKE '%hnsw%'").fetchall():
                target.execute(sql.SQL('DROP INDEX ko_samples.{}').format(sql.Identifier(index['indexname'])))
            counts = verify(target, endpoint=PLACEHOLDER)
        archive = docker('pg_dump', '-U', 'knowledgeops', '-d', temp, '--schema=ko_samples',
                         '--format=custom', '--compress=9', '--no-owner', '--no-acl')
        contents = docker('pg_restore', '--no-owner', '--no-acl', '--file=-', content=archive)
        if re.search(rb'ws-[a-z0-9]+\.cn-beijing|sk-[A-Za-z0-9_-]{20,}|github_pat_|BEGIN.*PRIVATE KEY', contents):
            raise ValueError('Potential credential or source workspace in export')
        BACKUP.parent.mkdir(parents=True, exist_ok=True)
        BACKUP.write_bytes(archive)
        manifest = {'format': 'postgresql-custom', 'schema': SCHEMA, 'exported_at': datetime.now(timezone.utc).isoformat(),
            'postgresql_major': 17, 'pgvector_version': '0.8.0', 'collection': COLLECTION,
            'embedding': signature, 'embedding_region': 'China (Beijing)',
            'counts': counts, 'archive_bytes': len(archive),
            'sha256': hashlib.sha256(archive).hexdigest(),
            'excluded': ['conversations', 'conversation_messages', 'agent state and business tables',
                         'ingestion_events', 'ko_embedding_batches', 'fake and unconfigured collections',
                         'source workspace endpoint', 'database owners and grants'],
            'sample_sha256': {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'sample_data/knowledge_base').glob('*.md'))}}
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({'backup':str(BACKUP.relative_to(ROOT)), 'bytes':len(archive), 'counts':counts}))
    finally:
        if created:
            with connect('knowledgeops', autocommit=True) as admin:
                admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(temp)))


def embedding_endpoint(value):
    parts = urlsplit(value.rstrip('/'))
    if (parts.scheme != 'https' or parts.username or parts.password or parts.port or parts.query or parts.fragment
            or parts.path != '/compatible-mode/v1'
            or not (parts.hostname == 'dashscope.aliyuncs.com' or re.fullmatch(
                r'[a-z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com', parts.hostname or ''))):
        raise ValueError('Use your Beijing text-embedding-v4 HTTPS Base URL ending /compatible-mode/v1')
    return value.rstrip('/')


def restore_backup(name, endpoint):
    endpoint = embedding_endpoint(endpoint)
    archive = BACKUP.read_bytes()
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    if hashlib.sha256(archive).hexdigest() != manifest['sha256']:
        raise ValueError('Backup SHA-256 differs from manifest; restore refused')
    with connect(name, autocommit=True) as db:
        if int(db.execute('SHOW server_version_num').fetchone()['server_version_num'])//10000 != 17:
            raise ValueError('Use the provided PostgreSQL 17 Compose service')
        db.execute('SELECT pg_advisory_lock(52006001)')
        if db.execute("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='ko_samples') AS present").fetchone()['present']:
            raise ValueError('ko_samples already exists; nothing overwritten. Restore into a fresh database instead.')
        contents = docker('pg_restore', '--no-owner', '--no-acl', '--file=-', content=archive)
        binding = sql.SQL("UPDATE ko_samples.ko_collections SET signature=jsonb_set(signature,'{{endpoint}}',to_jsonb({}::text)) WHERE collection={};").format(
            sql.Literal(endpoint), sql.Literal(COLLECTION)).as_string(db).encode()
        docker('psql', '-X', '-U', 'knowledgeops', '-d', name, '-v', 'ON_ERROR_STOP=1', '--single-transaction',
               content=b'CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;\n'+contents+b'\n'+binding)
        with db.transaction():
            counts = verify(db, endpoint=endpoint)
        print(json.dumps({'schema':SCHEMA,'database':name,'collection':COLLECTION,'counts':counts,'model_api_calls':0}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('export', help='Export reviewed local ko_samples; source data is never modified')
    restore = sub.add_parser('restore', help='Restore into a NEW ko_samples in the Compose database')
    restore.add_argument('--database', default='knowledgeops', help='Existing fresh database on Compose vectors service')
    restore.add_argument('--embedding-base-url', required=True, help='Your own Beijing text-embedding-v4 Base URL (no Key)')
    args = parser.parse_args()
    try:
        if args.command == 'export':
            export_backup()
        else:
            restore_backup(args.database, args.embedding_base_url)
    except (ValueError, RuntimeError, psycopg.Error) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == '__main__':
    main()
