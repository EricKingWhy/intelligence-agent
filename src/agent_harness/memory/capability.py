"""Core 依赖的 Memory 原语；身份由可信上下文提供。

# 生命周期契约（#156 MEM-1；后续三票——解禁 LangMem / 冲突消解 / 遗忘入口——共用）

- `store`：新建一条记忆，id 由实现生成。
- `update`：**按 id 覆盖写（upsert by id）**，同 namespace 内 last-write-wins；id 不存在
  时按新记忆写入（与 `store` 的区别只是 id 由调用方给定）。这一层只提供**机制**：
  "该不该更新、更新哪一条"是冲突消解（#158）的策略，不要写进本层。
- `forget`：**硬删**（记录行与向量都真的消失，不留墓碑）。id 不存在 → `False`（幂等，
  "忘了又忘"不是错误）；存在但属于别的 tenant/user/scope → `PermissionError`（与
  `store` 同源；静默 False 会让调用方以为删掉了）；SESSION 绑定的记忆要求当前上下文
  绑定同一 session（与 `store`/`get` 同款），没有绑定时 `ValueError`——**仅当这条记忆
  确实存在时**才做归属/绑定校验，不存在的 id 一律幂等 `False`。
- 索引是**异步**跟进的：`store`/`update`/`forget` 只保证权威记录与 outbox 意图落盘，
  向量索引由 relay 收敛（失败保留意图、下轮重试，见 `record_store` 与 `outbox_relay`）。
  调用方不得假设"方法返回 ⟹ 检索已更新"。
- 并发与版本：见 `record_store` 模块文档（内容 last-write-wins；outbox `revision` 是
  索引同步的乐观令牌）。
"""

from typing import Protocol

from agent_harness.memory.types import MemoryEntry, MemoryScope


class MemoryCapability(Protocol):
    async def store(self, scope: MemoryScope, content: str, metadata: dict) -> str: ...
    async def update(self, memory_id: str, scope: MemoryScope, content: str, metadata: dict) -> str: ...
    async def forget(self, memory_id: str) -> bool: ...
    async def recall(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryEntry]: ...
    async def search(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryEntry]: ...
