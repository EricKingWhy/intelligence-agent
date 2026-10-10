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
from contextlib import suppress
from pathlib import Path

import pytest

from agent_harness.config import Settings
from agent_harness.storage import local_artifact
from agent_harness.storage.artifact import (
    BYTE_ARTIFACT_ID_PATTERN,
    compute_byte_artifact_id,
)
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


def _windows_unlink(path: str, *, dir_fd: int | None = None) -> None:
    """模拟 Windows 的 FILE_ATTRIBUTE_READONLY：无写位的常规文件 unlink 被拒。

    要按 `dir_fd` 解析（fd 版 rmtree 用 `os.unlink(entry.name, dir_fd=...)`），否则
    相对名会落到 CWD、模拟失效。
    """
    real_unlink = _windows_unlink.real  # type: ignore[attr-defined]
    try:
        st = os.stat(path, dir_fd=dir_fd)
    except OSError:
        return real_unlink(path, dir_fd=dir_fd)
    if stat.S_ISREG(st.st_mode) and not (st.st_mode & stat.S_IWUSR):
        raise PermissionError(13, "Access is denied (simulated Windows read-only file)")
    return real_unlink(path, dir_fd=dir_fd)


def _install_windows_unlink(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 `os.unlink` 换成 Windows 只读语义（见 `_windows_unlink`）。"""
    _windows_unlink.real = os.unlink  # type: ignore[attr-defined]
    monkeypatch.setattr(os, "unlink", _windows_unlink)


def _object_is_read_only(path: Path) -> bool:
    """对象是否处于发布时的只读态（与 `test_object_permission_is_read_only` 同判据）。"""
    file_stat = os.stat(path)
    if os.name == "nt":
        return bool(file_stat.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
    return stat.S_IMODE(file_stat.st_mode) == 0o400


class _FakeSys:
    """只提供 `version_info` 的 `sys` 替身（#922 版本分支用）。

    不 monkeypatch 真实 `sys.version_info`：那是解释器全局状态，pytest / anyio / asyncio
    自己也读它，改掉会连带歪曲框架的版本判断。
    """

    def __init__(self, version_info: tuple[int, int, int]) -> None:
        self.version_info = version_info


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


def test_discard_removes_readonly_receipt_hardlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 语义回归（#916）：只读回执删不掉 ⇒ discard 仍须带走回执、留下全局对象。

    发布出去的对象是只读的（`_publish_blob` 的 `chmod 0o400`），会话回执是它的 hardlink
    ⇒ Windows 上回执共享同一只读属性，`os.unlink` 抛 `PermissionError`；Linux 上 unlink
    不受只读位约束（只看目录写权限），所以这里显式把 `os.unlink` 换成 Windows 语义：无
    写位的常规文件不可删。修复前 `rmtree(..., ignore_errors=True)` 会把失败静默吞掉 ⇒
    回执残留（本用例红）。
    """
    store = _store(tmp_path, "sess-a")
    blob = asyncio.run(store.save_bytes("sess-a", b"readonly-receipt", mime_type="image/png"))
    receipt = _receipt_path(tmp_path, "sess-a", blob.artifact_id)
    assert receipt.is_file()
    assert not (os.stat(receipt).st_mode & stat.S_IWUSR), "回执应是只读的（与全局对象同 inode）"

    _install_windows_unlink(monkeypatch)

    discard_local_artifacts(_settings(tmp_path), "sess-a")

    assert not receipt.exists()
    assert _object_path(tmp_path, blob.artifact_id).is_file()
    assert (
        asyncio.run(_store(tmp_path, "sess-b").load_bytes(blob.artifact_id)).content
        == b"readonly-receipt"
    )


def test_discard_restores_read_only_on_global_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#923：Windows 语义下 discard 删掉只读回执后，全局字节对象必须**恢复只读**。

    对象发布时是 `0o400`，回执是它的 hardlink ⇒ Windows 上共享同一只读属性，清回执的
    只读位会连带清掉对象的（副作用原文见 `_read_only_retry_handler` docstring）。修法
    是在本次 discard 的局部状态里记下"实际清过只读位的回执"，删完后按回执路径里嵌的
    sha256 推导出全局对象、best-effort 恢复 `0o400`。
    """
    store = _store(tmp_path, "sess-a")
    blob = asyncio.run(
        store.save_bytes("sess-a", b"restore-readonly", mime_type="image/png")
    )
    object_path = _object_path(tmp_path, blob.artifact_id)
    assert _object_is_read_only(object_path)

    _install_windows_unlink(monkeypatch)
    discard_local_artifacts(_settings(tmp_path), "sess-a")

    assert not _receipt_path(tmp_path, "sess-a", blob.artifact_id).exists()
    assert object_path.is_file()
    assert _object_is_read_only(object_path), "discard 后全局对象必须回到发布时的只读位"
    assert (
        asyncio.run(_store(tmp_path, "sess-b").load_bytes(blob.artifact_id)).content
        == b"restore-readonly"
    )


def test_discard_twice_keeps_object_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """幂等：同一会话 discard 两次不崩，且每次结束后对象都是只读。

    第二次调用时会话目录已不在 ⇒ 不触发 rmtree ⇒ 本次 cleared_receipts 为空 ⇒ 恢复是 no-op。
    """
    blob = asyncio.run(
        _store(tmp_path, "sess-a").save_bytes("sess-a", b"twice", mime_type="image/png")
    )
    object_path = _object_path(tmp_path, blob.artifact_id)
    _install_windows_unlink(monkeypatch)

    discard_local_artifacts(_settings(tmp_path), "sess-a")
    discard_local_artifacts(_settings(tmp_path), "sess-a")

    assert not _receipt_path(tmp_path, "sess-a", blob.artifact_id).exists()
    assert _object_is_read_only(object_path)
    assert asyncio.run(_store(tmp_path, "sess-a").load_bytes(blob.artifact_id)).content == b"twice"


def test_discard_does_not_touch_objects_it_did_not_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只恢复**本次 discard 碰过的**对象：别的全局对象（此处刻意可写）不许被动。

    全量扫 `<root>/.attachments` 再 chmod 的实现会误改这里的可写对象——它可达性上与本次
    删除毫无关系（它在全局根里，本次删的是会话目录）。
    """
    blob = asyncio.run(
        _store(tmp_path, "sess-a").save_bytes("sess-a", b"touched", mime_type="image/png")
    )
    other_payload = b"not-touched-by-this-discard"
    other = _object_path(tmp_path, compute_byte_artifact_id(other_payload))
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(other_payload)
    os.chmod(other, 0o600)  # 模拟"不归本次 discard 管"的既有可写状态

    _install_windows_unlink(monkeypatch)
    discard_local_artifacts(_settings(tmp_path), "sess-a")

    assert _object_is_read_only(_object_path(tmp_path, blob.artifact_id))
    # 未触碰的对象必须保持 discard 前的可写态："全量扫再 chmod" 的实现会把它一并翻成只读，
    # 正是本用例要拦的回归。Windows 没有 POSIX 权限位（CPython 由只读属性合成 `st_mode`，
    # S_IMODE 只有 0o444/0o666 两态），等价观测是"未被翻成只读"——复用本文件
    # `_object_is_read_only` 的 nt 感知判据；POSIX 面保持原有的精确 0o600 断言（由 CI 覆盖）。
    if os.name == "nt":
        untouched_kept_writable = not _object_is_read_only(other)
    else:
        untouched_kept_writable = stat.S_IMODE(os.stat(other).st_mode) == 0o600
    assert untouched_kept_writable, (
        "未触碰的对象被改了权限：恢复只许作用于本次清过只读位的那几个对象"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX 专用：unlink 不看只读位（Windows 走清位回退）")
def test_discard_without_readonly_receipt_never_chmods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX 语义（unlink 不看只读位）下回调不触发 ⇒ cleared_receipts 为空 ⇒ 恢复是 no-op。

    用 spy 观察 `os.chmod`：一次都不许发生。只断言"对象仍是只读"是假绿——对象本来就没
    被动过，读不出"恢复逻辑有没有乱改别的路径"。
    """
    blob = asyncio.run(
        _store(tmp_path, "sess-a").save_bytes("sess-a", b"posix", mime_type="image/png")
    )
    calls: list[tuple[str, int]] = []
    real_chmod = os.chmod

    def _spy_chmod(path: str, mode: int, **kwargs: object) -> None:
        calls.append((str(path), mode))
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", _spy_chmod)
    discard_local_artifacts(_settings(tmp_path), "sess-a")

    assert calls == [], f"POSIX 上不应有任何 chmod（cleared_receipts 必为空），实收 {calls!r}"
    assert _object_is_read_only(_object_path(tmp_path, blob.artifact_id))


def test_discard_survives_object_removed_before_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """恢复是 best-effort：全局对象在恢复前被并发删掉 ⇒ discard 照样不抛。

    在 `os.unlink` 模拟层里"删回执的同时把对象也删掉"，精确落在清位与恢复之间的竞态窗口。
    """
    blob = asyncio.run(
        _store(tmp_path, "sess-a").save_bytes("sess-a", b"concurrent", mime_type="image/png")
    )
    object_path = _object_path(tmp_path, blob.artifact_id)
    _install_windows_unlink(monkeypatch)
    real_unlink = _windows_unlink.real  # type: ignore[attr-defined]

    def _unlink_and_drop_object(path: str, *, dir_fd: int | None = None) -> None:
        _windows_unlink(path, dir_fd=dir_fd)
        with suppress(FileNotFoundError):
            real_unlink(object_path)

    monkeypatch.setattr(os, "unlink", _unlink_and_drop_object)

    discard_local_artifacts(_settings(tmp_path), "sess-a")  # 不得抛

    assert not _receipt_path(tmp_path, "sess-a", blob.artifact_id).exists()
    assert not object_path.exists()


def test_restore_touches_only_unlinked_files_not_directories(tmp_path: Path) -> None:
    """纵深防御：恢复只由**文件**回执触发，目录（含名字像 sha 的）一概不登记。

    直接调回调，不依赖平台与权限位：`os.rmdir` 的目录只清位、不登记（目录不是对象，不共享
    全局对象的只读属性）；`os.unlink` 的文件才登记进 `cleared_receipts`。端到端构造既贵又
    在本机（root）构造不出——root 绕过权限位，rmtree 删空目录根本不报错、回调压根不触发。
    """
    cleared_receipts: set[Path] = set()
    handler = local_artifact._read_only_retry_handler(cleared_receipts)
    sha = "ab" * 32
    receipt_dir = _receipt_path(tmp_path, "sess-a", f"sha256:{sha}")
    receipt_dir.mkdir(parents=True)
    handler(os.rmdir, str(receipt_dir), None)
    assert cleared_receipts == set(), "os.rmdir 的目录不许记进 cleared_receipts"
    assert not receipt_dir.exists(), "清位后重试仍应删掉目录"
    receipt_file = _receipt_path(tmp_path, "sess-a", f"sha256:{'f' * 64}")
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_bytes(b"x")
    handler(os.unlink, str(receipt_file), None)
    assert cleared_receipts == {receipt_file}, "os.unlink 的文件必须登记"
    # 清位本身失败（路径已不在）⇒ 不登记、也不抛：登记没有 `cleared` 标志兜着，它的
    # "chmod 成功才登记"语义完全由 `suppress(OSError)` 所在的代码块承载。
    handler(os.unlink, str(tmp_path / "artifacts" / "sess-a" / "gone"), None)
    assert cleared_receipts == {receipt_file}, "chmod 失败的路径不许登记"


@pytest.mark.skipif(os.name == "nt", reason="符号链接环是 POSIX 语义（Windows 建链需特权）")
def test_restore_survives_symlink_loop_in_object_root(tmp_path: Path) -> None:
    """`_global_object_for_receipt` 的 `.resolve()` 撞符号链接环抛 `RuntimeError`（**不是**
    `OSError`）：恢复必须照样 best-effort 不抛，会话硬删不被连带打断（#923 P2-1）。
    """
    root = tmp_path / "artifacts"
    root.mkdir(parents=True)
    (root / ".attachments").symlink_to(".attachments/loop")  # 自指环 ⇒ resolve() 抛 RuntimeError
    session_dir = root / "sess-a"
    receipt = _receipt_path(tmp_path, "sess-a", f"sha256:{'ab' * 32}")
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(b"x")

    local_artifact._restore_object_read_only(receipt, session_dir, root)  # 不得抛


def test_global_object_for_receipt_rejects_malformed_paths(tmp_path: Path) -> None:
    """路径推导是信任边界：形状不符 / 越界一律返回 `None`（不许推导出任意路径）。"""
    root = tmp_path / "artifacts"
    session_dir = root / "sess-a"
    derive = local_artifact._global_object_for_receipt
    sha = "cd" * 32
    good = _sharded(session_dir / "attachments" / "objects", f"sha256:{sha}")

    assert derive(good, session_dir, root) == _object_path(tmp_path, f"sha256:{sha}")
    # 段数不对 / 中段不符 / 摘要非 64 位 hex / shard 与摘要前缀不符 → 一律拒绝。
    assert derive(session_dir / "attachments" / sha, session_dir, root) is None
    assert derive(session_dir / "objects" / sha[:2] / sha, session_dir, root) is None
    assert derive(
        session_dir / "attachments" / "objects" / sha[:2] / "nope", session_dir, root
    ) is None
    assert derive(session_dir / "attachments" / "objects" / "zz" / sha, session_dir, root) is None
    # 不在会话目录之下（`relative_to` 抛 ValueError）→ 拒绝。
    assert derive(
        root / "sess-b" / "attachments" / "objects" / sha[:2] / sha, session_dir, root
    ) is None

    # shard 目录是 symlink 且指向 root 之外：resolve 后逃逸出 objects 根 ⇒ 拒绝。
    # 这是 `_global_object_for_receipt` 里 `is_relative_to` 安全闸的守卫
    #（变异 `if False:` 后全绿，B 轴复审新 P3）。
    outside = tmp_path / "outside-root"
    outside.mkdir()
    escaped_shard = root / ".attachments" / "objects" / sha[:2]
    escaped_shard.parent.mkdir(parents=True, exist_ok=True)
    try:
        escaped_shard.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("建目录符号链接需特权（Windows 无提权时跳过）")
    escaped_receipt = session_dir / "attachments" / "objects" / sha[:2] / sha
    assert derive(escaped_receipt, session_dir, root) is None


def test_sha256_pattern_matches_byte_artifact_id_digest_shape() -> None:
    """`_SHA256_PATTERN` 必须与 `artifact.BYTE_ARTIFACT_ID_PATTERN` 的摘要段同形（防漂移）。

    两者是各自独立的字面量：`artifact.py` 改了摘要形状而这里没跟，回执路径校验就会静默
    放过/误拒——本用例把这条注释承诺变成红灯。
    """
    assert (
        local_artifact._SHA256_PATTERN.pattern
        == BYTE_ARTIFACT_ID_PATTERN.pattern.removeprefix("sha256:")
    )


def test_discard_restores_global_object_when_receipt_is_a_legacy_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """升级场景边界（`_link_session_receipt` docstring）：回执与全局对象**不是同一 inode**。

    此时清回执的只读位不会波及全局对象；恢复按 sha 推导出的**全局对象路径**照常执行
    （best-effort，对已是只读的对象重复 chmod 无害），不为旧 inode 加特殊分支。
    """
    blob = asyncio.run(
        _store(tmp_path, "sess-a").save_bytes("sess-a", b"legacy-inode", mime_type="image/png")
    )
    object_path = _object_path(tmp_path, blob.artifact_id)
    receipt = _receipt_path(tmp_path, "sess-a", blob.artifact_id)
    # 把回执换成同字节的**独立 inode**（升级前旧对象的形状），两边都只读。只读文件在
    # Windows 上不可直接 unlink（抛 PermissionError）⇒ 先清只读位再删；这一步此刻仍作用于
    # 全局对象的 hardlink（共享属性），清位会连带把对象也变可写，故删后把对象复位回只读。
    # 清位沿用生产同一写法"原 mode 或上写位"（`_read_only_retry_handler`），不新造原语。
    os.chmod(receipt, os.stat(receipt).st_mode | stat.S_IWUSR)
    receipt.unlink()
    receipt.write_bytes(b"legacy-inode")
    os.chmod(receipt, 0o400)
    os.chmod(object_path, 0o400)
    assert receipt.stat().st_ino != object_path.stat().st_ino

    _install_windows_unlink(monkeypatch)
    discard_local_artifacts(_settings(tmp_path), "sess-a")

    assert not receipt.exists()
    assert _object_is_read_only(object_path)


@pytest.mark.parametrize(
    "fake_version, expected_kwarg",
    [
        pytest.param((3, 12, 0), "onexc", id="py312-plus-onexc"),
        pytest.param((3, 11, 0), "onerror", id="legacy-onerror"),
    ],
)
def test_discard_registers_version_appropriate_rmtree_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_version: tuple[int, int, int],
    expected_kwarg: str,
) -> None:
    """版本分支（#922）：3.12+ 传 `onexc=`，旧版本传 `onerror=`，且只传一个。

    版本用替身对象注入模块的 `sys`，不碰真实 `sys.version_info`（全局改它会让 pytest /
    anyio 自身的版本判断一起歪掉）。分支判定必须是**调用时**读的：import 时写死的话这里
    测不出来。两个参数值都不等于真实解释器版本，所以用例不依赖跑的是哪个 Python。
    """
    asyncio.run(
        _store(tmp_path, "sess-a").save_bytes("sess-a", b"x", mime_type="image/png")
    )
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(local_artifact, "sys", _FakeSys(fake_version))
    monkeypatch.setattr(
        local_artifact.shutil,
        "rmtree",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    discard_local_artifacts(_settings(tmp_path), "sess-a")

    assert len(captured) == 1, "会话目录存在时必须调用一次 rmtree"
    args, kwargs = captured[0]
    assert len(args) == 1, f"只有 path 占位置参数位，实收 {args!r}"
    assert set(kwargs) == {expected_kwarg}, (
        f"Python {fake_version} 应且只应注册 {expected_kwarg}="
    )


@pytest.mark.parametrize(
    "fake_version, exc_is_tuple",
    [
        pytest.param((3, 12, 0), False, id="py312-plus-exc-instance"),
        pytest.param((3, 11, 0), True, id="legacy-exc-info-tuple"),
    ],
)
def test_discard_callback_receives_version_appropriate_exc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_version: tuple[int, int, int],
    exc_is_tuple: bool,
) -> None:
    """回调第三个参数随分支变（#922）：onexc ⇒ 异常**实例**；onerror ⇒ exc_info 三元组。

    端到端跑：模拟 Windows 只读文件语义（同 `test_discard_removes_readonly_receipt_hardlink`），
    再用 spy 包住真回调，既断言参数形态、也断言清只读位后的重试确实成功（回执被带走）。
    旧分支由真实解释器接受 `onerror=`（deprecated 但可用）执行。
    """
    store = _store(tmp_path, "sess-a")
    blob = asyncio.run(store.save_bytes("sess-a", b"readonly-receipt", mime_type="image/png"))
    receipt = _receipt_path(tmp_path, "sess-a", blob.artifact_id)
    assert receipt.is_file()

    seen: list[object] = []
    real_factory = local_artifact._read_only_retry_handler

    def _spy_factory(cleared_receipts: set[Path]) -> object:
        """包住回调工厂：记下回调收到的第三个参数，其余行为原样透传。"""
        inner = real_factory(cleared_receipts)

        def _spy(func: object, path: str, exc: object) -> None:
            seen.append(exc)
            inner(func, path, exc)  # type: ignore[arg-type]

        return _spy

    _install_windows_unlink(monkeypatch)
    monkeypatch.setattr(local_artifact, "sys", _FakeSys(fake_version))
    monkeypatch.setattr(local_artifact, "_read_only_retry_handler", _spy_factory)

    discard_local_artifacts(_settings(tmp_path), "sess-a")

    assert seen, "只读回执必须触发一次回调（否则本用例什么都没测到）"
    for exc in seen:
        if exc_is_tuple:
            assert isinstance(exc, tuple), f"onerror 应收到 exc_info 三元组，实收 {exc!r}"
            assert isinstance(exc[1], PermissionError), f"三元组第二位应是异常实例：{exc!r}"
        else:
            assert isinstance(exc, PermissionError), f"onexc 应收到异常实例，实收 {exc!r}"
    assert not receipt.exists(), "清只读位后重试必须把回执带走"
    assert _object_path(tmp_path, blob.artifact_id).is_file()


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


# ── #933 M-04：staged 自证必须校验**落盘字节**，不是内存里那份 ──────────────
#
# 缺陷：`_publish_blob` 的 self-check 是 `hashlib.sha256(data).hexdigest() != sha256`，
# 而 `sha256` 由同一份内存 `data` 派生（`compute_byte_artifact_id(data)` 在
# `_save_bytes_blocking` 里算出来）⇒ **恒真**，从未工作过。目的本来是"落盘字节 == 预期
# 摘要"（平台把字节写坏时在发布前拦住），实际比的是"内存 == 内存"。
#
# 修法依据（来源: DSH `5badb150` `attachment-local/src/store.ts:390-394` `digestFile`
# 与 `:214-229` `publishImmutableObject`）：校验读的是**文件字节**；去重命中时
# `:287`/`:363` 同样 `digestFile(target)` 读回已有对象。
#
# 下方两条用例是**消融实验**（ablation）：不改产品代码、只把"磁盘写坏"注入进来
# （挂钩 `os.write`，与同文件 #830 D2 用例同一手法），看自证是否真的发现得了。
# - 修前：`save_bytes` 静默返回，坏字节被发布成对象 ⇒ 用例红；
# - 修后：发布前抛 `OSError`，目标位置没有对象 ⇒ 用例绿。
# 断言刻意**不**挂钩具体校验原语（不 patch `hashlib.file_digest`）：换一种读回算法
# 不该让用例变红，只有"根本没读回"才该红——那是这条检查的意义所在。


def _emulate_corrupting_write(monkeypatch: pytest.MonkeyPatch, transform) -> None:
    """把 `os.write` 换成"先写坏再落盘"的替身（模拟平台改写字节 / 部分写）。"""
    real_write = os.write

    def corrupting_write(fd: int, data) -> int:
        return real_write(fd, transform(bytes(data)))

    monkeypatch.setattr(os, "write", corrupting_write)


def _residue(staging_dir: Path) -> list[Path]:
    return [p for p in staging_dir.iterdir()] if staging_dir.is_dir() else []


def test_publish_rejects_staged_bytes_expanded_by_platform_text_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """消融①：平台把 `0x0A` 撑成 `0x0D 0x0A`（Windows 文本模式形状）⇒ 发布前必须抛。

    这是票面点名的场景（`O_BINARY` 挡的是它，自证是第二道防线）。修前这道检查恒真，
    坏字节会被当成正常对象发布出去，直到读回才 404/占位符。
    """
    monkeypatch.delattr(os, "O_BINARY", raising=False)  # 让"平台改写"不再被 O_BINARY 挡住
    _emulate_corrupting_write(monkeypatch, lambda raw: raw.replace(b"\n", b"\r\n"))

    store = _store(tmp_path, "sess-a")
    payload = b"\x89PNG\r\n\x1a\n" + b"line\n" * 4

    with pytest.raises(OSError, match="do not match their publication digest"):
        asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))

    attachment_id = compute_byte_artifact_id(payload)
    assert not _object_path(tmp_path, attachment_id).exists(), "坏字节不得被发布成对象"
    assert not _receipt_path(tmp_path, "sess-a", attachment_id).exists(), "更不得留下回执"
    assert _residue(_staging_dir(tmp_path)) == [], "staging 不得留下残渣"


def test_publish_rejects_same_length_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """消融②：**等长**篡改（`0x0A` → `0x0D`）⇒ 仍必须抛（证明校验的是摘要，不是长度）。"""
    monkeypatch.delattr(os, "O_BINARY", raising=False)
    _emulate_corrupting_write(monkeypatch, lambda raw: raw.replace(b"\n", b"\r"))

    store = _store(tmp_path, "sess-a")
    payload = b"first line\nsecond line\n"

    with pytest.raises(OSError, match="do not match their publication digest"):
        asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))

    assert not _object_path(tmp_path, compute_byte_artifact_id(payload)).exists()
    assert _residue(_staging_dir(tmp_path)) == []


def test_publish_self_check_passes_for_byte_exact_staging(tmp_path: Path) -> None:
    """阳性对照：字节没被改写时自证通过、对象照常发布（这条不是恒真的假绿）。"""
    store = _store(tmp_path, "sess-a")
    payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"\n"

    blob = asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))

    assert _object_path(tmp_path, blob.artifact_id).read_bytes() == payload
    assert _residue(_staging_dir(tmp_path)) == []


# ── #933 M-16：读回候选是**可扩展的单一事实源** ─────────────────────────────
#
# 缺陷：`_blob_object_candidates` 硬编码两条路径，而**旁挂元数据读**（`_read_blob_meta`）
# 另写了一份自己的两条路径元组 ⇒ 布局演化时"加一条候选"要在两处各改一次，漏一处就得到
# "对象读得到、旁挂读不到"的静默错配（症状最轻：mime 退化成 octet-stream）。
#
# 修法：候选构造收口成一处，旁挂路径由候选派生（`候选.with_name(name + ".json")`）。
# 下方第二条用例就是这个 claim 的机械证明：**只**给候选列表加一条，对象与旁挂同时可读。


def test_blob_object_candidates_order_is_global_then_legacy_session(
    tmp_path: Path,
) -> None:
    """候选清单与顺序 = 布局契约本身（全局现行根 → 本会话升级兼容位）。"""
    store = _store(tmp_path, "sess-a")
    sha256 = "ab" * 32

    assert store._blob_object_candidates(sha256) == (
        tmp_path / "artifacts" / ".attachments" / "objects" / "ab" / sha256,
        tmp_path / "artifacts" / "sess-a" / "attachments" / "objects" / "ab" / sha256,
    ), "顺序即优先级：全局（现行）在前，本会话（升级兼容）在后"


def test_adding_a_candidate_layout_needs_no_consumer_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**只**加一条候选 ⇒ 对象读与旁挂读**同时**生效（M-16 的可扩展性判据）。

    改动前这条用例会红：`_read_blob_meta` 有自己的一份路径清单，新布局的旁挂读不到，
    mime 退化成 `application/octet-stream`。
    """
    store = _store(tmp_path, "sess-a")
    payload = b"third-layout-bytes"
    attachment_id = compute_byte_artifact_id(payload)
    sha256 = attachment_id.split(":", 1)[1]
    # 假想的下一次布局迁移：对象挪到 `<root>/legacy-v0/<sha[:2]>/<sha>`。
    extra = tmp_path / "artifacts" / "legacy-v0" / sha256[:2] / sha256
    extra.parent.mkdir(parents=True)
    extra.write_bytes(payload)
    extra.with_name(f"{sha256}.json").write_text(
        '{"mime_type": "image/webp"}', encoding="utf-8"
    )

    original = LocalArtifactStore._blob_object_candidates
    monkeypatch.setattr(
        LocalArtifactStore,
        "_blob_object_candidates",
        lambda self, sha: (*original(self, sha), extra),
    )

    loaded = asyncio.run(store.load_bytes(attachment_id))

    assert loaded.content == payload
    assert loaded.mime_type == "image/webp", (
        "旁挂元数据必须从**同一份候选**派生：加一条候选就该两处同时生效"
    )
