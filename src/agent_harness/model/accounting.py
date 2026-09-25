"""Provider 账目能力与成本归属（`#313` T5）。

本模块回答两个问题，它们是**同一个契约**的两面：

1. 这条 Provider 集成的响应里**能不能**拿到某个维度的账目（`ProviderAccounting`）
   —— 决定「显式 token/cost ceiling 能不能被强制执行」（判定在
   `agent/run_budget.py::validate_ceiling_enforceability`，422 口径见 `11 §6.1`）。
2. 单个响应里**这一次**的归属成本是多少（`cost_usd_from_response`）。

语义权威（本模块不复述，只引用）：`02 §5.1`（七个 counter 的定义与唯一计数点、
`cost_usd` 只在 Provider 给出可靠归属时累计）、`11 §6.1`（不可得 = unavailable，
MUST NOT 记为 0；显式 ceiling 在本链无法强制执行时 422）、ADR-0044 D2。

**为什么能力是声明的、不是探测的**：422 必须在**第一次 Provider 请求之前**给出
（`11 §6.1`「无副作用」），而"这条链会不会报 usage"只能由**集成本身**回答——
探测要么先发一次请求（违反"无副作用"），要么靠猜。所以能力是集成方对自己线格式的
陈述，落在 `create_chat_model` 造出来的模型这一层，由装配层取用。

**当前 Harness 的线格式（OpenAI 兼容 chat completions）能力**：
- `reports_usage=True`：`usage` 对象是线格式的一部分，集成把它映射成
  `usage_metadata`（`model/provider.py` 的 `ReasoningChatOpenAI`；提取见
  `agent/runtime.py::_usage_from_response`）。
- `reports_cost=False`：OpenAI 兼容线格式**没有**成本字段——空口无凭的费率表就是
  "编价"（`02 §5.1` 明文禁止），所以本链的显式 `max_cost_usd` 一律 422，
  而不是接受一个永远不会触发的 ceiling。将来有真正带归属成本的集成时，
  改这一个常量并在 `cost_usd_from_response` 的槽位上落地即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

#: `model/request.data.role`（`03 §3.4` 的 closeout 两值与 `02 §5.1` 的请求来源）。
#: 这三个值就是"每一次实际 Provider 请求"的全集：决策的 primary 尝试、同一次决策
#: 里 fallback 的第二次尝试、暂停前那一次有界 closeout。
PROVIDER_ROLE_PRIMARY = "primary"
PROVIDER_ROLE_FALLBACK = "fallback"
PROVIDER_ROLE_CLOSEOUT = "closeout"
PROVIDER_ROLES: frozenset[str] = frozenset(
    {PROVIDER_ROLE_PRIMARY, PROVIDER_ROLE_FALLBACK, PROVIDER_ROLE_CLOSEOUT}
)

#: `model/request.data.outcome`：这次请求有没有拿到响应。
#: 拿不到响应 ≠ 没发生过请求——它照样占 `model_requests` 一格（`02 §5.1`）。
REQUEST_OUTCOME_COMPLETED = "completed"
REQUEST_OUTCOME_FAILED = "failed"

#: 归属成本在响应元数据里的键（USD，十进制）。**这是集成契约**：谁在自己的
#: Provider 适配层拿到归属成本，就填这个键（值形见 `cost_usd_from_response`）。
COST_METADATA_KEY = "cost"


@dataclass(frozen=True)
class ProviderAccounting:
    """一条 Provider 集成在账目维度上的**能力声明**。

    两个维度各自独立：能报 usage 不等于能报成本（当前链正是这样）。
    `False` 的维度 = 该维度的显式 ceiling 无法强制执行 ⇒ 请求被拒（422），
    而不是接受一个永远不会触发的 ceiling（`11 §6.1`）。
    """

    reports_usage: bool
    reports_cost: bool

    def as_projection(self) -> dict[str, Any]:
        """可执行性投影（`11 §6.1`「token/cost 维度的可执行性」）。

        只暴露"能不能强制"，不暴露"为什么"——理由进日志/文档，不进契约
        （客户端要据此决定"能不能配这个 ceiling"，多一个字段就多一处漂移面）。
        """
        return {
            "max_total_tokens": "enforceable" if self.reports_usage else "unavailable",
            "max_cost_usd": "enforceable" if self.reports_cost else "unavailable",
        }


#: 当前 Harness 唯一真实集成（`model/provider.py::create_chat_model`，OpenAI 兼容
#: chat completions）的账目能力。换集成 = 改这一行，判定与投影都跟着它走。
HARNESS_MODEL_ACCOUNTING = ProviderAccounting(reports_usage=True, reports_cost=False)


def cost_usd_from_response(response: Any) -> Decimal | None:
    """从模型响应里读**Provider 归属的**USD 成本；没有就返回 `None`（绝不编价）。

    只认 `response_metadata[COST_METADATA_KEY]` 这一个槽位，且只接受**非负、
    有限**的值（`int` / `float` / `Decimal` / 十进制字符串——wire 上成本是十进制，
    二进制浮点相等不是契约，见 `11 §6.1`）。形状不合、负数、NaN/Inf 一律当作
    "该次响应没有归属成本"，返回 `None` —— 这是 unavailable 语义，**不是 0**：
    0 会被对账当成"这次不要钱"，而事实是"这次不知道多少钱"。

    字符串按十进制解析（`Decimal("0.0012")`）：JSON 里成本常以字符串形式给出，
    直接 `float()` 会引入与 wire 不等价的二进制近似。
    """
    metadata = getattr(response, "response_metadata", None)
    if not isinstance(metadata, dict):
        return None
    return _decimal_or_none(metadata.get(COST_METADATA_KEY))


def _decimal_or_none(raw: Any) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    elif isinstance(raw, str):
        try:
            value = Decimal(raw.strip())
        except (InvalidOperation, ValueError):
            return None
    else:
        return None
    if not value.is_finite() or value < 0:
        return None
    return value
