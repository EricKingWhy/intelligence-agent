"""Operation Ledger domain contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from pydantic import BaseModel, computed_field


class OperationState(str, Enum):
    """Durable lifecycle of one Tool invocation."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    NEED_RECONCILE = "NEED_RECONCILE"


class Operation(BaseModel):
    """Persisted identity and current state of one Tool invocation."""

    tool_call_id: str
    session_id: str
    run_id: str | None = None
    agent_id: str | None = None
    tool_name: str
    args_identity: str
    state: OperationState
    result_json: str | None = None
    artifact_ref: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    reconcile_meta: str | None = None

    @computed_field
    @property
    def operation_id(self) -> str:
        """Operation identity is exactly the originating tool_call_id."""
        return self.tool_call_id


class OperationContext(BaseModel):
    """Session identity attached to each Operation by the Runtime."""

    session_id: str
    run_id: str | None = None
    agent_id: str | None = None


class OperationLedger(ABC):
    """Async persistence boundary for Operation state."""

    @abstractmethod
    async def initialize(self) -> None:
        """Create the Ledger schema if it does not exist."""

    @abstractmethod
    async def create(self, operation: Operation) -> None:
        """Persist a new PENDING Operation."""

    @abstractmethod
    async def get(self, tool_call_id: str) -> Operation | None:
        """Load one Operation by its tool_call_id."""

    @abstractmethod
    async def update_state(
        self,
        tool_call_id: str,
        state: OperationState,
        *,
        result_json: str | None = None,
        artifact_ref: str | None = None,
        reconcile_meta: str | None = None,
    ) -> Operation:
        """Move one Operation to an allowed next state and return it."""

    @abstractmethod
    async def list_for_session(self, session_id: str) -> list[Operation]:
        """List a Session's Operations in creation order."""
