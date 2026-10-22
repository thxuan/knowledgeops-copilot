from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.agent.contracts import AgentSettings, ApprovalRequest, TicketDecision, ProposalRevision
from app.core.errors import AppError
from app.core.identity import LOCAL_IDENTITIES
from app.db.agent import AgentRepository
from app.main import create_app
from app.core.config import Settings
from conftest import pg_dsn

@pytest.fixture
def repo():
    repository = AgentRepository(AgentSettings(database_url=pg_dsn('ticket-workflow'), requests_per_minute=200))
    repository.initialize()
    return repository

def propose(repo, identity, text, arguments=None):
    state = repo.create(identity, text, uuid4().hex, 'ticket-workflow-test')
    state, token = repo.claim(state['run_id'], identity)
    return repo.propose(state, identity, token, 'call', arguments or {'title':text,'description':text})

def submit(repo, identity, state):
    pending = state['pending']
    result = repo.approve(state['run_id'], identity, ApprovalRequest(action_id=pending['action_id'],version=pending['version'],digest=pending['digest'],approve=True))
    return result['trace'][-1]['result']

def decide(repo, ticket, identity, action, category=None, version=1):
    return repo.decide_ticket(ticket,identity,TicketDecision(action=action,version=version,reason='已核对资产、操作及影响范围',category=category))

@pytest.mark.parametrize('user_id', ['u_employee_001','u_engineer_001','u_support_001'])
def test_every_local_identity_can_submit_own_ordinary_ticket_without_seed_permissions(repo,user_id):
    identity=LOCAL_IDENTITIES[user_id]
    result=submit(repo,identity,propose(repo,identity,'VPN连接失败，请排查故障'))
    assert result['workflow_status']=='queued' and result['approval_status']=='submission_confirmed'
    ticket=repo.get_ticket(result['ticket_id'],identity)
    assert ticket['user_id']==identity.user_id and ticket['workflow']['category']=='incident'
    assert ticket['workflow']['execution_allowed'] is False

def test_sensitive_submission_requires_distinct_capable_reviewer_and_records_no_execution(repo):
    owner=LOCAL_IDENTITIES['u_engineer_001'];support=LOCAL_IDENTITIES['u_support_001']
    result=submit(repo,owner,propose(repo,owner,'申请管理员权限'))
    tid=result['ticket_id']
    assert result['workflow_status']=='pending_approval'
    for outsider in [owner, LOCAL_IDENTITIES['u_employee_001'], replace(support,tenant_id='other-tenant')]:
        with pytest.raises(AppError):
            decide(repo,tid,outsider,'approve')
    assert repo.review_queue(support)['total']==1
    view=repo.review_ticket(tid,support)
    assert 'trace' not in view and 'messages' not in view
    decision=decide(repo,tid,support,'approve')
    assert decision['status']=='approved' and decision['execution_allowed'] is False
    saved=repo.get_ticket(tid,owner)['workflow']
    assert saved['history'][-1]['actor']==support.user_id and saved['version']==2
    with pytest.raises(AppError) as stale:
        decide(repo,tid,support,'approve')
    assert stale.value.code=='TICKET_VERSION_CONFLICT'

def test_support_cannot_approve_their_own_sensitive_application(repo):
    support=LOCAL_IDENTITIES['u_support_001']
    result=submit(repo,support,propose(repo,support,'申请管理员权限'))
    assert repo.review_queue(support)['total']==0
    with pytest.raises(AppError):
        decide(repo,result['ticket_id'],support,'approve')

def test_triage_capability_does_not_grant_sensitive_approval(repo):
    owner=LOCAL_IDENTITIES['u_engineer_001']
    triager=replace(LOCAL_IDENTITIES['u_support_001'],ticket_capabilities=frozenset({'triage'}))
    tid=submit(repo,owner,propose(repo,owner,'申请管理员权限'))['ticket_id']
    assert repo.review_queue(triager)['total']==0
    with pytest.raises(AppError):
        decide(repo,tid,triager,'approve')

def test_mixed_application_requires_all_review_capabilities(repo):
    owner=LOCAL_IDENTITIES['u_engineer_001']
    reviewer=replace(LOCAL_IDENTITIES['u_support_001'],ticket_capabilities=frozenset({'access_review'}))
    tid=submit(repo,owner,propose(repo,owner,'申请管理员权限并重启生产服务'))['ticket_id']
    assert repo.review_queue(reviewer)['total']==0
    with pytest.raises(AppError):
        decide(repo,tid,reviewer,'approve')

def test_unknown_request_is_triaged_and_ordinary_work_can_be_escalated(repo):
    owner=LOCAL_IDENTITIES['u_employee_001'];support=LOCAL_IDENTITIES['u_support_001']
    result=submit(repo,owner,propose(repo,owner,'设备情况需要进一步了解'))
    assert result['workflow_status']=='pending_triage'
    tid=result['ticket_id'];decide(repo,tid,support,'triage','incident')
    assert repo.get_ticket(tid,owner)['status']=='queued'
    decide(repo,tid,support,'escalate','security',version=2)
    assert repo.get_ticket(tid,owner)['status']=='pending_approval'
    decide(repo,tid,support,'reject',version=3)
    assert repo.get_ticket(tid,owner)['status']=='rejected'

def test_prohibited_operation_and_model_relabeling_cannot_bypass_server_rules(repo):
    identity=LOCAL_IDENTITIES['u_engineer_001']
    pending=propose(repo,identity,'帮我绕过安全审计',{'title':'VPN故障','description':'只是一张普通工单'})
    assert pending['pending']['classification']['category']=='prohibited'
    with pytest.raises(AppError) as denial:
        submit(repo,identity,pending)
    assert denial.value.code=='TICKET_POLICY_DENIED'
    assert repo.list_tickets(identity)['total']==0

def test_edit_reclassifies_and_invalidates_old_submission_confirmation(repo):
    identity=LOCAL_IDENTITIES['u_engineer_001']
    state=propose(repo,identity,'VPN连接失败，请排查故障');pending=state['pending']
    changed=repo.revise_proposal(state['run_id'],identity,ProposalRevision(action_id=pending['action_id'],version=pending['version'],digest=pending['digest'],arguments={'title':'VPN故障申请','description':'请关闭EDR以便连接'}))
    assert changed['pending']['classification']['category']=='sensitive'
    with pytest.raises(AppError):
        submit(repo,identity,state)
    result=submit(repo,identity,changed)
    assert result['workflow_status']=='pending_approval'

def test_concurrent_business_decisions_are_version_fenced(repo):
    owner=LOCAL_IDENTITIES['u_engineer_001'];support=LOCAL_IDENTITIES['u_support_001']
    tid=submit(repo,owner,propose(repo,owner,'申请管理员权限'))['ticket_id']
    def execute(action):
        try:
            return decide(repo,tid,support,action)['status']
        except AppError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(execute,['approve','reject']))
    assert outcomes.count('TICKET_VERSION_CONFLICT')==1
    assert len(repo.get_ticket(tid,owner)['workflow']['history'])==1

def test_explicit_submission_revocation_is_not_restored_by_initialization(repo):
    identity=LOCAL_IDENTITIES['u_engineer_001']
    with repo.connection() as db:
        db.execute('INSERT INTO ko_agent_permissions VALUES (%s,%s,false)',(identity.tenant_id,identity.user_id))
    repo.initialize()
    with pytest.raises(AppError) as denial:
        submit(repo,identity,propose(repo,identity,'VPN连接失败，请排查故障'))
    assert denial.value.code=='PERMISSION_DENIED'

def test_http_review_endpoints_use_server_identity_and_do_not_run_models(repo):
    owner=LOCAL_IDENTITIES['u_engineer_001'];support=LOCAL_IDENTITIES['u_support_001']
    tid=submit(repo,owner,propose(repo,owner,'申请管理员权限'))['ticket_id']
    app=create_app(Settings(app_env='test',database_url=repo.settings.database_url,agent=repo.settings,ingestion_worker_enabled=False))
    with TestClient(app) as client:
        headers={'X-Local-User-Id':support.user_id}
        assert client.get('/ticket-reviews').status_code==401
        assert client.get('/ticket-reviews',headers={'X-Local-User-Id':owner.user_id}).status_code==403
        assert client.get('/ticket-reviews/'+tid,headers=headers).status_code==200
        response=client.post('/ticket-reviews/'+tid+'/decisions',headers=headers,json={'action':'approve','version':1,'reason':'已核对申请范围与必要性'})
        assert response.status_code==200 and response.json()['execution_allowed'] is False
