"""#1494 AC3/AC4：提交 seam 的四个**真实 kill 窗口**与按 operation id / digest 对账。

每个用例都在子进程里跑到窗口边界，再由父进程 `process.kill()` 真杀（不是进程内异常
模拟；与 #1461 / protected-fact 两组 kill 用例同一形态）。窗口本身由
`_commit_kill_child.py` 打印一行 barrier 标出，父进程只在该行之后做现场断言。

覆盖的窗口（ADR-0058 D3/D8）：

1. **文件发布前**：回执 + before/after digest 已落盘、目标文件仍是 before ⇒ 对账补发布一次；
2. **替换后、回执置 published 前**：磁盘已是 after、回执仍 PREPARED ⇒ 同 operation id 重试
   回放，不重复追加；
3. **审批状态更新前**：内容已发布（`stgp_` 回执 COMMITTED）、staged 文件仍 pending ⇒
   重试审批按同一回执回放，再翻状态；
4. **两文件部分发布**：`remove_by_digest` 已清 `MEMORY.md`、未清 `USER.md` ⇒ 对账只补
   第二个目标，不重跑第一个。

冲突侧（AC3 末句）：窗口后有人手改了文件（磁盘既非 before 也非 after）⇒ 回执转
NEED_RECONCILE，**不覆盖**新编辑；同 operation id 的后续写入也只回声 "uncertain"。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent_harness.memory.v2.commit import MemoryCommit, build_markdown_commit
from agent_harness.memory.v2.file_state import (
    MemoryStateRoot,
    atomic_write_text,
    read_json,
)
from agent_harness.memory.v2.markdown_store import (
    ENTRY_DELIMITER,
    MarkdownMemoryStore,
    entries_digest,
)
from agent_harness.memory.v2.receipts import ReceiptState, ReceiptStore
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.memory.v2.write_approval import (
    APPROVED,
    PENDING,
    StagedWriteStore,
)

LOCAL = TrustedMemoryIdentity(tenant_id="local", user_id="local")
EXISTING = "已有偏好：使用 pnpm"
CONTENT = "新偏好：提交前先跑 ruff"

CHILD = Path(__file__).with_name("_commit_kill_child.py")
REPO_ROOT = Path(__file__).resolve().parents[3]
BARRIER = "AT_WINDOW"


# --------------------------------------------------------------------------------------
# 现场工具
# --------------------------------------------------------------------------------------


def _memory_path(root: Path) -> Path:
    return root / "memories" / "MEMORY.md"


def _user_path(root: Path) -> Path:
    return root / "memories" / "USER.md"


def _seed(root: Path, target: str, entries: list[str]) -> None:
    """窗口前的既有文件内容：直接落盘，不经 commit（不属于被测窗口）。"""
    path = _user_path(root) if target == "user" else _memory_path(root)
    atomic_write_text(path, ENTRY_DELIMITER.join(entries))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _commit(root: Path) -> MemoryCommit:
    engine = MarkdownMemoryStore(root / "memories")
    engine.load_from_disk()
    return build_markdown_commit(engine, root)


def _staged(root: Path) -> StagedWriteStore:
    commit = _commit(root)
    return StagedWriteStore(commit.root, commit)


def _receipt(root: Path, operation_id: str) -> dict:
    store = ReceiptStore(MemoryStateRoot(root / "memory-state"))
    payload = read_json(store.path_for(operation_id))
    assert payload is not None, f"expected a receipt for {operation_id!r}"
    return payload


async def _kill_at(window: str, root: Path, *args: str, before_kill=None) -> None:
    """跑子进程到 ``AT_WINDOW:<window>``，做现场断言，再真杀。"""
    child_env = dict(os.environ)
    child_env["PYTHONUTF8"] = "1"
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(CHILD), window, str(root), *args,
        cwd=str(REPO_ROOT), env=child_env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    barrier = f"{BARRIER}:{window}".encode()
    reached = False
    output: list[bytes] = []
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while loop.time() < deadline:
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=deadline - loop.time())
            except TimeoutError:
                break
            if not line:
                break
            output.append(line)
            if line.rstrip(b"\r\n") == barrier:
                reached = True
                break
        if not reached:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), timeout=10)
            stdout = await process.stdout.read()
            stderr = await process.stderr.read()
            raise AssertionError(
                f"child did not reach {barrier!r}: rc={process.returncode}, "
                f"output={output!r}, stdout={stdout!r}, stderr={stderr!r}")
        if before_kill is not None:
            before_kill()
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=10)
        await process.stdout.read()
        await process.stderr.read()
        assert process.returncode not in (None, 0), "必须是真 kill，不是正常退出"
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=10)
            await process.stdout.read()
            await process.stderr.read()


# --------------------------------------------------------------------------------------
# 窗口 1：文件发布前
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_before_publish_reconcile_republishes_exactly_once(tmp_path: Path) -> None:
    _seed(tmp_path, "memory", [EXISTING])
    operation = "mcf_kill_before_publish"
    observed: dict[str, object] = {}

    def _at_barrier() -> None:
        observed["receipt"] = _receipt(tmp_path, operation)
        observed["text"] = _read(_memory_path(tmp_path))

    await _kill_at(
        "before_publish", tmp_path, operation, CONTENT, before_kill=_at_barrier)

    # 窗口现场：意图与两份 digest 已在，文件仍是 before（AC3 第 1 个窗口）
    receipt = observed["receipt"]
    assert isinstance(receipt, dict)
    assert receipt["state"] == ReceiptState.PREPARED
    plan = receipt["targets"]["memory"]
    assert (plan["planned"], plan["published"]) == (True, False)
    assert plan["before_digest"] == entries_digest([EXISTING])
    assert plan["after_digest"] == entries_digest([EXISTING, CONTENT])
    assert observed["text"] == EXISTING
    assert CONTENT not in observed["text"]

    report = await _commit(tmp_path).reconcile()

    assert report.ok
    assert report.committed == [operation]
    assert _read(_memory_path(tmp_path)).count(CONTENT) == 1
    assert _receipt(tmp_path, operation)["state"] == ReceiptState.COMMITTED

    # 对账之后同 id 重试仍是回放：不再追加第二份（AC4「同 key 只一次逻辑更新」）
    again = await _commit(tmp_path).add("memory", CONTENT, operation_id=operation)
    assert again["success"] is True and again.get("replayed") is True
    assert _read(_memory_path(tmp_path)).count(CONTENT) == 1


@pytest.mark.asyncio
async def test_kill_before_publish_then_external_edit_needs_reconcile(tmp_path: Path) -> None:
    """AC3 末句：磁盘既非 before 也非 after ⇒ NEED_RECONCILE，绝不覆盖新编辑。"""
    _seed(tmp_path, "memory", [EXISTING])
    operation = "mcf_kill_then_edit"
    await _kill_at("before_publish", tmp_path, operation, CONTENT)

    edited = ENTRY_DELIMITER.join([EXISTING, "用户手改的新条目"])
    atomic_write_text(_memory_path(tmp_path), edited)

    report = await _commit(tmp_path).reconcile()

    assert report.ok is False
    assert report.committed == []
    [(failed, reason)] = report.needs_reconcile
    assert failed == operation
    assert "neither before nor after" in reason
    assert _read(_memory_path(tmp_path)) == edited, "对账不得覆盖窗口之后的新编辑"
    assert _receipt(tmp_path, operation)["state"] == ReceiptState.NEED_RECONCILE

    # 同 operation id 的后续写入只回报不确定，不盲重跑
    response = await _commit(tmp_path).add("memory", CONTENT, operation_id=operation)
    assert response["success"] is False
    assert response["needs_reconcile"] is True
    assert CONTENT not in _read(_memory_path(tmp_path))
    assert _read(_memory_path(tmp_path)) == edited


# --------------------------------------------------------------------------------------
# 窗口 2：替换后、回执置 published 前
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_after_replace_retry_replays_without_duplicate(tmp_path: Path) -> None:
    _seed(tmp_path, "memory", [EXISTING])
    operation = "mcf_kill_after_replace"
    observed: dict[str, object] = {}

    def _at_barrier() -> None:
        observed["text"] = _read(_memory_path(tmp_path))
        observed["receipt"] = _receipt(tmp_path, operation)

    await _kill_at(
        "after_replace", tmp_path, operation, CONTENT, before_kill=_at_barrier)

    # 窗口现场：磁盘已是 after，回执仍是 PREPARED + published=False
    assert observed["text"].count(CONTENT) == 1
    receipt = observed["receipt"]
    assert isinstance(receipt, dict)
    assert receipt["state"] == ReceiptState.PREPARED
    assert receipt["targets"]["memory"]["published"] is False

    response = await _commit(tmp_path).add("memory", CONTENT, operation_id=operation)

    assert response["success"] is True
    assert response.get("replayed") is True
    assert _read(_memory_path(tmp_path)).count(CONTENT) == 1
    assert _receipt(tmp_path, operation)["state"] == ReceiptState.COMMITTED
    # 收口后没有遗留待对账回执
    report = await _commit(tmp_path).reconcile()
    assert report.examined == 0


# --------------------------------------------------------------------------------------
# 窗口 3：审批状态更新前
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_before_approval_status_retry_replays_once(tmp_path: Path) -> None:
    _seed(tmp_path, "memory", [EXISTING])
    staged = _staged(tmp_path)
    record = await staged.stage(
        {"target": "memory", "action": "add", "content": CONTENT}, None, LOCAL)

    await _kill_at("before_approval_status", tmp_path, record.id)

    # 窗口现场：内容已经由审批重放发布（stgp_ 回执 COMMITTED），状态仍是 pending
    assert _read(_memory_path(tmp_path)).count(CONTENT) == 1
    after_kill = await _staged(tmp_path).get(record.id, LOCAL)
    assert after_kill is not None and after_kill.status == PENDING
    assert _receipt(tmp_path, f"stgp_{record.id}")["state"] == ReceiptState.COMMITTED

    decision, response = await _staged(tmp_path).approve(record.id, LOCAL)

    assert decision.allow is True
    assert response is not None and response.get("success") is True
    assert _read(_memory_path(tmp_path)).count(CONTENT) == 1, "重试审批不得写第二份"
    approved = await _staged(tmp_path).get(record.id, LOCAL)
    assert approved is not None and approved.status == APPROVED
    assert _receipt(tmp_path, f"stgp_{record.id}")["state"] == ReceiptState.COMMITTED


# --------------------------------------------------------------------------------------
# 窗口 4：两文件部分发布
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_between_two_file_publishes_reconcile_finishes_only_the_second(
    tmp_path: Path,
) -> None:
    memory_entry = "要清理的旧记忆条目"
    user_entry = "要清理的旧偏好条目"
    _seed(tmp_path, "memory", [EXISTING, memory_entry])
    _seed(tmp_path, "user", ["用户画像条目", user_entry])
    operation = "del_kill_partial"
    observed: dict[str, object] = {}

    def _at_barrier() -> None:
        observed["memory"] = _read(_memory_path(tmp_path))
        observed["user"] = _read(_user_path(tmp_path))
        observed["receipt"] = _receipt(tmp_path, operation)

    await _kill_at(
        "partial_publish", tmp_path, operation, memory_entry, user_entry,
        before_kill=_at_barrier)

    # 窗口现场：第一个目标已发布，第二个目标还没计划
    assert memory_entry not in observed["memory"]
    assert user_entry in observed["user"]
    receipt = observed["receipt"]
    assert isinstance(receipt, dict)
    assert receipt["state"] == ReceiptState.PREPARED
    assert receipt["targets"]["memory"]["published"] is True
    assert receipt["targets"]["user"]["planned"] is False

    report = await _commit(tmp_path).reconcile()

    assert report.ok
    assert report.committed == [operation]
    assert memory_entry not in _read(_memory_path(tmp_path))
    assert user_entry not in _read(_user_path(tmp_path))
    assert EXISTING in _read(_memory_path(tmp_path))
    assert "用户画像条目" in _read(_user_path(tmp_path))
    finished = _receipt(tmp_path, operation)
    assert finished["state"] == ReceiptState.COMMITTED
    assert finished["targets"]["user"]["published"] is True
