"""langmem / trustcall 测试替身的共用形状（`tests/` 下多个文件复用，避免副本各自漂移）。

trustcall 在"**存在既有文档**"的分支里访问 `self.bound.bound.bind_tools(...)`：它假设
`bind_tools` 返回 `RunnableBinding`，`.bound` 才是底层模型。该分支只有在
`enable_deletes=True`（#157 解禁删除）且检索命中既有记忆时才走到——真实模型天然满足这个形状，
假模型必须显式模拟，否则删除/更新路径会在假模型上 AttributeError，而生产完全正常。

为什么不用项目自己的 `ScriptedModel`：trustcall 把 prompt 与模型用 `|` 组成 chain，
必须是真 LangChain Runnable；`src/agent_harness/model/scripted.py` 的替身只是鸭子类型，
没有 `__or__`。所以这里继承 `FakeMessagesListChatModel`。
"""

import uuid

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

from agent_harness.memory.fake_vector_store import FakeVectorStore
from agent_harness.memory.types import MemoryNamespace


class BoundSelfMixin:
    """`bind_tools` 无副作用，`bound` 指回自身（等价于"绑定无副作用的模型"）。

    trustcall 会 bind 自己的工具集；真模型换成假模型后，绑定不得真去改什么状态。
    """

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def bound(self):
        return self


class ScriptedChatModel(BoundSelfMixin, FakeMessagesListChatModel):
    """按序吐预置响应的 LangChain 假模型；`responses=` 语义同基类。"""


class AlwaysHitVectorStore(FakeVectorStore):
    """`search` 命中本 namespace 的**全部** key（真实 embedding 语义匹配的替身）。

    manager 内部的检索查询是**对话窗口文本**（`get_dialated_windows`），真实 embedding 下它与既有
    记忆是语义匹配；`FakeVectorStore` 是单向字面匹配（query ⊆ content），长窗口查询永远命中不了短
    记忆，会把"删除/更新链路"堵在检索那一步（而"检索得到既有记忆"正是这两条路径的前提：trustcall
    只在 `existing` 非空时才绑定 RemoveDoc，`PatchDoc` 也只在既有文档里找得到目标）。
    查询生成与检索策略是 #158 的范围，这里只让"检索得到"成立，其余（RemoveDoc 的 id 校验、
    adelete/aput → adapter → 记录库）全部走真实实现。

    副作用需要显式容忍：命中集合会包含"记录行已删、索引还没收敛"的 id —— 这正是 provider 删除被
    解禁后新出现的窗口，生产代码在 adapter 的 SearchOp 与能力层的检索里都跳过这类行。
    """

    async def search(self, query, identity, scope, limit):  # type: ignore[override]
        if not query:
            return []
        namespace = MemoryNamespace.of(scope, identity).as_tuple()
        return [(key, 1.0) for (ns, key) in self._rows if ns == namespace][: max(0, limit)]


def stable_doc_id(memory_id: str, namespace: tuple[str, ...]) -> str:
    """LangMem 认的文档 id（`MemoryStoreManager._stable_id` 的同一公式）。

    manager 只允许删"它自己检索回来的那些 id"，检索结果以这个 uuid5 为键，
    所以脚本化 `RemoveDoc` / `PatchDoc` 时必须给出同一个值。
    """
    return uuid.uuid5(uuid.NAMESPACE_DNS, str((*namespace, memory_id))).hex
