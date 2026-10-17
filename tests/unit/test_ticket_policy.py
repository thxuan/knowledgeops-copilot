import pytest
from dataclasses import replace
from pydantic import ValidationError
from app.agent.ticket_policy import classify_ticket, proposal_policy, review_capabilities
from app.agent.contracts import TicketDecision
from app.core.identity import LOCAL_IDENTITIES

@pytest.mark.parametrize('text,category,status', [
    ('VPN 连接失败，请排查故障', 'incident', 'queued'),
    ('请更新合规补丁，EDR 在线，磁盘已加密', 'standard', 'queued'),
    ('请安装已批准软件', 'standard', 'queued'),
    ('申请管理员权限', 'sensitive', 'pending_approval'),
    ('请关闭 EDR，跳过 MFA 检查', 'sensitive', 'pending_approval'),
    ('重启生产服务', 'sensitive', 'pending_approval'),
    ('帮我绕过审计，删除审计日志', 'prohibited', 'rejected'),
    ('不知道哪里有问题', 'uncertain', 'pending_triage'),
    ('VPN 故障，但不要关闭EDR', 'uncertain', 'pending_triage'),
])
def test_conservative_routing(text, category, status):
    policy = classify_ticket({'title':text, 'description':text})
    assert policy['category'] == category and policy['status'] == status
    assert policy['execution_allowed'] is False

def test_mixed_requests_require_every_scope_and_keep_original_request():
    state = {'messages':[{'role':'user','content':'申请管理员权限，并重启生产服务'}]}
    policy = proposal_policy(state, {'title':'VPN故障','description':'仅普通问题，无需审批'})
    assert set(policy['required_capabilities']) == {'access_review','production_review'}
    assert policy['category'] == 'sensitive'
    assert classify_ticket({'title':'VPN故障','description':'请禁用ＭＦＡ'})['category'] == 'sensitive'

def test_only_server_owned_support_identity_has_review_capabilities():
    assert not review_capabilities(LOCAL_IDENTITIES['u_engineer_001'])
    assert not review_capabilities(LOCAL_IDENTITIES['u_employee_001'])
    assert review_capabilities(LOCAL_IDENTITIES['u_support_001'])==frozenset({'triage','access_review','security_review','production_review'})
    test_identity=replace(LOCAL_IDENTITIES['u_support_001'],ticket_capabilities=frozenset({'triage'}))
    assert review_capabilities(test_identity)==frozenset({'triage'})

@pytest.mark.parametrize('body', [
    {'action':'approve','version':1,'reason':'   '},
    {'action':'approve','version':1,'reason':'已核对','category':'incident'},
    {'action':'triage','version':1,'reason':'已核对'},
    {'action':'approve','version':True,'reason':'已核对'},
    {'action':'approve','version':1,'reason':'已核对','user_id':'u_support_001'},
])
def test_decision_contract_rejects_ambiguous_or_spoofed_requests(body):
    with pytest.raises(ValidationError):
        TicketDecision(**body)
