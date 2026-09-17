"""用户侧记忆入口（#159 B）：列出自己的记忆 + 硬删单条。

三条刻意的边界：

- **不接受 namespace 参数**（AC3/AC7）：tenant/user 由 `AuthSeamMiddleware` 从可信入口解析并
  绑定到 ContextVar，handler 只调契约（`MemoryCapability`）。归属校验在**领域层**再走一遍
  （`MemoryRecordStore.delete` 的 namespace 匹配）——不是只靠查询条件过滤。
- **不写 `SessionEvent`**（AC8）：记忆是 Capability 不是会话真相（不变量 #16/#22），
  审计走 `memory/audit.py` 的结构化日志（入口 `api`）。
- **只暴露 USER scope**：HTTP 请求没有 run 绑定的 session 上下文（`memory_session_var` 只在
  detached run 任务里设置），按 session 浏览记忆需要可信的会话上下文，不在本票范围。

来源闸复用项目 API 的 `require_trusted_origin`（ADR-0025 D1）：本地信任模式下只接受本机
来源；配置了 `jwt_secret` 时由认证层接管。**刻意不复制一份**——安全规则有两份副本就是两个
漂移面。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field

from agent_harness.capability.base import DegradeReason
from agent_harness.memory.audit import (
    ENTRY_API,
    OUTCOME_ABSENT,
    OUTCOME_DENIED,
    OUTCOME_FORGOTTEN,
    record_forget,
)
from agent_harness.memory.capability import MemoryCapability
from agent_harness.memory.errors import MemoryNotFound
from agent_harness.memory.types import MemoryEntry, MemoryScope, public_metadata
from agent_harness.web.domain_errors import memory_http_error
from agent_harness.web.projects import require_trusted_origin

if TYPE_CHECKING:
    from fastapi import FastAPI

#: 单页上限：记忆正文可能很长，列表必须有闸（客户端可传更小值）。
_MAX_LIMIT = 200

#: 装配期没能启用记忆时的**逐原因**说法（#225）。为什么要分开写：真机上
#: `CAPABILITIES` 里配着 memory、向量库连不上，装配期降级但路由层只能重复
#: "请在 CAPABILITIES 中配置 memory"——用户于是去改一个本来就配好的开关。
#: 前三码是**配置状态**（重试无用：`CAPABILITIES` 里少写/写错/缺前置项）；
#: `INIT_FAILED` 是**装配时出错**（改 CAPABILITIES 没用，要看日志、修它指向的东西）。
#: 机制与边界见 ADR-0010「补充（#225）」。
_DEGRADED_MESSAGE: dict[DegradeReason, str] = {
    DegradeReason.NOT_CONFIGURED: "memory capability 未启用：请在 CAPABILITIES 中配置 memory。",
    DegradeReason.DISABLED: (
        "memory capability 已在 CAPABILITIES 中登记但被禁用（enabled=false）："
        "请改为 enabled=true（或删掉这条登记）。"
    ),
    DegradeReason.MISSING_SETTINGS: (
        "memory capability 缺前置配置：向量检索（MILVUS_URI / MILVUS_TOKEN / "
        "MILVUS_COLLECTION）与嵌入模型（EMBEDDING_MODEL / EMBEDDING_BASE_URL / "
        "EMBEDDING_API_KEY）两组都齐才装配记忆。补齐后重启后端。"
    ),
    DegradeReason.INIT_FAILED: (
        "memory capability 初始化失败：CAPABILITIES 里已登记且启用，但装配时出错"
        "（记忆向量库不可达是常见原因）。这不是「没配置」——改 CAPABILITIES 没用；"
        "完整原因在后端日志的「capability 'memory' 初始化失败」那条里，排除后重启后端即可恢复。"
    ),
}


class MemorySummary(BaseModel):
    """列出一条记忆时给用户看的字段（不含 provider 内部载荷）。"""

    id: str
    content: str
    scope: MemoryScope
    metadata: dict = Field(default_factory=dict)
    created_at: str


class MemoryDeleted(BaseModel):
    """硬删成功的结果。"""

    id: str
    deleted: bool


def _summary(entry: MemoryEntry) -> MemorySummary:
    return MemorySummary(id=entry.id, content=entry.content, scope=entry.scope,
                         metadata=public_metadata(entry.metadata), created_at=entry.created_at)


def register_memory_routes(app: FastAPI) -> None:
    """把记忆路由挂到既有 app（`create_app` 里一行调用的接入面）。"""

    async def _capability() -> MemoryCapability:
        _, wiring = await app.state.agent.get_wiring()
        components = wiring.memory
        if components is None:
            # 能力缺席 → 503（比 404 诚实："能力没启用"不是"这个资源不存在"）。
            # 但"为什么缺席"必须分得开（#225）：`wiring.degradations` 里记着装配期
            # 的分类原因，**缺省 = 不在 CAPABILITIES 里**（见该字段的说明）。
            # `detail` 是 `{code, message}`：code 给机器（前端据它决定"未启用"还是
            # "故障 + 重试"），message 给人。判别走码，不走中文。
            recorded = wiring.degradations.get("memory")
            reason = DegradeReason.NOT_CONFIGURED if recorded is None else DegradeReason(recorded)
            raise HTTPException(
                status_code=503,
                detail={"code": reason.value, "message": _DEGRADED_MESSAGE[reason]},
            )
        return components.capability

    @app.get("/api/memories")
    async def list_memories(
        limit: int = Query(50, ge=1, le=_MAX_LIMIT, description="单页条数上限"),
        offset: int = Query(0, ge=0, description="跳过的条数（按创建时间倒序）"),
        _: None = Depends(require_trusted_origin),
    ) -> list[MemorySummary]:
        """列出**当前身份**的记忆（USER scope，分页）。

        读的是权威记录而不是向量检索：管理界面要的是"我的记忆都有哪些"，不是"哪几条最像
        某个 query"（`MemoryCapability.list_entries` 的契约）。
        """
        capability = await _capability()
        try:
            entries = await capability.list_entries(MemoryScope.USER, limit, offset)
        except PermissionError as error:
            # 认证通过但身份没有 "user" scope（`MemoryNamespace.of` 的授权校验）。不翻译就会
            # 以未登记领域异常的形状冒成 500——AC6 要的是明确状态码；同一身份的 DELETE 也是
            # 403，两个入口必须给同一个答案。
            raise memory_http_error(error) from error
        return [_summary(entry) for entry in entries]

    @app.delete("/api/memories/{memory_id}")
    async def forget_memory(
        memory_id: str, _: None = Depends(require_trusted_origin)
    ) -> MemoryDeleted:
        """硬删一条记忆（不可恢复；与模型工具同一个领域动词）。

        状态码语义：删掉 → 200；id 不存在 → **404**（不是幂等 204：用户对着一个具体 id 点
        删除，"这条已经不在了"是要报出来的结果）；存在但不能由**这个入口**删除 → **403**
        （领域层的归属校验如实上报，不伪装成 404）。"不能由这个入口删"包含两种：属于别人，
        以及属于当前身份但 scope 是 SESSION——HTTP 请求没有可信会话绑定，按 id 解析不出那一行
        （`row_namespace_matches` 的既定语义），本票的用户 API 只暴露 USER scope。领域层的
        `forget` 仍是幂等 False——那是对后台路径的契约，入口层在这里把它显式化成结果。
        """
        capability = await _capability()
        try:
            forgotten = await capability.forget(memory_id)
        except PermissionError as error:
            record_forget(entry_point=ENTRY_API, memory_id=memory_id, outcome=OUTCOME_DENIED)
            raise memory_http_error(error) from error

        if not forgotten:
            record_forget(entry_point=ENTRY_API, memory_id=memory_id, outcome=OUTCOME_ABSENT)
            error = MemoryNotFound(memory_id)
            raise memory_http_error(error) from error

        record_forget(entry_point=ENTRY_API, memory_id=memory_id, outcome=OUTCOME_FORGOTTEN)
        return MemoryDeleted(id=memory_id, deleted=True)
