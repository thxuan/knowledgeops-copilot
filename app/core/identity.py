"""Server-owned mapping for local test identities only."""

from dataclasses import dataclass
from fastapi import Header, Request
from app.core.errors import AppError


@dataclass(frozen=True)
class Identity:
    user_id: str
    tenant_id: str
    audiences: frozenset[str]


LOCAL_IDENTITIES = {
    "u_employee_001": Identity("u_employee_001", "tenant_clearwater", frozenset({"all-employees"})),
    "u_engineer_001": Identity("u_engineer_001", "tenant_clearwater", frozenset({"all-employees", "engineering-cn"})),
    "u_support_001": Identity("u_support_001", "tenant_clearwater", frozenset({"all-employees", "it-support"})),
}


def get_identity(request: Request, x_local_user_id: str | None = Header(default=None)) -> Identity:
    identity = LOCAL_IDENTITIES.get(x_local_user_id or "")
    if identity is None:
        raise AppError("UNAUTHENTICATED", "Valid local test identity is required", 401)
    request.state.user_id = identity.user_id
    return identity
