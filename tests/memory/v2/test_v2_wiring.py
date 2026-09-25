"""#298 / MEM-V2-2 T7b：记忆形成管线的**装配**（配置面 → 一条真的能跑的管线）。

分层：管线内部的行为在 `test_v2_executor` / `test_v2_runner` 里对着替身验；终结臂**在哪两条
语句之间**通知在 `test_v2_runtime_seam` 里用共享时间线验。本文件只回答三件事——

1. 什么时候装配（缺模型角色 / 记忆被关掉 / 调用方没给会话日志 ⇒ 不装配）；
2. 装配出来连到哪（作业库与记录库同一个文件、并发上限取自配置、挂上 lifecycle 与 runtime 接缝）；
3. 装配失败时**不**拖垮整个应用（按 OPTIONAL 降级；V1 capability 与显式工具仍可用）。

最后一条用例是本票唯一的端到端：真 SQLite + 真会话日志 + 真 runner（只有模型是替身），
它证 AC10——**慢记忆模型不挡可见答复**。放在这一层而不是 seam 那一层的原因：seam 用
`model=object()` 直调终结臂（臂在 loop 之外），它证的是"通知发生在这一行"，而"入队之后
答复立刻返回、形成在后台跑完"需要真 job 库与真执行器——那些只有装配之后才存在。
"""

from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest

from agent_harness.agent.types import STATUS_COMPLETED
from agent_harness.capability.base import (
    CapabilityError,
    CapabilityRegistry,
    DegradeReason,
)
from agent_harness.capability.config import parse_capabilities_config
from agent_harness.capability.wiring import wire_capabilities
from agent_harness.config import Settings
from agent_harness.memory.fake_capability import FakeMemoryCapability
from agent_harness.memory.v2 import assembly as v2_assembly
from agent_harness.memory.v2.assembly import (
    MEMORY_V2_DATABASE_NAME,
    build_memory_formation,
)
from agent_harness.memory.v2.jobs import MemoryJobStage, SqliteMemoryV2JobStore
from agent_harness.memory.v2.runner import MemoryFormationNotifier
from agent_harness.session import (
    MODEL_COMPLETED,
    RUN_STARTED,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)

RUN_ID = "run-wiring"


# --------------------------------------------------------------------------------------
# 配置与替身
# --------------------------------------------------------------------------------------


def _settings(tmp_path, **overrides) -> Settings:
    """一份"记忆能力齐备 + 主记忆角色可解析"的设置。

    `MODEL_PROVIDER=senseaudio` 是让 `resolve_memory_roles` 走"默认链恰好就是该 provider"
    那一支（PRD §5.3.3 固定 `memory.primary` → `senseaudio`）。milvus / embedding 那几项
    是 V1 记忆组件的既有就绪条件——本文件多数用例把组件换成替身，但保留它们让设置形状与
    生产一致（否则"就绪"这件事在用例里是另一套判据）。
    """
    values: dict = {
        "_env_file": None,
        "workspace_dir": str(tmp_path),
        "model_provider": "senseaudio", "model_name": "memory-primary",
        "model_api_key": "test-only",
        "milvus_uri": "https://example.test", "milvus_token": "test-only",
        "milvus_collection": "col", "embedding_model": "m",
        "embedding_base_url": "https://x", "embedding_api_key": "k",
    }
    values.update(overrides)
    return Settings(**values)


class _FakeMemoryComponents:
    """provider seam 的 Fake（与 `tests/capability/test_wiring.py` 同形）：契约只需
    `capability` / `writeback` / 生命周期——装配方不伸进内部组件（ADR-0024 D6）。"""

    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self.capability = FakeMemoryCapability()
        self.writeback = object()

    async def initialize(self) -> None: self.initialized = True

    async def close(self) -> None: self.closed = True


def _patch_components(monkeypatch) -> _FakeMemoryComponents:
    fake = _FakeMemoryComponents()
    monkeypatch.setattr(
        "agent_harness.capability.factories.build_memory_components",
        lambda settings, *, provider="builtin": fake,
    )
    return fake


def _sessions(tmp_path) -> JsonlSessionStore:
    return JsonlSessionStore(root=tmp_path / "sessions")


async def _table_names(database) -> set[str]:
    async with aiosqlite.connect(database) as connection:
        cursor = await connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {row[0] for row in await cursor.fetchall()}


# --------------------------------------------------------------------------------------
# 1. 闸门：缺什么就不装配
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_resolvable_primary_role_there_is_no_pipeline(tmp_path) -> None:
    """`memory.primary` 解析不出 ⇒ `None`，而且**不建库文件**（闸门在动磁盘之前）。

    为什么必须在装配期挡住：主角色缺席时执行器**每个 job** 都会降级成 `no_primary_model`
    并给用户发一条 `memory/degraded`——缺配置会变成每轮一次的告警噪音，还会让"记忆不形成"
    看起来像运行期故障。默认设置（`MODEL_PROVIDER=deepseek`）就是这一档，所以这条也是
    "升级到本笔之后的默认行为"的判据。
    """
    sessions = _sessions(tmp_path)
    plain = Settings(_env_file=None, workspace_dir=str(tmp_path))

    assert await build_memory_formation(plain, sessions=sessions) is None
    assert not (tmp_path / MEMORY_V2_DATABASE_NAME).exists(), \
        "闸门必须在建库之前——否则每台没配角色的机器都会留下一个空库"


@pytest.mark.asyncio
async def test_recall_is_wired_even_without_a_formation_model_role(tmp_path, monkeypatch) -> None:
    _patch_components(monkeypatch)
    wiring = await wire_capabilities(
        CapabilityRegistry(), parse_capabilities_config('{"memory": {"provider": "langmem"}}'),
        settings=_settings(tmp_path, model_provider="deepseek"),
        sessions=_sessions(tmp_path),
    )

    assert wiring.memory_formation is None
    assert wiring.memory_v2 is not None
    assert {provider.name for provider in wiring.context_providers} == {"memory"}
    tool_names = {tool.name for tool in wiring.tools}
    assert {"retrieve_memory", "remember_this", "forget_memory"} <= tool_names
    assert "retrieve_memory_v2" not in tool_names
    await wiring.aclose()


@pytest.mark.asyncio
async def test_a_resolvable_primary_role_builds_the_pipeline(tmp_path) -> None:
    """可解析出主角色 ⇒ 真 runner，且作业表与记录表在**同一个库文件**里。

    同库不是省事：AC6/AC7 要的"终态与副作用一起落地"是**一个 SQLite 事务**
    （`jobs.commit_with_outcome` 是那笔事务的唯一入口）。分库会把这条性质变成分布式事务，
    而"记忆写了、job 还停在 FORMING"正是恢复扫描会重跑一遍的中间态。
    """
    runner = await build_memory_formation(_settings(tmp_path), sessions=_sessions(tmp_path))

    assert runner is not None
    assert isinstance(runner, MemoryFormationNotifier), "接入 Runtime 的端口契约"
    database = tmp_path / MEMORY_V2_DATABASE_NAME
    assert database.exists()
    assert {"memory_v2_jobs", "memory_v2_records", "memory_v2_outbox"} <= await _table_names(database)
    await runner.aclose()


@pytest.mark.asyncio
async def test_the_global_concurrency_limit_comes_from_settings(tmp_path) -> None:
    """R11 的"configurable"：设置里写 2，装配出来的 runner 就真的是 2。

    判据取 `runner.max_concurrency` 而不是计时探针：`Semaphore` 没有公开容量读数，
    而"配置有没有流到这里"是这一层唯一要证的事（并发行为本身由
    `test_v2_runner.test_global_concurrency_is_configurable` 用真 job 验）。
    """
    runner = await build_memory_formation(
        _settings(tmp_path, memory_v2_max_concurrency=2), sessions=_sessions(tmp_path),
    )

    assert runner is not None
    assert runner.max_concurrency == 2
    await runner.aclose()


@pytest.mark.asyncio
async def test_a_backup_role_is_not_required(tmp_path) -> None:
    """只有主角色也能装配：`memory.fallback` 缺席是**合法**形态（R9 的备用阶梯跳过）。

    反过来（把备用当闸门）会让"只配了一个上游"的部署整条管线缺席，而 PRD 从未要求两个
    角色都在——`MemoryModelRoles.has_fallback` 才是"备用那一段跑不跑"的开关。
    """
    settings = _settings(tmp_path)
    assert settings.model_provider == "senseaudio" and not settings.fallback_model_provider

    runner = await build_memory_formation(settings, sessions=_sessions(tmp_path))

    assert runner is not None
    await runner.aclose()


# --------------------------------------------------------------------------------------
# 2. 与 capability 装配的接缝
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_pipeline_hangs_on_the_wiring_and_its_lifecycle(tmp_path, monkeypatch) -> None:
    """`wire_capabilities(..., sessions=)` 把管线挂在 wiring 上，并挂进 lifecycle。

    两条都要：`memory_formation` 是 `build_runtime` **按名字**取它注入 Runtime 的地方；
    `lifecycle` 是进程退出时唯一会调 `aclose()` 的地方——只挂前者的话，进程退出时服务
    循环与在飞 job 没人收（`aclose` 的语义是"先停泵、再有界排空"）。
    """
    _patch_components(monkeypatch)
    wiring = await wire_capabilities(
        CapabilityRegistry(), parse_capabilities_config('{"memory": {"provider": "langmem"}}'),
        settings=_settings(tmp_path), sessions=_sessions(tmp_path),
    )

    runner = wiring.memory_formation
    assert runner is not None and isinstance(runner, MemoryFormationNotifier)
    assert runner in wiring.lifecycle

    await wiring.aclose()
    # 关闭之后不再接活（`aclose` 置位 _closed）——用行为判，不伸进私有属性。
    assert await runner.notify_run_finished(
        session_id="s", run_id="r", terminal_status=STATUS_COMPLETED, events=[],
    ) is None


@pytest.mark.asyncio
async def test_without_a_session_store_governance_and_v1_tools_remain_but_auto_context_is_disabled(
    tmp_path, monkeypatch,
) -> None:
    """调用方不传会话日志 ⇒ 治理 service 与 V1 工具保留，不走特权自动注入。

    `sessions=None` 时没有可安全解析可信项目身份的 V2 recall 上下文；因此自动 recall 为空，
    但治理 service、V1 capability、写入器和显式工具仍可用。
    """
    fake = _patch_components(monkeypatch)
    wiring = await wire_capabilities(
        CapabilityRegistry(), parse_capabilities_config('{"memory": {"provider": "langmem"}}'),
        settings=_settings(tmp_path),
    )

    assert wiring.memory_v2 is not None, "治理 API 不依赖 session store"
    assert wiring.memory_v2 in wiring.lifecycle
    assert wiring.memory_formation is None
    assert wiring.memory is fake and wiring.memory_writer is fake.writeback
    assert wiring.context_providers == []
    assert {tool.name for tool in wiring.tools} >= {
        "retrieve_memory", "remember_this", "forget_memory",
    }
    await wiring.aclose()
    assert "memory" not in wiring.degradations, "V1 记忆没有降级，别登记成降级"


@pytest.mark.asyncio
async def test_memory_disabled_means_no_pipeline(tmp_path) -> None:
    """PRD §5.6.4「关闭记忆 = 同时关掉自动抽取与自动召回」的落点。

    闸门取**装配结果**（`wiring.memory is not None`）而不是新增一个开关，所以关闭形态
    天然覆盖。刻意再添一个 `memory_v2_enabled` 只会多一处要与 V1 记忆保持一致的地方——
    而它一旦漂移，症状是"关了记忆还在形成"。
    """
    wiring = await wire_capabilities(
        CapabilityRegistry(), parse_capabilities_config('{"memory": {"enabled": false}}'),
        settings=_settings(tmp_path), sessions=_sessions(tmp_path),
    )

    assert wiring.memory is None and wiring.memory_formation is None
    assert wiring.degradations["memory"] == DegradeReason.DISABLED.value


@pytest.mark.asyncio
async def test_memory_components_missing_means_no_pipeline(tmp_path) -> None:
    """V1 记忆因**配置不齐**而降级时，V2 管线同样缺席（不 patch 工厂，走真实判据）。

    两者共用同一个闸门，所以这条钉的是"闸门取的是装配结果而不是配置项"：设置里
    `memory` 是开着的（没有 `enabled: false`），缺席是**组件构造出来是 None**造成的。
    """
    wiring = await wire_capabilities(
        CapabilityRegistry(), parse_capabilities_config('{"memory": {}}'),
        settings=Settings(_env_file=None, workspace_dir=str(tmp_path)),
        sessions=_sessions(tmp_path),
    )

    assert wiring.memory is None and wiring.memory_formation is None
    assert wiring.degradations["memory"] == DegradeReason.MISSING_SETTINGS.value


@pytest.mark.asyncio
async def test_a_broken_pipeline_degrades_without_failing_the_wiring(tmp_path, monkeypatch) -> None:
    """V2 管线起不来 ⇒ OPTIONAL 降级；V1 capability 保留但不自动注入特权记忆。

    反向行为（让异常穿出去）会把"V2 记忆的一个装配故障"升级成**整个应用起不来**——
    而 V1 capability、写入器与显式检索工具仍可用。自动回退到 V1 的 SystemMessage 注入会
    违反 recalled-text 的非特权要求。降级原因写进 `degradations`，路由层才分得清"没配"
    与"配了但坏了"（#225 的同一诉求）。
    """
    fake = _patch_components(monkeypatch)

    async def _boom(*args, **kwargs):
        raise RuntimeError("memory-v2 assembly is broken")

    monkeypatch.setattr(v2_assembly, "build_memory_formation", _boom)
    wiring = await wire_capabilities(
        CapabilityRegistry(), parse_capabilities_config('{"memory": {"provider": "langmem"}}'),
        settings=_settings(tmp_path), sessions=_sessions(tmp_path),
    )

    assert wiring.memory_formation is None
    assert wiring.memory is fake and wiring.memory_writer is fake.writeback
    assert wiring.context_providers == []
    assert {tool.name for tool in wiring.tools} >= {"retrieve_memory"}
    assert wiring.degradations["memory_v2"] == DegradeReason.INIT_FAILED.value


@pytest.mark.asyncio
async def test_optional_v2_import_failure_degrades_after_disabling_privileged_v1_context(
    tmp_path, monkeypatch,
) -> None:
    import sys

    _patch_components(monkeypatch)
    monkeypatch.setitem(sys.modules, "agent_harness.memory.v2.assembly", None)

    wiring = await wire_capabilities(
        CapabilityRegistry(), parse_capabilities_config('{"memory": {"provider": "langmem"}}'),
        settings=_settings(tmp_path), sessions=_sessions(tmp_path),
    )

    assert wiring.context_providers == []
    assert wiring.degradations["memory_v2"] == DegradeReason.INIT_FAILED.value
    assert "retrieve_memory" in {tool.name for tool in wiring.tools}


# --------------------------------------------------------------------------------------
# 3. 端到端：AC10（真 SQLite、真会话日志、真 runner；只有模型是替身）
# --------------------------------------------------------------------------------------


class _GatedInvoker:
    """**被闸门挡住**的模型替身：调用会一直等 `release`，所以"形成还没跑完"是可判定的
    状态，而不是靠计时猜的。`order` 是共享时间线（谁先发生）。"""

    def __init__(self, *, order: list[str]) -> None:
        self.release = asyncio.Event()
        self.completed = False
        self._order = order

    async def __call__(self, call) -> str:
        self._order.append("model")
        await self.release.wait()
        self.completed = True
        # `NO_MEMORY`：这条用例要证的是**不挡答复**，不是写没写记忆——挑一条不写盘、
        # 但走完整个 stage 编排（含终态提交）的路径，断言因此只落在"job 跑完了"。
        return json.dumps(
            {"decision": "NO_MEMORY", "candidates": [], "skip_reason": "no_durable_value"},
            ensure_ascii=False,
        )


@pytest.mark.asyncio
async def test_the_visible_answer_does_not_wait_for_a_slow_memory_model(
    tmp_path, monkeypatch,
) -> None:
    """AC10：慢记忆模型不挡可见答复——入队返回时这一轮的形成**一定**还没跑完。

    判据是**因果**而不是时延：模型卡在闸门上（`release` 未置位），因此

    - 通知返回后 job 仍在非终态（`QUEUED`）——它没有等形成；
    - 松闸 + `drain()` 之后同一行变成 `COMPLETED`，且模型确实被调用过一次
      （`order[0] == "model"`）——证明"没等"不是"根本没跑"。

    再加一个 5 秒上界：如果哪天有人把入队改成 `await` 形成（ticket 明令禁止），这条会以
    `TimeoutError` 响亮失败，而不是把测试挂死。
    """
    sessions = _sessions(tmp_path)
    session = Session.start(sessions)
    session.append(USER_MESSAGE, {"content": "请以后都用 pnpm 装依赖"})
    session.append(RUN_STARTED, {"turn_index": 1}, run_id=RUN_ID)
    session.append(MODEL_COMPLETED, {"content": "好的"}, run_id=RUN_ID)

    order: list[str] = []
    invoker = _GatedInvoker(order=order)
    monkeypatch.setattr(v2_assembly, "ChatModelInvoker", lambda *a, **k: invoker)

    runner = await build_memory_formation(_settings(tmp_path), sessions=sessions)
    assert runner is not None
    database = tmp_path / MEMORY_V2_DATABASE_NAME
    jobs = SqliteMemoryV2JobStore(database)
    await jobs.initialize()
    events = sessions.read_events(session.session_id)

    try:
        async with asyncio.timeout(5):
            job = await runner.notify_run_finished(
                session_id=session.session_id, run_id=RUN_ID,
                terminal_status=STATUS_COMPLETED, events=events,
            )
        assert job is not None, "合格的一轮必须建出一个 job（AC1）"

        pending = await jobs.get(job.job_id)
        assert pending.stage is MemoryJobStage.QUEUED, \
            "通知返回时这一轮的形成还没跑完——它没有等模型"
        assert invoker.completed is False, "正控：模型确实还卡在闸门上"

        invoker.release.set()
        await runner.drain()
        settled = await jobs.get(job.job_id)
        assert settled.stage is MemoryJobStage.COMPLETED
        assert invoker.completed is True and order == ["model"], \
            "形成真的跑过一次——否则上一条断言只是证明了'没跑'"
    finally:
        invoker.release.set()
        await runner.aclose()


# --------------------------------------------------------------------------------------
# 4. 配置类故障不能降级成"没配"（T8 两轴审查 P2）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_broken_model_catalog_fails_loudly_instead_of_degrading(
    tmp_path, monkeypatch,
) -> None:
    """`roles.resolve_memory_roles` 抛的 `ConfigError` 必须**穿透**装配层。

    为什么：`model.config.ConfigError` **不是** `CapabilityError` 的子类，而
    `_wire_memory_formation` 里那条宽 `except Exception` 原本会把它一起吞掉 ⇒
    `degradations["memory_v2"] = init_failed`。运维看到的是"**没配**"（一个缺省状态）
    而不是"**配错了**"（`AGENT_MODELS` 坏），真正原因只剩 traceback 里一行 warning。
    这与 `roles.py` 开头的契约（"配错了要响亮"）及主循环对 `CapabilityError` 的分流
    都相反，所以修法是上抛成 `CapabilityError(init_failed)`——降级只留给外部/环境故障。

    判别性：把 `except ConfigError: raise` 那一支删掉，本用例转红（拿到 `degradations`
    而不是异常）。
    """
    _patch_components(monkeypatch)
    settings = _settings(tmp_path, agent_models="{这不是合法 JSON")
    with pytest.raises(CapabilityError) as caught:
        await wire_capabilities(
            CapabilityRegistry(),
            parse_capabilities_config('{"memory": {"provider": "langmem"}}'),
            settings=settings, sessions=_sessions(tmp_path),
        )
    assert caught.value.code == "init_failed"
