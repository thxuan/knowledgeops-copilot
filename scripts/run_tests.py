"""Run offline regressions in an isolated PostgreSQL schema (no model API calls)."""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.db.postgres import DEFAULT_DATABASE_URL
from scripts.setup_agent_demo import setup

def main():
    url = os.getenv('KNOWLEDGEOPS_TEST_DATABASE_URL') or DEFAULT_DATABASE_URL
    schema = 'ko_test_run_' + uuid4().hex
    scoped = make_conninfo(url, options=f'-c search_path={schema},public')
    created = False
    try:
        with psycopg.connect(url) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        created = True
        setup(scoped)
        env = {k:v for k,v in os.environ.items() if not k.startswith('KNOWLEDGEOPS_')}
        env.update(KNOWLEDGEOPS_DATABASE_URL=scoped, KNOWLEDGEOPS_TEST_DATABASE_URL=scoped,
                   KNOWLEDGEOPS_TEST_VECTOR_DATABASE_URL=scoped,
                   KNOWLEDGEOPS_TEST_AGENT_READER_URL=make_conninfo(scoped,user='knowledgeops_agent_reader',password='local-agent-read-only'),
                   PYTHONUTF8='1')
        return subprocess.call([sys.executable,'-B','-m','pytest','-p','no:cacheprovider',*sys.argv[1:]],cwd=ROOT,env=env)
    finally:
        if created:
            with psycopg.connect(url) as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
if __name__ == '__main__':
    raise SystemExit(main())
