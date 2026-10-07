"""字节路径的 Provider 契约：FakeArtifactStore + MinioArtifactStore（#822 AC）。

MinIO 用既有 SDK 替身（`_FakeSDKSession` / `_FakeS3Client`）离线验证：
key 必须带会话前缀（隔离来源）、ContentType 往返、缺对象 → KeyError、畸形 id 不发请求。
"""

from __future__ import annotations

import asyncio
from typing import Self

import pytest

from agent_harness.config import Settings
from agent_harness.storage.artifact import FakeArtifactStore, compute_byte_artifact_id


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


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def read(self) -> bytes:
        return self._data


class _FakeS3Client:
    def __init__(self, *, error: Exception | None = None, payload: bytes = b"") -> None:
        self.requests: list[dict] = []
        self._error = error
        self._payload = payload

    async def put_object(self, **kwargs: object) -> dict:
        self.requests.append(kwargs)
        return {}

    async def get_object(self, **kwargs: object) -> dict:
        self.requests.append(kwargs)
        if self._error is not None:
            raise self._error
        return {"Body": _FakeBody(self._payload), "ContentType": "image/png"}


class _FakeSDKSession:
    def __init__(self, client: _FakeS3Client) -> None:
        self._client = client

    def client(self, _service: str, **_kwargs: object):
        client = self._client

        class _ClientCM:
            async def __aenter__(self) -> _FakeS3Client:
                return client

            async def __aexit__(self, *exc: object) -> bool:
                return False

        return _ClientCM()


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


def test_minio_save_bytes_uses_session_prefixed_key(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _minio_store("sess-a")
    client = _FakeS3Client()
    monkeypatch.setattr(store, "_sdk_session", _FakeSDKSession(client))
    payload = b"png-bytes"

    blob = asyncio.run(store.save_bytes("sess-a", payload, mime_type="image/png"))

    sha = blob.artifact_id.split(":", 1)[1]
    assert client.requests[0]["Key"] == f"sess-a/attachments/{sha}"
    assert client.requests[0]["Bucket"] == "b"
    assert client.requests[0]["ContentType"] == "image/png"
    assert client.requests[0]["Body"] == payload


def test_minio_load_bytes_roundtrip_and_namespaced_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"stored"
    store = _minio_store("sess-a")
    client = _FakeS3Client(payload=payload)
    monkeypatch.setattr(store, "_sdk_session", _FakeSDKSession(client))
    sha = compute_byte_artifact_id(payload).split(":", 1)[1]

    loaded = asyncio.run(store.load_bytes(f"sha256:{sha}"))

    assert loaded.content == payload
    assert loaded.mime_type == "image/png"
    assert client.requests[0]["Key"] == f"sess-a/attachments/{sha}"


def test_minio_load_bytes_missing_object_is_key_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _minio_store("sess-a")
    missing = store._client_error({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    monkeypatch.setattr(store, "_sdk_session", _FakeSDKSession(_FakeS3Client(error=missing)))

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

    monkeypatch.setattr(store, "_sdk_session", _FakeSDKSession(_ExplodingClient()))  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        asyncio.run(store.load_bytes(bad))
