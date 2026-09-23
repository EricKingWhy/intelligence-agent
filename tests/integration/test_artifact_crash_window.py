"""Crash-window Gate for an Artifact saved before its SessionEvent/result is written."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agent_harness.config import Settings
from agent_harness.recovery.coordinator import RecoveryCoordinator
from agent_harness.session import (
    ARTIFACT_EXTERNALIZED,
    TOOL_CALL,
    TOOL_RESULT,
    JsonlSessionStore,
    Session,
    detect_dangling,
)
from agent_harness.storage import OperationState, SqliteOperationLedger
from agent_harness.storage.artifact import compute_artifact_id
from agent_harness.storage.local_artifact import LocalArtifactStore
from agent_harness.storage.s3_artifact import S3ArtifactStore
from agent_harness.tooling.result import ToolResult
from tests.integration._artifact_kill_child import (
    ARTIFACT_CONTENT,
    OVERFLOW_CHARS,
    TOOL_CALL_ID,
    TOOL_NAME,
)

_CHILD = Path(__file__).with_name("_artifact_kill_child.py")
_CHILD_TIMEOUT_SECONDS = 60
_RECOVER_TIMEOUT_SECONDS = 30


class ArtifactCleanupError(RuntimeError):
    """An owned object prefix still contains objects after cleanup."""


@pytest.fixture
def artifact_gate_root(tmp_path: Path) -> Path:
    """Own and verify removal of all per-test local files, including crash debris."""
    root = tmp_path / "root"
    yield root
    if root.exists():
        shutil.rmtree(root)
    assert not root.exists(), "artifact Gate left local session/store files behind"


async def _list_prefix(client, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    continuation_token: str | None = None
    while True:
        params = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token:
            params["ContinuationToken"] = continuation_token
        page = await client.list_objects_v2(**params)
        keys.extend(item["Key"] for item in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return keys
        continuation_token = page.get("NextContinuationToken")
        if not continuation_token:
            raise ArtifactCleanupError("object listing truncated without a continuation token")


async def _delete_owned_prefix(client, bucket: str, prefix: str) -> None:
    keys = await _list_prefix(client, bucket, prefix)
    for offset in range(0, len(keys), 1000):
        await client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": key} for key in keys[offset:offset + 1000]],
                    "Quiet": True},
        )
    remaining = await _list_prefix(client, bucket, prefix)
    if remaining:
        raise ArtifactCleanupError(
            f"artifact cleanup left {len(remaining)} object(s) under owned prefix {prefix!r}"
        )


def _run_kill(root: Path, *, provider: str, session_id: str) -> int:
    config = {
        "root": str(root),
        "provider": provider,
        "session_id": session_id,
        "marker_path": str(root / "side-effects" / "executed.txt"),
        "artifact_dir": str(root / "artifacts"),
    }
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, str(_CHILD), json.dumps(config)],
        timeout=_CHILD_TIMEOUT_SECONDS,
        env=env,
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode


def _crashed_state(root: Path, session_id: str):
    session_store = JsonlSessionStore(root / "sessions")
    events = session_store.read_events(session_id)
    ledger = SqliteOperationLedger(root / "state.db")
    return session_store, ledger, events


async def _recover(root: Path, session_id: str, *, disable_synthesis: bool = False) -> Session:
    session_store = JsonlSessionStore(root / "sessions")
    ledger = SqliteOperationLedger(root / "state.db")
    await ledger.initialize()
    coordinator = RecoveryCoordinator(
        session_store=session_store,
        workspace_registry=None,
        operation_ledger=ledger,
        database_path=root / "state.db",
    )
    if disable_synthesis:
        coordinator._decide = lambda *args, **kwargs: None
    return await asyncio.wait_for(
        coordinator.recover(session_id), timeout=_RECOVER_TIMEOUT_SECONDS,
    )


def _assert_recovered_result(session: Session, expected_artifact_id: str) -> ToolResult:
    assert detect_dangling(session.events) == []
    calls = [event for event in session.events if event.type == TOOL_CALL]
    results = [event for event in session.events if event.type == TOOL_RESULT]
    assert len(calls) == 1
    assert calls[0].data["tool_call_id"] == TOOL_CALL_ID
    assert len(results) == 1
    assert results[0].data["tool_call_id"] == TOOL_CALL_ID
    result = ToolResult.model_validate_json(results[0].data["content"])
    assert result.ok is True
    assert result.artifact_ref == expected_artifact_id
    return result


def _assert_recovered_artifact_is_ui_openable(
    session: Session, session_id: str, artifact_id: str,
) -> None:
    externalized = [
        event for event in session.events if event.type == ARTIFACT_EXTERNALIZED
    ]
    assert len(externalized) == 1
    assert externalized[0].data == {
        "artifact_id": artifact_id,
        "session_id": session_id,
        "source_tool": TOOL_NAME,
        "tool_call_id": TOOL_CALL_ID,
        "size": None,
        "mime_type": None,
    }
    result_index = next(
        index for index, event in enumerate(session.events)
        if event.type == TOOL_RESULT and event.data["tool_call_id"] == TOOL_CALL_ID
    )
    assert session.events.index(externalized[0]) < result_index


def _assert_crash_window(root: Path, session_id: str, events: list) -> str:
    calls = [event for event in events if event.type == TOOL_CALL]
    results = [event for event in events if event.type == TOOL_RESULT]
    assert len(calls) == 1
    assert calls[0].data["tool_call_id"] == TOOL_CALL_ID
    assert results == []
    assert not [event for event in events if event.type == ARTIFACT_EXTERNALIZED]
    assert detect_dangling(events) == [TOOL_CALL_ID]
    marker = root / "side-effects" / "executed.txt"
    assert marker.read_text(encoding="utf-8") == "executed\n"
    return compute_artifact_id(ARTIFACT_CONTENT)


async def run_local_artifact_crash_window(root: Path) -> None:
    """Phase 16's local Artifact crash window; caller owns and removes ``root``."""
    session_id = f"artifact-local-{uuid4().hex}"
    assert _run_kill(root, provider="local", session_id=session_id) == 137

    session_store, ledger, crashed_events = _crashed_state(root, session_id)
    artifact_id = _assert_crash_window(root, session_id, crashed_events)
    operation = await ledger.get(session_id, TOOL_CALL_ID)
    assert operation is not None and operation.state is OperationState.SUCCEEDED
    assert operation.artifact_ref == artifact_id
    assert operation.result_json is not None

    store = LocalArtifactStore(
        Settings(_env_file=None, artifact_dir=str(root / "artifacts"),
                 artifact_overflow_chars=OVERFLOW_CHARS),
        session_id=session_id,
    )
    stored = await store.load(artifact_id)
    assert stored.content == ARTIFACT_CONTENT
    artifact_files = sorted(path.name for path in (root / "artifacts" / session_id).iterdir())
    assert artifact_files == [artifact_id, f"{artifact_id}.json"]

    recovered = await _recover(root, session_id)
    result = _assert_recovered_result(recovered, artifact_id)
    assert result.artifact_ref == operation.artifact_ref
    assert (root / "side-effects" / "executed.txt").read_text(encoding="utf-8") == "executed\n"
    _assert_recovered_artifact_is_ui_openable(recovered, session_id, artifact_id)

    # A second recovery is idempotent and must not append another result or artifact event.
    recovered_again = await _recover(root, session_id)
    assert len([event for event in recovered_again.events if event.type == TOOL_RESULT]) == 1
    assert len([event for event in recovered_again.events
                if event.type == ARTIFACT_EXTERNALIZED]) == 1
    assert sorted(path.name for path in (root / "artifacts" / session_id).iterdir()) == artifact_files
    assert session_store.read_events(session_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_artifact_recovery_gate_fails_if_terminal_result_synthesis_is_removed(
    artifact_gate_root: Path,
) -> None:
    root = artifact_gate_root
    session_id = f"artifact-mutation-{uuid4().hex}"
    assert _run_kill(root, provider="local", session_id=session_id) == 137
    _session_store, _ledger, crashed_events = _crashed_state(root, session_id)
    artifact_id = _assert_crash_window(root, session_id, crashed_events)

    mutated = await _recover(root, session_id, disable_synthesis=True)
    with pytest.raises(AssertionError, match="artifact-gate-call"):
        _assert_recovered_result(mutated, artifact_id)


@pytest.mark.qiniu
@pytest.mark.asyncio
async def test_qiniu_artifact_crash_window_recovers_and_cleans_random_prefix(
    artifact_gate_root: Path,
) -> None:
    settings = Settings(artifact_overflow_chars=OVERFLOW_CHARS)
    required = {
        "ARTIFACT_STORE_ENDPOINT": settings.artifact_store_endpoint,
        "ARTIFACT_STORE_BUCKET": settings.artifact_store_bucket,
        "ARTIFACT_STORE_ACCESS_KEY": settings.artifact_store_access_key.get_secret_value(),
        "ARTIFACT_STORE_SECRET_KEY": settings.artifact_store_secret_key.get_secret_value(),
        "ARTIFACT_STORE_REGION": settings.artifact_store_region,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        pytest.skip(f"Configure Qiniu artifact settings: {', '.join(missing)}")

    aioboto3 = pytest.importorskip("aioboto3")
    session_id = f"artifact-qiniu-{uuid4().hex}"
    prefix = f"{session_id}/"
    client_kwargs = {
        "endpoint_url": settings.artifact_store_endpoint,
        "region_name": settings.artifact_store_region,
        "aws_access_key_id": settings.artifact_store_access_key.get_secret_value(),
        "aws_secret_access_key": settings.artifact_store_secret_key.get_secret_value(),
    }
    sdk_session = aioboto3.Session()
    async with sdk_session.client("s3", **client_kwargs) as client:
        bucket = settings.artifact_store_bucket
        assert await _list_prefix(client, bucket, prefix) == []
        try:
            root = artifact_gate_root
            assert _run_kill(root, provider="qiniu", session_id=session_id) == 137
            session_store, ledger, crashed_events = _crashed_state(root, session_id)
            artifact_id = _assert_crash_window(root, session_id, crashed_events)
            operation = await ledger.get(session_id, TOOL_CALL_ID)
            assert operation is not None and operation.state is OperationState.SUCCEEDED
            assert operation.artifact_ref == artifact_id
            keys = await _list_prefix(client, bucket, prefix)
            assert keys == [f"{prefix}{artifact_id}"]

            remote_store = S3ArtifactStore(settings, session_id=session_id)
            stored = await remote_store.load(artifact_id)
            assert stored.content == ARTIFACT_CONTENT

            recovered = await _recover(root, session_id)
            result = _assert_recovered_result(recovered, artifact_id)
            assert result.artifact_ref == operation.artifact_ref
            _assert_recovered_artifact_is_ui_openable(
                recovered, session_id, artifact_id,
            )
            assert (root / "side-effects" / "executed.txt").read_text(
                encoding="utf-8"
            ) == "executed\n"
            recovered_again = await _recover(root, session_id)
            assert len([event for event in recovered_again.events
                        if event.type == TOOL_RESULT]) == 1
            _assert_recovered_artifact_is_ui_openable(
                recovered_again, session_id, artifact_id,
            )
            assert session_store.read_events(session_id)
        finally:
            await _delete_owned_prefix(client, bucket, prefix)


@pytest.mark.asyncio
async def test_artifact_cleanup_gate_raises_when_owned_prefix_remains() -> None:
    class ResidualClient:
        async def list_objects_v2(self, **_params):
            return {"Contents": [{"Key": "owned-prefix/residual"}], "IsTruncated": False}

        async def delete_objects(self, **_params):
            return {"Errors": [{"Code": "AccessDenied"}]}

    with pytest.raises(ArtifactCleanupError, match="left 1 object"):
        await _delete_owned_prefix(ResidualClient(), "bucket", "owned-prefix/")
