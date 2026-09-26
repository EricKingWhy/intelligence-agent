"""Persistence contracts and default local adapters."""

from agent_harness.storage.checkpoint import (
    Checkpoint,
    CheckpointBoundary,
    CheckpointPolicy,
    CheckpointStore,
    EveryStep,
    NoCheckpoint,
    OnStableBoundary,
)
from agent_harness.storage.operation import (
    UNPROVEN_SIDE_EFFECT_KEY,
    Operation,
    OperationContext,
    OperationLedger,
    OperationState,
    has_unproven_side_effect,
    needs_reconcile,
    unproven_meta,
)
from agent_harness.storage.session_meta import SessionMeta, SessionMetaStore
from agent_harness.storage.sqlite import (
    SqliteCheckpointStore,
    SqliteOperationLedger,
    SqliteSessionMetaStore,
)

__all__ = [
    "UNPROVEN_SIDE_EFFECT_KEY",
    "Checkpoint",
    "CheckpointBoundary",
    "CheckpointPolicy",
    "CheckpointStore",
    "EveryStep",
    "NoCheckpoint",
    "OnStableBoundary",
    "Operation",
    "OperationContext",
    "OperationLedger",
    "OperationState",
    "has_unproven_side_effect",
    "needs_reconcile",
    "unproven_meta",
    "SessionMeta",
    "SessionMetaStore",
    "SqliteCheckpointStore",
    "SqliteOperationLedger",
    "SqliteSessionMetaStore",
]
