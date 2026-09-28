"""#298 / MEM-V2-2：一次 run 终结后，是否该触发记忆形成（AC1）。

纯函数：输入是终态形状 + 事件流 + 设置开关，输出是"合格 / 不合格 + 稳定 reason"。
不碰 Runtime、不读时钟、不写盘——AC1 的两半（合格终态各建一个 job、被排除的终态一个都不建）
因此可以完全脱离 Runtime 逐条钉住。

# 判定只看两个事实

`events` 是**本次 run** 的事件（`Session.since(run_start)`）。判定只问两件事：
有没有真实用户发言、有没有模型的成功回复。两者都是"存在性"判断，所以多给几条不会误判。

# "真实用户发言"的判定是复用的，不是重写的

`data["injected_by"]` 非空 = runtime 注入的样板消息（同错熔断纠偏），不计入。
该判定在 V1 `memory/extractor.py` 里已有实现、且被两处测试直接钉住，这里直接引用——
抄一份到 V2 必然漂移，而漂移的后果正是"注入消息被当成真实用户发言"这个 V1 修过的缺陷。

# reason 的优先级（PRD 的罗列不是优先级）

§5.1.2 把排除项列成一行，那是清单。这里的顺序按**信息量**排：

1. `extraction_disabled` —— 用户级开关，最上层的事实；
2. `cancelled` —— 取消 / 孤儿回收：run 根本没跑完，别的原因都只是噪声；
3. `unsupported_terminal_failure` —— 未获批的失败终态（含 `context_window_exceeded`
   与通用 `failed`；前者正是"本轮没调过模型"那一类）；
4. `no_user_input` —— 没有真实用户发言；
5. `explicit_opt_out` —— 用户明确说"别记这次"；
6. `no_model_call` —— 兜底：已是获批终态却没有任何模型调用事件（事实不完整）。

第 6 条排最后是刻意的：它覆盖的形态现实中几乎不出现。若提前，一个"用户要求退出"的 run
会被报成"没调过模型"，日志的归因就被带偏了。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
)
from agent_harness.memory.extractor import _is_runtime_injected
from agent_harness.session import MODEL_COMPLETED, USER_MESSAGE, SessionEvent

#: 能触发记忆形成的终态：正常完成 + 一张获批的受控失败（同错熔断）。
#: `#312`（T4）起预算/保险丝到顶**不再是失败终态**，而是非终态 `run/paused`
#: （`02 §5.2` / `03 §3.4`）——那时的 run 还没结束，抽记忆是过早的；同 run 由
#: `run/resumed` 接回，最终仍会走到完成臂并在这里入队。故旧的
#: `max_steps_exceeded` 从白名单移除（该终态在运行时已不可达）。
ELIGIBLE_TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED, STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
})

#: 取消类终态（ADR-0016 §2.1：断连消费 = `cancelled`；孤儿回收 = `orphaned`）。
#: 这两值在代码里以字面量出现（`agent/runtime.py` 的默认值与 `session/runmanager.py`
#: 的 supplier），没有常量可引——改那两处时要同时改这里。
CANCELLED_TERMINAL_STATUSES = frozenset({"cancelled", "orphaned"})

#: 逐轮退出标记（PRD §5.6.2「不要记住这次对话」）：`user/message` 的
#: `data[MEMORY_OPT_OUT_FIELD]` 为真 ⇒ 本轮不做记忆形成，且只影响本轮。
#: 产出方是命令 / UI 路径（后续票据），消费方是本模块——两边都引这个常量，别各写一份字面量。
MEMORY_OPT_OUT_FIELD = "memory_opt_out"

#: "模型真的回复过"的 durable 证据。取 `model/completed` 而**不**取 `model/failed`：
#: 只有成功的回复才带来可形成记忆的对话内容；而"只有失败、没有成功"的 run 必然终结在
#: 未获批的失败终态上（`failed`），本来就过不了终态那一关。多收一个 `model/failed`
#: 会是一条永远走不到的兜底分支，而不是多一层保护。
_MODEL_RESPONSE_EVENT_TYPE = MODEL_COMPLETED
_OPT_OUT_TEXT = re.compile(
    r"(?:\b(?:do\s+not|don't|dont)\s+remember\s+(?:this|the\s+current)\s+"
    r"(?:chat|conversation|session)\b|"
    r"不要记住(?:这|本)次(?:聊天|对话)|不要把(?:这|本)次对话记下来|本轮不要记忆)",
    re.IGNORECASE,
)


class FormationSkipReason(str, Enum):
    """不合格的稳定归因码。

    刻意与 PRD §6.2 的 `skip_reason`（`no_durable_value` 等）**分开**：那些是**模型**对
    这次内容下的判断，这些是**运行时**对"该不该跑"下的判断。合成一套会让
    "模型说没有值得记的"与"这次压根不该跑"在观测里无法区分。
    """

    EXTRACTION_DISABLED = "extraction_disabled"
    CANCELLED = "cancelled"
    UNSUPPORTED_TERMINAL_FAILURE = "unsupported_terminal_failure"
    NO_USER_INPUT = "no_user_input"
    EXPLICIT_OPT_OUT = "explicit_opt_out"
    NO_MODEL_CALL = "no_model_call"


@dataclass(frozen=True, slots=True)
class RunEndEligibility:
    """判定结果。两个字段是同一事实的两面，构造期即拒绝自相矛盾的组合。"""

    eligible: bool
    skip_reason: FormationSkipReason | None

    def __post_init__(self) -> None:
        if self.eligible and self.skip_reason is not None:
            raise ValueError("an eligible run carries no skip reason")
        if not self.eligible and self.skip_reason is None:
            raise ValueError("an ineligible run must carry a skip reason")


def decide_run_end_eligibility(
    *, terminal_status: str, events: Iterable[SessionEvent],
    extraction_enabled: bool = True,
) -> RunEndEligibility:
    """这个 run 终结时该不该入队一个记忆形成 job（`eligible=True` ⇒ 恰好一个，R1）。"""
    if not extraction_enabled:
        return _skip(FormationSkipReason.EXTRACTION_DISABLED)
    # 下面两条（取消类 / 未获批的终态）在**生产**路径上到不了这里：运行时只有正常完成臂
    # 与两张获批的受控失败臂会通知记忆形成（`agent/runtime.py::_notify_memory_formation`
    # 的全部调用点），取消 / 上下文超限 / 异常三条臂压根不调它。它们仍然留着——这是纯函数
    # 层的第二道兜底，判据是"白名单之外一律 fail closed"，将来有人给新臂接上通知时，
    # 错的那一支不会静默通过。（T8 两轴审查 P3：runtime 侧的注释原读起来像这两条可达。）
    if terminal_status in CANCELLED_TERMINAL_STATUSES:
        return _skip(FormationSkipReason.CANCELLED)
    if terminal_status not in ELIGIBLE_TERMINAL_STATUSES:
        return _skip(FormationSkipReason.UNSUPPORTED_TERMINAL_FAILURE)
    materialized = list(events)
    genuine_user_events = [
        event for event in materialized
        if event.type == USER_MESSAGE and not _is_runtime_injected(event)
    ]
    if not genuine_user_events:
        return _skip(FormationSkipReason.NO_USER_INPUT)
    if any(_opts_out(event) for event in genuine_user_events):
        return _skip(FormationSkipReason.EXPLICIT_OPT_OUT)
    if not any(event.type == _MODEL_RESPONSE_EVENT_TYPE for event in materialized):
        return _skip(FormationSkipReason.NO_MODEL_CALL)
    return RunEndEligibility(eligible=True, skip_reason=None)


def _skip(reason: FormationSkipReason) -> RunEndEligibility:
    return RunEndEligibility(eligible=False, skip_reason=reason)


def _opts_out(event: SessionEvent) -> bool:
    """该用户消息是否声明"别记这次"，只影响包含该 user/message 的当前 run。"""
    data = event.data if isinstance(event.data, dict) else {}
    content = data.get("content")
    return bool(data.get(MEMORY_OPT_OUT_FIELD)) or (
        isinstance(content, str) and bool(_OPT_OUT_TEXT.search(content))
    )
