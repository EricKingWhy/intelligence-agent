"""#275 站点 2：`LocalArtifactStore` 的同步体重活下放线程。

站点 2 的红线是「本地 artifact 的同步文件 IO 跑在 async 函数体里」。实测（2M 字符 /
6MB 落盘，`docs/PERF_BASELINE.md` B7 节）：`save` 整个函数体 37ms 中位、`load` 38ms 中位，
都远超票面 5ms 阈值。这里锁两类东西：

**红线（改造前会红的，是真正的行为断言，不是 ImportError）**
- `test_save_blocking_write_runs_off_loop_thread`：直接观测 `_write_atomic` 被调用时
  所在的线程。改造前它在事件循环线程上（= 测试协程所在线程）；改造后在 anyio 工作线程上。
- `test_load_blocking_read_runs_off_loop_thread`：同理观测 `Path.read_bytes`。
  这两条只用**改造前就存在**的内部名字，所以改造前跑出来的是断言失败而不是导入错误。

**不变式守卫（改造前后都必须绿）**
- AC5：`FileNotFoundError` → `KeyError`；**`PermissionError` 不得被吞成 `KeyError`**
  （票面 Scope lock 逐字要求）。后者同时验证票面 Risks 里那条「搬线程后异常层级变化」——
  anyio 会把原异常重新抛出，所以断言的是**原始类型**，顺带证明它没被包成别的。
- 「内容先、元数据后」的顺序：元数据那次落盘失败时，内容必须仍可读、元数据如实为 `None`
  （#185 AC4 的"缺失即 None"，也是 `local_artifact.py:94-96` 原子纪律的崩溃窗口）。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from agent_harness.config import Settings
from agent_harness.storage.artifact import compute_artifact_id
from agent_harness.storage.local_artifact import LocalArtifactStore

SESSION = "sess-offload-1"
CONTENT = "line-1\nline-2\n" * 256


def _store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(
        Settings(artifact_dir=str(tmp_path / "artifacts")), session_id=SESSION,
    )


async def _save(store: LocalArtifactStore):
    return await store.save(
        SESSION, CONTENT, mime_type="text/plain", source_tool="bash",
        tool_call_id="tc-1",
    )


class TestOffload:
    @pytest.mark.asyncio
    async def test_save_blocking_write_runs_off_loop_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _store(tmp_path)
        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []
        original = LocalArtifactStore._write_atomic

        def spy(path: Path, body: bytes) -> None:
            seen.append(threading.current_thread())
            original(path, body)

        # 打在**实例**上：函数是非数据描述符，实例属性不会被绑定，签名保持 (path, body)
        monkeypatch.setattr(store, "_write_atomic", spy)

        await _save(store)

        assert len(seen) == 2, "save 必须落两次盘：内容 + 元数据（顺序另有守卫）"
        assert all(t is not loop_thread for t in seen), (
            "落盘仍发生在事件循环线程 "
            f"{loop_thread.name} 上——站点 2 的 save 没搬"
        )

    @pytest.mark.asyncio
    async def test_load_blocking_read_runs_off_loop_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _store(tmp_path)
        saved = await _save(store)
        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []
        original = Path.read_bytes

        def spy(self: Path) -> bytes:
            seen.append(threading.current_thread())
            return original(self)

        monkeypatch.setattr(Path, "read_bytes", spy)

        loaded = await store.load(saved.artifact_id)

        assert loaded.content == CONTENT
        assert seen, "load 必须真的读盘"
        assert all(t is not loop_thread for t in seen), (
            "读取仍发生在事件循环线程上——站点 2 的 load 没搬"
        )


class TestExceptionContract:
    @pytest.mark.asyncio
    async def test_not_found_maps_to_keyerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """搬线程后映射不许变：not-found → `KeyError`（三实现同契约）。"""
        store = _store(tmp_path)
        saved = await _save(store)

        def missing(self: Path) -> bytes:
            raise FileNotFoundError(saved.artifact_id)

        monkeypatch.setattr(Path, "read_bytes", missing)
        with pytest.raises(KeyError):
            await store.load(saved.artifact_id)

    @pytest.mark.asyncio
    async def test_permission_error_is_not_swallowed_as_key_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC5 + Scope lock：`PermissionError` 必须照常外泄成 500。

        把它吞成 `KeyError` 会把"服务端异常"谎报成"不存在"（404）——注释里逐字写死的
        纪律。这条同时证明 anyio 没有把线程里的异常包成别的类型。
        """
        store = _store(tmp_path)
        saved = await _save(store)

        def denied(self: Path) -> bytes:
            raise PermissionError("simulated EACCES")

        monkeypatch.setattr(Path, "read_bytes", denied)
        with pytest.raises(PermissionError):
            await store.load(saved.artifact_id)

    @pytest.mark.asyncio
    async def test_content_lands_before_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """内容先、元数据后：旁挂文件写失败 ⇒ 内容仍在、元数据如实为 None。"""
        store = _store(tmp_path)
        original = LocalArtifactStore._write_atomic

        def flaky(path: Path, body: bytes) -> None:
            if path.name.endswith(".json"):
                raise OSError("simulated crash on sidecar write")
            original(path, body)

        monkeypatch.setattr(store, "_write_atomic", flaky)

        with pytest.raises(OSError):
            await _save(store)

        # 换一个未打补丁的实例读回（同目录、同 session 命名空间）
        loaded = await _store(tmp_path).load(compute_artifact_id(CONTENT))
        assert loaded.content == CONTENT, "内容写失败就会毁掉 #185 AC4 的崩溃窗口"
        assert loaded.source_tool is None
        assert loaded.tool_call_id is None
        # 不伪造成空串（#185 AC4）
        assert loaded.mime_type == "application/octet-stream"
