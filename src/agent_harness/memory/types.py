"""Memory 领域值与已授权 namespace。"""

from contextvars import ContextVar
from enum import Enum

from pydantic import BaseModel, Field

from agent_harness.identity import IdentityContext

memory_session_var: ContextVar[str | None] = ContextVar("memory_session", default=None)


class MemoryScope(str, Enum):
    GLOBAL = "global"
    TENANT = "tenant"
    USER = "user"
    SESSION = "session"
    AGENT = "agent"


class MemoryEntry(BaseModel):
    id: str
    content: str
    metadata: dict = Field(default_factory=dict)
    score: float | None = None
    created_at: str
    scope: MemoryScope
    indexed: bool = False


def scope_to_namespace(scope: MemoryScope, identity: IdentityContext) -> tuple[str, ...]:
    if scope not in (MemoryScope.USER, MemoryScope.SESSION):
        raise NotImplementedError(f"Memory scope {scope.value} is not implemented")
    if scope.value not in identity.scopes:
        raise PermissionError("Memory scope is not authorized")
    if scope is MemoryScope.SESSION:
        session_id = memory_session_var.get()
        if not session_id:
            raise ValueError("SESSION scope requires a trusted session binding")
        return ("memories", identity.tenant_id, identity.user_id, scope.value, session_id)
    return ("memories", identity.tenant_id, identity.user_id, scope.value)
