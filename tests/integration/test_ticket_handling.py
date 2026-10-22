"""Human handling lifecycle preserves approval and requester confirmation boundaries."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from app.agent.contracts import TicketActivity
from app.core.identity import LOCAL_IDENTITIES
from app.core.errors import AppError
from tests.integration.test_ticket_workflow import repo,propose,submit,decide

def act(repo,tid,identity,action,version):
    return repo.activity(tid,identity,TicketActivity(action=action,version=version,reason='已人工核对本次处理结果与影响范围'))

def test_claim_comment_resolve_owner_close_reopen_and_notifications(repo):
    owner=LOCAL_IDENTITIES['u_engineer_001'];support=LOCAL_IDENTITIES['u_support_001']
    tid=submit(repo,owner,propose(repo,owner,'VPN连接失败，请排查故障'))['ticket_id']
    assert act(repo,tid,support,'claim',1)['status']=='in_progress'
    assert act(repo,tid,owner,'comment',2)['version']==3
    assert act(repo,tid,support,'resolve',3)['status']=='resolved'
    with pytest.raises(AppError):act(repo,tid,support,'close',4)
    assert act(repo,tid,owner,'close',4)['status']=='closed'
    assert act(repo,tid,owner,'reopen',5)['status']=='queued'
    saved=repo.get_ticket(tid,owner)
    assert saved['assignee'] is None and saved['resolution'] is None and len(saved['workflow']['history'])==5
    notices=repo.notifications(owner)['items'];assert notices and all(n['own_ticket'] for n in notices)
    first=notices[0]['notification_id'];repo.read_notification(first,owner)
    with pytest.raises(AppError):repo.read_notification(first,support)
    assert repo.notifications(support)['items']

def test_sensitive_ticket_cannot_be_claimed_before_business_approval(repo):
    owner=LOCAL_IDENTITIES['u_engineer_001'];support=LOCAL_IDENTITIES['u_support_001']
    tid=submit(repo,owner,propose(repo,owner,'申请管理员权限'))['ticket_id']
    with pytest.raises(AppError):act(repo,tid,support,'claim',1)
    decide(repo,tid,support,'approve')
    assert act(repo,tid,support,'claim',2)['execution_allowed'] is False
    with pytest.raises(AppError):act(repo,tid,owner,'resolve',3)

def test_concurrent_claims_and_cross_tenant_are_denied(repo):
    from dataclasses import replace
    owner=LOCAL_IDENTITIES['u_employee_001'];support=LOCAL_IDENTITIES['u_support_001']
    tid=submit(repo,owner,propose(repo,owner,'VPN连接失败，请排查故障'))['ticket_id']
    with pytest.raises(AppError):act(repo,tid,replace(support,tenant_id='other'),'claim',1)
    def claim(_):
        try:return act(repo,tid,support,'claim',1)['status']
        except AppError as e:return e.code
    with ThreadPoolExecutor(2) as pool:outcomes=list(pool.map(claim,range(2)))
    assert sorted(outcomes)==sorted(['in_progress','TICKET_VERSION_CONFLICT'])


def test_escalate_during_handling_and_sensitive_reopen_require_new_approval(repo):
    owner=LOCAL_IDENTITIES['u_engineer_001'];support=LOCAL_IDENTITIES['u_support_001']
    tid=submit(repo,owner,propose(repo,owner,'VPN连接失败，请排查故障'))['ticket_id']
    act(repo,tid,support,'claim',1)
    decide(repo,tid,support,'escalate','access',version=2)
    assert repo.get_ticket(tid,owner)['assignee'] is None
    with pytest.raises(AppError):act(repo,tid,support,'resolve',3)
    decide(repo,tid,support,'approve',version=3)
    act(repo,tid,support,'claim',4);act(repo,tid,support,'resolve',5)
    assert act(repo,tid,owner,'reopen',6)['status']=='pending_approval'
    with pytest.raises(AppError):act(repo,tid,support,'claim',7)
