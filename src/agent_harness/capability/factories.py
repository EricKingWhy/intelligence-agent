"""Builtin Capability factories（ADR-0010 Q5）：按 settings 构造 Provider 实例。

所有具体 SDK import 都在 factory 函数内部惰性执行——Core / 其他 capability
不因本模块的存在而依赖可选 extra（不变量 #21）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_harness.config import Settings
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model


class MemoryComponents:
    """聚合 Memory 子系统内各组件，统一生命周期（由装配方持有并关闭）。"""

    def __init__(self, capability: Any, records: Any, vectors: Any, relay: Any, writeback: Any) -> None:
        self.capability = capability
        self.records = records
        self.vectors = vectors
        self.relay = relay
        self.writeback = writeback

    async def initialize(self) -> None:
        # SQLite 记录库必须先建表，再让 relay 读取 outbox；向量库惰性建立 collection。
        if hasattr(self.records, "initialize"):
            await self.records.initialize()
        if hasattr(self.vectors, "initialize"):
            await self.vectors.initialize()

    async def close(self) -> None:
        # 关闭顺序：先停 relay（停止派生任务），再关写回任务池，最后断向量库连接。
        await self.relay.stop()
        await self.writeback.close()
        if hasattr(self.vectors, "close"):
            await self.vectors.close()


def build_memory_components(settings: Settings) -> MemoryComponents | None:
    """按 settings 决定是否能装配 Memory；配置不全返回 None（OPTIONAL_RUNTIME 降级）。

    与 .env 的两个最小集合对齐：
      - 向量检索：milvus_uri + milvus_token + milvus_collection（无则不做语义记忆）
      - 嵌入模型：embedding_model + embedding_base_url + embedding_api_key（无则无法嵌入）
    两者都齐才装配；任一缺失返回 None，Runtime 继续工作但没有记忆能力。
    """
    milvus_ready = bool(
        settings.milvus_uri
        and settings.milvus_token.get_secret_value()
        and settings.milvus_collection
    )
    embedding_ready = bool(
        settings.embedding_model
        and settings.embedding_base_url
        and settings.embedding_api_key.get_secret_value()
    )
    if not (milvus_ready and embedding_ready):
        return None

    from agent_harness.memory.embeddings import create_embeddings
    from agent_harness.memory.extractor import MemoryExtractor
    from agent_harness.memory.langmem_capability import LangMemMemoryCapability
    from agent_harness.memory.milvus_vector_store import MilvusVectorStore
    from agent_harness.memory.outbox_relay import OutboxRelay
    from agent_harness.memory.sqlite_record_store import SqliteMemoryRecordStore
    from agent_harness.memory.writeback import MemoryWriteback

    records = SqliteMemoryRecordStore(Path(settings.workspace_dir) / "memory.db")
    vectors = MilvusVectorStore(settings, create_embeddings(settings))
    capability = LangMemMemoryCapability(records, vectors)
    relay = OutboxRelay(records, vectors)
    writeback = MemoryWriteback(capability, MemoryExtractor(create_chat_model(ModelConfig.from_settings(settings))))
    return MemoryComponents(
        capability=capability,
        records=records,
        vectors=vectors,
        relay=relay,
        writeback=writeback,
    )
