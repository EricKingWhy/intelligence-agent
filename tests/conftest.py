"""测试共用夹具：SessionEvent 测试便利 + Settings 环境密封。"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_harness.config import Settings
from agent_harness.session import JsonlSessionStore, Session


def register_workspace_from_build_kwargs(kwargs: Mapping[str, Any]) -> None:
    """Keep mocked runtime builders' workspace precondition consistent with production."""
    kwargs["workspace_registry"].create(
        kwargs["session_id"],
        workspace_root=kwargs["workspace"],
        backend=kwargs.get("sandbox_backend") or "local",
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require-live-services",
        action="store_true",
        default=False,
        help="fail when a selected live_services/qiniu test is skipped or not executed",
    )


def pytest_collection_finish(session: pytest.Session) -> None:
    if not session.config.getoption("--require-live-services"):
        return
    session.config._required_live_nodes = {
        item.nodeid for item in session.items
        if "live_services" in item.keywords or "qiniu" in item.keywords
    }
    session.config._skipped_live_nodes = set()
    session.config._executed_live_nodes = set()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[object],
) -> Iterator[None]:
    outcome = yield
    if not item.config.getoption("--require-live-services"):
        return
    report = outcome.get_result()
    if item.nodeid not in item.config._required_live_nodes:
        return
    if report.skipped:
        item.config._skipped_live_nodes.add(item.nodeid)
    elif report.when == "call" and report.passed:
        item.config._executed_live_nodes.add(item.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not session.config.getoption("--require-live-services"):
        return
    required = session.config._required_live_nodes
    skipped = session.config._skipped_live_nodes
    executed = session.config._executed_live_nodes
    not_executed = required - skipped - executed
    if required and not skipped and not not_executed:
        return
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    print(
        "[live services gate] failed: "
        f"required={len(required)} executed={len(executed)} "
        f"skipped={len(skipped)} not_executed={len(not_executed)}"
    )
    for node_id in sorted(skipped | not_executed):
        print(f"[live services gate] not executed: {node_id}")


def make_session(tmp_path: str | Path) -> Session:
    """构造 ephemeral Session（用 tmp_path 做 SessionStore 根目录）。

    测试用：把"构造 Session + tmp_path JsonlStore"压成一行，让现有测试
    能批量替换为 event-sourced 版本。
    """
    store = JsonlSessionStore(root=tmp_path)
    return Session.start(store)


@contextmanager
def settings_env_sealed() -> Iterator[None]:
    """临时摘掉所有 Settings 字段对应的大写环境变量（`.env` 成为唯一来源）。

    `_clean_settings_env`（用例级）与 `requires_live_model`（会话级）共用这一条规则：
    两边必须读到**同一份**配置，否则守卫判的端点与用例真跑的端点可能不是同一个
    （实测踩过：`MODEL_API_KEY=""` 只遮蔽守卫、不遮蔽用例）。
    """
    settings_keys = {name.upper() for name in Settings.model_fields}
    saved: dict[str, str] = {}
    for key in list(os.environ):
        if key.upper() in settings_keys:
            saved[key] = os.environ.pop(key)
    try:
        yield
    finally:
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def _clean_settings_env(request: pytest.FixtureRequest) -> Iterator[None]:
    """Settings 相关环境变量清洗（集成 AI 移交发现，2026-09-05）。

    `uv run` 默认把 .env 装进进程环境——pydantic-settings 的优先级是
    init kwargs > os.environ > env_file，`_env_file=None` 只屏蔽文件、
    屏蔽不了环境变量。于是任何 .env 带模型配置的机器上，构造
    Settings(_env_file=None) 的单测会读到真实 MODEL_*/MILVUS_* 而假失败
    （preset 断言、缺 model_name 快速失败等全部漂移）。

    autouse 清洗所有 Settings 字段对应的大写环境变量；豁免 qiniu/live_services 标记的
    真实外部服务集成测试（它们显式依赖真实凭证）。deliberate setenv 的测试不受
    影响（monkeypatch.setenv 发生在本 fixture 之后的测试体内）。

    清洗规则住在 `settings_env_sealed()`：真实模型守卫夹具用的是同一条。
    """
    if "qiniu" in request.keywords or "live_services" in request.keywords:
        yield
        return
    with settings_env_sealed():
        yield


@pytest.fixture(autouse=True)
def _reset_sse_shutdown_latch() -> Iterator[None]:
    """用例前后各复位一次 sse_starlette 的进程级关机闩锁 `AppStatus.should_exit`。

    操作约束：该全局量一旦被翻成 `True` 就**没有复位路径**，会让此后同进程内每个 SSE 响应
    变成「200 + 零 `data:` 帧」；本仓库有 11 个用例会写 `server.should_exit = True` 去停真实
    uvicorn 服务。**不要删这两行 `_reset()`**——删掉后失败是**浮动的**、且「单模块全绿」，
    极难重新定位（这正是它此前被当成环境噪声放过的原因）。

    判据与机制：`docs/adr/0038-test-isolation-reset-sse-shutdown-latch.md`。
    """
    def _reset() -> None:
        try:
            from sse_starlette.sse import AppStatus
        except ImportError:  # sse_starlette 不在时（极少数纯离线子集）无闩锁可复位
            return
        AppStatus.should_exit = False

    _reset()
    yield
    _reset()


#: loopback 主机名白名单：发往这些主机的请求必须绕过任何代理。
#: 裸 `::1` 是唯一正确写法——方括号 `[::1]` 会让 httpx2 在**客户端构造期**抛
#: `InvalidURL: Invalid port: ':1]'`（`httpx2/_utils.py::get_environment_proxies`
#: 对 `[::1]` 走 `all://*[::1]` 分支，`URLPattern` 构造即炸）。
_LOOPBACK_PROXY_HOSTS = ("127.0.0.1", "localhost", "::1")


def _normalize_no_proxy_entry(entry: str) -> str:
    """去掉方括号 IPv6 字面量的方括号：`[::1]` → `::1`、`[::1]/128` → `::1/128`。

    方括号与裸形式语义等价（指同一地址），但 httpx2 `get_environment_proxies` 只把
    **裸** ipv6 认成 ipv6（走 `all://[addr]` 分支），方括号会落进 else 分支生成
    `all://*[::1]`、`URLPattern` 构造期炸 `InvalidURL`。故统一去方括号（保留可选的
    `/前缀`）。
    """
    if entry.startswith("[") and "]" in entry:
        inner, _, tail = entry[1:].partition("]")
        return inner + tail
    return entry


def ensure_loopback_proxy_exemption(env: MutableMapping[str, str]) -> None:
    """**就地**规范化 `NO_PROXY`/`no_proxy` 并补进 loopback 三项（返回 None）。

    不是纯函数：直接改写传入的 mapping 并返回 `None`（`os.environ` 本身就是
    MutableMapping，无法用"返回新 dict"的方式替换，故不用返回式签名）。

    豁免语义是 urllib → requests → httpx 一脉的成熟约定（来源：httpx 官方
    `docs/environment_variables.md`；httpx2 `_utils.py::get_environment_proxies`
    同构）：`NO_PROXY` 是逗号分隔的 hostname 列表，命中即绕过代理。大小写两个键都写
    且值一致——httpx2 经 `urllib.request.getproxies()` 读代理 env，而
    `getproxies_environment` 对变量名做 case-fold 并按"优先小写"归并（`NO_PROXY` 与
    `no_proxy` 都映射到同一个 `no` 键，小写拼写胜出）；两键值不一致时小写键会覆盖
    大写键，故必须写同样的值。驱动者是 httpx2（经 urllib），不是"urllib 对大写敏感"。

    写回前按 `get_environment_proxies` 的实际解析行为做三步规范化（均本机实证）：
    ① 去方括号（见 `_normalize_no_proxy_entry`，否则构造期炸）；
    ② 去重且大小写不敏感（httpx2 主机匹配大小写不敏感：URL host 归一化成小写、
    `localhost` 比较前 `.lower()`，故 `LOCALHOST` 与 `localhost` 是同一条；保留首次
    出现的原拼写）；
    ③ 去空白（条目 `strip()` 后按逗号重连）。

    已在的条目保留不重复追加；值恰为 `*`（`get_environment_proxies` 见 `*` 即返回
    全豁免）时不再追加 loopback，但仍做上述规范化去掉方括号。
    """
    for key in ("NO_PROXY", "no_proxy"):
        raw = env.get(key)
        if raw is None or not raw.strip():
            env[key] = ",".join(_LOOPBACK_PROXY_HOSTS)
            continue
        entries: list[str] = []
        seen: set[str] = set()
        for host in raw.split(","):
            host = _normalize_no_proxy_entry(host.strip())
            if not host:
                continue
            folded = host.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            entries.append(host)
        if "*" not in seen:
            for host in _LOOPBACK_PROXY_HOSTS:
                if host.casefold() not in seen:
                    seen.add(host.casefold())
                    entries.append(host)
        env[key] = ",".join(entries)


@pytest.fixture(autouse=True)
def _ensure_loopback_proxy_exemption() -> Iterator[None]:
    """#963：把 loopback 补进代理豁免（含规范化宿主已有的方括号条目），用例后恢复原值。

    宿主设了 `HTTP_PROXY`/`HTTPS_PROXY` 却没给 loopback 豁免时，httpx/httpx2 默认
    `trust_env=True` 会把发往 `127.0.0.1` 的请求送进代理 → 一切走真实本机 HTTP 服务
    的用例假红（issue #963 点名的 `test_http_transport_round_trip` 等）。在 conftest
    统一修，不逐个改用例。

    夹具名用 `ensure`（补进/规范化）而非 `seal`（摘掉/屏蔽）：它**追加**豁免，语义与
    公开函数 `ensure_loopback_proxy_exemption` 对齐。

    豁免只动 loopback 与宿主 `NO_PROXY` 里的方括号条目，不影响 `qiniu`/`live_services`
    等真实外部端点，故无需跳过；机制对齐 httpx 一脉的成熟语义（见
    `ensure_loopback_proxy_exemption` docstring）。save/restore 用 try/finally，避免
    污染同进程内后续用例。
    """
    saved = {key: os.environ[key] for key in ("NO_PROXY", "no_proxy") if key in os.environ}
    try:
        ensure_loopback_proxy_exemption(os.environ)
        yield
    finally:
        for key in ("NO_PROXY", "no_proxy"):
            os.environ.pop(key, None)
        os.environ.update(saved)


@pytest.fixture(scope="session")
def requires_live_model() -> Iterator[None]:
    """依赖真实模型端点的用例的环境守卫（`tests/live_model_guard.py`，2026-09-22）。

    显式 opt-in（**不是** autouse）：精确到真正打真实端点的用例/类——同一模块里
    不碰模型的用例（如 Phase 12 的 Tavily 检索、Phase 15/16 的 ScriptedModel 路径）照旧跑，
    不因模型环境坏掉而丢覆盖。

    边界两处，都在 opt-in 粒度上如实处理：① 覆盖集合 = 依赖 `.env` 主模型链的用例，
    别处的真实外部依赖（Phase 6/11 的真实 embedding / 存储端点、Docker 门控）不在此列；
    ② Phase 14 Gate 4 的断言面（fork 边界渲染）不看模型结果，但它**确实**打真实模型，
    故一并挂守卫——诚实优先：环境坏时它证明的东西比声称的少，宁可 skip 也不留一条
    「模型失败但断言恰好通过」的假绿。

    配置面：本夹具是 session 级，在 `_clean_settings_env` 之前生效，所以自己套了
    同一条密封规则（`settings_env_sealed()`）再构造 `Settings()`——守卫判的端点
    与用例真跑的端点是同一个（不密封的话，一个 `MODEL_API_KEY=""` 能让守卫读到
    「缺配置 ⇒ 放行」而用例读到 `.env` 里的真 key，两边各说各话）。

    判据、四态与 fail-closed 纪律见该模块 docstring；此处只做 pytest 策略：
    不可用 ⇒ skip（理由响亮），可用 / 判不出 / 配置不全 ⇒ 放行。
    """
    import asyncio

    from tests.live_model_guard import ensure_live_model

    with settings_env_sealed():
        asyncio.run(ensure_live_model(Settings()))
    yield
