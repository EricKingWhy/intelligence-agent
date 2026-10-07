"""LocalArtifactStore 字节路径（#822 MM-01）：staging → fsync → 原子发布 / 去重 / 隔离。

落盘算法来源: DeepSeek Harness `5badb150` `attachment-local/src/store.ts`（MIT）。
"""

from __future__ import annotations

import asyncio
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_harness.config import Settings
from agent_harness.storage.artifact import compute_byte_artifact_id
from agent_harness.storage.local_artifact import LocalArtifactStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        artifact_dir=str(tmp_path / "artifacts"),
        model_api_key="sk-test",
    )


def _store(tmp_path: Path, session_id: str) -> LocalArtifactStore:
    return LocalArtifactStore(_settings(tmp_path), session_id=session_id)


def _object_path(tmp_path: Path, session_id: str, attachment_id: str) -> Path:
    sha = attachment_id.split(":", 1)[1]
    return tmp_path / "artifacts" / session_id / "attachments" / "objects" / sha[:2] / sha


def test_bytes_roundtrip_is_byte_equal(tmp_path: Path) -> None:
    store = _store(tmp_path, "sess-a")
    payload = bytes(range(256)) * 10
    blob = asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))

    loaded = asyncio.run(store.load_bytes(blob.artifact_id))

    assert loaded.content == payload
    assert loaded.mime_type == "image/png"
    assert loaded.size == len(payload)
    assert blob.artifact_id == compute_byte_artifact_id(payload)
    assert blob.artifact_id.startswith("sha256:")
    assert len(blob.artifact_id) == len("sha256:") + 64


def test_dedup_same_bytes_same_id_and_one_object(tmp_path: Path) -> None:
    store = _store(tmp_path, "sess-a")
    payload = b"same-bytes" * 100
    first = asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))
    second = asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))

    assert first.artifact_id == second.artifact_id
    objects = list(
        (tmp_path / "artifacts" / "sess-a" / "attachments" / "objects").rglob("*")
    )
    files = [p for p in objects if p.is_file() and p.suffix != ".json"]
    assert len(files) == 1, "同一字节序列只应落一份对象"


def test_cross_session_id_is_not_readable(tmp_path: Path) -> None:
    """别的会话拿到 id 也取不到内容（授权来自会话命名空间）。"""
    writer = _store(tmp_path, "sess-a")
    blob = asyncio.run(writer.save_bytes("sess-a", b"secret", mime_type="image/png"))

    reader_b = _store(tmp_path, "sess-b")
    with pytest.raises(KeyError):
        asyncio.run(reader_b.load_bytes(blob.artifact_id))
    # 自己的会话读得到
    assert asyncio.run(_store(tmp_path, "sess-a").load_bytes(blob.artifact_id)).content == b"secret"


def test_missing_and_malformed_ids_raise_key_error(tmp_path: Path) -> None:
    store = _store(tmp_path, "sess-a")
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes("sha256:" + "0" * 64))
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes("not-a-valid-id"))
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes("sha256:XYZ"))


def test_tampered_object_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path, "sess-a")
    blob = asyncio.run(store.save_bytes("sess-a", b"original", mime_type="image/png"))
    path = _object_path(tmp_path, "sess-a", blob.artifact_id)
    # chmod 0o400 后仍可被 root 覆盖（本机 root）；这里直接改字节模拟带外篡改。
    os.chmod(path, 0o600)
    path.write_bytes(b"tampered")
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes(blob.artifact_id))


def test_object_permission_is_read_only(tmp_path: Path) -> None:
    store = _store(tmp_path, "sess-a")
    blob = asyncio.run(store.save_bytes("sess-a", b"perm", mime_type="image/png"))
    path = _object_path(tmp_path, "sess-a", blob.artifact_id)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o400


def test_halfway_failure_leaves_no_readable_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布阶段失败（模拟写失败/进程中断）⇒ 不产生可被读到的半文件、不留 staging。"""
    store = _store(tmp_path, "sess-a")
    payload = b"partial-should-not-survive"

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated publish failure")

    monkeypatch.setattr("agent_harness.storage.local_artifact.os.link", _boom)

    with pytest.raises(OSError):
        asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))

    attachment_id = compute_byte_artifact_id(payload)
    object_path = _object_path(tmp_path, "sess-a", attachment_id)
    assert not object_path.exists(), "失败的发布不得留下对象"
    # 读回也必然 KeyError（没有半文件可读）
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes(attachment_id))
    staging = tmp_path / "artifacts" / "sess-a" / "attachments" / "tmp"
    assert not staging.exists() or list(staging.iterdir()) == [], "staging 必须清空"


#: 子进程脚本：进入 `save_bytes` 写 staging 的中途后长睡，等父进程 SIGKILL。
#: 不是产品代码——只为把"进程在写 staging 中途被杀"做成可复现的真形状。
_KILL_CHILD_SCRIPT = '''\
"""进入 LocalArtifactStore.save_bytes 写 staging 中途后长睡，等父进程 SIGKILL。"""
import asyncio
import os
import time

_SENTINEL = os.environ["MM01_SENTINEL"]
_ARTIFACT_DIR = os.environ["MM01_ARTIFACT_DIR"]
_PAYLOAD = bytes(range(256)) * int(os.environ["MM01_REPEAT"])

_real_write = os.write
_state = {"first": True}


def _slow_write(fd, data):
    if _state["first"]:
        _state["first"] = False
        written = _real_write(fd, bytes(data[:4096]))
        with open(_SENTINEL, "w", encoding="utf-8") as handle:
            handle.write(str(written))
        time.sleep(120)  # 长睡：留给父进程 SIGKILL 的窗口
        return written
    return _real_write(fd, data)


os.write = _slow_write


def main():
    from agent_harness.config import Settings
    from agent_harness.storage.local_artifact import LocalArtifactStore

    settings = Settings(
        _env_file=None,
        workspace_dir=os.getcwd(),
        artifact_dir=_ARTIFACT_DIR,
        model_api_key="sk-test",
    )
    store = LocalArtifactStore(settings, session_id="sess-a")
    asyncio.run(store.save_bytes("sess-a", _PAYLOAD, mime_type="image/png"))


main()
'''


@pytest.mark.skipif(os.name == "nt", reason="POSIX 专用：真实 SIGKILL 一个子进程")
def test_kill_during_staging_leaves_no_readable_object(tmp_path: Path) -> None:
    """真实子进程在写 staging **中途**被 SIGKILL ⇒ 目标对象不存在、无半文件可读。

    结构不变量是"staging 完整 fsync 后才 hardlink 到目标"，此前只有一条
    `monkeypatch(os.link)` 制造的**发布失败**用例（`test_halfway_failure_...`），没有一次
    真实进程中断。这里补齐 AC 明写的"进程中断"：子进程把 `os.write` 改成"先写 4 KiB、
    落一个 sentinel、然后长睡"，父进程一读到 sentinel（证明确实处在写 staging 的中途）
    就 SIGKILL。

    为什么确定不 flaky：子进程在 sentinel 之后**睡到被杀**，父进程只在看到 sentinel 后
    才动手——两边没有"谁先到"的竞态。
    """
    repo_root = Path(__file__).resolve().parents[2]
    artifact_dir = tmp_path / "artifacts"
    sentinel = tmp_path / "writing.sentinel"
    repeat = 8192
    payload = bytes(range(256)) * repeat  # ~2 MiB：远超首个 4 KiB 分片
    script = tmp_path / "child_publish.py"
    script.write_text(_KILL_CHILD_SCRIPT, encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")  # 子进程必须导入**本工作树**的 src
    env["MM01_SENTINEL"] = str(sentinel)
    env["MM01_ARTIFACT_DIR"] = str(artifact_dir)
    env["MM01_REPEAT"] = str(repeat)

    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while not sentinel.exists():
            if proc.poll() is not None:
                _, err = proc.communicate()
                pytest.fail(
                    "子进程在进入 staging 中途前就退出了"
                    f"（rc={proc.returncode}）：{err.decode(errors='replace')}"
                )
            if time.monotonic() > deadline:
                pytest.fail("等待子进程进入 staging 中途超时（30s）")
            time.sleep(0.01)
        proc.kill()  # POSIX 上等于 SIGKILL
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert proc.returncode == -signal.SIGKILL, "进程必须是被真实 SIGKILL 杀死的"

    attachment_id = compute_byte_artifact_id(payload)
    object_path = _object_path(tmp_path, "sess-a", attachment_id)
    assert not object_path.exists(), "进程在发布前被杀，不得出现目标对象"
    with pytest.raises(KeyError):
        asyncio.run(_store(tmp_path, "sess-a").load_bytes(attachment_id))

    # staging 里确实留下了半个文件——它在 tmp/，不是可读对象路径（即 AC 的"可被读到"）。
    staging = artifact_dir / "sess-a" / "attachments" / "tmp"
    partials = [p for p in staging.rglob("*") if p.is_file()]
    assert partials, "应能观察到被中断时留下的 staging 半文件（证明真的杀在写入中途）"
    assert all(p.stat().st_size < len(payload) for p in partials), "半文件必须是**未写完**的"


def test_metadata_sidecar_missing_falls_back_to_octet_stream(tmp_path: Path) -> None:
    """内容才是事实：删掉元数据仍能读回字节，Content-Type 退化为 octet-stream。"""
    store = _store(tmp_path, "sess-a")
    blob = asyncio.run(store.save_bytes("sess-a", b"meta", mime_type="image/png"))
    sha = blob.artifact_id.split(":", 1)[1]
    meta = (
        tmp_path / "artifacts" / "sess-a" / "attachments" / "objects" / sha[:2]
        / f"{sha}.json"
    )
    meta.unlink()

    loaded = asyncio.run(store.load_bytes(blob.artifact_id))
    assert loaded.content == b"meta"
    assert loaded.mime_type == "application/octet-stream"
