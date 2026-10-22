"""Unified conversation, real-only configuration and credential boundaries."""
import json
import time
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.core.config import Settings
from app.agent.contracts import AgentSettings
from app.generation.provider import GenerationSettings
from conftest import pg_dsn

HEADERS = {"X-Local-User-Id": "u_engineer_001"}

class RouterModel:
    def __init__(self):
        self.calls = []
    async def generate(self, messages, settings):
        self.calls.append(messages)
        return '{"intent":"agent"}'

class TaskModel:
    def __init__(self):
        self.calls = []
    async def complete(self, messages, tools):
        self.calls.append(messages.copy())
        return {"message": {"role":"assistant", "content":'{"status":"needs_clarification","answer":"请补充故障系统。"}'}, "model":"real-test", "usage":{}}

@pytest.fixture
def session_app():
    url = pg_dsn("session")
    gs = GenerationSettings(provider="compatible", model_id="real-test", base_url="https://model.example/v1", api_key="private-secret")
    agent = AgentSettings(database_url=url, provider="compatible", model_id="real-test", api_key="private-secret", input_price=0, output_price=0)
    route, task = RouterModel(), TaskModel()
    app = create_app(Settings(app_env="test", database_url=url, generation=gs, agent=agent, ingestion_worker_enabled=False),generation_client=route,agent_model=task)
    with TestClient(app) as client:
        yield app, client, route, task


def test_real_only_and_safe_model_configuration(session_app):
    app,client,_,_ = session_app
    assert client.get("/workbench/model").status_code == 401
    view = client.get("/workbench/model",headers={'X-Local-User-Id':'u_admin_001'}).json()
    assert view["key_present"] and "private-secret" not in json.dumps(view)
    assert client.post("/workbench/chat",headers=HEADERS,json={"message":"x","generation":{"model":"fake"}}).status_code == 422
    body = {"base_url":"https://other.example/v1","model_id":"new-model","input_price":1,"output_price":2}
    assert client.put("/workbench/model",headers={'X-Local-User-Id':'u_admin_001'},json=body).status_code == 422
    assert app.state.chat.settings.api_key == "private-secret"
    body["api_key"] = "new-private-secret"
    response = client.put("/workbench/model",headers={'X-Local-User-Id':'u_admin_001'},json=body)
    assert response.status_code == 200
    assert "new-private-secret" not in response.text
    assert app.state.chat.settings.api_key == "new-private-secret"
    assert app.state.persistent_agent.settings.model_id == "new-model"
    assert app.state.persistent_agent.settings.input_price == 1


def test_unconfigured_model_never_falls_back(session_app):
    app,client,_,_ = session_app
    app.state.chat.settings = GenerationSettings()
    r = client.post("/workbench/message",headers=HEADERS,json={"message":"help", "request_key":"request-no-model"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "MODEL_NOT_CONFIGURED"


def test_context_roundtrip_and_duplicate_agent_request(session_app):
    app,client,route,task = session_app
    from app.core.identity import LOCAL_IDENTITIES
    identity = LOCAL_IDENTITIES[HEADERS["X-Local-User-Id"]]
    snapshot = app.state.conversations.load(None,identity)
    app.state.conversations.commit(snapshot,identity,"VPN 报错 TL-403",{"answer":"先刷新合规状态。"},[],2048)
    body = {"message":"还是不行，帮我创建工单", "conversation_id":snapshot["conversation_id"], "request_key":"same-task-key"}
    r=client.post("/workbench/message",headers=HEADERS,json=body)
    assert r.status_code == 200, r.text
    data=r.json();run=data["agent"]["run_id"]
    for _ in range(30):
        state=client.get("/agent/"+run,headers=HEADERS).json()
        if state["status"] == "completed": break
        time.sleep(.02)
    assert state["status"] == "completed"
    assert "TL-403" in json.dumps(task.calls,ensure_ascii=False)
    history=client.get("/conversations/"+data["conversation_id"],headers=HEADERS).json()
    assert history["messages"][-1]["content"] == "请补充故障系统。"
    again=client.post("/workbench/message",headers=HEADERS,json=body)
    assert again.json()["agent"]["run_id"] == run
    assert len(client.get("/conversations/"+data["conversation_id"],headers=HEADERS).json()["messages"]) == 4
    body["request_key"]="followup-task-key";body["message"]="是 VPN 客户端"
    assert client.post("/workbench/message",headers=HEADERS,json=body).status_code == 200
    assert "请补充故障系统" in json.dumps(route.calls[-1],ensure_ascii=False)
    denied=client.post("/workbench/message",headers={"X-Local-User-Id":"u_employee_001"},json=body)
    assert denied.status_code == 404


def test_active_task_blocks_model_change(session_app):
    app,client,_,_ = session_app
    from app.core.identity import LOCAL_IDENTITIES
    app.state.persistent_agent.repo.create(LOCAL_IDENTITIES[HEADERS["X-Local-User-Id"]],"task","pending-key","trace")
    r=client.put("/workbench/model",headers={'X-Local-User-Id':'u_admin_001'},json={"base_url":"https://model.example/v1","model_id":"changed","input_price":1,"output_price":1})
    assert r.status_code == 409
    assert app.state.chat.settings.model_id == "real-test"


@pytest.mark.parametrize("url",["http://remote.example/v1","https://name:secret@example.com/v1","https://example.com/v1?key=secret"])
def test_invalid_endpoint_is_rejected_without_mutation(session_app,url):
    app,client,_,_=session_app
    r=client.put("/workbench/model",headers={'X-Local-User-Id':'u_admin_001'},json={"base_url":url,"model_id":"real-test","api_key":"test"})
    assert r.status_code == 422
    assert app.state.chat.settings.base_url == "https://model.example/v1"


def test_connection_test_uses_existing_adapter_without_saving(session_app,monkeypatch):
    app,client,_,_=session_app
    import httpx
    from app.generation.provider import CompatibleGenerationClient
    seen=[]
    def provider(request):
        seen.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":'{"ok":true}'}}]})
    monkeypatch.setattr("app.api.workbench_session.make_generation_client",lambda settings:CompatibleGenerationClient(httpx.MockTransport(provider)))
    r=client.post("/workbench/model/test",headers={'X-Local-User-Id':'u_admin_001'},json={"base_url":"https://new.example/v1","model_id":"new-test","api_key":"test-key"})
    assert r.status_code == 200 and r.json()["ok"]
    assert seen[0]["model"] == "new-test"
    assert app.state.chat.settings.model_id == "real-test"
    assert "test-key" not in r.text
