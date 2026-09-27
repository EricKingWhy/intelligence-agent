"""Small Milvus adapter fake shared by V2 wiring and API tests."""

from __future__ import annotations

from typing import Any


class FakeMemoryVectorClient:
    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self.rows: dict[str, dict[str, Any]] = {}
        self.initialized = False
        self.closed = False
        self.fail_delete: Exception | None = None

    async def initialize(self) -> None:
        self.initialized = True

    async def connect(self) -> list[str]:
        return [self._settings.milvus_collection]

    async def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        await self.close()

    async def _embed(self, _text: str, *, document: bool = False) -> list[float]:
        return [0.25, 0.75]

    async def _call(self, operation: str, **kwargs: Any) -> Any:
        if operation == "upsert":
            for row in kwargs["data"]:
                self.rows[str(row["id"])] = row
            return None
        if operation == "delete":
            if self.fail_delete is not None:
                raise self.fail_delete
            memory_id = kwargs.get("filter_params", {}).get("memory")
            for key, row in list(self.rows.items()):
                if memory_id is None or row.get("memory_id") == memory_id:
                    self.rows.pop(key)
            return None
        if operation == "search":
            return [[
                {"entity": {"memory_id": row["memory_id"]}, "distance": 0.9}
                for row in self.rows.values()
            ]]
        if operation == "query":
            return [{"count(*)": len(self.rows)}]
        if operation == "describe_collection":
            return {
                "auto_id": False,
                "fields": [
                    {"name": "id", "type": "VARCHAR", "params": {}},
                    {"name": "memory_id", "type": "VARCHAR", "params": {}},
                    {"name": "tenant_id", "type": "VARCHAR", "params": {},
                     "is_partition_key": True},
                    {"name": "user_id", "type": "VARCHAR", "params": {}},
                    {"name": "scope", "type": "VARCHAR", "params": {}},
                    {"name": "session_id", "type": "VARCHAR", "params": {}},
                    {"name": "content", "type": "VARCHAR", "params": {}},
                    {"name": "metadata", "type": "JSON", "params": {}},
                    {"name": "vector", "type": "FLOAT_VECTOR", "params": {"dim": 2}},
                ],
            }
        if operation == "drop_collection":
            self.rows.clear()
            return None
        raise AssertionError(f"unexpected fake Milvus operation: {operation}")
