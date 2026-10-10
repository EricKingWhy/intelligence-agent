"""字节路径的 Provider 契约：FakeArtifactStore + MinioArtifactStore（#822 AC）。

MinIO 用共用 SDK 替身（`tests/s3_fakes.py` 的 `FakeSDKSession` / `FakeS3Client`）离线
验证：key 形状（全局对象根 + 会话回执）、ContentType 往返、缺对象 → KeyError、畸形 id
不发请求。

#933 M-01 起，本文件另钉**发送侧归属**（`load_uploaded_bytes` 只认本会话回执）：两个远端
Provider 的字节对象与 Local 同构地落在**全局内容寻址根**（`.attachments/objects/<sha[:2]>/<sha>`），
归属事实是每会话的**回执对象**（`{sid}/attachments/{sha}`，save 时随对象一起写）。
"""

from __future__ import annotations

import asyncio

import pytest

from agent_harness.config import Settings
from agent_harness.storage.artifact import FakeArtifactStore, compute_byte_artifact_id
from tests.s3_fakes import FakeKeyedS3Client, FakeS3Client, FakeSDKSession


def test_fake_store_byte_roundtrip_and_dedup() -> None:
    store = FakeArtifactStore()
    payload = b"fake-bytes" * 3
    first = asyncio.run(store.save_bytes("sess", payload, mime_type="image/png"))
    second = asyncio.run(store.save_bytes("sess", payload, mime_type="image/png"))
    assert first.artifact_id == second.artifact_id == compute_byte_artifact_id(payload)

    loaded = asyncio.run(store.load_bytes(first.artifact_id))
    assert loaded.content == payload
    assert loaded.mime_type == "image/png"
    assert loaded.size == len(payload)


def test_fake_store_missing_id_raises_key_error() -> None:
    store = FakeArtifactStore()
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes("sha256:" + "0" * 64))


def _minio_store(session_id: str):
    pytest.importorskip("aioboto3")
    from agent_harness.storage.minio_artifact import MinioArtifactStore

    settings = Settings(
        _env_file=None,
        workspace_dir=".",
        model_api_key="sk-test",
        minio_endpoint="http://127.0.0.1:9000",
        minio_bucket="b",
        minio_access_key="k",
        minio_secret_key="s",
    )
    return MinioArtifactStore(settings, session_id=session_id)


def test_minio_save_bytes_writes_receipt_key_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """回执 key（升级兼容位）先落 —— 顺序是失败面选择，见 `remote_byte_store` 模块顶注。

    #933 之前这条用例叫 `..._uses_session_prefixed_key`，锚的是"字节对象按会话命名空间寻址"
    ——那正是本票要杀死的误解。两条 key 的**完整**契约（回执 + 全局、顺序、body）由
    下方的参数化组 `test_remote_save_bytes_writes_global_object_and_session_receipt` 钉住。
    """
    store = _minio_store("sess-a")
    client = FakeS3Client()
    monkeypatch.setattr(store, "_sdk_session", FakeSDKSession(client))
    payload = b"png-bytes"

    blob = asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))

    sha = blob.artifact_id.split(":", 1)[1]
    assert client.requests[0]["Key"] == f"sess-a/attachments/{sha}"
    assert client.requests[0]["Bucket"] == "b"
    assert client.requests[0]["ContentType"] == "image/png"
    assert client.requests[0]["Body"] == payload


def test_minio_load_bytes_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """save → load 往返（用**按键存取**的替身：读回的必须是 save 真正落盘的字节）。

    #933 M-01 起 key 的形状本身由下方参数化用例组钉住（全局对象根 + 会话回执），
    这里只钉往返语义（ContentType、字节相等），不重复一份 key 期望值。
    """
    payload = b"stored"
    store = _minio_store("sess-a")
    client = FakeKeyedS3Client(
        not_found=store._client_error({"Error": {"Code": "NoSuchKey"}}, "GetObject"),
        content_type="image/png",
    )
    monkeypatch.setattr(store, "_sdk_session", FakeSDKSession(client))

    blob = asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))
    loaded = asyncio.run(store.load_bytes(blob.artifact_id))

    assert loaded.content == payload
    assert loaded.mime_type == "image/png"
    assert client.requests[-1]["Key"] == (
        f"{_GLOBAL_PREFIX}/{payload_sha(payload)[:2]}/{payload_sha(payload)}"
    ), "读回先试全局对象根"


def test_minio_load_bytes_missing_object_is_key_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _minio_store("sess-a")
    missing = store._client_error({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    monkeypatch.setattr(store, "_sdk_session", FakeSDKSession(FakeS3Client(error=missing)))

    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes("sha256:" + "a" * 64))


@pytest.mark.parametrize("bad", ["", "abc", "sha256:XYZ", "sha256:" + "A" * 64])
def test_minio_load_bytes_rejects_malformed_id_without_network(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _minio_store("sess-a")

    class _ExplodingClient:
        async def get_object(self, **_kwargs: object) -> dict:
            raise AssertionError("畸形 id 不得触发任何网络请求")

    monkeypatch.setattr(store, "_sdk_session", FakeSDKSession(_ExplodingClient()))
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes(bad))


# ── #933 M-01：远端 Provider 的全局寻址 + 发送侧归属（会话回执）──────────────
#
# 缺陷：`load_uploaded_bytes` 的 ABC 默认实现 = `load_bytes`，而 S3/MinIO 的字节 key 是
# `{session_id}/attachments/{sha}`（会话内命名空间）⇒ fork 出的子会话读不回父会话上传的字节
# （D1 在对象存储部署下未修复）；而"默认实现静默回落成另一种语义"本身也不该存在——来源:
# DSH `packages/attachment/attachment/src/index.ts:53-265`，那里没有默认实现的方法一律显式
# reject，不存在"默认路径换个语义"。
#
# 修法（两件事，缺一不可）：
# 1. 字节对象改落**全局**内容寻址 key `{prefix}/<sha[:2]>/<sha>`（来源: DSH
#    `attachment-local/src/store.ts:44-54` 单一全局根、oh-my-pi
#    `session/blob-store.ts:1-68` 扁平全局根 + "deduplication across sessions"）
#    ⇒ 跨会话（含 fork 子会话）可寻址；
# 2. 归属事实显式化成**每会话回执对象** `{sid}/attachments/{sha}`（与旧 key 同形）
#    ⇒ 发送侧闸门（PRD D5「属于本 session 上下文」）不放宽，且升级前的旧对象被双读回落兼容。
#
# 与 Local 的差异（刻意的）：对象存储没有 hardlink，回执是**对象副本**而非链接；发布顺序
# 反过来（回执先、对象后），不变量仍是"有回执 ⇒ 有对象"，即回执永远指向真实存在的字节。
#
# 参数化：两个远端 Provider 必须**同契约**（spec 06 §9「MinIO Provider 可替换 Local Provider」），
# 所以同一组用例跑两遍，而不是各写一份。

_GLOBAL_PREFIX = ".attachments/objects"


def payload_sha(payload: bytes) -> str:
    """字节 → 对象 key 里的 sha 段（`sha256:<hex>` 前缀之后的部分）。"""
    return compute_byte_artifact_id(payload).split(":", 1)[1]


def _remote_settings() -> Settings:
    """一份同时带两组对象存储配置的 Settings（两个 Provider 直接构造，不经过选择器）。"""
    return Settings(
        _env_file=None,
        workspace_dir=".",
        model_api_key="sk-test",
        minio_endpoint="http://127.0.0.1:9000",
        minio_bucket="b",
        minio_access_key="k",
        minio_secret_key="s",
        artifact_store_endpoint="https://s3.example.test",
        artifact_store_bucket="b",
        artifact_store_region="cn-east-1",
        artifact_store_access_key="k",
        artifact_store_secret_key="s",
    )


def _remote(provider: str, session_id: str) -> tuple[object, FakeKeyedS3Client]:
    """构造远端 store 并换上**按键存取**的 SDK 替身（多候选 key 要两次不同回包）。"""
    pytest.importorskip("aioboto3")
    if provider == "minio":
        from agent_harness.storage.minio_artifact import MinioArtifactStore

        store = MinioArtifactStore(_remote_settings(), session_id=session_id)
    else:
        from agent_harness.storage.s3_artifact import S3ArtifactStore

        store = S3ArtifactStore(_remote_settings(), session_id=session_id)
    client = FakeKeyedS3Client(
        not_found=store._client_error({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    )
    store._sdk_session = FakeSDKSession(client)
    return store, client


@pytest.fixture(params=["minio", "s3"])
def remote_provider(request: pytest.FixtureRequest) -> str:
    """两个远端 Provider（#933 M-01 的契约面必须在两边同时成立）。"""
    return str(request.param)


def test_remote_save_bytes_writes_global_object_and_session_receipt(
    remote_provider: str,
) -> None:
    """save 落**两份** key：本会话回执（归属事实）+ 全局对象根（跨会话可寻址）。"""
    store, client = _remote(remote_provider, "sess-a")
    payload = b"png-bytes"

    asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))

    sha = payload_sha(payload)
    assert client.put_keys == [
        f"sess-a/attachments/{sha}",
        f"{_GLOBAL_PREFIX}/{sha[:2]}/{sha}",
    ], "回执先落、对象后落（顺序即失败面选择，见 remote_byte_store 模块顶注）"
    assert set(client.objects.values()) == {payload}


def test_remote_fork_child_reads_parent_bytes(remote_provider: str) -> None:
    """**M-01 的 P1 症状**：父会话上传 → 子会话（fork）只拿到 id 也能读回字节。

    子会话 store 的会话段是自己的（`child/attachments/...`），它**没有**父会话回执 ⇒
    读回必须靠全局对象根命中。这正是 Local 在 #830 D1 修掉、远端一直没修的那条路径。
    """
    parent, client = _remote(remote_provider, "parent")
    payload = b"fork-inherited-bytes"
    blob = asyncio.run(parent.save_bytes("parent", payload, mime_type="image/png"))

    child, _ = _remote(remote_provider, "child")
    child._sdk_session = FakeSDKSession(client)  # 同一个 bucket = 同一份对象集合

    loaded = asyncio.run(child.load_bytes(blob.artifact_id))

    assert loaded.content == payload
    assert loaded.mime_type == "image/png"


def test_remote_load_bytes_falls_back_to_legacy_session_key(remote_provider: str) -> None:
    """升级前落在 `{sid}/attachments/{sha}` 的对象不因全局化而 brick（双读回落）。"""
    store, client = _remote(remote_provider, "sess-a")
    payload = b"pre-upgrade"
    sha = payload_sha(payload)
    client.objects[f"sess-a/attachments/{sha}"] = payload  # 旧 key：全局根没有它

    loaded = asyncio.run(store.load_bytes(f"sha256:{sha}"))

    assert loaded.content == payload
    assert client.keys == [
        f"{_GLOBAL_PREFIX}/{sha[:2]}/{sha}",  # 全局优先
        f"sess-a/attachments/{sha}",  # 未命中 → 回落旧 key
    ]


def test_remote_uploaded_bytes_is_session_scoped(remote_provider: str) -> None:
    """`load_uploaded_bytes` = 只看**本会话回执**：别的会话读得到对象、但拿不到归属。"""
    parent, client = _remote(remote_provider, "parent")
    payload = b"owned-by-parent"
    blob = asyncio.run(parent.save_bytes("parent", payload, mime_type="image/png"))

    child, _ = _remote(remote_provider, "child")
    child._sdk_session = FakeSDKSession(client)

    # 读回（内容寻址）：子会话读得到 —— fork 场景要的就是这条。
    assert asyncio.run(child.load_bytes(blob.artifact_id)).content == payload
    # 归属（发送侧闸门）：子会话没上传过 ⇒ KeyError；且**只**查自己的回执 key。
    client.requests.clear()
    with pytest.raises(KeyError):
        asyncio.run(child.load_uploaded_bytes(blob.artifact_id))
    assert client.keys == [f"child/attachments/{payload_sha(payload)}"], (
        "归属判据只看本会话回执：不得退化成'全局读得到就算归属'"
    )
    # 上传者自己两条都通。
    assert asyncio.run(parent.load_uploaded_bytes(blob.artifact_id)).content == payload


def test_remote_uploaded_bytes_legacy_object_is_still_owned(remote_provider: str) -> None:
    """升级前的旧对象（只有 `{sid}/attachments/{sha}`）仍是本会话上传过的字节。"""
    store, client = _remote(remote_provider, "sess-a")
    payload = b"legacy-owned"
    client.objects[f"sess-a/attachments/{payload_sha(payload)}"] = payload

    loaded = asyncio.run(store.load_uploaded_bytes(f"sha256:{payload_sha(payload)}"))

    assert loaded.content == payload


def test_remote_uploaded_bytes_malformed_id_makes_no_request(remote_provider: str) -> None:
    """归属路径同样先判形态（与读回路径同契约）：畸形 id 一个请求都不发。"""
    store, client = _remote(remote_provider, "sess-a")

    with pytest.raises(KeyError):
        asyncio.run(store.load_uploaded_bytes("sha256:XYZ"))

    assert client.requests == []


def test_remote_load_bytes_malformed_id_makes_no_request(remote_provider: str) -> None:
    """读回路径同理（既有契约的远端参数化版本）。"""
    store, client = _remote(remote_provider, "sess-a")

    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes("sha256:XYZ"))

    assert client.requests == []


def test_remote_tampered_global_object_is_rejected(remote_provider: str) -> None:
    """全局对象被带外篡改 ⇒ 自证失败 ⇒ KeyError（内容寻址承诺不许静默给错字节）。"""
    store, client = _remote(remote_provider, "sess-a")
    payload = b"good-bytes"
    sha = payload_sha(payload)
    client.objects[f"{_GLOBAL_PREFIX}/{sha[:2]}/{sha}"] = b"tampered!"

    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes(f"sha256:{sha}"))
