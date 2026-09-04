"""Real Zilliz Gate: connection and failure paths independent of embedding choice.

The semantic Runtime gate remains pending an explicitly configured embedding model.
Run explicitly with -m integration; no SDK mock or direct client business path.
"""

from uuid import uuid4

import pytest
from pydantic import SecretStr

from agent_harness.config import Settings
from agent_harness.identity import IdentityContext
from agent_harness.memory.milvus_vector_store import MilvusVectorStore
from agent_harness.memory.types import MemoryScope
from agent_harness.memory.vector_store import VectorStoreError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def gate_settings():
    settings = Settings()
    if not settings.milvus_uri or not settings.milvus_token.get_secret_value():
        pytest.skip("Real Milvus connection is not configured")
    if settings.milvus_collection != "memory_gate_test":
        pytest.skip("Gate requires the dedicated memory_gate_test collection")
    return settings


async def test_real_connection_and_missing_collection(gate_settings):
    vectors = MilvusVectorStore(gate_settings)
    try:
        collections = await vectors.connect()
        assert isinstance(collections, list)
    finally:
        await vectors.close()

    # A unique missing name verifies query error mapping without mutating any collection.
    missing = gate_settings.model_copy(update={"milvus_collection": "memory_gate_absent_" + uuid4().hex})
    vectors = MilvusVectorStore(missing)
    try:
        await vectors.connect()
        with pytest.raises(VectorStoreError) as error:
            await vectors.get("gate-no-record", IdentityContext("gate", "alice", ["user"]), MemoryScope.USER)
        assert error.value.code == "collection_not_found"
    finally:
        await vectors.close()


async def test_real_invalid_token_is_mapped(gate_settings):
    # Never modify the actual token or put it into assertions / exception messages.
    invalid = gate_settings.model_copy(update={"milvus_token": SecretStr("gate-deliberately-invalid-token")})
    vectors = MilvusVectorStore(invalid)
    try:
        with pytest.raises(VectorStoreError) as error:
            await vectors.connect()
        assert error.value.code == "authentication"
    finally:
        await vectors.close()
