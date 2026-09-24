"""分层预算的**唯一解析点**（`#308` T3：本票只强制 local turn fuse）。

## 语义来源（不在这里重新发明）

ADR-0044 D1/D8 与 `02 §5.1`：

- 三层控制不可互相替代，本票只实现第一层——**Local AgentRuntime fuse**：单个
  `AgentRuntime` 实例的 turn 保险丝，**不跨兄弟池化**；默认 `max_agent_turns = 500`；
- 配置优先级 `Deployment > AgentProfile > Session/Run request override > Tool policy`，
  **下层只能收窄**：越过生效上层 ceiling 的配置在**任何 model / tool / child 工作开始前**
  被拒绝（HTTP 422，`11 §6.1`），**不静默截断**；
- 公共字段是 `budget.local.max_agent_turns`；`max_steps` 是迁移期 deprecated alias
  （只发它 ⇒ 解释为根 AgentRuntime 的 local fuse；两者同时出现且相等 ⇒ 接受；不等 ⇒ 422）。
  alias 的**删除**由 `#320` 在证明零剩余调用方后执行——本模块因此不删它，也不猜它。

## 本票**不**实现的部分（读这段，别误以为漏了）

RunBudget（`#312`）与 SessionBudget（`#318`）尚未实现，所以这里**没有** turn 之外的
counter，也**没有** `budget.run` / `budget.session` 的解析。wire 形状那一侧由
`web/app.py` 的请求模型 `extra="forbid"` 挡住未知键（422，不是静默忽略）——将来谁实现
那些作用域，谁把字段加进请求模型，本模块再加对应解析。

`agent_turns` 的**计数点**就是既有的 Agent Loop 轮次计数（`runtime.py` 里"这一轮算一步"
那一行，在模型响应被规范化并接纳为决策之后递增）——本票不新增计数器，也不新增 loop
（车票 Must Not Do / §9.2）。

## 为什么是"解析"而不是"持有计数器"

本模块是**纯函数 + 值对象**：输入各层的声明值，输出生效值与来源。计数器与暂停/恢复状态
归 runtime 与后续票——这样"生效几轮、谁定的"可以在 HTTP 层被投影（`as_projection()`），
而"已经跑了几轮"永远只有一个 owner。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agent_harness.session.errors import SessionServiceError

logger = logging.getLogger(__name__)

#: 默认 local fuse（`02 §5.1` 权威表 / PRD 决策 3）。**不是**"另一个低位数字"：
#: 它是"模型不收敛"的最后一道防线，正常停止由"模型不再返回 tool_calls"决定。
DEFAULT_MAX_AGENT_TURNS = 500

#: 生效值来源（投影里如实回传，客户端据此显示"谁定的"）。
SOURCE_DEPLOYMENT = "deployment"
SOURCE_PROFILE = "agent_profile"
SOURCE_REQUEST = "budget.local.max_agent_turns"
SOURCE_ALIAS = "max_steps_alias"

#: 迁移期 alias 的字段名（`#320` 删除；本模块只把它当作一条输入）。
LEGACY_ALIAS_FIELD = "max_steps"

#: alias 来源时的 deprecation 提示（投影与响应头共用一份文案；日志文案 FREE，
#: 但这句会出现在**客户端可见**的投影里，所以固定下来）。
DEPRECATION_REPLACEMENT = "budget.local.max_agent_turns"


class BudgetRejection(SessionServiceError):
    """预算配置不可接受：**开工前**拒绝（无副作用，HTTP 422）。

    继承 `SessionServiceError` 是为了走 `web/domain_errors.py` 的**单一**状态码映射
    （同文件 docstring 已声明抛出点不限于 SessionService）。
    """


class BudgetAliasConflict(BudgetRejection):
    """`max_steps` 与新字段同时出现且**不等**（`02 §5.1` 冻结规则）。"""


class BudgetCeilingExceeded(BudgetRejection):
    """下层声明的 ceiling **越过**生效上层 ceiling（D1：下层只能收窄）。"""


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

    @property
    def used_legacy_alias(self) -> bool:
        """本次请求是否用了迁移期 alias（客户端 deprecation signal 的判据）。"""
        return self.source == SOURCE_ALIAS

    def as_projection(self) -> dict[str, Any]:
        """客户端可读的只读投影（`#308` Must Do「effective local fuse 的只读投影」）。

        形状刻意扁平：`max_agent_turns` / `source`，用了 alias 时多一个 `deprecation`
        段——HTTP 层把它映射成响应头（`X-Local-Max-Agent-Turns` / `X-Local-Fuse-Source`
        / `Warning`），CLI 与后续票直接读字段。**不含**已消耗 counter：那是 RunBudget
        （`#312`）的数据面，本票没有。
        """
        projection: dict[str, Any] = {
            "max_agent_turns": self.max_agent_turns,
            "source": self.source,
        }
        if self.used_legacy_alias:
            projection["deprecation"] = {
                "field": LEGACY_ALIAS_FIELD,
                "replacement": DEPRECATION_REPLACEMENT,
            }
        return projection


def _merge_turn_claims(
    *, request: int | None, alias: int | None
) -> tuple[int | None, str]:
    """合并「新字段」与「alias」两条声明（同一个请求上的两个字段）。

    返回 `(生效声明值, 来源)`。相等双字段**接受**（迁移期合法状态），并把来源记为
    alias —— 客户端还在发 deprecated 字段这件事必须继续可见（R5 的 deprecation
    signal 不能因为"恰好也发了新字段"而消失）。
    """
    if request is not None:
        _positive(request, layer=SOURCE_REQUEST)
    if alias is not None:
        _positive(alias, layer=LEGACY_ALIAS_FIELD)
    if request is None and alias is None:
        return None, ""
    if request is not None and alias is not None and request != alias:
        raise BudgetAliasConflict(
            f"{LEGACY_ALIAS_FIELD}={alias} 与 {SOURCE_REQUEST}={request} 冲突："
            f"两者同时出现时必须相等（迁移期 alias，见 02 §5.1）"
        )
    if alias is not None:
        logger.warning(
            "budget_local_fuse_alias_deprecated: 请求使用了 %s；请改用 %s",
            LEGACY_ALIAS_FIELD, DEPRECATION_REPLACEMENT,
        )
        return alias, SOURCE_ALIAS
    return request, SOURCE_REQUEST


def resolve_local_fuse(
    *,
    deployment: int = DEFAULT_MAX_AGENT_TURNS,
    profile: int | None = None,
    request: int | None = None,
    alias: int | None = None,
) -> LocalFuse:
    """解析生效 local fuse（纯函数，任何工作开始前调用）。

    各层语义（D1 / R3）：

    - `deployment`：Deployment **hard ceiling**，默认 500；operator 只能**下调**它是
      本特性的政策旋钮。它是本票在根路径上的"生效上层"。
    - `profile`：AgentProfile 的声明值，`None` = 继承上层（内置三档位就是 `None`——
      出厂设定不写死数字，见 `profiles.py`）。它只能**收窄**：大于 deployment 的
      声明是配置错误（拒绝，不静默取小值）。
    - `request` / `alias`：本次请求的覆盖，只能收窄到 ceiling 之下；越权 ⇒ 拒绝。

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

    claimed, claimed_source = _merge_turn_claims(request=request, alias=alias)
    if claimed is None:
        return LocalFuse(max_agent_turns=ceiling, source=ceiling_source)
    if claimed > ceiling:
        raise BudgetCeilingExceeded(
            f"请求的 local fuse={claimed} 超过生效 ceiling={ceiling}"
            f"（{ceiling_source}）：下层只能收窄（ADR-0044 D1）"
        )
    return LocalFuse(max_agent_turns=claimed, source=claimed_source)
