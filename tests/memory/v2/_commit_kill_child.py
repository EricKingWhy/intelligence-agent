"""Child process for the #1494 AC3 hard-kill windows of the commit seam.

Four windows, each a real ``SIGKILL`` (the parent kills this process after the
barrier line):

``before_publish``
    Receipt (PREPARED) and both digests are on disk, the target file still holds
    the *before* content. Recovery must republish exactly once.
``after_replace``
    ``os.replace`` already happened, the receipt has not been marked published.
    Recovery must see ``disk == after``, mark the commit complete and never append
    the entry a second time.
``before_approval_status``
    A staged write was replayed (content published, ``stgp_`` receipt committed)
    but the staged file still says ``pending``. Retrying the approval must replay
    the same operation id and flip the status without writing twice.
``partial_publish``
    A two-file ``remove_by_digest`` cleaned ``memory`` and died before ``user``.
    Recovery must finish the second file, not restart the first.

The handshake is one printed line (the parent's only observable) plus a permanent
block; the kill itself is done by the parent with ``process.kill()`` — no in-process
exception is used as a stand-in for a crash.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import agent_harness.memory.v2.write_approval as write_approval_module
from agent_harness.memory.v2 import write_approval as approval
from agent_harness.memory.v2.commit import build_markdown_commit
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore, entry_digest
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.memory.v2.write_approval import StagedWriteStore

BARRIER = "AT_WINDOW"

LOCAL = TrustedMemoryIdentity(tenant_id="local", user_id="local")


def _stall() -> None:
    """Block this thread forever; the parent kills the whole process."""
    threading.Event().wait()


def _window(name: str) -> None:
    print(f"{BARRIER}:{name}", flush=True)
    _stall()


async def _run(window: str, root: Path, argument: str, extras: list[str]) -> None:
    engine = MarkdownMemoryStore(root / "memories")
    engine.load_from_disk()
    commit = build_markdown_commit(engine, root)

    if window == "before_publish":
        const = str(extras[0])

        def _hook(path: Path, entries: list[str]) -> None:
            _window(window)

        engine._write_file = _hook  # type: ignore[method-assign]
        await commit.add("memory", const, operation_id=argument, origin="foreground")
        return

    if window == "after_replace":

        def _mark(operation_id: str, target: str) -> None:
            _window(window)

        commit._receipts.mark_target_published = _mark  # type: ignore[method-assign]
        await commit.add(
            "memory", str(extras[0]), operation_id=argument, origin="foreground")
        return

    if window == "partial_publish":
        digests = [entry_digest(entry) for entry in extras]

        def _partial(operation_id: str) -> None:
            _window(window)

        commit._receipts.mark_partial = _partial  # type: ignore[method-assign]
        await commit.remove_by_digest(digests, operation_id=argument, origin="foreground")
        return

    if window == "before_approval_status":
        staged_id = argument
        real_write_json = write_approval_module.write_json

        def _write_json(path: Path, payload: dict) -> None:
            if payload.get("status") == approval.APPROVED:
                _window(window)
            real_write_json(path, payload)

        write_approval_module.write_json = _write_json  # type: ignore[assignment]
        staged = StagedWriteStore(commit.root, commit)
        await staged.approve(staged_id, LOCAL)
        return

    raise AssertionError(f"unknown kill window: {window}")


async def main() -> None:
    window = sys.argv[1]
    root = Path(sys.argv[2])
    argument = sys.argv[3] if len(sys.argv) > 3 else ""
    await _run(window, root, argument, sys.argv[4:])


if __name__ == "__main__":
    asyncio.run(main())
