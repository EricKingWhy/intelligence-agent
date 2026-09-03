"""Agent 运行结果与流式事件的最小数据结构。

为什么独立成 types.py：
- runtime.py 只该关心"怎么驱动循环"，不该同时背负数据结构定义；
- 后续 Task（日志、错误回填、max_steps、流式）都要引用同一种结果形状，
  集中在一处修改入口更清晰。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 运行状态用字符串常量表达，而不是 Enum：
# - 字段少、分支简单，Enum 是过度抽象（违背 Day 3 "简洁优先"）；
# - 字符串在日志和断言里一眼能读，Debug 更直接。
# 正常完成
STATUS_COMPLETED = "completed"
# 不收敛兜底：模型反复请求工具直到 max_steps（Task 2 完整验证，Task 1 只留结构）
STATUS_MAX_STEPS_EXCEEDED = "max_steps_exceeded"


@dataclass
class AgentRunResult:
    """一次 AgentRuntime.run() 的最终结果。

    字段语义：
    - status: 正常结束用 completed；模型一直不收敛、撞到 max_steps 用 max_steps_exceeded。
    - final_text: 最终回答文本。正常完成时来自"没有 tool_calls 的那一轮模型的 content"，
      不是 Runtime 自己拼接的；max_steps_exceeded 时为空串（绝不伪造最终回答）。
    - steps: 模型被调用的轮数（不是工具个数）。一个含多个 tool_call 的响应仍算 1 步，
      避免 max_steps 出现 off-by-one。
    """

    status: str
    final_text: str
    steps: int

    @property
    def completed(self) -> bool:
        """便捷判断：是否模型自然停止（而非撞到 max_steps 兜底）。"""
        return self.status == STATUS_COMPLETED


@dataclass
class AgentEvent:
    """run_stream() 向外 yield 的流式事件信封（Phase 9）。

    定位（spec 11 §1）：AgentEvent 是 Runtime 向外发出的业务事件流，
    供 CLI / SSE / Web UI / Test / Trace 多面消费。与 Diagnostic Log 分层——
    AgentEvent 是可重放的业务事实，Diagnostic Log 是运维调试不可恢复。

    设计：
    - type 复用 SessionEvent 词汇（如 model/completed、tool/call），加上
      纯流式信号（model/started、model/delta）。
    - seq 对应 SessionEvent 的 seq；纯流式信号（model/delta）无 seq = None，
      表示"这条没持久化，刷新后从 model/completed 重建"。
    - data 是事件载荷 dict，形状跟 SessionEvent.data 一致（持久化事件）
      或流式专属（delta 的 {"delta": "..."} 等）。

    前端拿到后能据此区分：有 seq 的已被事实源记录，无 seq 的是 ephemeral 流式信号。
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int | None = None
    run_id: str | None = None
    step_id: int | None = None

    @property
    def is_durable(self) -> bool:
        """是否已被 SessionEvent 事实源记录（有 seq 即是）。"""
        return self.seq is not None
