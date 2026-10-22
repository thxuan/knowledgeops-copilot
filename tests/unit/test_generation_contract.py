import json
import pytest
from app.generation.context import SYSTEM_PROMPT
from app.generation.models import GeneratedClaims, REFUSAL, parse_generation


def test_server_assembles_claims_and_deduplicates_citations_in_order():
    output = parse_generation(json.dumps({"insufficient_context": False, "claims": [
        {"text": "连续 5 次失败锁定 15 分钟。", "citations": ["a", "b"]},
        {"text": "继续尝试不会缩短等待。", "citations": ["b", "a", "c"]}]}))
    assert output.answer == "连续 5 次失败锁定 15 分钟。\n继续尝试不会缩短等待。"
    assert output.citations == ["a", "b", "c"]


def test_model_schema_omits_redundant_answer_and_top_level_citations():
    schema = GeneratedClaims.model_json_schema()
    assert set(schema["properties"]) == {"insufficient_context", "claims", "follow_up"}
    assert schema["additionalProperties"] is False
    assert "不要输出 answer 或顶层 citations" in SYSTEM_PROMPT


@pytest.mark.parametrize("value", [
    {"insufficient_context": False, "claims": []},
    {"insufficient_context": "true", "claims": []},
    {"insufficient_context": False, "claims": [{"text": "   ", "citations": ["a"]}]},
    {"insufficient_context": False, "claims": [{"text": "fact", "citations": []}]},
    {"insufficient_context": True, "claims": [{"text": "unsupported fact", "citations": ["a"]}]},
    {"insufficient_context": True, "claims": [], "answer": "untrusted refusal facts"},
    {"insufficient_context": True, "claims": [], "citations": []},
])
def test_model_wire_contract_is_strict(value):
    with pytest.raises(ValueError):
        parse_generation(json.dumps(value))


def test_server_owns_refusal_and_keeps_external_answer_shape():
    answer = parse_generation('{"insufficient_context":true,"claims":[]}')
    assert answer.answer == REFUSAL
    assert answer.citations == answer.claims == []
    assert set(answer.model_dump()) == {"answer", "citations", "claims", "insufficient_context", "follow_up"}


def test_assembled_answer_and_citation_limits_still_apply():
    long_claims = [{"text": "x" * 4000, "citations": ["a"]} for _ in range(3)]
    with pytest.raises(ValueError):
        parse_generation(json.dumps({"insufficient_context": False, "claims": long_claims}))
    many_ids = [{"text": "fact", "citations": [str(n)]} for n in range(21)]
    with pytest.raises(ValueError):
        parse_generation(json.dumps({"insufficient_context": False, "claims": many_ids}))
