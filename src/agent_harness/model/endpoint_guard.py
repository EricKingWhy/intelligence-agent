"""模型端点误配置守卫（#1485 / SEC-07，裁决 25 **B**）：默认拦云元数据与 link-local。

裁决 B 而非 C：所有者直连 loopback / 内网照旧可用。本守卫**只**拦两类目标——

1. 云元数据端点：``169.254.0.0/16``（RFC 3927 link-local，含 ``169.254.169.254``、
   ECS ``169.254.170.2``、EKS Pod Identity ``169.254.170.23``）、阿里云
   ``100.100.100.200``、AWS IMDS IPv6 ``fd00:ec2::/32``；
2. IPv6 link-local ``fe80::/10``（含 OpenStack Nova ``fe80::a9fe:a9fe``）。

带内嵌 IPv4 的写法一并拆开再判：IPv4-mapped ``::ffff:``、NAT64 ``64:ff9b::/96`` 与
``64:ff9b:1::/48``、IPv4-compatible ``::/96``——它们在 ``ipaddress`` 里**都不**命中
IPv4 网段（实测），不拆就是白名单。

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

解析失败（``OSError``）在 hook 里**不吞**：吞掉再放行就是"解析器一抖就绕过"，
而真实连接用的是同一个解析器——放过它换不来可用性，只换来一条绕过路径。异常
原样上抛后由 openai 包成 ``APIConnectionError``，仍按瞬时连接失败（可重试）。

覆盖面：chat（``create_chat_model``）与 embedding（``create_embeddings``）两条
构造路径共用本模块；只守住前者等于只守住半个模型面。

第二项：``_BLOCKED_NETWORKS`` / ``_METADATA_HOSTNAMES`` 是**拷贝**，不随上游扩充
（新云厂商的元数据地址不会自动进来）。新证据出现时手工补这里，别指望同步。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    # #1527：httpx2 在本模块只出现在注解里（``from __future__ import annotations``）；
    # 运行期唯一构造点 ``guarded_clients`` 走 openai 客户端基类，httpx2 由其按需带上。
    import httpx2

#: 拦截网段。只放云元数据 / link-local——**不放** RFC1918、loopback、198.18/15。
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),  # RFC 3927 link-local（含 169.254.169.254）
    ipaddress.ip_network("100.100.100.200/32"),  # 阿里云元数据
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local（含 fe80::a9fe:a9fe）
    ipaddress.ip_network("fd00:ec2::/32"),  # AWS IMDS IPv6
)

#: 内嵌 IPv4 的过渡前缀：**低 32 位**就是那个 IPv4，要取出来再判。
#: 三个都实测过 ``::ffff:`` 之外它们**不**命中 IPv4 网段（与 mapped 同一坑）：
#: NAT64 well-known、RFC 8215 local-use（``64:ff9b:1::a9fe:a9fe``）、
#: IPv4-compatible ``::169.254.169.254``（RFC 4291 已弃用，但解析器仍接受）。
_EMBEDDED_V4_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("::/96"),
)

#: 云元数据主机名（大小写不敏感；末尾点先剥掉）。
_METADATA_HOSTNAMES = frozenset(
    {"metadata.google.internal", "metadata.amazonaws.com", "metadata", "instance-data"}
)


class EndpointBlockedError(RuntimeError):
    """目标被误配置守卫拒绝——请求未发出，凭据未发送。"""


#: 惰性构建的守卫客户端类缓存（#1527）。#1485 的两个子类原本放模块级，
#: ``from openai import …`` 因此成了 ``cli/agent.runtime → model.failure → 本模块``
#: 这条导入链的必经站——CLI 轻入口被拖去加载整个 provider SDK。导入期本模块
#: 只付 stdlib 成本；openai（及其 httpx2）推迟到真正构造客户端那一刻。
#: 多线程同时首调的竞态是**良性**的（最坏两对类对象、一份胜出，功能等价；
#: openai 首导本身被 import 锁串行化，类对象无任何外部身份消费者——审查
#: Correctness 轴 P4 已核），不为它加锁。
_GUARDED_CLIENT_CLASSES: tuple[type, type] | None = None


def _guarded_client_classes() -> tuple[type, type]:
    """构建（并缓存）挂 GC 关闭的守卫客户端类；基类来自 openai，故按需导入。

    守卫语义与 #1485 完全一致：仍继承 ``openai.DefaultHttpxClient`` /
    ``DefaultAsyncHttpxClient``（补齐 SDK 自己会用的 timeout / limits /
    follow_redirects=True），``__del__`` 的 GC 关闭与异步版"只在有运行中 loop
    时排队关闭"的限制原样保留；缓存保证多次构造拿到同一对类对象。
    """
    global _GUARDED_CLIENT_CLASSES
    if _GUARDED_CLIENT_CLASSES is not None:
        return _GUARDED_CLIENT_CLASSES
    from openai import DefaultAsyncHttpxClient, DefaultHttpxClient

    class _GuardedSyncClient(DefaultHttpxClient):  # type: ignore[misc]
        """`openai.DefaultHttpxClient` + GC 时关闭（SDK 自带 wrapper 未导出，见下）。"""

        def __del__(self) -> None:
            try:
                if not self.is_closed:
                    self.close()
            except Exception:  # noqa: BLE001, S110 — 析构期不能抛
                pass

    class _GuardedAsyncClient(DefaultAsyncHttpxClient):  # type: ignore[misc]
        """同上，异步版。

        与 openai `AsyncHttpxClientWrapper` 同形：只在**有运行中的 loop** 时能排队关闭，
        否则（解释器退出 / 同步上下文里被 GC）静默跳过——连接随进程回收，析构期不能抛。
        """

        def __del__(self) -> None:
            try:
                if not self.is_closed:
                    asyncio.get_running_loop().create_task(self.aclose())
            except Exception:  # noqa: BLE001, S110
                pass

    _GUARDED_CLIENT_CLASSES = (_GuardedSyncClient, _GuardedAsyncClient)
    return _GUARDED_CLIENT_CLASSES


def parse_allowlist(raw: str) -> frozenset[str]:
    """``MODEL_ENDPOINT_ALLOWLIST``（逗号分隔 ``host`` 或 ``host:port``）→ 条目集合。

    条目原样保留，匹配在 :func:`_is_allowed` 里做（同一份解析规则，不抄第二份）。
    """
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def validate_allowlist(raw: str) -> None:
    """构造期校验（`Settings` 的 field_validator 调它）：坏条目在启动期响亮失败。

    运行期 :func:`parse_allowlist` 保持宽容——同一个字符串到了请求路径再抛只会变成
    500。这里只拦"静默永不命中"的形态：带 scheme / 路径 / 空白、端口越界。
    ``_split_entry`` 对后两种是**吞掉**的（回落成整串当 host），配错了只会表现为
    "登记了例外却仍被拦"，排查成本远高于启动期报错。
    """
    for entry in parse_allowlist(raw):
        if "://" in entry or "/" in entry or any(c.isspace() for c in entry):
            raise ValueError(
                f"model_endpoint_allowlist 条目只接受 host 或 host:port，"
                f"收到 {entry!r}（不要带 scheme / 路径 / 空白）"
            )
        try:
            ipaddress.ip_address(entry.strip("[]"))
            continue  # 裸 IP（含裸 IPv6）：没有端口可校验
        except ValueError:
            pass
        try:
            _ = urlsplit("//" + entry).port
        except ValueError as error:
            raise ValueError(
                f"model_endpoint_allowlist 条目 {entry!r} 的端口无法解析：{error}"
            ) from error


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


def _effective_port(port: int | None, scheme: str | None) -> int | None:
    """URL 未写端口时补默认端口——否则 ``host:443`` 例外在 https 上永不命中。"""
    if port is not None:
        return port
    return {"http": 80, "https": 443}.get((scheme or "").lower())


def _is_allowed(
    host: str, port: int | None, allow: Iterable[str], scheme: str | None = None,
) -> bool:
    """所有者例外：host 相同且（条目无端口 或 端口相同）。"""
    wanted = host.strip().strip("[]").rstrip(".").lower()
    effective = _effective_port(port, scheme)
    for entry in allow:
        entry_host, entry_port = _split_entry(entry)
        if entry_host != wanted:
            continue
        if entry_port is None or entry_port == effective:
            return True
    return False


def _embedded_ipv4(address: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """过渡地址里可能内嵌的 IPv4（空 = 不是这类地址）。

    低 32 位对 ``/96`` 前缀是**精确**的。``64:ff9b:1::/48`` 按 RFC 6052 把 v4 放在
    bit 48-63 + 72-87，低 32 位那份是**防御性**判定（该前缀只用于 NAT64，误伤可由
    所有者例外豁免）；两种位置都取，宁可多判一条也不留写法差异。
    """
    number = int(address)
    found: list[ipaddress.IPv4Address] = []
    for prefix in _EMBEDDED_V4_PREFIXES:
        if address not in prefix:
            continue
        found.append(ipaddress.IPv4Address(number & 0xFFFFFFFF))
        if prefix.prefixlen == 48:
            high = (number >> 64) & 0xFFFF
            low = (number >> 40) & 0xFFFF
            found.append(ipaddress.IPv4Address((high << 16) | low))
    return found


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
        for embedded in _embedded_ipv4(address):
            reason = _address_rejection(embedded)
            if reason is not None:
                return reason
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

    URL 本身不可解析（scheme 坏）时返回 None——那是 ``validate_base_url`` 的责任域。
    但**端口**坏（``http://169.254.169.254:99999/v1``）不能因此整条跳过：那样保存期
    放行、请求期照样拦，表现为"存得进去、跑起来报守卫拒绝"。端口未知 ⇒ 只认不带端口
    的例外条目（带端口的条目无法证明匹配）。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None if _is_allowed(host, None, allow) else _host_rejection(host)
    if _is_allowed(host, port, allow, parts.scheme):
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


def _rejection_for(addresses: list[str]) -> str | None:
    for raw in addresses:
        reason = _host_rejection(raw)
        if reason is not None:
            return f"解析结果 {raw} 属于拦截面（{reason}）"
    return None


def _check_request(request: httpx2.Request, allow: frozenset[str]) -> str | None:
    """单跳请求的判据；返回拒绝原因（None = 放行）。

    解析失败（``OSError``）**不吞**：hook 抛什么，这一跳就到不了 transport。
    吞掉再放行等于"解析器一抖就绕过守卫"，而真实连接用的是同一个解析器，
    放过它换不来可用性、只换来一条绕过路径。异常原样上抛后由 openai 包成
    ``APIConnectionError``（cause 保留），仍按**瞬时**连接失败处理——DNS 抖动
    不会因此变成不可重试的配置错。
    """
    host = request.url.host
    port = request.url.port
    if not host or _is_allowed(host, port, allow, request.url.scheme):
        return None
    literal = _host_rejection(host)
    if literal is not None:
        return literal
    return _rejection_for(_resolved_addresses(host, port))


async def _acheck_request(request: httpx2.Request, allow: frozenset[str]) -> str | None:
    host = request.url.host
    port = request.url.port
    if not host or _is_allowed(host, port, allow, request.url.scheme):
        return None
    literal = _host_rejection(host)
    if literal is not None:
        return literal
    return _rejection_for(await _aresolved_addresses(host, port))


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

    sync_cls, async_cls = _guarded_client_classes()
    sync_client = sync_cls(
        base_url=base_url, event_hooks={"request": [_sync_hook]},
    )
    async_client = async_cls(
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
