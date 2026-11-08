"""Compare text Artifact metadata at the S3-compatible provider boundary."""

import hashlib
import json
import sys
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from agent_harness.config import Settings
from agent_harness.storage.artifact import Artifact, ArtifactStore
from agent_harness.storage.minio_artifact import MinioArtifactStore
from agent_harness.storage.s3_artifact import S3ArtifactStore
from tests.s3_fakes import FakeBody


class RecordingS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.put_requests: list[dict[str, object]] = []
        self.get_requests: list[dict[str, object]] = []
        self.raw_get_responses: list[dict[str, object]] = []
        self.get_error: Exception | None = None

    async def put_object(self, **request: object) -> dict[str, object]:
        self.put_requests.append(dict(request))
        bucket, key = str(request["Bucket"]), str(request["Key"])
        stored = {
            "Body": request["Body"],
            "ContentType": request["ContentType"],
        }
        if "Metadata" in request:
            stored["Metadata"] = request["Metadata"]
        self.objects[(bucket, key)] = stored
        return {}

    async def get_object(self, **request: object) -> dict[str, object]:
        self.get_requests.append(dict(request))
        if self.get_error is not None:
            raise self.get_error
        stored = self.objects[(str(request["Bucket"]), str(request["Key"]))]
        raw_response = dict(stored)
        self.raw_get_responses.append(raw_response)
        return {
            "Body": FakeBody(stored["Body"]),
            **{key: value for key, value in stored.items() if key != "Body"},
        }


class RecordingSDKSession:
    def __init__(self, client: RecordingS3Client) -> None:
        self.client_instance = client
        self.client_calls: list[tuple[str, dict[str, Any]]] = []

    def client(self, service: str, **kwargs: Any):
        self.client_calls.append((service, dict(kwargs)))
        client = self.client_instance

        class ClientContext:
            async def __aenter__(self) -> RecordingS3Client:
                return client

            async def __aexit__(self, *exc: object) -> bool:
                return False

        return ClientContext()


def provider_settings(provider: str) -> tuple[Settings, str, str, str, str]:
    if provider == "s3":
        endpoint, bucket, access, secret = (
            "https://s3.example.test", "s3-bucket", "s3-access", "s3-secret"
        )
        settings = Settings(
            _env_file=None,
            artifact_store_endpoint=endpoint,
            artifact_store_bucket=bucket,
            artifact_store_region="test-region-1",
            artifact_store_access_key=access,
            artifact_store_secret_key=secret,
        )
    else:
        endpoint, bucket, access, secret = (
            "https://minio.example.test", "minio-bucket", "minio-access", "minio-secret"
        )
        settings = Settings(
            _env_file=None,
            minio_endpoint=endpoint,
            minio_bucket=bucket,
            minio_access_key=access,
            minio_secret_key=secret,
        )
    return settings, endpoint, bucket, access, secret


@pytest.mark.asyncio
async def test_s3_and_minio_text_metadata_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("botocore")
    content = "abc\né"
    body = content.encode("utf-8")
    expected_id = hashlib.sha256(body).hexdigest()[:16]
    observed: dict[
        str, tuple[Artifact, Artifact, dict[str, Any], dict[str, Any]]
    ] = {}

    for name, store_type in (
        ("s3", S3ArtifactStore),
        ("minio", MinioArtifactStore),
    ):
        settings, endpoint, bucket, access, secret = provider_settings(name)
        client = RecordingS3Client()
        sdk_session = RecordingSDKSession(client)
        monkeypatch.setitem(
            sys.modules, "aioboto3",
            SimpleNamespace(Session=lambda session=sdk_session: session),
        )
        store = store_type(settings, session_id="session-1")

        saved = await store.save(
            "session-1",
            content,
            mime_type="text/plain; charset=utf-8",
            source_tool="shell",
            tool_call_id="call-42",
        )
        loaded = await store.load(saved.artifact_id)

        put_request = client.put_requests[0]
        raw_get_response = client.raw_get_responses[0]
        assert len(client.put_requests) == len(client.get_requests) == 1
        assert saved.artifact_id == loaded.artifact_id == expected_id
        assert saved.session_id == loaded.session_id == "session-1"
        assert saved.size == loaded.size == len(body)
        assert saved.mime_type == loaded.mime_type == "text/plain; charset=utf-8"
        assert saved.content is None and loaded.content == content
        assert put_request["Bucket"] == bucket
        assert put_request["Key"] == f"session-1/{expected_id}"
        assert put_request["Body"] == body
        assert put_request["ContentType"] == "text/plain; charset=utf-8"
        assert client.get_requests[0] == {
            "Bucket": bucket,
            "Key": f"session-1/{expected_id}",
        }
        assert raw_get_response["Body"] == body
        assert raw_get_response["ContentType"] == "text/plain; charset=utf-8"

        assert sdk_session.client_calls
        for service, kwargs in sdk_session.client_calls:
            assert service == "s3"
            assert kwargs["endpoint_url"] == endpoint
            assert kwargs["aws_access_key_id"] == access
            assert kwargs["aws_secret_access_key"] == secret
            assert kwargs["config"].signature_version == "s3v4"
            assert kwargs["config"].s3 == {"addressing_style": "path"}

        if name == "s3":
            assert "Metadata" in put_request
            assert "Metadata" in raw_get_response
            persisted = json.loads(put_request["Metadata"]["artifact"])
            returned = json.loads(raw_get_response["Metadata"]["artifact"])
            assert persisted == returned
            assert persisted["artifact_id"] == expected_id
            assert persisted["session_id"] == "session-1"
            assert persisted["size"] == len(body)
            assert persisted["mime_type"] == "text/plain; charset=utf-8"
            assert persisted["source_tool"] == "shell"
            assert persisted["tool_call_id"] == "call-42"
            assert persisted["created_at"] == saved.created_at
            assert loaded.source_tool == "shell"
            assert loaded.tool_call_id == "call-42"
            assert loaded.created_at == saved.created_at
            assert datetime.fromisoformat(loaded.created_at) is not None
        else:
            assert "Metadata" not in put_request
            assert "Metadata" not in raw_get_response
            assert loaded.source_tool is None
            assert loaded.tool_call_id is None
            assert loaded.created_at is None

        observed[name] = (saved, loaded, put_request, raw_get_response)

    s3_saved, s3_loaded, s3_put, s3_get = observed["s3"]
    minio_saved, minio_loaded, minio_put, minio_get = observed["minio"]
    assert s3_put["Body"] == minio_put["Body"] == body
    assert s3_get["Body"] == minio_get["Body"] == body
    assert s3_saved.artifact_id == s3_loaded.artifact_id == expected_id
    assert minio_saved.artifact_id == minio_loaded.artifact_id == expected_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "store_type"),
    (("s3", S3ArtifactStore), ("minio", MinioArtifactStore)),
)
async def test_remote_artifact_load_rejects_hash_mismatch_and_propagates_access_denied(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    store_type: type[ArtifactStore],
) -> None:
    pytest.importorskip("botocore")
    from botocore.exceptions import ClientError

    settings, _endpoint, bucket, _access, _secret = provider_settings(provider)
    client = RecordingS3Client()
    sdk_session = RecordingSDKSession(client)
    monkeypatch.setitem(
        sys.modules, "aioboto3", SimpleNamespace(Session=lambda: sdk_session)
    )
    store = store_type(settings, session_id="session-1")
    saved = await store.save(
        "session-1", "integrity", mime_type="text/plain",
        source_tool="shell", tool_call_id="call-42",
    )
    key = (bucket, f"session-1/{saved.artifact_id}")

    client.objects[key]["Body"] = b"tampered"
    with pytest.raises(KeyError, match="hash mismatch"):
        await store.load(saved.artifact_id)

    client.objects[key]["Body"] = b"integrity"
    client.get_error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject"
    )
    with pytest.raises(ClientError) as error:
        await store.load(saved.artifact_id)
    assert error.value.response["Error"]["Code"] == "AccessDenied"
