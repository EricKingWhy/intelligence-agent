"""测试共用夹具：SessionEvent 测试便利 + Settings 环境密封。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_harness.config import Settings
from agent_harness.session import JsonlSessionStore, Session


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

    autouse 清洗所有 Settings 字段对应的大写环境变量；豁免 qiniu 标记的
    真实集成测试（它们显式依赖真实凭证）。deliberate setenv 的测试不受
    影响（monkeypatch.setenv 发生在本 fixture 之后的测试体内）。

    清洗规则住在 `settings_env_sealed()`：真实模型守卫夹具用的是同一条。
    """
    if "qiniu" in request.keywords:
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
