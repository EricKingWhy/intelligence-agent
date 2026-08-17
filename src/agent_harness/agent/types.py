"""Agent 运行结果的最小数据结构。

为什么独立成 types.py：
- runtime.py 只该关心"怎么驱动循环"，不该同时背负数据结构定义；
- 后续 Task（日志、错误回填、max_steps）都要引用同一种结果形状，
  集中在一处修改入口更清晰。

Day 3 Task 1 只需要最小字段：状态 / 最终文本 / 模型轮数。
"""

from __future__ import annotations

from dataclasses import dataclass

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
