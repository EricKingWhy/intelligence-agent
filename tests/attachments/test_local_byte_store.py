"""LocalArtifactStore 字节路径（#822 MM-01；#830 D1 对象根全局化）。

落盘算法来源: DeepSeek Harness `5badb150` `attachment-local/src/store.ts`（MIT）。
对象根全局化 + 跨会话去重的来源: DSH `store.ts:47-54`、oh-my-pi
`packages/coding-agent/src/session/blob-store.ts:40-68`（均 MIT，见
`docs/agents/830-d1-global-attachment-store-design-proposal.md` §0）。

布局（`<root>` = `artifact_dir`）：

    <root>/.attachments/objects/<sha[:2]>/<sha>       全局字节对象（跨会话去重）
    <root>/.attachments/objects/<sha[:2]>/<sha>.json  对象元数据（旁挂，可缺）
    <root>/.attachments/tmp/<uuid>                    staging
    <root>/<sid>/attachments/objects/<sha[:2]>/<sha>  会话上传回执（hardlink）

两条读语义：`load_bytes` = 内容寻址读回（全局优先 → 回落本会话旧路径，向后兼容）；
`load_uploaded_bytes` = **本会话上传过**（只看回执/旧路径），是发送侧归属判据。
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
from agent_harness.storage.local_artifact import (
    LocalArtifactStore,
    discard_local_artifacts,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        artifact_dir=str(tmp_path / "artifacts"),
        model_api_key="sk-test",
    )


def _store(tmp_path: Path, session_id: str) -> LocalArtifactStore:
    return LocalArtifactStore(_settings(tmp_path), session_id=session_id)


def _sharded(root: Path, attachment_id: str) -> Path:
    sha = attachment_id.split(":", 1)[1]
    return root / sha[:2] / sha


def _object_path(tmp_path: Path, attachment_id: str) -> Path:
    """全局对象路径（与会话无关）。"""
    return _sharded(tmp_path / "artifacts" / ".attachments" / "objects", attachment_id)


def _receipt_path(tmp_path: Path, session_id: str, attachment_id: str) -> Path:
    """会话上传回执路径（旧布局位，hardlink 指向全局对象）。"""
    return _sharded(
        tmp_path / "artifacts" / session_id / "attachments" / "objects", attachment_id
    )


def _meta_path(tmp_path: Path, attachment_id: str) -> Path:
    """全局对象旁挂元数据路径。"""
    obj = _object_path(tmp_path, attachment_id)
    return obj.with_name(f"{obj.name}.json")


def _staging_dir(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / ".attachments" / "tmp"


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
    # 对象落在全局根；会话侧只有一条同 inode 的回执（不复制字节）。
    assert _object_path(tmp_path, blob.artifact_id).is_file()
    receipt = _receipt_path(tmp_path, "sess-a", blob.artifact_id)
    assert receipt.is_file()
    assert receipt.stat().st_ino == _object_path(tmp_path, blob.artifact_id).stat().st_ino


def test_dedup_same_bytes_same_id_and_one_object(tmp_path: Path) -> None:
    store = _store(tmp_path, "sess-a")
    payload = b"same-bytes" * 100
    first = asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))
    second = asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))

    assert first.artifact_id == second.artifact_id
    objects = [
        p for p in (tmp_path / "artifacts" / ".attachments" / "objects").rglob("*")
        if p.is_file() and p.suffix != ".json"
    ]
    assert len(objects) == 1, "同一字节序列只应落一份对象"


def test_same_bytes_across_sessions_share_one_object(tmp_path: Path) -> None:
    """跨会话去重（#830 D1 的核心收益）：两个会话上传同字节 → 全局仍只 1 份。"""
    payload = b"shared-across-sessions" * 50

    a = asyncio.run(_store(tmp_path, "sess-a").save_bytes("sess-a", payload, mime_type="image/png"))
    b = asyncio.run(_store(tmp_path, "sess-b").save_bytes("sess-b", payload, mime_type="image/png"))

    assert a.artifact_id == b.artifact_id
    objects = [
        p for p in (tmp_path / "artifacts" / ".attachments" / "objects").rglob("*")
        if p.is_file() and p.suffix != ".json"
    ]
    assert len(objects) == 1
    # 两个会话各有自己的回执（各指同一 inode）。
    receipts = [
        _receipt_path(tmp_path, sid, a.artifact_id) for sid in ("sess-a", "sess-b")
    ]
    assert all(r.is_file() for r in receipts)
    inodes = {r.stat().st_ino for r in receipts}
    assert len(inodes) == 1, "回执必须是同一 inode 的 hardlink（零拷贝）"


def test_cross_session_bytes_readable_but_not_owned(tmp_path: Path) -> None:
    """`load_bytes` 跨会话可读（内容寻址）；`load_uploaded_bytes` 仍按会话拒（归属）。"""
    writer = _store(tmp_path, "sess-a")
    blob = asyncio.run(writer.save_bytes("sess-a", b"secret", mime_type="image/png"))

    reader_b = _store(tmp_path, "sess-b")
    # 读回语义：全局内容寻址 ⇒ 别的会话也读得到（"谁能读"由调用方的引用闸门负责）。
    assert asyncio.run(reader_b.load_bytes(blob.artifact_id)).content == b"secret"
    # 归属语义：没上传过的会话拿不到（发送侧 422 的判据）。
    with pytest.raises(KeyError):
        asyncio.run(reader_b.load_uploaded_bytes(blob.artifact_id))
    # 上传者自己两条都通。
    own = _store(tmp_path, "sess-a")
    assert asyncio.run(own.load_bytes(blob.artifact_id)).content == b"secret"
    assert asyncio.run(own.load_uploaded_bytes(blob.artifact_id)).content == b"secret"


def test_legacy_session_scoped_object_is_still_readable(tmp_path: Path) -> None:
    """升级前落在会话命名空间里的对象不因对象根全局化而 brick（向后兼容读法）。"""
    store = _store(tmp_path, "sess-a")
    payload = b"pre-upgrade-bytes"
    attachment_id = compute_byte_artifact_id(payload)
    legacy = _receipt_path(tmp_path, "sess-a", attachment_id)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(payload)
    meta = legacy.with_name(f"{legacy.name}.json")
    meta.write_text('{"mime_type": "image/webp"}', encoding="utf-8")

    assert _object_path(tmp_path, attachment_id).exists() is False  # 全局确实没有
    loaded = asyncio.run(store.load_bytes(attachment_id))
    assert loaded.content == payload
    assert loaded.mime_type == "image/webp"  # 旧旁挂也被回落读到
    # 归属：旧对象的会话路径本身就是回执位。
    assert asyncio.run(store.load_uploaded_bytes(attachment_id)).content == payload
    # 旧对象**只**在本会话命名空间里（全局根没有它）⇒ 别的会话两条都读不到。
    # 这是登记的已知缺口：升级前上传的旧对象 fork 出的子会话仍读不回（见方案文档 §8.3）。
    other = _store(tmp_path, "sess-b")
    with pytest.raises(KeyError):
        asyncio.run(other.load_bytes(attachment_id))
    with pytest.raises(KeyError):
        asyncio.run(other.load_uploaded_bytes(attachment_id))


def test_tampered_global_object_falls_back_to_intact_session_copy(tmp_path: Path) -> None:
    """全局对象"存在但自证失败"不得终止回落：本会话同 sha 的完好副本仍应读得到。

    回落的未命中判据必须包含"存在但 hash 不自证"（带外篡改 / 写坏），而不只是
    `FileNotFoundError`；否则一份被篡改的全局对象会遮蔽升级前落在会话路径里的合法副本，
    把本可读的回退成 `KeyError`。
    """
    store = _store(tmp_path, "sess-a")
    payload = b"intact-session-copy"
    attachment_id = compute_byte_artifact_id(payload)

    # 升级前落在会话路径的完好旧对象（旧布局位同时就是上传回执位）。
    legacy = _receipt_path(tmp_path, "sess-a", attachment_id)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(payload)

    # 全局对象**存在**，但字节被带外篡改（sha 不再自证）。
    tampered = _object_path(tmp_path, attachment_id)
    tampered.parent.mkdir(parents=True, exist_ok=True)
    tampered.write_bytes(b"tampered-global-object")

    loaded = asyncio.run(store.load_bytes(attachment_id))
    assert loaded.content == payload, "全局候选自证失败后必须继续回落，不得遮蔽完好的会话副本"
    assert loaded.size == len(payload)
    # 归属读只看会话位（完好），不被全局候选的损坏影响。
    assert asyncio.run(store.load_uploaded_bytes(attachment_id)).content == payload


def test_missing_and_malformed_ids_raise_key_error(tmp_path: Path) -> None:
    store = _store(tmp_path, "sess-a")
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes("sha256:" + "0" * 64))
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes("not-a-valid-id"))
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes("sha256:XYZ"))
    with pytest.raises(KeyError):
        asyncio.run(store.load_uploaded_bytes("sha256:" + "0" * 64))
    with pytest.raises(KeyError):
        asyncio.run(store.load_uploaded_bytes("sha256:XYZ"))


def test_tampered_object_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path, "sess-a")
    blob = asyncio.run(store.save_bytes("sess-a", b"original", mime_type="image/png"))
    path = _object_path(tmp_path, blob.artifact_id)
    # chmod 0o400 后仍可被 root 覆盖（本机 root）；这里直接改字节模拟带外篡改。
    os.chmod(path, 0o600)
    path.write_bytes(b"tampered")
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes(blob.artifact_id))
    # 回执指同一 inode ⇒ 篡改同样被归属读拒（不自证就一律不可读）。
    with pytest.raises(KeyError):
        asyncio.run(store.load_uploaded_bytes(blob.artifact_id))


def test_object_permission_is_read_only(tmp_path: Path) -> None:
    store = _store(tmp_path, "sess-a")
    blob = asyncio.run(store.save_bytes("sess-a", b"perm", mime_type="image/png"))
    path = _object_path(tmp_path, blob.artifact_id)
    # Windows 没有 POSIX 权限位：CPython 按只读属性合成 st_mode（只读 ⇒ 0o444、
    # 可写 ⇒ 0o666），`os.chmod(path, 0o400)` 在 Windows 上不可满足 ⇒ 用原生
    # FILE_ATTRIBUTE_READONLY 更精确。
    file_stat = os.stat(path)
    if os.name == "nt":
        assert file_stat.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    else:
        assert stat.S_IMODE(file_stat.st_mode) == 0o400


def test_session_delete_drops_receipt_but_keeps_global_object(tmp_path: Path) -> None:
    """会话硬删只带走回执（该会话目录），全局对象留在原地（孤儿回收不在本票范围）。"""
    store = _store(tmp_path, "sess-a")
    blob = asyncio.run(store.save_bytes("sess-a", b"survives", mime_type="image/png"))

    discard_local_artifacts(_settings(tmp_path), "sess-a")

    assert not _receipt_path(tmp_path, "sess-a", blob.artifact_id).exists()
    assert _object_path(tmp_path, blob.artifact_id).is_file()
    assert (
        asyncio.run(_store(tmp_path, "sess-b").load_bytes(blob.artifact_id)).content
        == b"survives"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX 专用：会话名不得吃掉全局对象根")
def test_session_named_like_the_global_root_cannot_eat_it(tmp_path: Path) -> None:
    """名为 `attachments` / `.attachments` 的会话删不掉全局对象根（前导点点目录纪律）。

    `discard_local_artifacts` 是"setting + session_id 拼路径"的删除入口；全局根用
    **前导点**目录名（`SESSION_KEY_PATTERN` 不允许前导点）⇒ 构造上拼不出它。
    """
    blob = asyncio.run(
        _store(tmp_path, "sess-a").save_bytes("sess-a", b"global", mime_type="image/png")
    )
    for name in ("attachments", "objects", "tmp"):
        discard_local_artifacts(_settings(tmp_path), name)

    assert _object_path(tmp_path, blob.artifact_id).is_file()


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
    object_path = _object_path(tmp_path, attachment_id)
    assert not object_path.exists(), "失败的发布不得留下对象"
    # 读回也必然 KeyError（没有半文件可读）
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes(attachment_id))
    # 归属读也不能凭空成立（对象都没发布 ⇒ 回执不存在）
    with pytest.raises(KeyError):
        asyncio.run(store.load_uploaded_bytes(attachment_id))
    staging = _staging_dir(tmp_path)
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

    前提假设：monkeypatch 后的 `_slow_write` 把**首次** `os.write` 当作写入 staging 的判据，这依赖
    "进入 `save_bytes` 写 staging 前无其它 Python 级 `os.write` 调用"。当前成立；若未来
    在 `save_bytes` 之前（或更早的导入/初始化阶段）新增其它 Python 级 `os.write`，
    sentinel 会在非 staging 写时被触达，使本用例**假红**（而非假绿）——fail-loud，不会
    把回归放过。
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
    assert not _object_path(tmp_path, attachment_id).exists(), (
        "进程在发布前被杀，不得出现目标对象"
    )
    with pytest.raises(KeyError):
        asyncio.run(_store(tmp_path, "sess-a").load_bytes(attachment_id))
    with pytest.raises(KeyError):
        asyncio.run(_store(tmp_path, "sess-a").load_uploaded_bytes(attachment_id))

    # staging 里确实留下了半个文件——它在 tmp/，不是可读对象路径（即 AC 的"可被读到"）。
    staging = _staging_dir(tmp_path)
    partials = [p for p in staging.rglob("*") if p.is_file()]
    assert partials, "应能观察到被中断时留下的 staging 半文件（证明真的杀在写入中途）"
    assert all(p.stat().st_size < len(payload) for p in partials), "半文件必须是**未写完**的"


def test_metadata_sidecar_missing_falls_back_to_octet_stream(tmp_path: Path) -> None:
    """内容才是事实：删掉元数据仍能读回字节，Content-Type 退化为 octet-stream。"""
    store = _store(tmp_path, "sess-a")
    blob = asyncio.run(store.save_bytes("sess-a", b"meta", mime_type="image/png"))
    _meta_path(tmp_path, blob.artifact_id).unlink()

    loaded = asyncio.run(store.load_bytes(blob.artifact_id))
    assert loaded.content == b"meta"
    assert loaded.mime_type == "application/octet-stream"


# ── #830 D2：发布必须走二进制模式（Windows CRT 文本模式会撑开 `0x0A`）──────
#
# `_publish_blob` 用 `os.open` 落 staging 文件；Windows 的 CRT 在**没有** `O_BINARY`
# 时默认文本模式，写盘把 `0x0A` 撑成 `0x0D 0x0A` ⇒ 对象字节数变化 ⇒ 读回时
# `load_bytes` 的 content-addressable 自证报 `content hash mismatch`（附件静默降级成
# 占位符）。Linux 无文本模式，所以缺陷只在 Windows 复现（GA #37941646937 job `cli`
# 步骤 5：`tests/test_cli_image.py` 2 failed）。

#: 模拟层的 `O_BINARY` 位。Linux 上 `os.O_BINARY` 不存在（`getattr` 取 0），只有把它
#: 注入 `os` 模块，`_publish_blob` 的 `getattr(os, "O_BINARY", 0)` 才能在 Linux 上被观测。
#: 注意：`0x8000` 与内核 `O_LARGEFILE`（`asm-generic/fcntl.h:51`）同位，本仓产品码不设
#: 该位且真 `os.open` 前**必须**剥掉它（`flags & ~_EMULATED_O_BINARY`），否则会污染
#: 真实 flag 位。下方的 `emulating_open` 已如此处理。
_EMULATED_O_BINARY = 0x8000


def test_published_object_bytes_are_not_newline_expanded(tmp_path: Path) -> None:
    """含 `0x0A` 的字节对象落盘后必须**逐字节**等于原文（#830 D2）。

    断言刻意落在**落盘字节**上而不是只断言 roundtrip：Windows 上文本模式会在
    `save_bytes` 与 `load_bytes` 之间同时发生，只断言 roundtrip 不足以把"文件被改写"
    与"读回转换"分开；直接读对象文件才是缺陷的现场（也是证据包建议的补强读数）。
    """
    store = _store(tmp_path, "sess-a")
    payload = b"\x89PNG\r\n\x1a\n" + bytes([0x0A]) * 8 + b"\r\n\x0a"
    blob = asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))

    object_path = _object_path(tmp_path, blob.artifact_id)
    assert object_path.read_bytes() == payload, "落盘字节被平台换行转换改写"
    loaded = asyncio.run(store.load_bytes(blob.artifact_id))
    assert loaded.content == payload


@pytest.mark.skipif(
    os.name == "nt",
    reason="本用例模拟 Windows CRT；真 Windows 上由 test_published_object_bytes_are_not_newline_expanded 覆盖",
)
def test_publish_blob_forces_binary_mode_under_emulated_text_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把 Windows CRT 的文本模式搬成 **Linux 上可红**的回归（#830 D2）。

    上一条用例在 Linux 上恒绿（Linux 没有文本模式），而 Linux 正是 CI 真正跑测试的
    平台：只有这条用例能在 Linux 上守住"发布必须带 `O_BINARY`"这条纪律。做法是把
    平台行为注入进来——`os.O_BINARY` 补一位模拟位、`os.open` 把它剥掉后再交给真
    `os.open`（Linux 不认这个 flag），并让**不带**该位的 fd 走 CRT 文本模式语义
    （写盘时 `0x0A` -> `0x0D 0x0A`）。缺陷机理与真 Windows 逐条同形。

    模拟层按 **fd 号**记状态 ⇒ 每次 open 都要**双向**刷新它（带位就从集合里删）：fd 号会被
    复用，只往里加、不删的话，同一个号先被别人不带位地打开、再被本次发布打开时，陈旧记录
    还在 ⇒ 对象那次写被误判成文本模式 ⇒ 用例假红。以"本次 open 的真实位"为准。

    **识别面(不许改错层)**:本用例识别的是 **open flag 路线**——它挂钩 `os.open` 的
    `flags` 位。若产品改用 `msvcrt.setmode(fd, os.O_BINARY)` 这类**运行时切 fd** 的等价
    修法,模拟层不会看到那个位 ⇒ 本例假红。换产品修法请**同步改模拟层**(在 `emulating_open`
    里按路线登记),别把用例删成恒绿。
    """
    monkeypatch.setattr(os, "O_BINARY", _EMULATED_O_BINARY, raising=False)
    real_open, real_write = os.open, os.write
    text_mode_fds: set[int] = set()

    def emulating_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags & ~_EMULATED_O_BINARY, *args, **kwargs)
        if flags & _EMULATED_O_BINARY:
            text_mode_fds.discard(fd)
        else:
            text_mode_fds.add(fd)
        return fd

    def emulating_write(fd, data):
        if fd in text_mode_fds:
            data = bytes(data).replace(b"\n", b"\r\n")
        return real_write(fd, data)

    monkeypatch.setattr(os, "open", emulating_open)
    monkeypatch.setattr(os, "write", emulating_write)

    store = _store(tmp_path, "sess-a")
    payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    blob = asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))

    assert _object_path(tmp_path, blob.artifact_id).read_bytes() == payload
    assert asyncio.run(store.load_bytes(blob.artifact_id)).content == payload
