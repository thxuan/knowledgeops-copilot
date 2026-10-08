"""Shared connection settings; preserve DSN options, including search_path."""
from contextlib import contextmanager
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

DEFAULT_DATABASE_URL = "postgresql://knowledgeops:local-day5-only@127.0.0.1:55432/knowledgeops"

def connection_options(database_url, *, statement_timeout=30000, lock_timeout=10000):
    existing = conninfo_to_dict(database_url).get("options", "")
    return f"{existing} -c statement_timeout={statement_timeout} -c lock_timeout={lock_timeout}"

def database_identity(database_url):
    config = conninfo_to_dict(database_url)
    return tuple(config.get(k, default) for k, default in
                 (("host", ""), ("port", "5432"), ("dbname", "")))

@contextmanager
def connection(database_url):
    with psycopg.connect(database_url, connect_timeout=5, row_factory=dict_row,
                         options=connection_options(database_url)) as db:
        yield db
