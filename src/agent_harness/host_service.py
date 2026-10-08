"""本机唯一服务：端点发现状态文件 + token 限权通道 + 附着核验（W-11 / #355）。

同一用户数据根同时只有一处 Python 服务持有 `InstanceLock` 与 Session 写入权
（ARCH-7 / #150）；桌面 / TS TUI / 本机 Web 都附着到它。本模块提供附着协议的
客户端与发布面（语义来源见票面「方案依据」：DSH 单 Host 模型 PORT DESIGN +
Jupyter Server 默认 loopback + token 默认启用 PORT DESIGN，不复制代码）：

- **端点状态文件** `<workspace_dir>/.host-service.json`：服务就绪后原子写入
  pid / port / protocol_version / owner / started_at / service_uuid——**只含
  非秘密字段**（协议字段不暴露 secret）。附着方先读它，再用 loopback 套接字
  **证明**服务真的活着：进程死（或端口不复存在）时探针返回 STALE，冷启动路径
  据此才去竞争 `InstanceLock`——OS 锁随进程死自释放，所以「锁残留」从不强抢
  活服务；探针拒绝恰恰证明没有活服务可附着。
- **token 限权通道**：REUSE ADR-0032 §5 的 `Credentials` seam（Windows 上即
  当前用户的凭据管理器，按 OS 用户隔离）。username 以 `host-service/` 前缀 +
  数据根哈希命名空间化，同一台机器多个数据根互不串扰。token 不进 URL、不进
  状态文件、不进日志/诊断输出。
- **附着核验四态**：`ABSENT`（无状态文件）→ 可冷启动；`STALE`（端口拒绝 /
  健康面形状不符 / protocol_version 不匹配）→ 残留物，可冷启动；`AUTH_FAILED`
  （服务活着但本机通道里没有可用 token，如另一 OS 用户启动了服务）→ **禁止
  冷启动**——锁在活进程手里，强抢正是本票要挡的形状；`ATTACHABLE` → 只附着。

测试/CI 接缝：环境变量 `AGENT_HARNESS_HOST_CREDENTIALS=memory` 显式设置时用
内存凭据后端（生产不设，走系统凭据管理器）。这只是把「注入内存后端」的既有
测试姿势延伸到跨进程测试，不改变生产通道。
"""

from __future__ import annotations

import asyncio
import enum
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets as secrets_module
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_harness.instance_lock import InstanceLock, InstanceLockError
from agent_harness.model.provider_store import (
    Credentials,
    MemoryCredentialStore,
    SystemCredentialStore,
)

if TYPE_CHECKING:
    from agent_harness.config import Settings

__all__ = [
    "AUTH_PROBE_PATH",
    "DEFAULT_ATTACH_WAIT_SECONDS",
    "ENDPOINT_FILENAME",
    "HOST_CREDENTIALS_ENV",
    "HOST_PROTOCOL_VERSION",
    "HOST_SKILLS_NONCE_HEADER",
    "HOST_SKILLS_PROOF_HEADER",
    "HOST_SKILLS_PROOF_WINDOW_SECONDS",
    "HOST_SKILLS_RESPONSE_PROOF_HEADER",
    "HOST_SKILLS_TIMESTAMP_HEADER",
    "HOST_TOKEN_TTL_SECONDS",
    "AttachResult",
    "AttachStatus",
    "FileCredentialStore",
    "HostEndpointInfo",
    "HostServiceError",
    "HostTokenStore",
    "ServeOutcome",
    "attach_probe",
    "clear_endpoint",
    "default_credentials",
    "describe_lock_error_with_attach",
    "ensure_service",
    "host_skills_request_proof",
    "host_skills_response_proof",
    "host_token_username",
    "publish_endpoint",
    "query_attached_skill_catalog",
    "read_endpoint",
    "serve_once",
]

#: 附着协议版本：健康面形状或端口语义破坏性变更时递增（探针按它拒绝旧形状）。
HOST_PROTOCOL_VERSION = 1

#: 服务在数据根下发布的端点状态文件（非秘密字段）。
ENDPOINT_FILENAME = ".host-service.json"

#: 凭据管理器里 host token 的 username 命名空间前缀（service 复用 ADR-0032 坐标）。
_TOKEN_USERNAME_PREFIX = "host-service/"

#: 跨进程测试注入内存凭据后端的环境变量（生产不设）。
HOST_CREDENTIALS_ENV = "AGENT_HARNESS_HOST_CREDENTIALS"

#: 附着核验用的**已认证**探测端点：既有路由里最便宜的一条只读 GET。
AUTH_PROBE_PATH = "/api/sessions?limit=1"

#: loopback 连接/HTTP 探测超时（秒）。
PROBE_TIMEOUT_SECONDS = 2.0

#: serve 冷启动输家的「二次检查」窗口（env `AGENT_HARNESS_ATTACH_WAIT_SECONDS` 可覆盖）。
DEFAULT_ATTACH_WAIT_SECONDS = 15.0

#: 服务签发的身份 token 有效期（本地个人服务；过期后附着探针按 AUTH_FAILED 处理，
#: 重启服务轮换——token 只在限权通道里，泄露面就是当前 OS 用户）。
HOST_TOKEN_TTL_SECONDS = 7 * 24 * 3600

#: 锁竞争输家二次检查的轮询间隔（秒）。
_ATTACH_POLL_INTERVAL = 0.25

# Bound local JSON reads so a stale or unexpected loopback service cannot force
# an unbounded allocation during attach probing.
_MAX_JSON_RESPONSE_BYTES = 8 * 1024 * 1024
HOST_SKILLS_NONCE_HEADER = "X-Agent-Harness-Nonce"
HOST_SKILLS_PROOF_HEADER = "X-Agent-Harness-Proof"
HOST_SKILLS_TIMESTAMP_HEADER = "X-Agent-Harness-Timestamp"
HOST_SKILLS_RESPONSE_PROOF_HEADER = "X-Agent-Harness-Response-Proof"
_HOST_SKILLS_NONCE_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_HOST_SKILLS_SERVICE_UUID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_HOST_SKILLS_TIMESTAMP_PATTERN = re.compile(r"[0-9]{1,12}\Z")
HOST_SKILLS_PROOF_WINDOW_SECONDS = 30


def host_skills_request_proof(
    token: str, nonce: str, service_uuid: str, timestamp: str
) -> str:
    """Authenticate a one-use catalog query without transmitting the host bearer token."""
    _validate_host_skills_proof_fields(nonce, service_uuid, timestamp)
    message = (
        b"agent-harness-host:v1\nGET\n/api/skills\n"
        + nonce.encode("ascii")
        + b"\n"
        + service_uuid.encode("ascii")
        + b"\n"
        + timestamp.encode("ascii")
    )
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def host_skills_response_proof(
    token: str,
    nonce: str,
    service_uuid: str,
    timestamp: str,
    status: int,
    body: bytes,
) -> str:
    """Bind an authenticated catalog response to its request challenge and body."""
    _validate_host_skills_proof_fields(nonce, service_uuid, timestamp)
    body_digest = hashlib.sha256(body).hexdigest().encode("ascii")
    message = (
        b"agent-harness-host:v1\nRESPONSE\nGET\n/api/skills\n"
        + nonce.encode("ascii")
        + b"\n"
        + service_uuid.encode("ascii")
        + b"\n"
        + timestamp.encode("ascii")
        + b"\n"
        + str(status).encode("ascii")
        + b"\n"
        + body_digest
    )
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _validate_host_skills_proof_fields(nonce: str, service_uuid: str, timestamp: str) -> None:
    if not _HOST_SKILLS_NONCE_PATTERN.fullmatch(nonce):
        raise ValueError("host Skills challenge nonce must be 32 random bytes in hex")
    if not _HOST_SKILLS_SERVICE_UUID_PATTERN.fullmatch(service_uuid):
        raise ValueError("host Skills proof requires a 16-byte service UUID in hex")
    if not _HOST_SKILLS_TIMESTAMP_PATTERN.fullmatch(timestamp):
        raise ValueError("host Skills proof timestamp must be decimal Unix seconds")


class HostServiceError(RuntimeError):
    """本机服务在运行但附着被拒（如 token 属于另一 OS 用户）——禁止冷启动强抢。"""


class FileCredentialStore(Credentials):
    """测试/CI 专用跨进程凭据后端（`AGENT_HARNESS_HOST_CREDENTIALS=file:<path>`）。

    仅测试环境显式选择；生产默认走系统凭据管理器（见 `default_credentials`）。
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def available(self) -> bool:
        return True

    def _load(self) -> dict[str, str]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(f".tmp-{uuid.uuid4().hex}")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def set(self, username: str, password: str) -> None:
        data = self._load()
        data[username] = password
        self._save(data)

    def get(self, username: str) -> str | None:
        return self._load().get(username)

    def delete(self, username: str) -> None:
        data = self._load()
        if username in data:
            data.pop(username)
            self._save(data)


class AttachStatus(str, enum.Enum):
    """附着核验结论（见模块 docstring 的四态语义）。"""

    ABSENT = "absent"
    STALE = "stale"
    AUTH_FAILED = "auth_failed"
    ATTACHABLE = "attachable"


@dataclass
class HostEndpointInfo:
    """端点状态文件载荷。**不得**加入任何秘密字段（token 走凭据通道）。

    `pid` **仅供诊断**（与 `InstanceLock` 锁文件里的 pid 同一哲学）：Windows 上
    venv 启动器存在进程代际，实际服务进程 pid 与启动者 spawn 的直子 pid 可能
    不一致；附着身份的实证 = spawn 后新铸的 token（限权通道）+ 健康面形状 +
    鉴权面，三者全过才算 ATTACHABLE。
    """

    pid: int
    port: int
    protocol_version: int = HOST_PROTOCOL_VERSION
    auth_required: bool = True
    owner: str = ""
    started_at: str = ""
    service_uuid: str = ""

    def __post_init__(self) -> None:
        if not self.owner:
            self.owner = getpass.getuser()
        if not self.started_at:
            self.started_at = datetime.now(UTC).isoformat()
        if not self.service_uuid:
            self.service_uuid = uuid.uuid4().hex


@dataclass
class AttachResult:
    """`attach_probe` 的结论。`detail` 面向日志/排查，不含 token。"""

    status: AttachStatus
    endpoint: HostEndpointInfo | None
    detail: str = ""


def default_credentials() -> Credentials:
    """生产 = 系统凭据管理器；显式设 `AGENT_HARNESS_HOST_CREDENTIALS` 时走测试接缝。

    `memory` → 进程内存后端（同进程测试）；`file:<path>` → 跨进程文件后端
    （serve 子进程测试父子共享）；不设 → 系统凭据管理器。
    """
    value = os.environ.get(HOST_CREDENTIALS_ENV, "").strip()
    if value == "memory":
        return MemoryCredentialStore()
    if value.startswith("file:"):
        return FileCredentialStore(value[len("file:") :].strip())
    return SystemCredentialStore()


def _endpoint_path(root: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.fspath(root))) / ENDPOINT_FILENAME


def publish_endpoint(root: str | os.PathLike[str], info: HostEndpointInfo) -> None:
    """原子发布端点状态文件（临时文件 + `os.replace`，不留半文件）。"""
    path = _endpoint_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    try:
        tmp.write_text(
            json.dumps(asdict(info), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_endpoint(root: str | os.PathLike[str]) -> HostEndpointInfo | None:
    """读端点状态文件；不存在 / 损坏 / 形状不符都返回 `None`（按无服务处理）。"""
    try:
        payload = json.loads(_endpoint_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        info = HostEndpointInfo(
            pid=int(payload["pid"]),
            port=int(payload["port"]),
            protocol_version=int(payload.get("protocol_version", 0)),
            auth_required=bool(payload.get("auth_required", True)),
            owner=str(payload.get("owner", "")),
            started_at=str(payload.get("started_at", "")),
            service_uuid=str(payload.get("service_uuid", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return info


def clear_endpoint(root: str | os.PathLike[str]) -> None:
    """清除端点状态文件（幂等）。"""
    try:
        _endpoint_path(root).unlink(missing_ok=True)
    except OSError:
        pass


def host_token_username(root: str | os.PathLike[str]) -> str:
    """host token 在凭据管理器里的 username：命名空间 + 数据根哈希。

    用 realpath + normcase 归一（与 `instance_lock._key_for` 同一姿势）——同一
    数据根的不同路径拼写必须落到同一条凭据上。
    """
    real = os.path.normcase(os.path.realpath(os.fspath(root)))
    digest = hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]
    return f"{_TOKEN_USERNAME_PREFIX}{digest}"


class HostTokenStore:
    """host 服务握手 token 的限权本机通道（REUSE `Credentials` seam）。

    token 存当前 OS 用户的凭据管理器（ADR-0032 §5）；另一 OS 用户读不到 ⇒
    「错用户附着失败」的语义由通道本身保证，探针把「服务活着但取不到/用不了
    token」归类 `AUTH_FAILED` 而不是 STALE。
    """

    def __init__(self, credentials: Credentials | None = None) -> None:
        self._credentials = credentials if credentials is not None else default_credentials()

    def set_token(self, root: str | os.PathLike[str], token: str) -> None:
        self._credentials.set(host_token_username(root), token)

    def get_token(self, root: str | os.PathLike[str]) -> str | None:
        return self._credentials.get(host_token_username(root))

    def clear_token(self, root: str | os.PathLike[str]) -> None:
        self._credentials.delete(host_token_username(root))


#: loopback 专用 opener：显式禁用系统代理——urlopen 默认读 HTTP_PROXY/注册表
#: 代理，本机回环探测被代理转发会被重置（实测 WinError 10054），附着核验
#: 绝不允许经第三方代理。
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _read_bounded_body(response: Any) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise ValueError("local service returned an invalid Content-Length") from error
        if declared_length < 0 or declared_length > _MAX_JSON_RESPONSE_BYTES:
            raise ValueError("local service JSON response exceeds size limit")
    body = response.read(_MAX_JSON_RESPONSE_BYTES + 1)
    if len(body) > _MAX_JSON_RESPONSE_BYTES:
        raise ValueError("local service JSON response exceeds size limit")
    return body


async def _loopback_get_bytes_with_deadline(
    port: int,
    path: str,
    *,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    """GET one fixed loopback route with an absolute deadline and bounded body."""
    async with asyncio.timeout(timeout):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            request_headers = {
                "Host": "127.0.0.1",
                "Accept": "application/json",
                "Connection": "close",
                **headers,
            }
            request = f"GET {path} HTTP/1.1\r\n" + "".join(
                f"{name}: {value}\r\n" for name, value in request_headers.items()
            ) + "\r\n"
            writer.write(request.encode("ascii"))
            await writer.drain()

            status_line = await reader.readline()
            if len(status_line) > 8192 or not status_line.endswith(b"\r\n"):
                raise ValueError("local service returned an invalid HTTP status line")
            parts = status_line[:-2].decode("ascii").split(" ", 2)
            if len(parts) < 2 or not parts[0].startswith("HTTP/"):
                raise ValueError("local service returned an invalid HTTP status line")
            status = int(parts[1])

            response_headers: dict[str, str] = {}
            header_bytes = 0
            while True:
                line = await reader.readline()
                header_bytes += len(line)
                if header_bytes > 64 * 1024 or not line.endswith(b"\r\n"):
                    raise ValueError("local service returned invalid or oversized HTTP headers")
                if line == b"\r\n":
                    break
                try:
                    name, value = line[:-2].decode("latin-1").split(":", 1)
                except ValueError as error:
                    raise ValueError("local service returned an invalid HTTP header") from error
                key = name.strip().lower()
                if key in response_headers:
                    raise ValueError("local service returned duplicate HTTP headers")
                response_headers[key] = value.strip()
            if "transfer-encoding" in response_headers:
                raise ValueError("local service returned an unsupported transfer encoding")
            try:
                body_size = int(response_headers["content-length"])
            except (KeyError, ValueError) as error:
                raise ValueError("local service returned an invalid Content-Length") from error
            if body_size < 0 or body_size > _MAX_JSON_RESPONSE_BYTES:
                raise ValueError("local service JSON response exceeds size limit")
            body = await reader.readexactly(body_size)
            return status, response_headers, body
        finally:
            writer.close()


def _http_get_json(url: str, *, token: str | None, timeout: float) -> tuple[int, object]:
    request = urllib.request.Request(url)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    with _LOOPBACK_OPENER.open(request, timeout=timeout) as response:
        return response.status, json.loads(_read_bounded_body(response).decode("utf-8"))


def query_attached_skill_catalog(
    root: str | os.PathLike[str],
    *,
    credentials: Credentials | None = None,
    connect_timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Read the live Skills catalog using nonce HMACs, never sending the bearer token.

    Returns ``None`` when the endpoint is absent, stale, older than the challenge
    extension, unauthenticated, or returns an invalidly signed response.
    """
    endpoint = read_endpoint(root)
    if endpoint is None or not endpoint.auth_required:
        return None
    if not _HOST_SKILLS_SERVICE_UUID_PATTERN.fullmatch(endpoint.service_uuid):
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return None
    deadline = time.monotonic() + connect_timeout
    try:
        health_status, _, health_body = asyncio.run(
            _loopback_get_bytes_with_deadline(
                endpoint.port, "/api/health", headers={}, timeout=max(0.001, deadline - time.monotonic())
            )
        )
        health = json.loads(health_body.decode("utf-8"))
    except (OSError, ValueError, TimeoutError, EOFError, RecursionError):
        return None
    if (
        health_status != 200
        or not isinstance(health, dict)
        or health.get("status") != "ok"
        or health.get("protocol_version") != HOST_PROTOCOL_VERSION
    ):
        return None

    token = HostTokenStore(credentials).get_token(root)
    if not token:
        return None
    nonce = secrets_module.token_hex(32)
    timestamp = str(int(time.time()))
    proof_headers = {
        HOST_SKILLS_NONCE_HEADER: nonce,
        HOST_SKILLS_TIMESTAMP_HEADER: timestamp,
        HOST_SKILLS_PROOF_HEADER: host_skills_request_proof(
            token, nonce, endpoint.service_uuid, timestamp
        ),
    }
    try:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            return None
        status, response_headers, body = asyncio.run(
            _loopback_get_bytes_with_deadline(
                endpoint.port, "/api/skills", headers=proof_headers, timeout=timeout
            )
        )
        response_proof = response_headers.get(HOST_SKILLS_RESPONSE_PROOF_HEADER.lower(), "")
        expected_proof = host_skills_response_proof(
            token, nonce, endpoint.service_uuid, timestamp, status, body
        )
        if status != 200 or not hmac.compare_digest(response_proof, expected_proof):
            return None
        payload = json.loads(body.decode("utf-8"))
    except (OSError, ValueError, TimeoutError, EOFError, RecursionError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("skills"), list):
        return None
    return payload


def attach_probe(
    root: str | os.PathLike[str],
    *,
    credentials: Credentials | None = None,
    connect_timeout: float = PROBE_TIMEOUT_SECONDS,
) -> AttachResult:
    """核验数据根上是否有**可附着**的本机服务（四态，见模块 docstring）。

    只读 + loopback：探针从不写状态、从不碰锁——冷启动与否由调用方按结论决定。
    """
    endpoint = read_endpoint(root)
    if endpoint is None:
        return AttachResult(AttachStatus.ABSENT, None, "端点状态文件不存在或不可读")

    # 第一步：loopback TCP 能连上才谈得上「活服务」。端口拒绝/超时 = 残留物。
    try:
        with socket.create_connection(("127.0.0.1", endpoint.port), timeout=connect_timeout):
            pass
    except OSError as error:
        return AttachResult(
            AttachStatus.STALE, endpoint, f"127.0.0.1:{endpoint.port} 连接失败（{error.__class__.__name__}）"
        )

    # 第二步：健康面必须是我们协议形状的服务（而非恰好占用同端口的别的程序）。
    try:
        status, payload = _http_get_json(
            f"http://127.0.0.1:{endpoint.port}/api/health", token=None, timeout=connect_timeout
        )
    except (OSError, ValueError) as error:
        return AttachResult(
            AttachStatus.STALE, endpoint, f"健康面请求失败（{error.__class__.__name__}）"
        )
    if status != 200 or not isinstance(payload, dict) or payload.get("status") != "ok":
        return AttachResult(AttachStatus.STALE, endpoint, f"健康面形状不符（HTTP {status}）")
    if payload.get("protocol_version") != HOST_PROTOCOL_VERSION:
        return AttachResult(
            AttachStatus.STALE,
            endpoint,
            f"协议版本不符（服务 {payload.get('protocol_version')} != 本端 {HOST_PROTOCOL_VERSION}）",
        )

    if not endpoint.auth_required:
        # 协议上 auth_required 恒 True；出现 False 的载荷说明是异常/伪造状态。
        return AttachResult(AttachStatus.STALE, endpoint, "端点声称无需鉴权（协议外形状）")

    # 第三步：用限权通道里的 token 打一条已认证探测路由。取不到 token 或被 401
    # 都是 AUTH_FAILED——服务活着，冷启动路径必须停（锁在活进程手里）。
    token = HostTokenStore(credentials).get_token(root)
    if not token:
        return AttachResult(
            AttachStatus.AUTH_FAILED, endpoint, "本机凭据通道中没有该数据根的 host token"
        )
    try:
        status, _ = _http_get_json(
            f"http://127.0.0.1:{endpoint.port}{AUTH_PROBE_PATH}", token=token, timeout=connect_timeout
        )
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return AttachResult(
                AttachStatus.AUTH_FAILED, endpoint, f"已认证探测被拒（HTTP {error.code}）"
            )
        return AttachResult(
            AttachStatus.AUTH_FAILED, endpoint, f"已认证探测失败（HTTP {error.code}）"
        )
    except (OSError, ValueError) as error:
        return AttachResult(
            AttachStatus.AUTH_FAILED, endpoint, f"已认证探测失败（{error.__class__.__name__}）"
        )
    if status != 200:
        return AttachResult(AttachStatus.AUTH_FAILED, endpoint, f"已认证探测失败（HTTP {status}）")
    return AttachResult(AttachStatus.ATTACHABLE, endpoint, "健康面 + 鉴权均通过")


# ── 服务面：serve 冷启动闭环（S2） ───────────────────────────────────────────


@dataclass
class ServeOutcome:
    """`serve_once` 的结论：`served=True` = 本进程就是服务；否则为附着既有服务后
    的干净退出（`endpoint` = 发现的既有服务）。"""

    served: bool
    endpoint: HostEndpointInfo | None = None


def _attach_wait_seconds() -> float:
    raw = os.environ.get("AGENT_HARNESS_ATTACH_WAIT_SECONDS", "").strip()
    try:
        return float(raw) if raw else DEFAULT_ATTACH_WAIT_SECONDS
    except ValueError:
        return DEFAULT_ATTACH_WAIT_SECONDS


def serve_once(
    settings: Settings,
    *,
    credentials: Credentials | None = None,
    host: str = "127.0.0.1",
    stop_when: Callable[[], bool] | None = None,
) -> ServeOutcome:
    """单机服务的冷启动闭环（阻塞直到停机）。

    竞态闭环（W-11 工作指令 1）：先试锁（竞争启动）；锁在别人手里 ⇒ **二次检查**
    ——窗口内轮询附着探针，发现赢家就附着退出（绝不另起第二个写者）；窗口内
    探针 AUTH_FAILED ⇒ 服务活着但本凭据不可用，响亮拒绝；始终 ABSENT/STALE ⇒
    锁在非服务写者（如在途 CLI 命令）手里，原样抛 `InstanceLockError`。

    拿到锁后：绑定 `127.0.0.1` 随机端口（`port=0` 受管）→ 生成/沿用 jwt 密钥 →
    签发限权身份 token 进凭据通道 → 发布端点文件 → 用与附着方**同一条探针**做
    启动自验 → 就绪行打到 stdout（只含端口，无 token）。**拿到锁之后的每一段
    都在 try/finally 里**：任何启动/运行异常都会清端点 + 清 token + 放锁
    （审查 P3 修复：此前装配段在 try 外，异常靠进程退出兜底）。

    `stop_when` 是轮询停机谓词（嵌入方/测试用；CLI 传 None 靠 KeyboardInterrupt）。
    本机个人服务不开宽松 CORS（`enable_cors=False`）：本地 UI 同源/桌面壳加载，
    Vite dev 直连属显式开发配置，走 `create_app` 直调路径。
    """
    root = settings.workspace_dir
    creds = credentials if credentials is not None else default_credentials()
    token_store = HostTokenStore(creds)
    try:
        lock = InstanceLock(root).acquire()
    except InstanceLockError as error:
        deadline = time.monotonic() + _attach_wait_seconds()
        last: AttachResult | None = None
        while time.monotonic() < deadline:
            last = attach_probe(root, credentials=creds)
            if last.status is AttachStatus.ATTACHABLE:
                return ServeOutcome(served=False, endpoint=last.endpoint)
            time.sleep(_ATTACH_POLL_INTERVAL)
        if last is not None and last.status is AttachStatus.AUTH_FAILED and last.endpoint:
            raise HostServiceError(
                f"本机服务已在 127.0.0.1:{last.endpoint.port} 运行，但当前用户没有可用的"
                f" host token（凭据通道可能属于另一 OS 用户）：{last.detail}。"
                "已拒绝另起第二个写者。"
            ) from error
        raise  # 原始 InstanceLockError：锁在非服务写者手里，且无服务可附着

    import jwt
    import uvicorn
    from pydantic import SecretStr

    from agent_harness.web.app import create_app

    endpoint: HostEndpointInfo | None = None
    configured = settings.jwt_secret.get_secret_value() if settings.jwt_secret else ""
    secret = configured if configured.strip() else secrets_module.token_hex(32)
    effective = settings.model_copy(update={"jwt_secret": SecretStr(secret)})
    now = int(time.time())
    service_uuid = uuid.uuid4().hex
    token = jwt.encode(
        {
            "tenant_id": "local",
            "user_id": getpass.getuser(),
            "scopes": ["user", "session"],
            "service_uuid": service_uuid,
            "iat": now,
            "exp": now + HOST_TOKEN_TTL_SECONDS,
        },
        secret,
        algorithm="HS256",
    )

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(effective, enable_cors=False, host_service_token=token),
            host=host,
            port=0,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, name="agent-harness-host-service", daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if not thread.is_alive():
                raise RuntimeError("服务线程在启动阶段退出（端口占用/装配失败，见日志）")
            if server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("服务 60s 未完成启动")
        port = int(server.servers[0].sockets[0].getsockname()[1])
        endpoint = HostEndpointInfo(pid=os.getpid(), port=port, service_uuid=service_uuid)
        token_store.set_token(root, token)
        publish_endpoint(root, endpoint)
        # 启动自验：与附着方完全同一条探针（健康面形状 + 限权 token 鉴权）。
        verify_deadline = time.monotonic() + 10.0
        while True:
            probe = attach_probe(root, credentials=creds)
            if probe.status is AttachStatus.ATTACHABLE:
                break
            if time.monotonic() > verify_deadline:
                raise RuntimeError(f"启动自验失败：{probe.detail}")
            time.sleep(0.1)
        print(f"HOST_SERVICE_READY http://127.0.0.1:{port}", flush=True)
        while (
            not server.should_exit
            and thread.is_alive()
            and not (stop_when is not None and stop_when())
        ):
            time.sleep(0.2)
    finally:
        clear_endpoint(root)
        token_store.clear_token(root)
        if not server.should_exit:
            server.should_exit = True
        if thread.is_alive():
            thread.join(timeout=10)
        lock.release()
    return ServeOutcome(served=True, endpoint=endpoint)


def ensure_service(
    root: str | os.PathLike[str],
    *,
    credentials: Credentials | None = None,
    spawn: Callable[[], Any],
    timeout: float = 60.0,
    poll_interval: float = 0.25,
) -> HostEndpointInfo:
    """桌面/TUI/Web 冷启动入口消费的原语：附着 → 否则冷启动 → 二次检查。

    服务已可附着 ⇒ 直接返回端点（绝不调用 `spawn`）；否则冷启动（spawn）并轮询
    等待任一可达服务——自己输掉锁竞争而赢家就绪时，二次检查同样发现赢家。
    超时抛 `RuntimeError`（带最后一次探针 detail 与冷启动进程退出码）。
    """
    creds = credentials if credentials is not None else default_credentials()
    result = attach_probe(root, credentials=creds)
    if result.status is AttachStatus.ATTACHABLE and result.endpoint is not None:
        return result.endpoint
    proc = spawn()
    deadline = time.monotonic() + timeout
    last = result
    while time.monotonic() < deadline:
        last = attach_probe(root, credentials=creds)
        if last.status is AttachStatus.ATTACHABLE and last.endpoint is not None:
            return last.endpoint
        time.sleep(poll_interval)
    detail = f"冷启动后 {timeout:.0f}s 内没有可附着的服务：{last.detail}"
    rc = proc.poll() if proc is not None and hasattr(proc, "poll") else None
    if rc is not None:
        detail += f"（冷启动进程已退出 rc={rc}）"
    raise RuntimeError(detail)


def describe_lock_error_with_attach(root: str | os.PathLike[str], error: InstanceLockError) -> str:
    """CLI 旧锁报错的附着感知文案：能发现活服务就指向它，不给「再起一个」的暗示。"""
    try:
        result = attach_probe(root, credentials=default_credentials())
    except (OSError, ValueError):  # 探针自身故障不得吞掉原始锁错误
        return str(error)
    if result.status is AttachStatus.ATTACHABLE and result.endpoint is not None:
        return (
            f"{error}\n"
            f"  已有本机服务在运行：http://127.0.0.1:{result.endpoint.port}"
            "—— 请附着该服务（同一数据根不允许第二个写者）。"
        )
    return str(error)
