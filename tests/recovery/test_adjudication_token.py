"""Recovery adjudication token contract tests (#253)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from agent_harness.recovery import RecoveryAdjudicationToken
from agent_harness.storage import Operation, OperationState


def _operation(
    *,
    session_id: str = "session-1",
    tool_call_id: str = "call-1",
    state: OperationState = OperationState.NEED_RECONCILE,
    args_identity: str = '{"command":"migrate"}',
    result_json: str | None = None,
) -> Operation:
    return Operation(
        session_id=session_id,
        tool_call_id=tool_call_id,
        run_id="run-1",
        agent_id="agent-1",
        tool_name="bash",
        args_identity=args_identity,
        state=state,
        result_json=result_json,
    )


def test_token_is_immutable_and_contains_compound_operation_identity() -> None:
    token = RecoveryAdjudicationToken.from_operation(_operation())

    assert token.session_id == "session-1"
    assert token.operation_id == "call-1"
    assert token.state is OperationState.NEED_RECONCILE
    assert len(token.state_fingerprint) == 64
    assert not hasattr(token, "__dict__")
    with pytest.raises(FrozenInstanceError):
        token.state = OperationState.UNKNOWN  # type: ignore[misc]


def test_state_fingerprint_uses_frozen_sha256_encoding() -> None:
    token = RecoveryAdjudicationToken.from_operation(_operation())

    assert token.state_fingerprint == (
        "7416b3e85744f415b290c23fa5796651049b7455347335ed95c597d884fe6dae"
    )


def test_same_snapshot_produces_equal_deterministic_token() -> None:
    operation = _operation()

    assert RecoveryAdjudicationToken.from_operation(
        operation
    ) == RecoveryAdjudicationToken.from_operation(operation)


def test_tampered_state_fingerprint_invalidates_token() -> None:
    operation = _operation()
    token = RecoveryAdjudicationToken.from_operation(operation)

    assert not replace(token, state_fingerprint="0" * 64).matches(operation)


def test_identity_change_invalidates_old_token() -> None:
    operation = _operation()
    token = RecoveryAdjudicationToken.from_operation(operation)

    assert token.matches(operation)
    assert not token.matches(operation.model_copy(update={"session_id": "session-2"}))
    assert not token.matches(operation.model_copy(update={"tool_call_id": "call-2"}))


@pytest.mark.parametrize(
    "state",
    [state for state in OperationState if state is not OperationState.NEED_RECONCILE],
)
def test_state_change_invalidates_old_token(state: OperationState) -> None:
    operation = _operation()
    token = RecoveryAdjudicationToken.from_operation(operation)

    assert not token.matches(operation.model_copy(update={"state": state}))


def test_token_excludes_args_and_result_payloads() -> None:
    token = RecoveryAdjudicationToken.from_operation(_operation())
    changed_payload = _operation(
        args_identity='{"command":"delete production"}',
        result_json='{"secret":"do-not-copy"}',
    )

    assert token.matches(changed_payload)
    assert "delete production" not in repr(token)
    assert "do-not-copy" not in repr(token)


def test_unknown_and_need_reconcile_remain_distinct_states() -> None:
    unknown = RecoveryAdjudicationToken.from_operation(
        _operation(state=OperationState.UNKNOWN)
    )
    pending = RecoveryAdjudicationToken.from_operation(
        _operation(state=OperationState.NEED_RECONCILE)
    )

    assert unknown.state is OperationState.UNKNOWN
    assert pending.state is OperationState.NEED_RECONCILE
    assert unknown != pending
