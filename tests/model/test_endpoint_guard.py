"""#1485：模型端点误配置守卫（裁决 25 **B**）——拦云元数据 / link-local，放行 loopback。

票面验收（受控服务器/解析器，**绝不向真实元数据端点发请求**）：

- 解析后地址、IPv4/IPv6、重定向落入 link-local、无重定向路径、具体端点例外；
  被拦目标**零请求、零凭据**。
- 所有者 loopback/LAN 的保存与连接测试照旧通过；例外只对登记的端点生效。
- 错误明确说明"被误配置守卫拒绝"；不加任何二次审批。

受控手段：`httpx2.MockTransport` 充当大多数用例的可计数服务器（逐跳可计数、可回 302），
`endpoint_guard` 的解析函数由受控结果驱动。只有真实 socket 用例会在本机开监听器：
所有者 loopback 连通性检查绑定 127.0.0.1；DNS rebinding 用例绑定 0.0.0.0 并记录
目标地址，使用受控解析器在 127.0.0.1 与 127.0.0.2 间切换。用例不访问外网或元数据地址。
"""

from __future__ import annotations

import asyncio
import http.server
import ipaddress
import json
import select
import socket
import socketserver
import ssl
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta

import anyio
import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from langchain_core.messages import HumanMessage
from openai import APIConnectionError

from agent_harness.config import Settings
from agent_harness.memory.embeddings import create_embeddings
from agent_harness.model import endpoint_guard
from agent_harness.model.config import ModelConfig, endpoint_allowlist
from agent_harness.model.endpoint_guard import (
    EndpointBlockedError,
    guarded_clients,
    misconfiguration_rejection,
    parse_allowlist,
    url_rejection,
    validate_allowlist,
)
from agent_harness.model.provider import create_chat_model
from agent_harness.model.provider_store import (
    MemoryCredentialStore,
    ProviderStore,
    ProviderStoreError,
)

_METADATA_IP = "169.254.169.254"


def _chat_completion() -> dict:
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
        "model": "deepseek-chat",
        "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "pong"},
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class _Recorder:
    """受控服务器：记录每一跳的 host 与 Authorization 头，按脚本回 302/200。"""

    def __init__(self, redirect_to: str | None = None) -> None:
        self.redirect_to = redirect_to
        self.hosts: Counter[str] = Counter()
        self.host_headers: list[str | None] = []
        self.auth_headers: list[str | None] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.hosts[request.url.host] += 1
        self.host_headers.append(request.headers.get("host"))
        self.auth_headers.append(request.headers.get("authorization"))
        if self.redirect_to is not None:
            location, self.redirect_to = self.redirect_to, None  # 只跳一次
            return httpx2.Response(
                302, headers={"location": location}, request=request,
            )
        return httpx2.Response(200, json=_chat_completion(), request=request)


@pytest.fixture
def fake_resolver(monkeypatch):
    """受控解析器：按主机名给出地址；未登记的主机名回落真实解析。"""

    table: dict[str, list[str]] = {}
    real_sync = endpoint_guard._resolved_addresses
    real_async = endpoint_guard._aresolved_addresses

    def _sync(host: str, port: int | None) -> list[str]:
        return table[host] if host in table else real_sync(host, port)

    async def _async(host: str, port: int | None) -> list[str]:
        return table[host] if host in table else await real_async(host, port)

    monkeypatch.setattr(endpoint_guard, "_resolved_addresses", _sync)
    monkeypatch.setattr(endpoint_guard, "_aresolved_addresses", _async)
    return table


def _model(base_url: str, allowlist: tuple[str, ...] = ()):
    """按真实构造路径建模型，再把传输层换成可计数的受控服务器。"""
    config = ModelConfig(
        provider="deepseek", model_name="deepseek-chat", api_key="sk-test",
        base_url=base_url, temperature=0.2, endpoint_allowlist=allowlist,
    )
    return create_chat_model(config, request_timeout=5.0)


def _install(model, recorder: _Recorder) -> None:
    """把两条客户端都换成可计数传输。

    必须连 `_mounts` 一起清空：环境里的 `HTTP_PROXY` 会给非 NO_PROXY 主机挂上
    代理 transport，只换 `_transport` 的话重定向目标会绕过计数（实测）。
    """
    for client in (model.root_client, model.root_async_client):
        inner = client._client
        inner._mounts = {}
        inner._transport._inner = httpx2.MockTransport(recorder)
        if isinstance(inner, httpx2.AsyncClient):
            inner._transport._client_factory = lambda: httpx2.AsyncClient(
                transport=httpx2.MockTransport(recorder),
            )
        else:
            inner._transport._client_factory = lambda: httpx2.Client(
                transport=httpx2.MockTransport(recorder),
            )


async def _invoke(model) -> None:
    await model.ainvoke([HumanMessage(content="ping")], max_tokens=1)


def _invoke_sync(model) -> None:
    model.invoke([HumanMessage(content="ping")], max_tokens=1)


# ── 保存期：IP 字面量与元数据主机名 ──────────────────────────────────


class TestSaveTimeRejection:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://169.254.170.2/v2/credentials",
            "http://169.254.170.23/v1",
            "http://100.100.100.200/latest/meta-data/",
            "http://[fe80::1]/v1",
            "http://[fe80::a9fe:a9fe]/v1",
            "http://[fd00:ec2::254]/v1",
            "http://[::ffff:169.254.169.254]/v1",  # IPv4-mapped
            "http://[64:ff9b::a9fe:a9fe]/v1",  # NAT64 内嵌 IPv4
            "http://[64:ff9b:1::a9fe:a9fe]/v1",  # RFC 8215 local-use NAT64
            "http://[64:ff9b:1:a9fe::fea9:0]/v1",  # 同上，按 RFC 6052 分片摆放
            "http://[::169.254.169.254]/v1",  # IPv4-compatible（RFC 4291 弃用但仍可解析）
            "http://metadata.google.internal/",
            "http://metadata.amazonaws.com/",
            "http://instance-data/",
            # 归一化：大小写与末尾点不改变判定
            "http://METADATA.GOOGLE.INTERNAL/",
            "http://metadata.google.internal./",
            "http://169.254.169.254:99999/v1",  # 端口越界：保存期也必须拦
        ],
    )
    def test_metadata_and_link_local_literals_are_rejected(self, url):
        reason = url_rejection(url)
        assert reason is not None, url

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.deepseek.com",
            "https://api.example.com/v1",
            "http://127.0.0.1:11434/v1",  # 所有者 loopback 直连
            "http://localhost:8000/v1",
            "http://192.168.1.5:8000/v1",  # RFC1918
            "http://10.0.0.7/v1",
            "http://[::1]:8080/v1",
            "http://198.18.222.105/v1",  # 沙箱通配 DNS 段：必须放行
            "http://[64:ff9b::808:808]/v1",  # NAT64 里的公网地址：内嵌 v4 不在拦截面
            "http://[::0.0.0.1]/v1",  # IPv4-compatible 的 loopback
        ],
    )
    def test_owner_and_private_endpoints_are_allowed(self, url):
        assert url_rejection(url) is None, url

    @pytest.mark.parametrize(
        "url",
        [
            "http://0xa9fea9fe/v1",  # 单整数十六进制 = 169.254.169.254（POSIX inet_aton）
            "http://0XA9FEA9FE/v1",  # 十六进制大小写不敏感
            "http://0x00a9fea9fe/v1",  # 前导零不改变地址（inet_aton 认，长度上限不能判成域名）
            "http://2852039166/v1",  # 单整数十进制
            "http://0251.0376.0251.0376/v1",  # 八进制分段
            "http://00000251.0376.0251.0376/v1",  # 同上，补零写法
            "http://169.254.0xa9.0xfe/v1",  # 十进制 + 十六进制混写
            "http://169.254.0xa9.0xfe:99999/v1",  # 端口坏也不能跳过判定
        ],
    )
    def test_legacy_numeric_metadata_forms_are_rejected(self, url):
        """非规范数字 IPv4 与规范写法是同一个地址，保存期不能只认规范写法。"""
        assert url_rejection(url) is not None, url

    @pytest.mark.parametrize(
        "url",
        [
            "http://0x7f.1:11434/v1",  # loopback 传统写法：裁决 25 B 照旧放行
            "http://0x007f.1:11434/v1",  # 同上，补零写法
            "http://2130706433/v1",
            "http://0177.0.0.1/v1",
            "http://127.1/v1",
            "http://0x01020304/v1",  # 1.2.3.4：公网，不在拦截面
            "http://0xa9fea9fe.com/v1",  # 长得像数字但不是数字地址 ⇒ 仍按域名
            "http://169.254.169.254.example.com/v1",
        ],
    )
    def test_legacy_numeric_forms_keep_owner_endpoints_allowed(self, url):
        assert url_rejection(url) is None, url

    @pytest.mark.parametrize(
        "url", ["http://" + "9" * 40 + "/v1", "http://0x" + "f" * 20 + "/v1"],
    )
    def test_oversized_numeric_host_is_not_an_exception(self, url):
        """超长数字串不是地址；守卫只能判"放行"，不能把保存打成 500。"""
        assert url_rejection(url) is None, url

    def test_legacy_parser_matches_inet_aton_on_sample(self):
        """常见 legacy forms 与本机 ``socket.inet_aton`` 对照。

        算法固定采用 POSIX ``inet_aton`` 语义；部分平台接受集更窄，因此最大八进制
        单段写法另用固定预期验证，不让 Windows 的差异改变跨平台结果。
        """
        samples = [
            "0xa9fea9fe", "0x00a9fea9fe", "0xA9FEA9FE", "2852039166",
            "0251.0376.0251.0376", "00000251.0376.0251.0376", "169.254.0xa9.0xfe",
            "0x7f.1", "0x007f.1", "2130706433", "0177.0.0.1", "127.1",
            "0", "00", "0x0000", "0x00000000000000a9fea9fe",
            "0x", "09", "0009", "256.1.1.1", "1.2.3.4.5", "0xa9fea9fe.com",
            "4294967296", "1.16777216",
            # 剥前导零不能"补出"合法写法：``00X4`` 不是任何进制（inet_aton 也拒）。
            "00X4", "00XB0", "000x7f.1",
            # 全零十六进制段也走剥零：``0x`` + ≥9 个 0 的段 aton 认（= 0 字节），
            # 剥完不能回退原串（那样长度超限 ⇒ 判成域名 ⇒ link-local 保存期放行）。
            "0x" + "0" * 9, "169.254.0x000000000.1",
        ]
        for host in samples:
            try:
                expected = ipaddress.IPv4Address(socket.inet_aton(host))
            except OSError:
                expected = None
            assert endpoint_guard._legacy_ipv4(host) == expected, host
        assert endpoint_guard._legacy_ipv4("037777777777") == ipaddress.IPv4Address(
            "255.255.255.255",
        )

    def test_store_create_rejects_metadata_target_with_guard_message(self, tmp_path):
        store = ProviderStore(tmp_path / "p.json", MemoryCredentialStore())
        with pytest.raises(ProviderStoreError, match="误配置守卫"):
            store.create({
                "id": "leak", "base_url": "http://169.254.169.254/latest/meta-data/",
                "models": [{"model_id": "m"}],
            })
        assert store.list_entries() == []

    def test_store_update_rejects_metadata_target(self, tmp_path):
        store = ProviderStore(tmp_path / "p.json", MemoryCredentialStore())
        store.create({"id": "p", "base_url": "http://127.0.0.1:11434/v1",
                      "models": [{"model_id": "m"}]})
        with pytest.raises(ProviderStoreError, match="误配置守卫"):
            store.update("p", {"base_url": "http://[fe80::1]/v1"})
        assert store.get_entry("p")["base_url"] == "http://127.0.0.1:11434/v1"

    def test_owner_loopback_and_lan_save_and_resolve(self, tmp_path):
        store = ProviderStore(tmp_path / "p.json", MemoryCredentialStore())
        store.create({"id": "lan", "base_url": "http://192.168.1.5:8000/v1",
                      "models": [{"model_id": "local-model"}], "api_key": "sk-test"})
        store.create({"id": "loop", "base_url": "http://127.0.0.1:11434/v1",
                      "models": [{"model_id": "local-model"}]})
        assert store.get_entry("lan")["base_url"] == "http://192.168.1.5:8000/v1"
        assert store.get_entry("loop")["base_url"] == "http://127.0.0.1:11434/v1"

    def test_allowlist_exception_only_covers_registered_endpoint(self, tmp_path):
        store = ProviderStore(
            tmp_path / "p.json", MemoryCredentialStore(),
            allowlist=parse_allowlist("169.254.169.254:8080"),
        )
        store.create({"id": "exempt", "base_url": "http://169.254.169.254:8080/v1",
                      "models": [{"model_id": "m"}]})
        with pytest.raises(ProviderStoreError, match="误配置守卫"):
            # 同 host 换端口 = 另一个端点，例外不覆盖
            store.update("exempt", {"base_url": "http://169.254.169.254:8081/v1"})
        with pytest.raises(ProviderStoreError, match="误配置守卫"):
            store.create({"id": "other", "base_url": "http://[fe80::1]/v1",
                          "models": [{"model_id": "m"}]})

    def test_allowlist_port_matches_url_default_port(self):
        """``host:443`` 必须能放行 ``https://host``（URL 里没写端口 ≠ 端口不同）。"""
        assert url_rejection("https://169.254.169.254/v1",
                             allow=("169.254.169.254:443",)) is None
        assert url_rejection("http://169.254.169.254/v1",
                             allow=("169.254.169.254:80",)) is None
        # 默认端口只在同 scheme 下等价：http 上的 :443 例外不成立。
        assert url_rejection("http://169.254.169.254/v1",
                             allow=("169.254.169.254:443",)) is not None

    def test_preexisting_blocked_entry_can_still_write_last_test(self, tmp_path):
        """守卫上线前已存的元数据地址：不改 base_url 的 update 不能被拦。

        否则「测试连接」写回 last_test 时抛 ProviderStoreError ⇒ 500，正好把
        AC(c) 要给的「被误配置拒绝」结论盖成内部错误。拦不拦由请求期 hook 定。
        """
        path = tmp_path / "p.json"
        path.write_text(json.dumps({"version": 1, "providers": [{
            "id": "legacy", "label": "旧配置",
            "base_url": f"http://{_METADATA_IP}/latest",
            "models": [{"model_id": "m"}],
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }]}), encoding="utf-8")
        store = ProviderStore(path, MemoryCredentialStore())

        store.update("legacy", {"last_test": {"ok": False, "reason": "endpoint_blocked"}})

        assert store.get_entry("legacy")["last_test"]["reason"] == "endpoint_blocked"
        # 守卫没被绕过：显式改 base_url 仍拒绝。
        with pytest.raises(ProviderStoreError, match="误配置守卫"):
            store.update("legacy", {"base_url": f"http://{_METADATA_IP}/v1"})


# ── 请求期：逐跳、零请求、零凭据 ─────────────────────────────────────


class TestRequestTimeGuard:
    @pytest.mark.asyncio
    async def test_no_redirect_path_reaches_owner_loopback(self, fake_resolver):
        recorder = _Recorder()
        model = _model("http://127.0.0.1:11434/v1")
        _install(model, recorder)
        await _invoke(model)
        assert recorder.hosts["127.0.0.1"] == 1
        # 凭据正常随请求走（loopback 是所有者端点，不受守卫影响）
        assert recorder.auth_headers[0].startswith("Bearer ")

    @pytest.mark.asyncio
    async def test_resolved_link_local_address_blocks_with_zero_requests(
        self, fake_resolver,
    ):
        fake_resolver["metadata.test"] = [_METADATA_IP]
        recorder = _Recorder()
        model = _model("http://metadata.test/v1")
        _install(model, recorder)

        with pytest.raises(Exception) as excinfo:
            await _invoke(model)

        assert misconfiguration_rejection(excinfo.value) is not None
        assert recorder.hosts == Counter()  # 零请求
        assert recorder.auth_headers == []  # 零凭据

    @pytest.mark.asyncio
    async def test_resolved_ipv6_link_local_blocks(self, fake_resolver):
        fake_resolver["metadata6.test"] = ["fe80::1"]
        recorder = _Recorder()
        model = _model("http://metadata6.test/v1")
        _install(model, recorder)
        with pytest.raises(Exception) as excinfo:
            await _invoke(model)
        assert misconfiguration_rejection(excinfo.value) is not None
        assert recorder.hosts == Counter()

    @pytest.mark.asyncio
    async def test_mixed_resolution_blocks_when_any_address_is_blocked(
        self, fake_resolver,
    ):
        """解析出多个地址时逐个判：只要有一个落在拦截面就拒绝（防绕过）。"""
        fake_resolver["mixed.test"] = ["10.0.0.7", _METADATA_IP]
        recorder = _Recorder()
        model = _model("http://mixed.test/v1")
        _install(model, recorder)
        with pytest.raises(Exception) as excinfo:
            await _invoke(model)
        assert misconfiguration_rejection(excinfo.value) is not None
        assert recorder.hosts == Counter()

    @pytest.mark.asyncio
    async def test_empty_dns_answers_fail_closed(self, fake_resolver):
        fake_resolver["empty.test"] = []
        recorder = _Recorder()
        model = _model("http://empty.test/v1")
        _install(model, recorder)

        with pytest.raises(APIConnectionError) as excinfo:
            await _invoke(model)

        assert isinstance(excinfo.value.__cause__, socket.gaierror)
        assert recorder.hosts == Counter()
        assert recorder.auth_headers == []

    @pytest.mark.asyncio
    async def test_redirect_into_link_local_is_blocked_per_hop(self, fake_resolver):
        """第一跳合法 → 302 到元数据目标：第二跳在发出前被拦，零请求零凭据。"""
        fake_resolver["metadata.test"] = [_METADATA_IP]
        recorder = _Recorder(redirect_to="http://metadata.test/v1/chat/completions")
        model = _model("http://127.0.0.1:11434/v1")
        _install(model, recorder)

        with pytest.raises(Exception) as excinfo:
            await _invoke(model)

        assert misconfiguration_rejection(excinfo.value) is not None
        assert recorder.hosts["127.0.0.1"] == 1
        assert recorder.hosts["metadata.test"] == 0
        assert len(recorder.auth_headers) == 1  # 只有第一跳，且确实带了凭据

    @pytest.mark.asyncio
    async def test_safe_redirect_rechecks_and_pins_each_hostname(self, fake_resolver):
        fake_resolver["source.test"] = ["198.18.1.1"]
        fake_resolver["redirect.test"] = ["198.18.1.2"]
        recorder = _Recorder(redirect_to="http://redirect.test/v1/chat/completions")
        model = _model("http://source.test/v1")
        _install(model, recorder)

        await _invoke(model)

        assert recorder.hosts == Counter({"198.18.1.1": 1, "198.18.1.2": 1})
        assert recorder.host_headers == ["source.test", "redirect.test"]
        assert recorder.auth_headers[0].startswith("Bearer ")
        assert recorder.auth_headers[1] is None

    @pytest.mark.asyncio
    async def test_allowlisted_endpoint_exception_applies_per_request(
        self, fake_resolver,
    ):
        fake_resolver["metadata.test"] = [_METADATA_IP]
        recorder = _Recorder(redirect_to="http://metadata.test/v1/chat/completions")
        model = _model("http://127.0.0.1:11434/v1", allowlist=("metadata.test",))
        _install(model, recorder)

        await _invoke(model)  # 例外生效：第二跳被放行

        assert recorder.hosts["metadata.test"] == 1
        assert recorder.auth_headers[0].startswith("Bearer ")

    @pytest.mark.asyncio
    async def test_literal_blocked_base_url_never_opens_a_connection(self):
        """字面量元数据地址：构造不报错，请求在 hook 里被拦，传输层零请求。"""
        recorder = _Recorder()
        model = _model(f"http://{_METADATA_IP}/v1")
        _install(model, recorder)
        with pytest.raises(Exception) as excinfo:
            await _invoke(model)
        assert "误配置守卫" in str(misconfiguration_rejection(excinfo.value))
        assert recorder.hosts == Counter()

    @pytest.mark.asyncio
    async def test_guard_error_message_names_the_guard(self, fake_resolver):
        fake_resolver["metadata.test"] = [_METADATA_IP]
        model = _model("http://metadata.test/v1")
        _install(model, _Recorder())
        with pytest.raises(Exception) as excinfo:
            await _invoke(model)
        rejection = misconfiguration_rejection(excinfo.value)
        assert isinstance(rejection, EndpointBlockedError)
        assert "误配置守卫" in str(rejection)
        assert _METADATA_IP in str(rejection)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "base_url",
        [
            "http://0xa9fea9fe/v1",
            "http://2852039166/v1",
            "http://169.254.0xa9.0xfe/v1",
        ],
    )
    async def test_legacy_numeric_metadata_literal_blocks_at_request_time(self, base_url):
        """保存期的新写法判定与请求期是同一份 `_host_rejection`：字面量同样零请求。"""
        recorder = _Recorder()
        model = _model(base_url)
        _install(model, recorder)
        with pytest.raises(Exception) as excinfo:
            await _invoke(model)
        assert misconfiguration_rejection(excinfo.value) is not None
        assert recorder.hosts == Counter()
        assert recorder.auth_headers == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("literal", ["0xa9fea9fe", "2852039166"])
    async def test_legacy_numeric_resolved_address_blocks(self, fake_resolver, literal):
        """解析器给出传统写法（例如系统 resolver 直接回字符串）时同样拦。"""
        fake_resolver["metadata.test"] = [literal]
        recorder = _Recorder()
        model = _model("http://metadata.test/v1")
        _install(model, recorder)
        with pytest.raises(Exception) as excinfo:
            await _invoke(model)
        assert misconfiguration_rejection(excinfo.value) is not None
        assert recorder.hosts == Counter()

    @pytest.mark.asyncio
    async def test_legacy_numeric_loopback_still_reaches_the_endpoint(self, fake_resolver):
        """`0x7f.1` 之类是 loopback：裁决 25 B 放行，请求照常到达（不被新规则误伤）。"""
        recorder = _Recorder()
        model = _model("http://0x7f.1:11434/v1")
        _install(model, recorder)
        await _invoke(model)
        assert recorder.hosts["127.0.0.1"] == 1
        assert recorder.host_headers == ["0x7f.1:11434"]
        assert recorder.auth_headers[0].startswith("Bearer ")

    def test_sync_invoke_path_is_also_guarded(self, fake_resolver):
        """同步 hook 与异步 hook 是两份代码：`model.invoke` 必须同样零请求。

        `evaluation/real_eval/judge.py` 走的就是同步路径——只测 ainvoke 会让
        同步 hook 被改成 `return None` 后仍全绿。
        """
        fake_resolver["metadata.test"] = [_METADATA_IP]
        recorder = _Recorder()
        model = _model("http://metadata.test/v1")
        _install(model, recorder)
        with pytest.raises(Exception) as excinfo:
            _invoke_sync(model)
        assert misconfiguration_rejection(excinfo.value) is not None
        assert recorder.hosts == Counter()
        assert recorder.auth_headers == []

    @pytest.mark.asyncio
    async def test_resolver_failure_is_not_a_bypass(self, fake_resolver, monkeypatch):
        """解析器抛 OSError 时**不得**放行（fail-open 就是一条构造得出来的绕过）。

        请求连 transport 都不该到；异常类型不必是守卫拒绝（它按瞬时连接失败处理，
        可重试），但"零请求"是硬要求。

        `ainvoke` 走的是 `_aresolved_addresses`——patch 同步版只会让真实 DNS 接手，
        测试会因"本机恰好也解析失败"而假绿（#1485 复核 F-02）。
        """
        async def _boom(host: str, port: int | None) -> list[str]:
            raise OSError("resolver unavailable")

        monkeypatch.setattr(endpoint_guard, "_aresolved_addresses", _boom)
        recorder = _Recorder()
        model = _model("http://metadata.test/v1")
        _install(model, recorder)
        with pytest.raises(APIConnectionError) as excinfo:
            await _invoke(model)
        # 仍是瞬时连接失败（可重试），不是守卫拒绝——别把 DNS 抖动说成配置错。
        assert misconfiguration_rejection(excinfo.value) is None
        assert isinstance(excinfo.value.__cause__, OSError)
        assert recorder.hosts == Counter()
        assert recorder.auth_headers == []

    def test_sync_resolver_failure_is_not_a_bypass(self, fake_resolver, monkeypatch):
        """同步 hook 的同一性质：`_resolved_addresses` 抛 OSError 时同样零请求。"""

        def _boom(host: str, port: int | None) -> list[str]:
            raise OSError("resolver unavailable")

        monkeypatch.setattr(endpoint_guard, "_resolved_addresses", _boom)
        recorder = _Recorder()
        model = _model("http://metadata.test/v1")
        _install(model, recorder)
        with pytest.raises(APIConnectionError) as excinfo:
            _invoke_sync(model)
        assert misconfiguration_rejection(excinfo.value) is None
        assert isinstance(excinfo.value.__cause__, OSError)
        assert recorder.hosts == Counter()
        assert recorder.auth_headers == []


# ── 守卫客户端本身 ──────────────────────────────────────────────────


class TestGuardedClients:
    def test_clients_keep_sdk_redirect_and_timeout_defaults(self):
        sync_client, async_client = guarded_clients(base_url="http://127.0.0.1:1/v1")
        try:
            assert sync_client.follow_redirects is True
            assert async_client.follow_redirects is True
        finally:
            sync_client.close()

    def test_parse_allowlist_trims_and_drops_blanks(self):
        assert parse_allowlist(" a ,, b:1 , ") == frozenset({"a", "b:1"})

    @pytest.mark.parametrize(
        "entry", ["https://metadata.test", "metadata.test/v1", "a b", "host:99999"],
    )
    def test_bad_allowlist_entry_fails_at_startup(self, entry):
        """静默永不命中的条目（带 scheme / 路径 / 空白 / 端口越界）在构造期报错。"""
        with pytest.raises(ValueError):
            validate_allowlist(entry)

    def test_embeddings_client_is_guarded_too(self, fake_resolver):
        """embedding 端点走同一个守卫：配成元数据地址同样零请求。

        它自己一条构造路径（`memory/embeddings.py`），不测就漏——只守住 chat
        的地址等于只守住半个模型面。
        """
        fake_resolver["metadata.test"] = [_METADATA_IP]
        settings = Settings(
            _env_file=None, embedding_model="m",
            embedding_base_url="http://metadata.test/v1", embedding_api_key="sk-test",
        )
        embeddings = create_embeddings(settings)
        recorder = _Recorder()
        inner = embeddings.client._client
        inner._mounts = {}
        inner._transport = httpx2.MockTransport(recorder)

        with pytest.raises(Exception) as excinfo:
            embeddings.embed_query("hi")

        assert misconfiguration_rejection(excinfo.value) is not None
        assert recorder.hosts == Counter()
        assert recorder.auth_headers == []

    def test_settings_rejects_bad_allowlist_at_construction(self):
        with pytest.raises(ValueError):
            Settings(_env_file=None, model_endpoint_allowlist="https://metadata.test")

    def test_settings_accepts_host_and_host_port_entries(self):
        settings = Settings(
            _env_file=None, model_endpoint_allowlist=" fe80::1 , metadata.test:8080 ",
        )
        assert endpoint_allowlist(settings) == ("fe80::1", "metadata.test:8080")


# ── 所有者 loopback 的端到端连接测试（真实本机服务）──────────────────


class _ChatHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # 驼峰由 stdlib BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps(_chat_completion()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


@pytest.mark.asyncio
async def test_owner_loopback_connection_test_still_works():
    """AC(b)：所有者配的 loopback 模型"保存 + 连接测试"照旧通过（真实本机服务）。"""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ChatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        settings = Settings(
            _env_file=None, model_api_key="sk-test", model_provider="deepseek",
            model_name="deepseek-chat",
        )
        config = ModelConfig(
            provider="deepseek", model_name="deepseek-chat", api_key="sk-test",
            base_url=f"http://{host}:{port}/v1",
            temperature=settings.temperature,
            endpoint_allowlist=endpoint_allowlist(settings),
        )
        model = create_chat_model(config, request_timeout=10.0)
        await model.ainvoke([HumanMessage(content="ping")], max_tokens=1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _ObservedChatHandler(_ChatHandler):
    def do_POST(self) -> None:
        self.server.request_observations.append(
            (self.connection.getsockname()[0], self.headers.get("Host")),
        )
        super().do_POST()


@pytest.fixture
def rebind_http_server():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), _ObservedChatHandler)
    server.request_observations = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _install_rebinding_resolver(monkeypatch, port: int) -> list[str]:
    real_getaddrinfo = socket.getaddrinfo
    answers: list[str] = []

    def _getaddrinfo(host, requested_port, *args, **kwargs):
        if host == "dns-rebinding.test" and requested_port == port:
            address = "127.0.0.1" if not answers else "127.0.0.2"
            answers.append(address)
            return real_getaddrinfo(address, requested_port, *args, **kwargs)
        return real_getaddrinfo(host, requested_port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    return answers


def _install_async_rebinding_resolver(monkeypatch, port: int) -> list[str]:
    import anyio._core._sockets as anyio_sockets

    loop = asyncio.get_running_loop()
    real_loop_getaddrinfo = loop.getaddrinfo
    real_anyio_getaddrinfo = anyio_sockets.getaddrinfo
    answers: list[str] = []

    def _answer(host: str, requested_port: int) -> str | None:
        if host != "dns-rebinding.test" or requested_port != port:
            return None
        address = "127.0.0.1" if not answers else "127.0.0.2"
        answers.append(address)
        return address

    async def _loop_getaddrinfo(host, requested_port, *args, **kwargs):
        address = _answer(host, requested_port)
        return await real_loop_getaddrinfo(
            address or host, requested_port, *args, **kwargs,
        )

    async def _anyio_getaddrinfo(host, requested_port, **kwargs):
        address = _answer(host, requested_port)
        if address is None:
            return await real_anyio_getaddrinfo(host, requested_port, **kwargs)
        return await real_loop_getaddrinfo(address, requested_port, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", _loop_getaddrinfo)
    monkeypatch.setattr(anyio_sockets, "getaddrinfo", _anyio_getaddrinfo)
    return answers


def _local_guarded_clients(port: int):
    clients = guarded_clients(base_url=f"http://dns-rebinding.test:{port}/v1/")
    for client in clients:
        client._mounts.clear()
    return clients


@pytest.fixture
def fallback_http_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ObservedChatHandler)
    server.request_observations = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("use_async_client", [False, True])
@pytest.mark.asyncio
async def test_transport_falls_back_to_next_checked_dns_address(
    fake_resolver, fallback_http_server, use_async_client,
):
    """首个已检查地址不可达时，继续尝试同一 DNS 答案里的后续地址。"""
    port = fallback_http_server.server_port
    fake_resolver["dns-rebinding.test"] = ["127.0.0.2", "127.0.0.1"]
    sync_client, async_client = _local_guarded_clients(port)
    try:
        if use_async_client:
            response = await async_client.post("probe", json={})
        else:
            response = sync_client.post("probe", json={})
        assert response.status_code == 200
    finally:
        sync_client.close()
        await async_client.aclose()

    assert fallback_http_server.request_observations == [
        ("127.0.0.1", f"dns-rebinding.test:{port}"),
    ]


def test_sync_transport_connects_to_the_address_checked_by_guard(
    monkeypatch, rebind_http_server,
):
    port = rebind_http_server.server_address[1]
    answers = _install_rebinding_resolver(monkeypatch, port)
    sync_client, async_client = _local_guarded_clients(port)
    try:
        response = sync_client.post("probe", json={})
        assert response.status_code == 200
    finally:
        sync_client.close()
        asyncio.run(async_client.aclose())

    assert answers == ["127.0.0.1"]
    assert rebind_http_server.request_observations == [
        ("127.0.0.1", f"dns-rebinding.test:{port}"),
    ]


@pytest.mark.asyncio
async def test_async_transport_connects_to_the_address_checked_by_guard(
    monkeypatch, rebind_http_server,
):
    port = rebind_http_server.server_address[1]
    answers = _install_async_rebinding_resolver(monkeypatch, port)
    sync_client, async_client = _local_guarded_clients(port)
    try:
        response = await async_client.post("probe", json={})
        assert response.status_code == 200
    finally:
        sync_client.close()
        await async_client.aclose()

    assert answers == ["127.0.0.1"]
    assert rebind_http_server.request_observations == [
        ("127.0.0.1", f"dns-rebinding.test:{port}"),
    ]


class _HostIdentityRedirectHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.server.request_hosts.append(self.headers.get("Host"))
        if self.path == "/redirect":
            body = b""
            self.send_response(302)
            self.send_header(
                "Location", f"https://b.test:{self.server.server_port}/final",
            )
        else:
            body = b"ok"
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


@pytest.fixture
def host_identity_redirect_server(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "a.test"),
    ])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("a.test")]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    cert_path = tmp_path / "server-cert.pem"
    key_path = tmp_path / "server-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _HostIdentityRedirectHandler,
    )
    server.request_hosts = []
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, cert_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _FallbackConnectProxyHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request_line = self.rfile.readline().decode("ascii").strip()
        _method, target, _version = request_line.split(" ", 2)
        while self.rfile.readline().strip():
            pass
        self.server.connect_targets.append(target)

        if target.startswith("[2001:db8::1]:"):
            status = self.server.rejection_status
            reason = {
                403: "Forbidden",
                407: "Proxy Authentication Required",
                502: "Bad Gateway",
                504: "Gateway Timeout",
            }[status]
            self.wfile.write(
                f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n\r\n".encode(),
            )
            self.wfile.flush()
            return

        host, port = target.rsplit(":", 1)
        upstream = socket.create_connection((host, int(port)), timeout=5)
        try:
            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.connection.setblocking(False)
            upstream.setblocking(False)
            sockets = (self.connection, upstream)
            while True:
                readable, _writable, _exceptional = select.select(sockets, (), (), 5)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    destination = upstream if source is self.connection else self.connection
                    destination.sendall(data)
        finally:
            upstream.close()


class _ThreadingConnectProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def fallback_connect_proxy():
    proxy = _ThreadingConnectProxy(("127.0.0.1", 0), _FallbackConnectProxyHandler)
    proxy.connect_targets = []
    proxy.rejection_status = 502
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        yield proxy
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("use_async_client", [False, True])
@pytest.mark.asyncio
async def test_cross_host_https_redirect_rechecks_tls_identity(
    monkeypatch, fake_resolver, host_identity_redirect_server, use_async_client,
):
    """同 IP 的两个 HTTPS host 不得复用只验证了前一 host 的 TLS 连接。"""
    import httpx2._transports.default as httpx2_default

    server, cert_path = host_identity_redirect_server
    port = server.server_port
    fake_resolver.update({"a.test": ["127.0.0.1"], "b.test": ["127.0.0.1"]})
    monkeypatch.setattr(
        httpx2_default,
        "create_ssl_context",
        lambda verify=True, cert=None, trust_env=True: ssl.create_default_context(
            cafile=str(cert_path),
        ),
    )
    sync_client, async_client = guarded_clients(base_url=f"https://a.test:{port}/")
    sync_client._mounts.clear()
    async_client._mounts.clear()
    try:
        if use_async_client:
            with pytest.raises(httpx2.ConnectError, match="certificate verify failed"):
                await async_client.get("redirect")
        else:
            with pytest.raises(httpx2.ConnectError, match="certificate verify failed"):
                sync_client.get("redirect")
    finally:
        sync_client.close()
        await async_client.aclose()

    assert server.request_hosts == [f"a.test:{port}"]


@pytest.mark.parametrize("use_async_client", [False, True])
@pytest.mark.parametrize(
    ("rejection_status", "should_fallback"),
    [(502, True), (504, True), (403, False), (407, False)],
)
@pytest.mark.asyncio
async def test_https_proxy_brackets_ipv6_connect_and_falls_back_to_checked_address(
    monkeypatch, fake_resolver, host_identity_redirect_server, fallback_connect_proxy,
    rejection_status, should_fallback, use_async_client,
):
    import httpx2._transports.default as httpx2_default

    server, cert_path = host_identity_redirect_server
    port = server.server_port
    fake_resolver["a.test"] = ["2001:db8::1", "127.0.0.1"]
    fallback_connect_proxy.rejection_status = rejection_status
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv(
        "HTTPS_PROXY", f"http://127.0.0.1:{fallback_connect_proxy.server_address[1]}",
    )
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setattr(
        httpx2_default,
        "create_ssl_context",
        lambda verify=True, cert=None, trust_env=True: ssl.create_default_context(
            cafile=str(cert_path),
        ),
    )
    sync_client, async_client = guarded_clients(base_url=f"https://a.test:{port}/")
    try:
        if should_fallback:
            if use_async_client:
                response = await async_client.get("probe")
            else:
                response = sync_client.get("probe")
            assert response.status_code == 200
        else:
            with pytest.raises(httpx2.ProxyError):
                if use_async_client:
                    await async_client.get("probe")
                else:
                    sync_client.get("probe")
    finally:
        sync_client.close()
        await async_client.aclose()

    expected_targets = [f"[2001:db8::1]:{port}"]
    expected_hosts = []
    if should_fallback:
        expected_targets.append(f"127.0.0.1:{port}")
        expected_hosts.append(f"a.test:{port}")
    assert fallback_connect_proxy.connect_targets == expected_targets
    assert server.request_hosts == expected_hosts


@pytest.mark.parametrize("address", ["198.18.1.1", "2001:db8::1"])
def test_sync_pin_preserves_host_and_sets_original_tls_sni(fake_resolver, address):
    fake_resolver["model.test"] = [address]
    observed = []
    sync_client, async_client = guarded_clients(base_url="https://model.test/v1/")
    sync_client._mounts.clear()
    sync_client._transport._inner = httpx2.MockTransport(
        lambda request: _observe_pinned_request(request, observed),
    )
    try:
        response = sync_client.get("probe")
    finally:
        sync_client.close()
        asyncio.run(async_client.aclose())

    assert response.request.url.host == "model.test"
    assert observed == [(address, "model.test", "model.test")]


@pytest.mark.parametrize("address", ["198.18.1.1", "2001:db8::1"])
@pytest.mark.asyncio
async def test_async_pin_preserves_host_and_sets_original_tls_sni(fake_resolver, address):
    fake_resolver["model.test"] = [address]
    observed = []
    sync_client, async_client = guarded_clients(base_url="https://model.test/v1/")
    async_client._mounts.clear()
    async_client._transport._inner = httpx2.MockTransport(
        lambda request: _observe_pinned_request(request, observed),
    )
    try:
        response = await async_client.get("probe")
    finally:
        sync_client.close()
        await async_client.aclose()

    assert response.request.url.host == "model.test"
    assert observed == [(address, "model.test", "model.test")]


def _observe_pinned_request(request: httpx2.Request, observed: list[tuple]) -> httpx2.Response:
    observed.append((
        request.url.host,
        request.headers.get("host"),
        request.extensions.get("sni_hostname"),
    ))
    return httpx2.Response(200, json=_chat_completion(), request=request)


class _SlowAsyncCloseStream(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        yield b"chunk"

    async def aclose(self) -> None:
        await anyio.lowlevel.checkpoint()
        self.closed = True


class _SlowAsyncCloseTransport(httpx2.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, request=request)

    async def aclose(self) -> None:
        pass


class _SlowAsyncCloseClient:
    def __init__(self) -> None:
        self._transport = _SlowAsyncCloseTransport()
        self.closed = False

    async def aclose(self) -> None:
        await anyio.lowlevel.checkpoint()
        self.closed = True


def test_temporary_async_stream_closes_under_cancellation():
    stream = _SlowAsyncCloseStream()
    client = _SlowAsyncCloseClient()
    wrapper = endpoint_guard._ClosingAsyncByteStream(stream, client)

    async def scenario() -> None:
        with anyio.CancelScope() as scope:
            scope.cancel()
            await wrapper.aclose()
            await anyio.lowlevel.checkpoint()
        assert stream.closed is True
        assert client.closed is True

    anyio.run(scenario)


def test_temporary_async_request_closes_client_under_cancellation(monkeypatch):
    client = _SlowAsyncCloseClient()
    transport = endpoint_guard._PinnedAsyncTransport(
        _SlowAsyncCloseTransport(), None,
    )
    transport._primary_origin = ("https", "primary.test", 443)
    transport._client_factory = lambda: client

    async def send_cancelled(*args, **kwargs):
        await anyio.lowlevel.checkpoint()

    monkeypatch.setattr(endpoint_guard, "_send_async", send_cancelled)

    async def scenario() -> None:
        with anyio.CancelScope() as scope:
            scope.cancel()
            with pytest.raises(anyio.get_cancelled_exc_class()):
                await transport.handle_async_request(
                    httpx2.Request("GET", "https://redirect.test/"),
                )
            assert client.closed is True

    anyio.run(scenario)
