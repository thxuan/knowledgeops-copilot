"""Start the real-model workbench using existing environment configuration."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    # Prefer the existing sample corpus only when the user did not select a database.
    if not os.getenv('KNOWLEDGEOPS_DATABASE_URL') and not os.getenv('KNOWLEDGEOPS_VECTOR_DATABASE_URL'):
        from app.db.postgres import DEFAULT_DATABASE_URL, connection
        from psycopg.conninfo import make_conninfo
        try:
            with connection(DEFAULT_DATABASE_URL) as db:
                present = db.execute("SELECT to_regclass('ko_samples.documents') AS name").fetchone()['name']
            if present:
                os.environ['KNOWLEDGEOPS_DATABASE_URL'] = make_conninfo(DEFAULT_DATABASE_URL, options='-c search_path=ko_samples,public')
        except Exception:
            pass  # Normal startup will report database availability.
    import uvicorn
    # Do not strip or replace the user's generation, embedding or Agent settings.
    print("KnowledgeOps: http://127.0.0.1:8000/ — configure a real model in Model settings", flush=True)
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000)
