"""分层预算的**唯一解析点**（`#308` T3：本票只强制 local turn fuse）。

语义权威是 ADR-0044 D1/D8 与 `02 §5.1`（三层控制不可互相替代、配置优先级
`Deployment > AgentProfile > Session/Run request override > Tool policy`、下层只能收窄、
越权在**任何 model / tool / child 工作开始前**拒绝且不静默截断），本模块不复述它们，
只做一件事：把各层的声明值合成一个生效 `LocalFuse` + 来源。

本票**不**实现的（别误以为漏了）：RunBudget（`#312`）与 SessionBudget（`#318`），
所以这里没有 turn 之外的 counter，也没有 `budget.run` / `budget.session` 的解析
（wire 形状由 `web/app.py` 请求模型的 `extra="forbid"` 挡住，422 而非静默忽略）。
`agent_turns` 的计数点就是既有的 Agent Loop 轮次计数（`runtime.py` 里"这一轮算一步"
那一行，在模型响应被规范化并接纳为决策之后递增）——本票不新增计数器、不新增 loop。

迁移期 alias `max_steps` 已由 `#320` 移除（`02 §5.1` 的 contract 阶段收口）：本模块只认
`budget.local.max_agent_turns`；旧客户端发 `max_steps` 由请求模型的 `extra="forbid"`
按未知字段 422 拒绝，不再有任何静默解释。

之所以是"解析"而不是"持有计数器"：本模块是**纯函数 + 值对象**，计数器与暂停/恢复状态
归 runtime 与后续票。这样"生效几轮、谁定的"能在 HTTP 层被投影（`as_projection()`），
而"已经跑了几轮"永远只有一个 owner。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_harness.session.errors import SessionServiceError

#: 默认 local fuse（`02 §5.1` 权威表 / PRD 决策 3）。**不是**"另一个低位数字"：
#: 它是"模型不收敛"的最后一道防线，正常停止由"模型不再返回 tool_calls"决定。
DEFAULT_MAX_AGENT_TURNS = 500

#: 生效值来源（投影里如实回传，客户端据此显示"谁定的"）。
SOURCE_DEPLOYMENT = "deployment"
SOURCE_PROFILE = "agent_profile"
SOURCE_REQUEST = "budget.local.max_agent_turns"


class BudgetRejection(SessionServiceError):
    """预算配置不可接受：**开工前**拒绝（无副作用，HTTP 422）。

    继承 `SessionServiceError` 是为了走 `web/domain_errors.py` 的**单一**状态码映射
    （同文件 docstring 已声明抛出点不限于 SessionService）。
    """


class BudgetCeilingExceeded(BudgetRejection):
    """下层声明的 ceiling **越过**生效上层 ceiling（D1：下层只能收窄）。"""


class BudgetConflict(SessionServiceError):
    """预算生命周期请求与当前**持久化状态**冲突（HTTP 409，零副作用）。

    与 `BudgetRejection`（422）的分界按 `11 §6.1`：**形状非法**是 422（字段缺失、
    值域不对、越权 ceiling），**状态对不上**是 409（version 过期、
    ceiling 不足以继续、活动 run 冲突、缺少所需变更依据、存在未 reconcile 副作用）。
    继承 `SessionServiceError` 的理由同 `BudgetRejection`：走 `web/domain_errors.py`
    的**单一**状态码映射。
    """


def _positive(value: int, *, layer: str) -> int:
    """形状闸门：local fuse 是正整数。

    wire 侧由 pydantic（`ge=1`）先挡一次；这里再挡一次是因为领域入口还有 CLI /
    内部调用方——"非正数"在语义上不是"很小的预算"，而是无意义的配置。
    """
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BudgetRejection(f"{layer} 必须是正整数：{value!r}")
    return value


@dataclass(frozen=True)
class LocalFuse:
    """生效的 local turn fuse + 它的来源（只读投影的数据面）。"""

    max_agent_turns: int
    source: str

    def as_projection(self) -> dict[str, Any]:
        """客户端可读的只读投影（`#308` Must Do「effective local fuse 的只读投影」）。

        形状刻意扁平：`max_agent_turns` / `source`。HTTP 层把它映射成响应头
        （`X-Local-Max-Agent-Turns` / `X-Local-Fuse-Source`），CLI 与后续票直接读字段。
        **不含**已消耗 counter：那是 RunBudget（`#312`）的数据面，本票没有。
        """
        return {
            "max_agent_turns": self.max_agent_turns,
            "source": self.source,
        }


def resolve_local_fuse(
    *,
    deployment: int = DEFAULT_MAX_AGENT_TURNS,
    profile: int | None = None,
    request: int | None = None,
) -> LocalFuse:
    """解析生效 local fuse（纯函数，任何工作开始前调用）。

    各层语义（D1 / R3）：

    - `deployment`：Deployment **hard ceiling**，默认 500；operator 只能**下调**它是
      本特性的政策旋钮。它是本票在根路径上的"生效上层"。
    - `profile`：AgentProfile 的声明值，`None` = 继承上层（内置三档位就是 `None`——
      出厂设定不写死数字，见 `profiles.py`）。它只能**收窄**：大于 deployment 的
      声明是配置错误（拒绝，不静默取小值）。
    - `request`：本次请求的覆盖，只能收窄到 ceiling 之下；越权 ⇒ 拒绝。

    生效值 = 各层显式声明中的**最小**值（未声明即继承），来源如实回传。
    """
    deployment = _positive(deployment, layer=SOURCE_DEPLOYMENT)

    ceiling = deployment
    ceiling_source = SOURCE_DEPLOYMENT
    if profile is not None:
        profile = _positive(profile, layer=SOURCE_PROFILE)
        if profile > deployment:
            raise BudgetCeilingExceeded(
                f"{SOURCE_PROFILE}={profile} 超过 {SOURCE_DEPLOYMENT} ceiling={deployment}："
                f"下层只能收窄（ADR-0044 D1）"
            )
        ceiling = profile
        ceiling_source = SOURCE_PROFILE

    if request is None:
        return LocalFuse(max_agent_turns=ceiling, source=ceiling_source)
    _positive(request, layer=SOURCE_REQUEST)
    if request > ceiling:
        raise BudgetCeilingExceeded(
            f"请求的 local fuse={request} 超过生效 ceiling={ceiling}"
            f"（{ceiling_source}）：下层只能收窄（ADR-0044 D1）"
        )
    return LocalFuse(max_agent_turns=request, source=SOURCE_REQUEST)
