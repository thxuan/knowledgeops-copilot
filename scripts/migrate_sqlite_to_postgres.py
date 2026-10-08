"""One-time, read-only SQLite import; the application itself only uses PostgreSQL."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from psycopg import sql
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.config import Settings
from app.db.ingestion import IngestionRepository
from app.db.snapshots import TABLES, initialize_core, restore_rows, database_label

def migrate(source_path, database_url):
    source_path = Path(source_path).resolve(strict=True)
    if not source_path.is_file():
        raise ValueError("Source must be a SQLite file")
    # One consistent source snapshot, never write or delete the original file.
    with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        available = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "documents" not in available:
            raise ValueError("Not a KnowledgeOps intake database")
        tables = {table: [dict(row) for row in source.execute(f'SELECT * FROM "{table}"')]
                  if table in available else [] for table in TABLES}
    for row in tables["documents"]:
        row.setdefault("active", 1)
        row.setdefault("revision", 1)
        row.setdefault("updated_at", row["created_at"])
    target = IngestionRepository(database_url)
    initialize_core(target)
    with target.connection() as db:
        # Block concurrent writes during emptiness check and the all-or-nothing import.
        db.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
            sql.SQL(",").join(map(sql.Identifier, TABLES))))
        if any(db.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {}) AS present").format(
                sql.Identifier(table))).fetchone()["present"] for table in TABLES):
            raise ValueError("Target core tables must be empty; use a separate database/schema for another import")
        counts = restore_rows(db, tables)
        for table, count in counts.items():
            actual = db.execute(sql.SQL("SELECT count(*) AS value FROM {}").format(
                sql.Identifier(table))).fetchone()["value"]
            if actual != count:
                raise ValueError("Imported row count mismatch")
    return {"source": str(source_path), "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "target": database_label(database_url), "counts": counts, "source_preserved": True}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--report", type=Path, default=ROOT / "data/postgresql-migration.json")
    args = parser.parse_args()
    result = migrate(args.source, args.database_url or Settings.from_env().database_url)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
