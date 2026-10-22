"""Explicit local PostgreSQL demo setup. Existing permissions are preserved."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from psycopg import sql
from app.agent.contracts import AgentSettings
from app.core.config import Settings
from app.core.identity import LOCAL_IDENTITIES
from app.db.agent import AgentRepository
READER = 'knowledgeops_agent_reader'
def setup(url, *, business_only=False):
    repo = AgentRepository(AgentSettings(database_url=url))
    repo.initialize()
    with repo.connection() as db:
        exists = db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (READER,)).fetchone()
        if not exists:
            db.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Identifier(READER), sql.Literal('local-agent-read-only')))
        schema = db.execute('SELECT current_schema() AS name').fetchone()['name']
        db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(READER)))
        db.execute(sql.SQL('GRANT SELECT ON ko_agent_business, ko_ticket_business TO {}').format(sql.Identifier(READER)))
    for identity in LOCAL_IDENTITIES.values():
        repo.seed_business(identity) if business_only else repo.seed_demo(identity)
    return repo
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database-url', default=Settings.from_env().database_url)
    parser.add_argument("--business-only", action="store_true", help="Add business fixtures without creating or restoring write permissions")
    args = parser.parse_args()
    if not args.database_url:
        parser.error('Set KNOWLEDGEOPS_DATABASE_URL or --database-url')
    setup(args.database_url, business_only=args.business_only)
    print('Agent tables, local fixtures and SELECT-only business role are ready. Existing permissions preserved.')
