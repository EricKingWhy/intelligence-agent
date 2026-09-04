"""Session-scoped S3 ArtifactStore；SDK 仅在构造 Provider 时加载。"""

import importlib
import json
import re
from datetime import UTC, datetime

from agent_harness.config import Settings
from agent_harness.storage.artifact import (
    Artifact,
    ArtifactSlice,
    ArtifactStore,
    _slice_lines,
    compute_artifact_id,
)


class S3ArtifactStore(ArtifactStore):
    """绑定 Session 命名空间，无需内存索引即可恢复 artifact_id 的 S3 key。"""

    def __init__(self, settings: Settings, *, session_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise ValueError("session_id must be a single safe key segment")
        if not all((settings.artifact_store_endpoint, settings.artifact_store_bucket,
                    settings.artifact_store_access_key, settings.artifact_store_secret_key,
                    settings.artifact_store_region)):
            raise ValueError("S3ArtifactStore requires artifact_store_* configuration")
        try:
            sdk = importlib.import_module("aioboto3")
        except ModuleNotFoundError as error:
            raise RuntimeError("S3ArtifactStore requires pip install 'intelligence-agent[artifact]'") from error
        self._sdk_session = sdk.Session()
        self._client_error = importlib.import_module("botocore.exceptions").ClientError
        config_type = importlib.import_module("botocore.config").Config
        self._session_id = session_id
        self._bucket = settings.artifact_store_bucket
        self._client_kwargs = {
            "endpoint_url": settings.artifact_store_endpoint,
            "region_name": settings.artifact_store_region,
            "aws_access_key_id": settings.artifact_store_access_key,
            "aws_secret_access_key": settings.artifact_store_secret_key,
            "config": config_type(signature_version="s3v4", s3={"addressing_style": "path"}),
        }

    async def save(
        self, session_id: str, content: str, *, mime_type: str,
        source_tool: str, tool_call_id: str,
    ) -> Artifact:
        if session_id != self._session_id:
            raise ValueError("save session_id must match the store namespace")
        body = content.encode("utf-8")
        artifact = Artifact(
            artifact_id=compute_artifact_id(content), session_id=session_id,
            size=len(body), mime_type=mime_type, source_tool=source_tool,
            tool_call_id=tool_call_id,
            created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        )
        async with self._sdk_session.client("s3", **self._client_kwargs) as client:
            await client.put_object(
                Bucket=self._bucket, Key=f"{session_id}/{artifact.artifact_id}",
                Body=body, ContentType=mime_type,
                Metadata={"artifact": json.dumps(artifact.model_dump(exclude={"content"}),
                                                  ensure_ascii=True)},
            )
        return artifact

    async def load(self, artifact_id: str) -> Artifact:
        if not re.fullmatch(r"[0-9a-f]{16}", artifact_id):
            raise KeyError(f"Artifact '{artifact_id}' does not exist")
        async with self._sdk_session.client("s3", **self._client_kwargs) as client:
            try:
                response = await client.get_object(
                    Bucket=self._bucket, Key=f"{self._session_id}/{artifact_id}",
                )
            except self._client_error as error:
                if error.response.get("Error", {}).get("Code") == "NoSuchKey":
                    raise KeyError(f"Artifact '{artifact_id}' does not exist") from error
                raise
            async with response["Body"] as stream:
                content = (await stream.read()).decode("utf-8")
        artifact = Artifact.model_validate_json(response["Metadata"]["artifact"])
        return artifact.model_copy(update={"content": content})

    async def inspect(
        self, artifact_id: str, *, start_line: int | None = None,
        end_line: int | None = None, keyword: str | None = None, max_lines: int = 200,
    ) -> ArtifactSlice:
        artifact = await self.load(artifact_id)
        assert artifact.content is not None
        all_lines = artifact.content.splitlines()
        lines, truncated = _slice_lines(
            all_lines, start_line=start_line, end_line=end_line,
            keyword=keyword, max_lines=max_lines,
        )
        return ArtifactSlice(
            artifact_id=artifact_id, lines=lines, total_lines=len(all_lines),
            returned_lines=len(lines), truncated=truncated,
            query={"start_line": start_line, "end_line": end_line,
                   "keyword": keyword, "max_lines": max_lines},
        )
