"""Server-owned mapping for local test identities only."""

from dataclasses import dataclass
from fastapi import Depends, Header, Request
from app.core.errors import AppError


@dataclass(frozen=True)
class Identity:
    user_id: str
    tenant_id: str
    audiences: frozenset[str]
    ticket_capabilities: frozenset[str] = frozenset()
    platform_capabilities: frozenset[str] = frozenset()


LOCAL_IDENTITIES = {
    "u_employee_001": Identity("u_employee_001", "tenant_clearwater", frozenset({"all-employees"})),
    "u_engineer_001": Identity("u_engineer_001", "tenant_clearwater", frozenset({"all-employees", "engineering-cn"})),
    "u_support_001": Identity("u_support_001", "tenant_clearwater", frozenset({"all-employees", "it-support"}),
        frozenset({"triage", "access_review", "security_review", "production_review", "handle"})),
    "u_admin_001": Identity("u_admin_001", "tenant_clearwater", frozenset(), frozenset(), frozenset({"manage_platform"})),
}


def get_identity(request: Request, x_local_user_id: str | None = Header(default=None)) -> Identity:
    if request.app.state.settings.auth_mode == 'accounts':
        from app.db.accounts import COOKIE
        identity, csrf, _ = request.app.state.accounts.session(request.cookies.get(COOKIE))
        if request.method not in {'GET','HEAD','OPTIONS'}:
            if request.headers.get('origin',request.app.state.settings.public_origin)!=request.app.state.settings.public_origin:
                raise AppError('ORIGIN_DENIED','请求来源校验失败。',403)
            import secrets
            if not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),csrf):
                raise AppError('CSRF_REQUIRED','请刷新登录状态后重试。',403)
        request.state.user_id = identity.user_id
        return identity
    identity = LOCAL_IDENTITIES.get(x_local_user_id or "")
    if identity is None:
        raise AppError("UNAUTHENTICATED", "Valid local test identity is required", 401)
    request.state.user_id = identity.user_id
    return identity


def require_platform(identity: Identity = Depends(get_identity)):
    if 'manage_platform' not in identity.platform_capabilities:
        raise AppError('PERMISSION_DENIED','仅平台管理员可以管理连接、模型与账号。',403)
    return identity
