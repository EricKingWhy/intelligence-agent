"""Memory 领域值与已授权 namespace。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, Field

from agent_harness.identity import IdentityContext

memory_session_var: ContextVar[str | None] = ContextVar("memory_session", default=None)

#: namespace 元组的根段——LangGraph/LangMem 边界与 Milvus PK 的既有契约（ADR-0008/0009）。
NAMESPACE_ROOT = "memories"


class MemoryScope(str, Enum):
    GLOBAL = "global"
    TENANT = "tenant"
    USER = "user"
    SESSION = "session"
    AGENT = "agent"


class MemoryEntry(BaseModel):
    id: str = Field(min_length=1)
    content: str
    metadata: dict = Field(default_factory=dict)
    score: float | None = None
    created_at: str
    scope: MemoryScope
    indexed: bool = False


@dataclass(frozen=True, slots=True)
class MemoryNamespace:
    """Memory namespace 值对象：("memories", tenant_id, user_id, scope[, session_id])。

    把"合法形状 + scope 授权 + session 绑定"收进一处，替代散落各存储后端的
    魔法下标（namespace[4]）与位置重建。SESSION scope 需要 session_id：
    显式传入优先，否则回退 memory_session_var（ADR-0009 的可信上下文约定，
    依赖由此在签名上可见）。
    """

    parts: tuple[str, ...]

    @classmethod
    def of(
        cls,
        scope: MemoryScope,
        identity: IdentityContext,
        *,
        session_id: str | None = None,
    ) -> MemoryNamespace:
        if scope not in (MemoryScope.USER, MemoryScope.SESSION):
            raise NotImplementedError(f"Memory scope {scope.value} is not implemented")
        if scope.value not in identity.scopes:
            raise PermissionError("Memory scope is not authorized")
        if scope is MemoryScope.SESSION:
            bound = session_id or memory_session_var.get()
            if not bound:
                raise ValueError("SESSION scope requires a trusted session binding")
            return cls((NAMESPACE_ROOT, identity.tenant_id, identity.user_id, scope.value, bound))
        return cls((NAMESPACE_ROOT, identity.tenant_id, identity.user_id, scope.value))

    @classmethod
    def authorize(cls, parts: Iterable[str], identity: IdentityContext) -> MemoryNamespace:
        """校验来自外部边界（LangMem op）的 namespace 元组：形状 + 归属。

        形状/根段/授权 scope 不符抛 PermissionError；SESSION 绑定缺失沿用
        of() 的 ValueError 语义（与本模块既有行为一致）。
        """
        candidate = tuple(parts)
        if len(candidate) not in (4, 5) or candidate[0] != NAMESPACE_ROOT:
            raise PermissionError("Memory namespace is not authorized")
        namespace = cls(candidate)
        expected = cls.of(namespace.scope, identity)
        if namespace != expected:
            raise PermissionError("Memory namespace is not authorized")
        return namespace

    @property
    def scope(self) -> MemoryScope:
        return MemoryScope(self.parts[3])

    @property
    def session_id(self) -> str | None:
        return self.parts[4] if len(self.parts) == 5 else None

    def as_tuple(self) -> tuple[str, ...]:
        """LangGraph/LangMem 边界需要的原生 tuple 形状。"""
        return self.parts

    def as_json(self) -> str:
        """SQLite 记录层的序列化形状（与既有存储逐字节兼容）。"""
        return json.dumps(self.parts)

    def pk_parts(self, memory_id: str) -> list[str]:
        """Milvus 主键材料：namespace + memory_id（哈希前的稳定列表）。"""
        return [*self.parts, memory_id]


def scope_to_namespace(scope: MemoryScope, identity: IdentityContext) -> tuple[str, ...]:
    """兼容入口：等价于 MemoryNamespace.of(scope, identity).as_tuple()。"""
    return MemoryNamespace.of(scope, identity).as_tuple()
