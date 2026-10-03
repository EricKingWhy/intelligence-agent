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

import enum
import getpass
import hashlib
import json
import os
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent_harness.model.provider_store import (
    Credentials,
    MemoryCredentialStore,
    SystemCredentialStore,
)

__all__ = [
    "AUTH_PROBE_PATH",
    "ENDPOINT_FILENAME",
    "HOST_CREDENTIALS_ENV",
    "HOST_PROTOCOL_VERSION",
    "AttachResult",
    "AttachStatus",
    "HostEndpointInfo",
    "HostTokenStore",
    "attach_probe",
    "clear_endpoint",
    "default_credentials",
    "host_token_username",
    "publish_endpoint",
    "read_endpoint",
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


class AttachStatus(str, enum.Enum):
    """附着核验结论（见模块 docstring 的四态语义）。"""

    ABSENT = "absent"
    STALE = "stale"
    AUTH_FAILED = "auth_failed"
    ATTACHABLE = "attachable"


@dataclass
class HostEndpointInfo:
    """端点状态文件载荷。**不得**加入任何秘密字段（token 走凭据通道）。"""

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
    """生产 = 系统凭据管理器；显式设 `AGENT_HARNESS_HOST_CREDENTIALS=memory` 走内存后端。"""
    if os.environ.get(HOST_CREDENTIALS_ENV, "").strip().lower() == "memory":
        return MemoryCredentialStore()
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


def _http_get_json(url: str, *, token: str | None, timeout: float) -> tuple[int, object]:
    request = urllib.request.Request(url)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


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
