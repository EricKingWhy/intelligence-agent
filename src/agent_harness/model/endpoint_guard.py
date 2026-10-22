"""模型端点误配置守卫（#1485 / SEC-07，裁决 25 **B**）：默认拦云元数据与 link-local。

裁决 B 而非 C：所有者直连 loopback / 内网照旧可用。本守卫**只**拦两类目标——

1. 云元数据端点：``169.254.0.0/16``（RFC 3927 link-local，含 ``169.254.169.254``、
   ECS ``169.254.170.2``、EKS Pod Identity ``169.254.170.23``）、阿里云
   ``100.100.100.200``、AWS IMDS IPv6 ``fd00:ec2::/32``；
2. IPv6 link-local ``fe80::/10``（含 OpenStack Nova ``fe80::a9fe:a9fe``）。

RFC1918 / loopback / ``198.18.0.0/15`` 一律放行。``198.18/15`` 尤其不能拦：沙箱的
通配 DNS 会把**任意**主机名解析进该段（实测 ``api.example.com`` → 198.18.222.105），
照搬 ``langchain_core`` 默认策略（= 裁决 C 的全私网拦截）会让本机所有模型调用假红。

两层执行点：

- **保存期**（``ProviderStore``）：只判 IP 字面量与元数据主机名，**不做 DNS**——
  保存期解析只会拿到 ``198.18.x.x`` 之类，既判不出真伪又拖慢保存。
- **请求期**（``create_chat_model`` 注入的 httpx2 客户端 request hook）：httpx2
  ``_send_handling_redirects`` 的循环体**首行**就是 request hook，因此**每一跳**
  重定向发出前都会过一遍——先解析主机名，再逐个解析结果判；拒绝时请求根本没到
  transport，**零请求、零凭据**（Authorization 头不会离开进程）。

所有者例外只来自 ``Settings.model_endpoint_allowlist``（env / .env）。Web 请求体是
``ConfigDict(extra="forbid")``，外部输入无法设置或改写它，也就不能自行把某个地址
"晋升"为可信端点，更借不到已存凭据。

来源（REUSE 取值与思路，不引依赖）：blocklist 成员、IPv4-mapped / NAT64 拆解方式取自
``langchain_core/_security/_policy.py``。不直接 import 的两个理由：其模块注释明示是
**internal、非公开 API**（可随时改动或移除）；其默认策略 ``block_private_ips=True``
``block_localhost=True`` 正是本票裁决**明确否决**的 C 方案，且 ``SSRFSafeTransport``
是 httpx 实现，与本仓 openai 3.0.0 使用的 httpx2 客户端不兼容。

残余风险（诚实登记）：本守卫判"解析器给出的地址"，不是"实际连上的地址"——httpx2
连接时会再解析一次，理论上存在 DNS rebinding 的 TOCTOU 窗口。彻底封堵需要自建
pin-IP transport（复制一份 ``SSRFSafeTransport`` 到 httpx2），超出"轻量防误配"的
本票范围；见完工报告"残余风险"。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

import httpx2
from openai import DefaultAsyncHttpxClient, DefaultHttpxClient

#: 拦截网段。只放云元数据 / link-local——**不放** RFC1918、loopback、198.18/15。
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),  # RFC 3927 link-local（含 169.254.169.254）
    ipaddress.ip_network("100.100.100.200/32"),  # 阿里云元数据
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local（含 fe80::a9fe:a9fe）
    ipaddress.ip_network("fd00:ec2::/32"),  # AWS IMDS IPv6
)

#: NAT64 well-known prefix：``64:ff9b::a9fe:a9fe`` 这类形态要取出内嵌 IPv4 再判。
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")

#: 云元数据主机名（大小写不敏感；末尾点先剥掉）。
_METADATA_HOSTNAMES = frozenset(
    {"metadata.google.internal", "metadata.amazonaws.com", "metadata", "instance-data"}
)


class EndpointBlockedError(RuntimeError):
    """目标被误配置守卫拒绝——请求未发出，凭据未发送。"""


class _GuardedSyncClient(DefaultHttpxClient):  # type: ignore[misc]
    """`openai.DefaultHttpxClient` + GC 时关闭（SDK 自带 wrapper 未导出，见下）。"""

    def __del__(self) -> None:
        try:
            if not self.is_closed:
                self.close()
        except Exception:  # noqa: BLE001, S110 — 析构期不能抛
            pass


class _GuardedAsyncClient(DefaultAsyncHttpxClient):  # type: ignore[misc]
    """同上，异步版（openai 的 `AsyncHttpxClientWrapper` 同款实现）。"""

    def __del__(self) -> None:
        try:
            if not self.is_closed:
                asyncio.get_running_loop().create_task(self.aclose())
        except Exception:  # noqa: BLE001, S110
            pass


def parse_allowlist(raw: str) -> frozenset[str]:
    """``MODEL_ENDPOINT_ALLOWLIST``（逗号分隔 ``host`` 或 ``host:port``）→ 条目集合。

    条目原样保留，匹配在 :func:`_is_allowed` 里做（同一份解析规则，不抄第二份）。
    """
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _split_entry(entry: str) -> tuple[str, int | None]:
    """``host`` / ``host:port`` / ``[v6]:port`` → (裸 host, port)。

    IPv6 带端口必须写方括号（``[fe80::1]:8080``）；裸 ``fe80::1`` 会先撞上
    ``urlsplit`` 的端口解析，按"整串都是 host"处理。
    """
    try:
        parts = urlsplit("//" + entry)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return entry.strip().strip("[]").rstrip(".").lower(), None
    if not host:
        return entry.strip().strip("[]").rstrip(".").lower(), None
    return host.rstrip(".").lower(), port


def _is_allowed(host: str, port: int | None, allow: Iterable[str]) -> bool:
    """所有者例外：host 相同且（条目无端口 或 端口相同）。"""
    wanted = host.strip().strip("[]").rstrip(".").lower()
    for entry in allow:
        entry_host, entry_port = _split_entry(entry)
        if entry_host != wanted:
            continue
        if entry_port is None or entry_port == port:
            return True
    return False


def _address_rejection(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> str | None:
    """单个地址是否落在拦截面；返回人读原因（None = 放行）。"""
    if isinstance(address, ipaddress.IPv6Address):
        # IPv4-mapped（::ffff:169.254.169.254）在 ipaddress 里**不**命中 IPv4 网段，
        # 必须取出内嵌 v4 再判（实测：in 169.254.0.0/16 为 False）。
        mapped = address.ipv4_mapped
        if mapped is not None:
            return _address_rejection(mapped)
        if address in _NAT64_PREFIX:
            return _address_rejection(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    for network in _BLOCKED_NETWORKS:
        if address.version == network.version and address in network:
            return f"{address} 属于 {network}"
    return None


def _host_rejection(host: str) -> str | None:
    """主机名/IP 字面量的**本地**判据（不解析 DNS）。"""
    name = host.strip().strip("[]").rstrip(".").lower()
    if not name:
        return None
    if name in _METADATA_HOSTNAMES:
        return f"云元数据主机名 {name}"
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return None
    return _address_rejection(address)


def url_rejection(url: str, *, allow: Iterable[str] = ()) -> str | None:
    """保存期判据：URL 的 host 是拦截面目标则返回原因（不解析 DNS）。

    URL 本身不可解析（scheme/端口坏）时返回 None——那是 ``validate_base_url``
    的责任域，不在本守卫报错。
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    if _is_allowed(host, port, allow):
        return None
    return _host_rejection(host)


def _blocked_error(host: str, reason: str) -> EndpointBlockedError:
    return EndpointBlockedError(
        f"目标被误配置守卫拒绝：{host}（{reason}）。"
        "如确为所有者自用端点，请在 MODEL_ENDPOINT_ALLOWLIST 显式登记该端点"
        "（host 或 host:port）"
    )


def _resolved_addresses(host: str, port: int | None) -> list[str]:
    """同步解析（测试用它注入受控解析结果）。"""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _aresolved_addresses(host: str, port: int | None) -> list[str]:
    """异步解析（不阻塞事件循环）。"""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _rejection_for(host: str, port: int | None, addresses: list[str]) -> str | None:
    for raw in addresses:
        reason = _host_rejection(raw)
        if reason is not None:
            return f"解析结果 {raw} 属于拦截面（{reason}）"
    return None


def _check_request(request: httpx2.Request, allow: frozenset[str]) -> str | None:
    """单跳请求的判据；返回拒绝原因（None = 放行）。"""
    host = request.url.host
    port = request.url.port
    if not host or _is_allowed(host, port, allow):
        return None
    literal = _host_rejection(host)
    if literal is not None:
        return literal
    try:
        addresses = _resolved_addresses(host, port)
    except OSError:
        # 解析失败交给真实连接报"连不上"，本守卫不越俎代庖。
        return None
    return _rejection_for(host, port, addresses)


async def _acheck_request(request: httpx2.Request, allow: frozenset[str]) -> str | None:
    host = request.url.host
    port = request.url.port
    if not host or _is_allowed(host, port, allow):
        return None
    literal = _host_rejection(host)
    if literal is not None:
        return literal
    try:
        addresses = await _aresolved_addresses(host, port)
    except OSError:
        return None
    return _rejection_for(host, port, addresses)


def guarded_clients(
    *, base_url: str, allow: Iterable[str] = (),
) -> tuple[httpx2.Client, httpx2.AsyncClient]:
    """构造一对带守卫 request hook 的客户端（openai SDK 默认值，见模块注释）。

    用 ``openai.DefaultHttpxClient`` / ``DefaultAsyncHttpxClient`` 而非裸
    ``httpx2.Client``：它们补齐 SDK 自己会用的 ``timeout`` / ``limits`` /
    ``follow_redirects=True``，避免"换成自定义 client 就换成 httpx2 默认值"。
    SDK 的 ``SyncHttpxClientWrapper`` / ``AsyncHttpxClientWrapper``（多一个 GC 关闭）
    未导出，这里用同款子类补上，防止客户端连接泄漏。
    """
    allowed = frozenset(allow)

    def _sync_hook(request: httpx2.Request) -> None:
        reason = _check_request(request, allowed)
        if reason is not None:
            raise _blocked_error(request.url.host, reason)

    async def _async_hook(request: httpx2.Request) -> None:
        reason = await _acheck_request(request, allowed)
        if reason is not None:
            raise _blocked_error(request.url.host, reason)

    sync_client = _GuardedSyncClient(
        base_url=base_url, event_hooks={"request": [_sync_hook]},
    )
    async_client = _GuardedAsyncClient(
        base_url=base_url, event_hooks={"request": [_async_hook]},
    )
    return sync_client, async_client


def misconfiguration_rejection(error: BaseException) -> EndpointBlockedError | None:
    """沿异常链找守卫拒绝（openai 把非 OpenAIError 包成 APIConnectionError，cause 保留）。"""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, EndpointBlockedError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None
