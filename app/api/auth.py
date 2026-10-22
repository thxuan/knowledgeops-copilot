"""Password login without an external identity provider; no default credentials."""
import asyncio
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from app.core.errors import AppError
from app.core.identity import Identity, get_identity, require_platform
from app.db.accounts import COOKIE

router=APIRouter()
UI=Path(__file__).resolve().parents[1]/'web'

class Login(BaseModel):
    username:str=Field(min_length=1,max_length=80)
    password:str=Field(min_length=1,max_length=256,repr=False)

class NewAccount(Login):
    display_name:str=Field(min_length=1,max_length=80)
    role:str=Field(pattern='^(employee|engineer|support|admin)$')
    user_id:str|None=Field(default=None,pattern=r'^[A-Za-z0-9_-]{1,80}$')

class Enabled(BaseModel):
    enabled:bool

@router.get('/auth/config')
async def auth_config(request:Request):
    mode=request.app.state.settings.auth_mode
    setup_required=False
    if mode=='accounts':
        with request.app.state.accounts.connection() as db:
            setup_required=db.execute('SELECT 1 FROM ko_accounts LIMIT 1').fetchone() is None
    return {'mode':mode,'setup_required':setup_required}

@router.get('/login',include_in_schema=False)
async def login_page():
    return FileResponse(UI/'login.html')

@router.get('/admin/accounts',include_in_schema=False)
async def account_page():
    return FileResponse(UI/'accounts.html')

@router.get('/auth/me')
async def me(request:Request,identity:Identity=Depends(get_identity)):
    profile={'user_id':identity.user_id,'display_name':identity.user_id}
    csrf=None
    if request.app.state.settings.auth_mode=='accounts':
        _,csrf,profile=await asyncio.to_thread(request.app.state.accounts.session,request.cookies.get(COOKIE))
    return {**profile,'csrf_token':csrf,'platform_capabilities':sorted(identity.platform_capabilities),
            'ticket_capabilities':sorted(identity.ticket_capabilities)}

@router.post('/auth/login')
async def login(body:Login,request:Request,response:Response):
    settings=request.app.state.settings
    if settings.auth_mode!='accounts':
        raise AppError('AUTH_MODE','当前是本地演示模式，无账号登录。',409)
    if request.headers.get('X-Requested-With')!='KnowledgeOps' or request.headers.get('origin',settings.public_origin)!=settings.public_origin:
        raise AppError('ORIGIN_DENIED','登录来源校验失败。',403)
    token,_,profile=await asyncio.to_thread(request.app.state.accounts.login,body.username,body.password,
        request.client.host if request.client else 'unknown',settings.auth_session_seconds)
    # Rotate the current browser session; never accept a client-chosen session identifier.
    await asyncio.to_thread(request.app.state.accounts.logout,request.cookies.get(COOKIE))
    response.set_cookie(COOKIE,token,max_age=settings.auth_session_seconds,httponly=True,
                        secure=settings.auth_cookie_secure,samesite='lax',path='/')
    return profile

@router.post('/auth/logout')
async def logout(request:Request,response:Response,identity:Identity=Depends(get_identity)):
    await asyncio.to_thread(request.app.state.accounts.logout,request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE,path='/')
    return {'logged_out':True}

@router.get('/auth/accounts')
async def accounts(request:Request,identity:Identity=Depends(require_platform)):
    with request.app.state.accounts.connection() as db:
        rows=db.execute('SELECT * FROM ko_accounts WHERE tenant_id=%s ORDER BY username',(identity.tenant_id,)).fetchall()
        return {'items':[{**request.app.state.accounts.public(r),
                         'platform_capabilities':__import__('json').loads(r['platform_capabilities'])} for r in rows]}

@router.post('/auth/accounts',status_code=201)
async def create(body:NewAccount,request:Request,identity:Identity=Depends(require_platform)):
    if request.app.state.settings.auth_mode!='accounts':
        raise AppError('AUTH_MODE','请先按文档启用账号模式并初始化管理员。',409)
    audiences={'employee':{'all-employees'},'engineer':{'all-employees','engineering-cn'},
               'support':{'all-employees','it-support'},'admin':set()}[body.role]
    capabilities={'triage','access_review','security_review','production_review','handle'} if body.role=='support' else set()
    user=Identity(body.user_id or 'u_'+uuid4().hex,identity.tenant_id,frozenset(audiences),frozenset(capabilities),
                  frozenset({'manage_platform'} if body.role=='admin' else set()))
    return await asyncio.to_thread(request.app.state.accounts.create,body.username,body.password,body.display_name,user,actor=identity.user_id)

@router.put('/auth/accounts/{user_id}/enabled')
async def enable(user_id:str,body:Enabled,request:Request,identity:Identity=Depends(require_platform)):
    return await asyncio.to_thread(request.app.state.accounts.set_enabled,user_id,body.enabled,identity)


@router.get('/auth/principals')
async def principals(request:Request,identity:Identity=Depends(get_identity)):
    if request.app.state.settings.auth_mode=='demo':
        from app.core.identity import LOCAL_IDENTITIES
        labels={'u_employee_001':'普通员工','u_engineer_001':'工程师','u_support_001':'IT 支持','u_admin_001':'平台管理员'}
        return {'items':[{'user_id':u.user_id,'display_name':labels[u.user_id]} for u in LOCAL_IDENTITIES.values() if u.tenant_id==identity.tenant_id]}
    with request.app.state.accounts.connection() as db:
        return {'items':[dict(r) for r in db.execute('SELECT user_id,display_name FROM ko_accounts WHERE tenant_id=%s AND enabled=true ORDER BY display_name',(identity.tenant_id,))]}
