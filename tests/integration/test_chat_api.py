from tests.doubles import ExtractiveGenerationDouble
from conftest import pg_dsn
import asyncio
import json
from dataclasses import replace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.api.schemas import ChatRequest
from app.core.config import Settings
from app.core.identity import LOCAL_IDENTITIES, Identity
from app.core.errors import AppError
from app.generation.context import dump
from tests.doubles import ExtractiveGenerationDouble
from app.main import create_app
from app.retrieval.objects import make_citation
from dataclasses import asdict

HEADERS = {"X-Local-User-Id": "u_engineer_001"}
OTHER = {"X-Local-User-Id": "u_employee_001"}
IDENTITY = LOCAL_IDENTITIES["u_engineer_001"]
VPN = "TL-403 设备修复后等待十分钟，禁止关闭安全检查。"
MAIL = "MAIL-552 附件大小上限是 25 MB，禁止通过个人邮箱外发。"

class LocalRetrieval:
    """Test double for ranking only; citations and ACLs use the real repository."""
    def __init__(self, app, hits):
        self.app, self.hits, self.queries = app, hits, []
    async def retrieve(self, query, identity, **kwargs):
        self.queries.append(query)
        hits = []
        for hit in self.hits:
            try:
                self.app.state.document_repository.citation(hit["chunk_id"], hit["citation"]["chunk_set_id"], identity)
            except AppError:
                continue
            ids = kwargs.get("filters", {}).get("document_ids")
            if ids is None or hit["document_id"] in ids:
                hits.append(hit)
        return {"hits": hits[:kwargs.get("top_k", 5)]}

def seed(app, client, text, source):
    receipt = client.post("/documents", headers=HEADERS,
        files={"file": (source + ".md", ("# 支持\n\n" + text).encode())},
        data={"source": source, "version": "v1"}).json()
    app.state.ingestion_worker.process_next()
    doc = receipt["document_id"]
    result = client.post(f"/documents/{doc}/chunks", headers=HEADERS, json={}).json()
    return [{"chunk_id": c["chunk_id"], "document_id": doc, "text": c["text"], "score": 1,
             "source": c["source"], "version": c["version"], "citation": asdict(make_citation(c, result["chunk_set_id"]))}
            for c in result["chunks"]]

@pytest.fixture
def chat_app(tmp_path):
    settings = Settings(app_env="test", database_url=pg_dsn("chat.db"), ingestion_worker_enabled=False)
    app = create_app(settings)
    with TestClient(app) as client:
        hits = seed(app, client, VPN, "vpn") + seed(app, client, MAIL, "mail")
        app.state.chat.vectors = LocalRetrieval(app, hits)
        yield app, client, settings, hits

def ask(client, message="TL-403 设备修复后等待多久？", cid=None, **kwargs):
    response = client.post("/chat", headers=HEADERS, json={"message": message, "conversation_id": cid, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()

def test_multi_turn_reference_and_current_evidence(chat_app):
    app, client, _, hits = chat_app
    first = ask(client)
    assert first["status"] == "answered" and first["answer"] == VPN
    second = ask(client, "这个设备修复后等待多久？", first["conversation_id"])
    assert second["answer"] == VPN
    assert "TL-403" in second["context"]["retrieval_query"]
    assert second["context"]["history_message_ids"]
    citation = second["citations"][0]
    citation_response = client.get(citation["url"], headers=HEADERS)
    assert citation_response.headers["content-type"] == "application/json; charset=utf-8"
    assert citation_response.json()["text"].rstrip().endswith(VPN)
    assert client.get(f"/conversations/{first['conversation_id']}", headers=OTHER).status_code == 404
    assert client.post("/chat", headers=OTHER, json={"message":"它是什么？",
        "conversation_id":first["conversation_id"]}).status_code == 404
    assert client.post("/chat", headers=HEADERS, json={"message":"x", "user_id":"other"}).status_code == 422
    history = client.get(f"/conversations/{first['conversation_id']}", headers=HEADERS).json()
    assert len(history["messages"]) == 4 and history["revision"] == 2

def test_multi_turn_user_correction(chat_app):
    _, client, _, _ = chat_app
    first = ask(client)
    correction = ask(client, "不是 TL-403，我问的是 MAIL-552 附件大小上限。", first["conversation_id"])
    assert correction["answer"] == MAIL
    followup = ask(client, "这个附件大小上限是多少？", first["conversation_id"])
    assert followup["answer"] == MAIL
    assert followup["claims"][0]["citations"] == [followup["citations"][0]["chunk_id"]]

def test_multi_turn_long_history_summary_restart_and_original_lookup(chat_app):
    app, client, settings, hits = chat_app
    # Keep this regression's deliberately small budget independent of app defaults.
    settings = replace(settings, generation=replace(settings.generation, input_limit=16384))
    app.state.chat.settings = settings.generation
    cid = ask(client)["conversation_id"]
    for n in range(14):
        value = ask(client, f"备注 {n}：不是 MAIL-552，是 TL-403；禁止关闭安全检查。" + "背景记录。" * 100, cid)
        assert value["context"]["input_token_estimate"] + settings.generation.output_limit <= settings.generation.input_limit
    saved = client.get(f"/conversations/{cid}", headers=HEADERS, params={"limit":100}).json()
    assert saved["summary"]["records"]
    assert sum(len(m["content"].encode("utf-8")) for m in saved["messages"]) > settings.generation.input_limit
    for record in saved["summary"]["records"]:
        original = next(m for m in saved["messages"] if m["message_id"] == record["message_id"])
        assert original["content"] == record["text"]
    restarted = create_app(settings)
    with TestClient(restarted) as fresh:
        restarted.state.chat.vectors = LocalRetrieval(restarted, hits)
        assert fresh.get(f"/conversations/{cid}", headers=HEADERS).json()["revision"] == 15
        answer = ask(fresh, "这个设备修复后等待多久？", cid)
        assert answer["answer"] == VPN

def test_no_answer_even_with_hits_and_empty_candidates(chat_app):
    app, client, _, _ = chat_app
    no_answer = ask(client, "火星基地的咖啡价格是多少？")
    assert no_answer["status"] == "refused" and not no_answer["citations"]
    assert no_answer["context"]["retrieval_hit_count"] > 0
    empty = ask(client, filters={"document_ids":[]})
    assert empty["status"] == "refused" and empty["context"]["generation_attempts"] == 0

class Scripted:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = 0
    async def generate(self, messages, settings):
        self.calls += 1
        value = next(self.outputs)
        if callable(value):
            return await value(messages, settings)
        return value

def valid(text=VPN, cid="invented"):
    return dump({"insufficient_context":False,
                 "claims":[{"text":text, "citations":[cid]}]})

@pytest.mark.parametrize("bad", ["not json", "{}", valid(), dump({"answer":"泄露未经验证的事实",
    "citations":[], "claims":[], "insufficient_context":"true"})])
def test_invalid_outputs_are_rejected_and_raw_content_not_persisted(chat_app, bad):
    app, client, _, _ = chat_app
    model = Scripted([bad, bad])
    app.state.chat.client = model
    result = ask(client)
    assert result["status"] == "rejected" and result["reason"] == "invalid_generation"
    assert result["insufficient_context"] and not result["citations"] and model.calls == 2
    history = client.get(f"/conversations/{result['conversation_id']}", headers=HEADERS).json()
    assert history["messages"][-1]["payload"]["status"] == "rejected"
    assert history["messages"][-1]["content"] != bad

def test_repair_success_and_refusal_text_is_server_owned(chat_app):
    app, client, _, _ = chat_app
    app.state.chat.client = Scripted(["{", ExtractiveGenerationDouble().generate])
    assert ask(client)["status"] == "answered"
    app.state.chat.client = Scripted([dump({"claims":[], "insufficient_context":True})])
    refused = ask(client)
    assert refused["status"] == "refused"
    from app.generation.models import REFUSAL
    assert refused["answer"] == REFUSAL

def test_only_evidence_actually_in_prompt_can_be_cited(chat_app):
    app, client, settings, hits = chat_app
    # Oversized hit is retrieved but excluded from the actual generation budget.
    oversized = {**hits[0], "text":"巨大" * 20000}
    app.state.chat.vectors.hits = [oversized, hits[-1]]
    app.state.chat.client = Scripted([valid(cid=oversized["chunk_id"])] * 2)
    result = ask(client, "MAIL-552 附件大小上限？")
    assert result["context"]["evidence_count"] == 1 and result["status"] == "rejected"

def test_revocation_during_generation_does_not_commit_and_history_redacts(chat_app):
    app, client, _, hits = chat_app
    cid = ask(client)["conversation_id"]
    async def revoke(messages, settings):
        result = await ExtractiveGenerationDouble().generate(messages, settings)
        app.state.document_repository.permissions(hits[0]["document_id"], IDENTITY, [])
        return result
    app.state.chat.client = Scripted([revoke])
    response = client.post("/chat", headers=HEADERS, json={"message":"TL-403 设备修复后等待多久？", "conversation_id":cid})
    assert response.status_code == 409 and response.json()["error"]["code"] == "EVIDENCE_CHANGED"
    history = client.get(f"/conversations/{cid}", headers=HEADERS).json()
    assert history["revision"] == 1
    assert history["messages"][-1]["redacted"] and "十分钟" not in history["messages"][-1]["content"]

def test_timeout_preserves_original_conversation(chat_app):
    app, client, settings, _ = chat_app
    cid = ask(client)["conversation_id"]
    async def slow(messages, settings):
        await asyncio.sleep(1)
    app.state.chat.client = Scripted([slow])
    app.state.chat.settings = replace(settings.generation, timeout_seconds=0.01)
    response = client.post("/chat", headers=HEADERS, json={"message":"TL-403 等待多久？", "conversation_id":cid})
    assert response.status_code == 504
    assert client.get(f"/conversations/{cid}", headers=HEADERS).json()["revision"] == 1

def test_concurrent_turn_has_one_atomic_winner(chat_app):
    app, client, _, _ = chat_app
    cid = ask(client)["conversation_id"]
    class Barrier:
        def __init__(self):
            self.entered = 0
            self.ready = asyncio.Event()
        async def generate(self, messages, settings):
            self.entered += 1
            if self.entered == 2:
                self.ready.set()
            await self.ready.wait()
            return await ExtractiveGenerationDouble().generate(messages, settings)
    async def run():
        app.state.chat.client = Barrier()
        body = ChatRequest(message="TL-403 设备修复后等待多久？", conversation_id=cid)
        return await asyncio.gather(app.state.chat.run(body, IDENTITY), app.state.chat.run(body, IDENTITY),
                                    return_exceptions=True)
    outcomes = asyncio.run(run())
    assert sum(isinstance(o, dict) for o in outcomes) == 1
    errors = [o for o in outcomes if isinstance(o, AppError)]
    assert errors[0].code == "CONVERSATION_CONFLICT"
    history = client.get(f"/conversations/{cid}", headers=HEADERS).json()
    assert history["revision"] == 2 and len(history["messages"]) == 4

def test_not_configured_and_blank_input(tmp_path):
    app = create_app(Settings(database_url=pg_dsn("empty.db"), ingestion_worker_enabled=False))
    with TestClient(app) as client:
        assert client.post("/chat", headers=HEADERS, json={"message":"VPN"}).status_code == 503
        assert client.post("/chat", headers=HEADERS, json={"message":"  "}).status_code == 422

def test_oversized_recent_correction_refuses_reference_instead_of_old_summary(chat_app):
    app, client, settings, _ = chat_app
    cid = ask(client)["conversation_id"]
    # Store a correction that fits the request/model window, but not the history sub-budget.
    ask(client, "不是 TL-403，是 MAIL-552，禁止外发。" + "较长背景。" * 180, cid)
    app.state.chat.client = Scripted([])
    answer = ask(client, "这个限制是什么？", cid)
    assert answer["status"] == "refused"
    assert answer["reason"] == "reference_history_exceeds_budget"
    assert answer["context"]["generation_attempts"] == 0

def test_conversation_owner_also_requires_tenant(chat_app):
    app, client, _, _ = chat_app
    cid = ask(client)["conversation_id"]
    with pytest.raises(AppError) as error:
        app.state.conversations.load(cid, Identity(IDENTITY.user_id, "other-tenant", frozenset()))
    assert error.value.status_code == 404

def test_multiple_followups_keep_entity_context(chat_app):
    _, client, _, _ = chat_app
    cid = ask(client)["conversation_id"]
    ask(client, "它的设备修复后等待多久？", cid)
    answer = ask(client, "然后它的设备修复后等待多久？", cid)
    assert "TL-403" in answer["context"]["retrieval_query"]
    assert answer["answer"] == VPN



@pytest.mark.parametrize("failure,code", [
    ("extra", "schema_invalid"),
    ("claims", "missing_cited_claims"),
    ("outside", "citation_outside_evidence"),
    ("schema", "schema_invalid"),
])
def test_targeted_repair_and_safe_diagnostics(chat_app, monkeypatch, failure, code):
    from app.generation import service
    app, client, _, hits = chat_app
    logs, prompts = [], []
    monkeypatch.setattr(service.logger, "info", lambda value: logs.append(json.loads(value)))
    marker = "invalid-private-output-marker"
    bad = json.loads(valid(cid=hits[0]["chunk_id"]))
    if failure == "extra":
        bad[marker] = marker
    elif failure == "claims":
        bad["claims"] = []
    elif failure == "outside":
        bad = json.loads(valid(cid=marker))
    else:
        bad["insufficient_context"] = "false"
        bad[marker] = marker

    async def first(messages, settings):
        prompts.append(json.loads(json.dumps(messages)))
        return dump(bad)

    async def second(messages, settings):
        prompts.append(json.loads(json.dumps(messages)))
        assert service.REPAIR_HINTS[code] in messages[0]["content"]
        return await ExtractiveGenerationDouble().generate(messages, settings)

    app.state.chat.client = Scripted([first, second])
    result = ask(client)
    assert result["status"] == "answered" and result["context"]["generation_attempts"] == 2
    assert prompts[0][1] == prompts[1][1]
    expected = {"event": "generation.validation_failed",
                "conversation_id": result["conversation_id"], "attempt": 1,
                "phase": "draft", "validation_code": code}
    if failure == "extra":
        expected["schema_issues"] = [{"field": "<extra>", "type": "extra_forbidden"}]
    elif failure == "schema":
        expected["schema_issues"] = [
            {"field": "insufficient_context", "type": "bool_type"},
            {"field": "<extra>", "type": "extra_forbidden"},
        ]
    assert logs == [expected]
    assert marker not in dump(logs) and marker not in dump(prompts)


def test_refusal_with_claims_repair_preserves_strict_contract(chat_app, monkeypatch):
    from app.generation import service
    from app.generation.models import REFUSAL
    app, client, _, _ = chat_app
    logs, prompts = [], []
    monkeypatch.setattr(service.logger, "info", lambda value: logs.append(json.loads(value)))
    empty_refusal = {"insufficient_context": True, "claims": [
        {"text": "拒答不能夹带事实", "citations": ["invented"]}]}

    async def empty(messages, settings):
        prompts.append(json.loads(json.dumps(messages)))
        return dump(empty_refusal)

    async def repaired(messages, settings):
        prompts.append(json.loads(json.dumps(messages)))
        assert service.REPAIR_HINTS["refusal_has_claims"] in messages[0]["content"]
        return dump({"insufficient_context": True, "claims": []})

    app.state.chat.client = Scripted([empty, repaired])
    result = ask(client, "火星基地的咖啡价格是多少？")
    assert result["status"] == "refused" and result["reason"] == "insufficient_context"
    assert result["answer"] == REFUSAL and result["citations"] == result["claims"] == []
    assert result["context"]["generation_attempts"] == 2
    assert prompts[0][1] == prompts[1][1]
    assert logs[0]["validation_code"] == "refusal_has_claims"

    # Refusals containing claims stay rejected if the provider does not repair them.
    app.state.chat.client = Scripted([dump(empty_refusal)] * 2)
    again = ask(client, "火星基地的咖啡价格是多少？")
    assert again["status"] == "rejected" and again["reason"] == "invalid_generation"


def test_claims_only_payload_is_rendered_by_server_and_stored(chat_app):
    app, client, _, hits = chat_app
    first, second = hits[0]["chunk_id"], hits[1]["chunk_id"]
    raw = dump({"insufficient_context": False, "claims": [
        {"text": "第一条完整结论。", "citations": [first]},
        {"text": "第二条完整结论。", "citations": [first, second]}]})
    app.state.chat.client = Scripted([raw])
    result = ask(client)
    assert result["status"] == "answered" and result["context"]["generation_attempts"] == 1
    assert result["answer"] == "第一条完整结论。" + chr(10) + "第二条完整结论。"
    assert [c["chunk_id"] for c in result["citations"]] == [first, second]
    history = client.get(f"/conversations/{result['conversation_id']}", headers=HEADERS).json()
    assert history["messages"][-1]["content"] == result["answer"]


def test_extra_model_answer_cannot_bypass_claims_contract(chat_app):
    app, client, _, hits = chat_app
    raw = dump({"insufficient_context": False, "claims": [
        {"text": "合法格式结论。", "citations": [hits[0]["chunk_id"]]}],
        "answer": "未经引用的额外事实"})
    app.state.chat.client = Scripted([raw, raw])
    result = ask(client)
    assert result["status"] == "rejected"
    assert "额外事实" not in result["answer"]
