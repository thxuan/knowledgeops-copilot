"""Exercise the actual API with Fake models and real PostgreSQL, in demo.py's scratch schema."""
import json
import logging
from pathlib import Path
import sys
import time
from uuid import uuid4
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.main import create_app

def main():
    settings = Settings.from_env()
    if settings.embedding.provider != "fake" or settings.generation.provider != "fake" or settings.agent.provider != "fake":
        raise ValueError("Smoke demo requires Fake providers; use scripts/demo.py smoke")
    import psycopg
    with psycopg.connect(settings.database_url) as db:
        schema = db.execute("SELECT current_schema()").fetchone()[0]
    if not schema.startswith("ko_demo_check_"):
        raise ValueError("Smoke demo only writes to demo.py's scratch schema")
    logging.getLogger("knowledgeops").setLevel(logging.WARNING)
    app = create_app(settings)
    checks = []
    engineer = {"X-Local-User-Id": "u_engineer_001"}
    employee = {"X-Local-User-Id": "u_employee_001"}
    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
    with TestClient(app) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, headers=engineer, **kwargs)
            if response.status_code >= 400:
                raise AssertionError(f"{method} {path}: {response.status_code} {response.text}")
            return response.json()
        def wait_run(run_id):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                state = request("GET", "/agent/" + run_id)
                if state["status"] != "running":
                    return state
                time.sleep(.05)
            raise AssertionError("Agent did not finish within 15s")
        check(request("GET", "/health")["status"] == "ok", "health")
        receipt = request("POST", "/documents", files={"file": ("demo.md", "# Demo\n\nDEMO-403：设备修复完成后等待十分钟。\n".encode())})
        deadline = time.monotonic() + 10
        while True:
            task = request("GET", "/ingestion/tasks/" + receipt["task_id"])
            if task["status"] in {"succeeded", "failed"}:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("Ingestion did not finish within 10s")
            time.sleep(.05)
        check(task["status"] == "succeeded", "upload and asynchronous parsing")
        duplicate = request("POST", "/documents", files={"file": ("demo.md", "# Demo\n\nDEMO-403：设备修复完成后等待十分钟。\n".encode())})
        check(duplicate["document_id"] == receipt["document_id"] and duplicate["duplicate"], "upload deduplication")
        doc = receipt["document_id"]
        request("POST", f"/documents/{doc}/chunks", json={})
        request("POST", f"/documents/{doc}/index")
        retrieval = request("POST", "/retrieve", json={"query": "DEMO-403 设备修复", "strategy": "hybrid", "filters": {"document_ids": [doc]}})
        check(bool(retrieval["hits"]), "hybrid retrieval")
        answer = request("POST", "/chat", json={"message": "DEMO-403 设备修复完成后等待多久？", "filters": {"document_ids": [doc]}})
        check(answer["status"] == "answered" and bool(answer["citations"]), "grounded answer and citations")
        check(client.get(answer["citations"][0]["url"], headers=engineer).status_code == 200, "citation read")
        forbidden = client.post("/retrieve", headers=employee, json={"query": "DEMO-403", "filters": {"document_ids": [doc]}})
        check(forbidden.status_code == 200 and not forbidden.json()["hits"], "cross-user evidence filtering")
        check(client.post("/chat", json={"message": "test"}).status_code == 401, "anonymous access rejected")
        calc = request("POST", "/agent", json={"message": "计算 12 + 28", "request_key": uuid4().hex})
        check(wait_run(calc["run_id"])["status"] == "completed", "Agent calculator")
        business = request("POST", "/agent", json={"message": "查询设备", "request_key": uuid4().hex})
        check(wait_run(business["run_id"])["status"] == "completed", "SELECT-only business tool")
        key = uuid4().hex
        ticket = request("POST", "/agent", json={"message": "创建工单", "request_key": key})
        pending = wait_run(ticket["run_id"])
        check(pending["status"] == "waiting_approval", "write pauses for approval")
        same = request("POST", "/agent", json={"message": "创建工单", "request_key": key})
        check(same["run_id"] == ticket["run_id"], "Agent request idempotence")
        approval = {k: pending["pending"][k] for k in ("action_id", "version", "digest")}
        approval["approve"] = True
        request("POST", f'/agent/{ticket["run_id"]}/approval', json=approval)
        check(wait_run(ticket["run_id"])["status"] == "completed", "approved write completes")
        request("POST", f'/agent/{ticket["run_id"]}/approval', json=approval)
        with app.state.persistent_agent.repo.connection() as db:
            count = db.execute("SELECT count(*) AS n FROM ko_agent_tickets WHERE action_id=%s", (approval["action_id"],)).fetchone()["n"]
        check(count == 1, "repeated approval creates one ticket")
    report = {"mode": "Fake providers + real PostgreSQL + in-process FastAPI HTTP client",
              "checks": sorted(set(checks)), "passed": len(set(checks)), "paid_model_calls": 0}
    (ROOT/"data").mkdir(exist_ok=True)
    (ROOT/"data/demo-smoke.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__ == "__main__":
    main()
