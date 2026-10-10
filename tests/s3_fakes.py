"""S3 / MinIO SDK 替身（测试共用）。

`tests/web/test_artifacts_api.py` 与 `tests/attachments/test_byte_store_providers.py`
都需要一个"记录请求、不发网络"的 s3 客户端替身，好在离线断言真实 key / ContentType。
此前两份几乎逐字相同（将来会漂移——审查 P3-2），故收进这里一份。`content_type` 是
`get_object` 回包的 ContentType，两处按需传（web 侧的文本路径默认 text/plain，字节路径
用 image/png）；`put_object` 供字节路径写入断言 `Body` / `ContentType`。
"""

from __future__ import annotations

from typing import Self


class FakeBody:
    """最小 S3 body 替身（`async with response["Body"] as stream`）。"""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def read(self) -> bytes:
        return self._data


class FakeS3Client:
    """记录请求的 s3 客户端替身：不发网络，就能断言真实 key。"""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        payload: bytes = b"",
        content_type: str = "text/plain",
    ) -> None:
        self.requests: list[dict] = []
        self._error = error
        self._payload = payload
        self._content_type = content_type

    async def put_object(self, **kwargs: object) -> dict:
        self.requests.append(kwargs)
        return {}

    async def get_object(self, **kwargs: object) -> dict:
        self.requests.append(kwargs)
        if self._error is not None:
            raise self._error
        return {"Body": FakeBody(self._payload), "ContentType": self._content_type}


class FakeSDKSession:
    """aioboto3 `Session` 替身：`client(...)` 返回一个 async context manager。

    `client` 收 `object`（不锁死替身类型）：调用方会塞"会爆炸的客户端"来断言
    畸形 id 不发网络，那类替身与本文件的 `FakeS3Client` 不同型。
    """

    def __init__(self, client: object) -> None:
        self._client = client

    def client(self, _service: str, **_kwargs: object):
        client = self._client

        class _ClientCM:
            async def __aenter__(self) -> object:
                return client

            async def __aexit__(self, *exc: object) -> bool:
                return False

        return _ClientCM()


class FakeKeyedS3Client:
    """**按键存取**的 s3 替身：支持多个对象、缺键抛 `NoSuchKey`、记录请求顺序。

    与 `FakeS3Client` 的分工：那个是"一个固定回包"的最小替身（够断言单键的 key 形状），
    本类用于**多候选 key 的读回落**与**回执归属**用例——那时同一个 store 要先试
    `.attachments/objects/…`、再试 `{session}/attachments/…`，两次 `get_object`
    必须得到**两个不同的答案**，固定回包做不到。

    `not_found` 由调用方从被测 store 的 `_client_error` 构造（botocore `ClientError`
    形状：`{"Error": {"Code": "NoSuchKey"}}`）——本替身不 import botocore，保持离线。
    """

    def __init__(self, *, not_found: Exception, content_type: str = "image/png") -> None:
        self.objects: dict[str, bytes] = {}
        self.requests: list[dict] = []
        self._not_found = not_found
        self._content_type = content_type

    async def put_object(self, **kwargs: object) -> dict:
        self.requests.append(kwargs)
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.objects[str(kwargs["Key"])] = body
        return {}

    async def get_object(self, **kwargs: object) -> dict:
        self.requests.append(kwargs)
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise self._not_found
        return {
            "Body": FakeBody(self.objects[key]),
            "ContentType": self._content_type,
        }

    @property
    def keys(self) -> list[str]:
        """按请求顺序记录的 key（`put_object` 与 `get_object` 都在内）。"""
        return [str(request["Key"]) for request in self.requests]

    @property
    def put_keys(self) -> list[str]:
        return [str(r["Key"]) for r in self.requests if "Body" in r]


__all__ = [
    "FakeBody",
    "FakeKeyedS3Client",
    "FakeS3Client",
    "FakeSDKSession",
]
