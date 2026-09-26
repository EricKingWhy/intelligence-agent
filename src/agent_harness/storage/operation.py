"""Operation Ledger domain contract."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from enum import Enum

from pydantic import BaseModel, computed_field


class OperationState(str, Enum):
    """Durable lifecycle of one Tool invocation."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    NEED_RECONCILE = "NEED_RECONCILE"


class Operation(BaseModel):
    """Persisted identity and current state of one Tool invocation."""

    tool_call_id: str
    session_id: str
    run_id: str | None = None
    agent_id: str | None = None
    tool_name: str
    args_identity: str
    state: OperationState
    result_json: str | None = None
    artifact_ref: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    reconcile_meta: str | None = None

    @computed_field
    @property
    def operation_id(self) -> str:
        """会话内标识（= tool_call_id）；Ledger 主键是 (session_id, tool_call_id) 复合键（C5）。

        tool_call_id 由模型生成、只在会话内唯一——单列主键会让跨会话复用
        同一 id 的两个 Operation 互相覆盖。
        """
        return self.tool_call_id


#: `reconcile_meta` 里"副作用状态未证"的标记键（`#315`）。
#: 写入者是 ToolExecutor（收尾那一刻它就**知道**这次尝试有没有证明力），读取者是
#: 所有要在"能不能安全继续"上做判断的地方：AgentRuntime 的 deadline 稳定边界与
#: Resume 的开工前闸门。三处读**同一份**落盘事实，谁都不另存一个内存标记。
UNPROVEN_SIDE_EFFECT_KEY = "unproven_side_effect"


def unproven_meta(*, error_code: str, note: str) -> str:
    """把"副作用未证"写进 `reconcile_meta`（JSON 文本，`07 §4` 该字段的语义）。

    与 `state` **并存**、不替换状态机里的任何一档：`state` 描述这次尝试本身走到
    哪里（未证时是 `UNKNOWN`，见 `07 §4`），本标记说明"尝试结束 ≠ 世界状态已知"。
    Reconcile 裁决落地时会用裁决内容**覆盖**这一格（`_commit_reconcile`）——
    覆盖即"疑问已解除"，所以不需要第二个清除标记。
    """
    return json.dumps(
        {UNPROVEN_SIDE_EFFECT_KEY: True, "error_code": error_code, "note": note},
        ensure_ascii=False,
    )


def has_unproven_side_effect(operation: Operation) -> bool:
    """这次调用的副作用是否**仍**"无法证明已完成或未开始"（`07 §7` / ADR-0044 D4）。

    宽容读法：`reconcile_meta` 缺失 / 非 JSON / 形状不对 / 已被裁决覆盖 ⇒ `False`
    （与 `_int_map` / `_deadline_or_none` 同一条纪律：腐烂的历史数据只让**这一项**
    失去标记，不让整个判定抛错）。缺标记 ⇒ 未证不成立——标记的**写入**才是
    "Executor 承认自己证不出结论"那一刻，本函数不替它猜。
    """
    meta = operation.reconcile_meta
    if not meta:
        return False
    try:
        parsed = json.loads(meta)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, dict) and parsed.get(UNPROVEN_SIDE_EFFECT_KEY) is True


def needs_reconcile(operation: Operation) -> bool:
    """这个 Operation 是否**在 reconcile 解除前**不允许 run 继续（`#315` / `03 §5`）。

    两条判据取或，都只看落盘事实：
    - 状态非终态。**`PENDING` 是例外**——`07 §6` 明文"能证明尚未启动 ⇒ 可按策略
      重执行"，它不是未证；`RUNNING` / `UNKNOWN` / `NEED_RECONCILE` 都是。
    - 带着"副作用未证"标记的行：尝试本身结束了（可能有终态），但世界状态未知。
    """
    if operation.state in (
        OperationState.RUNNING,
        OperationState.UNKNOWN,
        OperationState.NEED_RECONCILE,
    ):
        return True
    return has_unproven_side_effect(operation)


class OperationContext(BaseModel):
    """Session identity attached to each Operation by the Runtime."""

    session_id: str
    run_id: str | None = None
    agent_id: str | None = None


class OperationLedger(ABC):
    """Async persistence boundary for Operation state."""

    @abstractmethod
    async def initialize(self) -> None:
        """Create the Ledger schema if it does not exist."""

    @abstractmethod
    async def create(self, operation: Operation) -> None:
        """Persist a new PENDING Operation."""

    @abstractmethod
    async def get(self, session_id: str, tool_call_id: str) -> Operation | None:
        """Load one Operation by its (session_id, tool_call_id) composite key."""

    @abstractmethod
    async def update_state(
        self,
        session_id: str,
        tool_call_id: str,
        state: OperationState,
        *,
        result_json: str | None = None,
        artifact_ref: str | None = None,
        reconcile_meta: str | None = None,
    ) -> Operation:
        """Move one Operation to an allowed next state and return it."""

    @abstractmethod
    async def list_for_session(self, session_id: str) -> list[Operation]:
        """List a Session's Operations in creation order."""

    @abstractmethod
    async def delete_for_session(self, session_id: str) -> int:
        """Remove every Operation of a Session; return how many rows went away.

        会话硬删的一部分（ADR-0029）：append-only 事件日志（唯一真相源）被删除后，
        台账行描述的对象已不存在，也没有任何东西可供 reconcile——留着它只剩
        "会话不存在却有台账"这一种解释。

        幂等：未知 / 已清理干净的 session 返回 0、不抛错（重跑即自愈，ADR-0029 D3）。
        """
