"""Start the existing workbench in the imported enterprise-support schema."""
import argparse
import os
from pathlib import Path
import re
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from psycopg.conninfo import make_conninfo
    from app.db.postgres import DEFAULT_DATABASE_URL, connection
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("KNOWLEDGEOPS_DATABASE_URL") or DEFAULT_DATABASE_URL)
    parser.add_argument("--schema", default="ko_enterprise_zh")
    args = parser.parse_args()
    if not re.fullmatch(r"ko_enterprise_[a-z0-9_]{1,45}", args.schema):
        parser.error("Use a dedicated ko_enterprise_ schema")
    scoped = make_conninfo(args.database_url, options=f"-c search_path={args.schema},public")
    with connection(scoped) as db:
        exists = db.execute("SELECT to_regclass(%s) AS name", (args.schema + ".documents",)).fetchone()["name"]
    if not exists:
        parser.error("Run python scripts/enterprise_corpus.py import first")
    os.environ["KNOWLEDGEOPS_DATABASE_URL"] = scoped
    os.environ.setdefault("KNOWLEDGEOPS_AGENT_BUSINESS_DATABASE_URL",
        make_conninfo(scoped, user="knowledgeops_agent_reader", password="local-agent-read-only"))
    print(f"Enterprise support corpus: {args.schema}", flush=True)
    # Keep generation, embedding and Agent settings; reuse the established entry point.
    runpy.run_path(str(ROOT / "scripts/workbench.py"), run_name="__main__")


if __name__ == "__main__":
    main()
