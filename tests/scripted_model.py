"""ScriptedModel：不访问真实 API 的确定性模型替身。

设计目标（Day2 Task3）：
1. 按预设剧本依次返回 AIMessage，剧本耗尽时明确报错，绝不静默循环；
2. 每次被调用时记录 Request Snapshot：Runtime 到底发来了什么
   （messages 消息链 + bind_tools 绑定的工具描述）。

Snapshot 让测试能断言"Runtime 发给模型的内容"，这是真实模型给不了的。
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AIMessage, AnyMessage


@dataclass
class RequestSnapshot:
    """一次模型调用时 Runtime 实际发来的请求。"""

    messages: list[AnyMessage]
    tools: list | None = None


class ScriptedModel:
    """按剧本响应的假 ChatModel：bind_tools 记住工具，ainvoke 吐剧本 + 记快照。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)
        self._cursor = 0  # 下一条要吐的剧本下标
        self.bound_tools: list | None = None
        self.snapshots: list[RequestSnapshot] = []

    def bind_tools(self, tools: list, **kwargs) -> ScriptedModel:
        """记住绑定的工具描述；返回 self，让 demo 能链式调用。

        **kwargs 吞掉 strict 等 Provider 专属参数：替身不需要它们，但必须
        接受被测对象（demo）传过来的任何额外参数，否则签名不匹配会直接 TypeError。
        """
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages: list[AnyMessage], **kwargs) -> AIMessage:
        """吐下一条剧本 + 记录本次请求快照；剧本耗尽时明确报错。"""
        # 1. 剧本耗尽检查：调用次数超出预期就明确失败，绝不静默重复最后一条
        if self._cursor >= len(self._responses):
            raise RuntimeError("ScriptedModel 剧本耗尽：Runtime 调用次数超出预期")
        # 2. 记录快照：messages 必须拷贝（list(messages)）再存，否则 Runtime
        #    后续往同一个 list extend 新消息时，会把历史快照一起污染
        self.snapshots.append(
            RequestSnapshot(messages=list(messages), tools=self.bound_tools)
        )
        # 3. 消费剧本：返回当前下标的 AIMessage，然后 cursor 前进
        response = self._responses[self._cursor]
        self._cursor += 1
        return response