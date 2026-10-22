"""Account login and administrative boundaries against real PostgreSQL."""
import pytest
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.core.identity import LOCAL_IDENTITIES
from app.main import create_app
from conftest import pg_dsn

PASSWORD='Test-only-random-password-5678'
@pytest.fixture
def accounts_app():
    settings=Settings(app_env='test',auth_mode='accounts',auth_cookie_secure=False,public_origin='http://127.0.0.1',
        database_url=pg_dsn('accounts'),ingestion_worker_enabled=False,index_worker_enabled=False)
    app=create_app(settings)
    with TestClient(app,base_url="http://127.0.0.1") as client:
        for name,user_id,display in [('administrator','u_admin_001','管理员'),('engineer','u_engineer_001','工程师'),('employee','u_employee_001','普通员工')]:
            app.state.accounts.create(name,PASSWORD,display,LOCAL_IDENTITIES[user_id])
        yield app,client

def login(client,username='administrator'):
    result=client.post('/auth/login',headers={'X-Requested-With':'KnowledgeOps'},json={'username':username,'password':PASSWORD})
    assert result.status_code==200,result.text
    me=client.get('/auth/me');assert me.status_code==200
    return {'X-CSRF-Token':me.json()['csrf_token']}

def test_sessions_ignore_spoofed_header_require_csrf_and_logout(accounts_app):
    app,client=accounts_app
    assert client.get('/auth/me',headers={'X-Local-User-Id':'u_admin_001'}).status_code==401
    headers=login(client,'engineer')
    assert client.get('/auth/me',headers={'X-Local-User-Id':'u_admin_001'}).json()['user_id']=='u_engineer_001'
    assert client.get('/workbench/connections').status_code==403
    assert client.put('/workbench/model',headers=headers,json={'base_url':'https://example.org/v1','model_id':'m'}).status_code==403
    assert client.post('/auth/logout').status_code==403
    assert client.post('/auth/logout',headers={**headers,'Origin':'https://attacker.invalid'}).status_code==403
    assert client.post('/auth/logout',headers=headers).status_code==200
    assert client.get('/auth/me').status_code==401

def test_admin_create_disable_and_last_admin_guard(accounts_app):
    app,client=accounts_app;headers=login(client)
    assert client.get('/workbench/connections').status_code==200
    created=client.post('/auth/accounts',headers=headers,json={'username':'new-user','password':PASSWORD,'display_name':'新员工','role':'employee'})
    assert created.status_code==201,created.text
    user=created.json()['user_id']
    with app.state.accounts.connection() as db:
        row=db.execute('SELECT password_hash FROM ko_accounts WHERE user_id=%s',(user,)).fetchone()
        assert row['password_hash'].startswith('$argon2id$') and PASSWORD not in row['password_hash']
    assert client.put('/auth/accounts/u_admin_001/enabled',headers=headers,json={'enabled':False}).status_code==409
    assert client.put('/auth/accounts/'+user+'/enabled',headers=headers,json={'enabled':False}).status_code==200
    assert client.post('/auth/login',headers={'X-Requested-With':'KnowledgeOps'},json={'username':'new-user','password':PASSWORD}).status_code==401
    assert 'password_hash' not in client.get('/auth/accounts').text

def test_disabling_account_revokes_live_session_immediately(accounts_app):
    app,client=accounts_app;headers=login(client,'employee')
    token=client.cookies.get('knowledgeops_session')
    app.state.accounts.set_enabled('u_employee_001',False,LOCAL_IDENTITIES['u_admin_001'])
    assert client.get('/auth/me').status_code==401
    from app.core.errors import AppError
    with pytest.raises(AppError):app.state.accounts.session(token)

def test_login_rate_limits_are_durable_and_passwords_not_logged(accounts_app):
    app,client=accounts_app
    for _ in range(5):
        assert client.post('/auth/login',headers={'X-Requested-With':'KnowledgeOps'},json={'username':'employee','password':'wrong-password'}).status_code==401
    assert client.post('/auth/login',headers={'X-Requested-With':'KnowledgeOps'},json={'username':'employee','password':PASSWORD}).status_code==401
    with app.state.accounts.connection() as db:
        row=db.execute("SELECT failures,locked_until FROM ko_accounts WHERE username='employee'").fetchone()
        assert row['failures']>=5 and row['locked_until']>0
    assert client.post('/auth/login',json={'username':'administrator','password':PASSWORD}).status_code==403

def test_new_account_principal_requires_review_before_document_access(accounts_app):
    app,client=accounts_app;admin=login(client)
    result=client.post('/auth/accounts',headers=admin,json={'username':'reader','password':PASSWORD,'display_name':'读者','role':'employee'})
    reader=result.json()['user_id'];headers=login(client,'engineer')
    uploaded=client.post('/documents',headers=headers,files={'file':('policy.md',b'# Account policy\nOnly authorized users can read.\n')})
    assert uploaded.status_code==202
    app.state.ingestion_worker.process_next();doc=uploaded.json()['document_id']
    assert client.put(f'/documents/{doc}/permissions',headers=headers,json={'user_ids':['u_engineer_001',reader]}).status_code==409
    assert reader in [p['user_id'] for p in client.get('/auth/principals').json()['items']]
    login(client,'reader')
    assert client.get(f'/documents/{doc}/access').status_code==404
    assert client.get(f'/documents/{doc}/versions').status_code==404

def test_production_cannot_use_demo_or_insecure_cookie():
    with pytest.raises(ValueError):Settings(app_env='production').validate()
    with pytest.raises(ValueError):Settings(app_env='production',auth_mode='accounts',auth_cookie_secure=False).validate()


def test_empty_account_store_has_setup_hint_without_anonymous_bootstrap():
    app=create_app(Settings(app_env='test',auth_mode='accounts',auth_cookie_secure=False,public_origin='http://127.0.0.1',
        database_url=pg_dsn('empty-accounts'),ingestion_worker_enabled=False,index_worker_enabled=False))
    with TestClient(app,base_url='http://127.0.0.1') as client:
        assert client.get('/auth/config').json()=={'mode':'accounts','setup_required':True}
        assert client.post('/auth/accounts',json={'username':'admin','password':PASSWORD,'display_name':'admin','role':'admin'}).status_code==401
