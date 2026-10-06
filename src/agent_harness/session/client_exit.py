"""W-12（#356）选项 B：客户端明确退出信号 contract（类型 + status 常量）。

`ExitImpact` 的形状（"退出会打断什么"的多维结果，而非 go/no-go 布尔）**adapted**
自 DeepSeek Harness（MIT License）——只借结构与判断形状（哪些维度算"有活"），
数据源换成本仓 Operation Ledger / 会话队列，未移植其 TypeScript / Electron IPC：
- https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop-host/src/quit-inspection.ts
- https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop-host/src/update-tasks.ts

**关键差异（design decision 6，必须如实记录）**：DSH 的 quit-inspection 读失败 /
Host 超时会 `throw`，调用方（quit-confirmation）据此**弹确认框让用户取消**；本仓
把"读失败"折成 `uncertain=True` 的**偏 busy**。但本仓的明确退出信号**本身就是用户
的确认**（客户端主动声明要走了），所以 `uncertain` **不改变暂停决策**——它只让
`ClientExitOutcome` 如实记录"这次收口带着读不确定"，绝不当作"可以跳过暂停"的依据。

判据来源：`docs/agents/356-design.md` §2（contract）/§3（quit-inspection）/§4
（W-05 fail-closed）；上游 `02 §5.2.1`、`03 §3.4/§5`、`07 §4`。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_harness.session.progress import ProgressWriteOutcome
from agent_harness.storage import OperationState

#: `ClientExitOutcome.status` 的三个取值（穷尽，无第四态）。
CLIENT_EXIT_PAUSED = "paused"
CLIENT_EXIT_IGNORED_ALREADY_SETTLED = "ignored_already_settled"
CLIENT_EXIT_IGNORED_NOT_MANAGED = "ignored_not_managed"

#: `07 §4` 状态机里的**终态**：这些 state 的 Operation 已结清。其余
#: （PENDING / RUNNING / UNKNOWN / NEED_RECONCILE）都是非终态——PENDING 虽是
#: "能证明尚未启动"可重执行的一档，但作为"未结清的行"仍计入 pending。
SETTLED_OPERATION_STATES = frozenset({
    OperationState.SUCCEEDED,
    OperationState.FAILED,
    OperationState.CANCELLED,
})


@dataclass(frozen=True)
class ExitImpact:
    """quit-inspection 的只读结果（design decision 2/3/5/6）。

    每一维回答"退出会打断什么"。任读失败 ⇒ ``uncertain=True`` 偏 busy，
    但不得据此改变暂停决策（decision 6，见模块 docstring 的差异说明）。
    """

    session_id: str
    has_inflight_tool: bool          # 有在途工具（Ledger RUNNING 行）
    has_inflight_child: bool         # 有活动/待恢复子 Agent（agent_id 与本 run 不同）
    has_pending_operation: bool      # Ledger 有未结清的行（非 settled）
    needs_reconcile: bool            # Ledger 含 UNKNOWN / NEED_RECONCILE / 未证副作用
    has_queued_input: bool           # 会话队列里有未投递输入
    uncertain: bool                  # 任一维读取失败 ⇒ 偏 busy
    detail: tuple[str, ...]          # 逐维如实记录（含失败原因），不谎报

    @property
    def busy(self) -> bool:
        """"可能有活"的偏置读法：不确定一律按 busy 计（DSH fail-safe）。

        只用于如实报告与未来关停钩子的事实判断；**MUST NOT** 用来跳过暂停
        （decision 6：明确退出信号权威，uncertain 只影响"如实程度"）。
        """
        return (
            self.uncertain
            or self.has_inflight_tool
            or self.has_inflight_child
            or self.has_pending_operation
            or self.needs_reconcile
            or self.has_queued_input
        )


@dataclass(frozen=True)
class ClientExitOutcome:
    """信号处理结果（design decision 1/3/4/6/7）。

    ``paused_event_seq`` 只在信号**同步等待**到那条 ``run/paused`` 落盘时才有值；
    本实现不在信号内等待暂停（decision 3：置缺席后由既有运行时链在稳定边界收口），
    故正常路径下为 ``None``，并在 ``detail`` 里如实说明。
    """

    session_id: str
    status: str                          # 三个 CLIENT_EXIT_* 常量之一
    run_id: str | None                   # 收口的 run_id（同 run，不新开）
    uncertain: bool                      # 是否带读不确定（decision 6）
    impact: ExitImpact | None            # quit-inspection 结果（未跑时为 None）
    progress: ProgressWriteOutcome | None  # W-05 严格写结果（N/A 时为 None）
    paused_event_seq: int | None         # 那条 run/paused 的 seq（信号不等暂停时为 None）
    detail: str                          # 人类可读收口说明（含 N/A / 幂等原因）


class ClientExitError(Exception):
    """严格 W-05 写失败时的 fail-closed 中止（design decision 4）。

    抛出即代表：信号路径**中止**，run 维持原状继续跑。抛出后 MUST NOT 置缺席、
    MUST NOT 暂停。无 cwd 锚不抛本错——那是 N/A，不是失败（decision 4）。
    """

    def __init__(
        self, *, session_id: str, run_id: str | None,
        progress: ProgressWriteOutcome, detail: str,
    ) -> None:
        super().__init__(detail)
        self.session_id = session_id
        self.run_id = run_id
        self.progress = progress   # ok=False 的写结果，含 error_kind/reason
        self.detail = detail
