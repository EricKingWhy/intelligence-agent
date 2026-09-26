"""T8（`#316`）完成闸门 Live Gate 场景的**确定性**测试（不烧真实模型调用）。

真实 3/3 证据由 `python scripts/live_gate.py run --scenario completion-quiescence-gate`
产出并落进 `docs/live_gate/**`；本文件只钉**场景自身**的可机检事实，避免"跑了才知道
场景是不是恒 PASS / 恒恒 FAIL"：

1. **前置与道具是真的**：沙箱没有 python ⇒ 前置不成立（BLOCKED 面，不是 FAIL，也不 seed）；
   owner 脚本**先落副作用、再挂着**（⇒ 超时收尾时世界状态真的未知）；
2. **场景自己的方法在生产组合上跑通**：`_produce_unquiescent_owner` 用生产 `BashTool`
   （MUTATING）+ 生产 `ToolExecutor` + 生产 `SqliteOperationLedger`，在真沙箱里真的留下
   `UNKNOWN` + "副作用未证"；`_gate_readout` 按 durable 事实独立重算，归因到**账本行**
   （摘掉账本后没有 blocker）—— 这是"两条独立路径对账"的场景侧那一半；
3. **端到端（替身只替模型客户端那一个接缝）**：三条腿在真 Session / 真 Ledger / 真沙箱上
   跑完 ⇒ 断言集全绿，且 durable 面能独立读出"三次执行、两条终态、owner 最后被裁决结清"；
4. **诚实性**：把 owner 弄没（道具失效）或把**完成闸门**弄成恒通过（谓词聚合恒报静止）⇒
   必须判红，而且红的落点要对得上（含"产品理由与独立重算不一致"这一条）—— 否则本场景
   只是"跑了一遍"而不是"证明了一件事"。

替身模型跑场景**不算** Live Gate 证据（runner 的 `seams` 把那种运行锁到 FAIL；本文件也不
注册场景、不落盘证据）。离线替身只接在**模型客户端**这一个接缝上（`ModelConfig.from_settings`
+ `create_chat_model`）：工具 / 执行器 / 账本 / 运行时 / 会话全部是生产实现。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from agent_harness.agent.completion import QuiescenceReport
from agent_harness.config import Settings
from agent_harness.model.config import ModelConfig
from agent_harness.sandbox.local import LocalSubprocessSandbox
from agent_harness.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage import (
    OperationState,
    SqliteOperationLedger,
    has_unproven_side_effect,
    needs_reconcile,
)
from agent_harness.tooling import ErrorCode
from evaluation.live_gate.registry import ScenarioContext
from evaluation.live_gate.scenarios import completion as completion_module
from evaluation.live_gate.scenarios.completion import (
    OWNER_CALL_ID,
    OWNER_TOOL_NAME,
    POSITIVE_ARTIFACT,
    POSITIVE_CONTENT,
    REFUSED_ARTIFACT,
    REFUSED_CONTENT,
    RESUMED_ARTIFACT,
    RESUMED_CONTENT,
    SCENARIO,
    SCENARIO_ID,
    UNCERTAIN_SCRIPT,
    UNCERTAIN_SENTINEL,
    UNCERTAIN_SENTINEL_LINE,
    UNCERTAIN_SLEEP_SECONDS,
    UNCERTAIN_TOOL_TIMEOUT_SECONDS,
    _failure_text,
    _scenario_settings,
    _sentinel_count,
    _task,
)

#: 断言名清单（多一条 / 少一条都要被发现：断言集变了就是判据变了）。
EXPECTED_ASSERTIONS = {
    "positive_leg_completed_after_durable_tool_result",
    "positive_leg_artifact_on_disk",
    "ledger_rows_for_every_call",
    "unquiescent_owner_produced_by_production_execution",
    "owner_leg_leaves_no_owner_attributed_events",
    "no_tool_output_deltas_in_session",
    "refused_leg_blocked_with_stable_reason",
    "refusal_left_the_owner_untouched",
    "refused_leg_work_still_settled",
    "gate_readout_blocked_by_the_ledger_row",
    "gate_readout_follows_the_reconcile_chain",
    "owner_row_settles_only_via_the_reconcile_chain",
    "settled_work_reenters_the_gate_and_completes",
    "one_terminal_per_completed_run_no_duplicates",
    "production_tools_used",
    "no_dangling_tool_calls",
    "durable_replay_matches_live",
    "owner_side_effect_happened_exactly_once",
    "session_identity_present",
}


def _settings(**overrides: Any) -> Settings:
    """真实 `Settings`，但**不读仓库 `.env`**（单测不把部署机凭证带进进程）。"""
    return Settings(_env_file=None, **overrides)


def _context(
    tmp_path: Path, *, sandbox: Any | None = None, settings: Settings | None = None,
) -> ScenarioContext:
    return ScenarioContext(
        settings=settings if settings is not None else _settings(),
        sandbox=sandbox if sandbox is not None else LocalSubprocessSandbox(
            workspace_root=tmp_path / "workspace",
        ),
        session_root=tmp_path / "sessions",
        session_id="live-gate-completion-a1",
        attempt_index=1,
    )


class _WriteThenAnswerModel:
    """离线复刻真实模型的两步决策：先调一次生产 `write`，再给一句最终回答。

    产物名与内容从**任务文本**里解析（`completion._task` 的稳定措辞）—— 三条腿各自写自己的
    产物，所以"产物内容相等"这条断言是这条替身在真的按任务干活，而不是写死的常量。
    接缝只有模型客户端一处：`ModelConfig.from_settings` 与 `create_chat_model`（工具 / 执行器 /
    账本 / 运行时 / 会话全是生产实现）。
    """

    #: 任务文本里的产物契约（`_task` 的措辞；解析不出来就明确报错，不猜）。
    _CONTRACT = re.compile(r"创建\s*(\S+?)，内容恰好是\s*(\S+?)(?:（|$)", re.MULTILINE)

    def __init__(self, *, write: bool = True) -> None:
        self._cursor = 0
        self._write = write
        self.bound_tools: list | None = None
        self.snapshots: list[list[Any]] = []

    def bind_tools(self, tools: list, **kwargs: Any) -> _WriteThenAnswerModel:
        self.bound_tools = tools
        return self

    def _contract(self, messages: list[Any]) -> tuple[str, str]:
        for message in reversed(messages):
            content = getattr(message, "content", "")
            if not isinstance(content, str) or "内容恰好是" not in content:
                continue
            found = self._CONTRACT.search(content)
            if found is not None:
                return found.group(1), found.group(2)
        raise RuntimeError("任务文本里没有产物契约：本场景的任务措辞变了（替身无法解析）")

    def _next(self, messages: list[Any]) -> AIMessage:
        artifact, content = self._contract(messages)
        if self._write and self._cursor == 0:
            self._cursor += 1
            return AIMessage(
                content="",
                tool_calls=[{
                    "id": f"call-write-{artifact}", "name": "write",
                    "args": {"path": artifact, "content": content},
                }],
            )
        self._cursor += 1
        return AIMessage(content="文件已经写好。")

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        self.snapshots.append(list(messages))
        return self._next(messages)

    async def astream(self, messages: list[Any], **kwargs: Any):
        self.snapshots.append(list(messages))
        response = self._next(messages)
        yield AIMessageChunk(
            content=response.content, tool_calls=response.tool_calls or [],
        )


def _patch_provider(
    monkeypatch: pytest.MonkeyPatch, **model_kwargs: Any,
) -> list[_WriteThenAnswerModel]:
    """把**模型客户端**这一个接缝换成离线替身（其余全部是生产实现）。

    每次 `create_chat_model` 造一个**新**替身（它的 `_cursor == 0` 就是"一条腿两次决策"
    这一步的形状，三条腿各造一个才与真实运行同构）。返回造出来的全部替身，调用方能据此
    核对运行时绑给模型的生产工具集 —— 否则 `bound_tools` 是没人看的状态。
    """
    monkeypatch.setattr(
        ModelConfig, "from_settings",
        classmethod(lambda cls, settings: SimpleNamespace(
            model_name="offline-scripted", provider_id="offline", fallback=None,
        )),
    )
    created: list[_WriteThenAnswerModel] = []

    def _factory(config: Any, request_timeout: Any = None) -> _WriteThenAnswerModel:
        model = _WriteThenAnswerModel(**model_kwargs)
        created.append(model)
        return model

    monkeypatch.setattr("agent_harness.model.provider.create_chat_model", _factory)
    return created


def _run_scenario(ctx: ScenarioContext) -> Any:
    return asyncio.run(SCENARIO.run(ctx))


def _red(assertions: list[Any]) -> set[str]:
    return {item.name for item in assertions if not item.ok}


def _details(assertions: list[Any]) -> dict[str, str]:
    return {item.name: item.detail for item in assertions if not item.ok}


# ── 前置与道具：真沙箱 ────────────────────────────────────────────────────


def test_prepare_blocks_without_python_and_seeds_the_owner_script(tmp_path):
    """没有 python ⇒ 前置不成立（不 seed）；有 ⇒ seed owner 脚本、不预置产物 / sentinel。"""

    class _NoPythonSandbox:
        def exec(self, command: str) -> SimpleNamespace:
            return SimpleNamespace(exit_code=1, stdout="", stderr="not found")

        def write_text(self, name: str, content: str) -> None:  # pragma: no cover
            raise AssertionError("前置不成立时不该 seed 任何文件")

    problems = asyncio.run(SCENARIO.prepare(_context(tmp_path, sandbox=_NoPythonSandbox())))
    assert problems and "python" in problems[0]

    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "workspace")
    ctx = _context(tmp_path, sandbox=sandbox)
    assert asyncio.run(SCENARIO.prepare(ctx)) == []

    script = sandbox.read_text(UNCERTAIN_SCRIPT)
    assert UNCERTAIN_SENTINEL in script and str(UNCERTAIN_SLEEP_SECONDS) in script
    # 追加模式是"跑了正好一次"那条断言的承重件：覆盖写会让重跑与单跑在读数上同形
    # （哨兵行数会恒为 1），于是 `owner_side_effect_happened_exactly_once` 变成装饰。
    assert '.open("a"' in script, "哨兵必须是追加写，否则数不出重跑"
    assert _sentinel_count(sandbox) == 0, "副作用只能由那次真实调用产生"
    for artifact in (POSITIVE_ARTIFACT, REFUSED_ARTIFACT, RESUMED_ARTIFACT):
        try:
            sandbox.read_text(artifact)
        except FileNotFoundError:
            pass
        else:  # pragma: no cover - 预置产物会让"模型真的写了文件"变成假事实
            raise AssertionError(f"{artifact} 不该被 seed")


def test_owner_script_lands_its_side_effect_before_hanging(tmp_path):
    """owner 脚本先落副作用、再挂着 ⇒ 超时收尾时世界状态**真的**未知。"""
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "workspace")
    ctx = _context(tmp_path, sandbox=sandbox)
    assert asyncio.run(SCENARIO.prepare(ctx)) == []

    killed = sandbox.exec(f"python {UNCERTAIN_SCRIPT}", timeout=1.0)
    assert killed.timed_out is True
    assert sandbox.read_text(UNCERTAIN_SENTINEL).strip() == UNCERTAIN_SENTINEL_LINE


def test_owner_timeout_constants_are_ordered():
    """`工具超时 < 脚本挂起时长` 才是"未证"的前提（改任意一个都要重新论证）。"""
    assert UNCERTAIN_TOOL_TIMEOUT_SECONDS < UNCERTAIN_SLEEP_SECONDS
    assert UNCERTAIN_TOOL_TIMEOUT_SECONDS < 60.0, "生产 bash 超时是 60s，本场景只调小不调大"


# ── 场景自己的方法：生产组合上的机制 ──────────────────────────────────────


def test_produce_owner_and_gate_readout_on_the_production_composition(tmp_path):
    """`_produce_unquiescent_owner` + `_gate_readout`：真实一行 UNKNOWN + 归因到账本。

    与端到端用例的分工：这条只跑**道具与读数**（不跑模型），把机制钉在可复现的规模上；
    端到端那条再证明"三条腿连起来也成立"。
    """
    from agent_harness.web.app import AppState

    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "workspace")
    ctx = _context(tmp_path, sandbox=sandbox)
    assert asyncio.run(SCENARIO.prepare(ctx)) == []
    store = JsonlSessionStore(root=ctx.session_root)

    async def _leg() -> dict[str, Any]:
        state = AppState(_scenario_settings(ctx))
        try:
            await state.ensure_stores()
            session = Session.start(
                store, session_id=ctx.session_id, cwd=sandbox.workspace_root,
            )
            owner = await completion_module._produce_unquiescent_owner(
                ctx, ledger=state.operation_ledger, session=session,
            )
            # 行快照读在**这条腿自己的产出时刻**：后面两步会把同一行合法地裁决掉，
            # 读在结清之后就看不到"它当时是未结清的"（离线端到端用例复现过这个红）。
            produced_row = await state.operation_ledger.get(ctx.session_id, OWNER_CALL_ID)
            before = await completion_module._gate_readout(
                store, state.operation_ledger, ctx.session_id,
            )
            await state.operation_ledger.update_state(
                ctx.session_id, OWNER_CALL_ID, OperationState.NEED_RECONCILE,
            )
            classified = await completion_module._gate_readout(
                store, state.operation_ledger, ctx.session_id,
            )
            await state.operation_ledger.update_state(
                ctx.session_id, OWNER_CALL_ID, OperationState.SUCCEEDED,
                result_json='{"reconciled": "live-gate"}',
                reconcile_meta='{"verdict": "side_effect_confirmed"}',
            )
            settled = await completion_module._gate_readout(
                store, state.operation_ledger, ctx.session_id,
            )
            return {
                "owner": owner,
                "produced_row": produced_row,
                "row": await state.operation_ledger.get(ctx.session_id, OWNER_CALL_ID),
                "before": before, "classified": classified, "settled": settled,
            }
        finally:
            await state.shutdown()

    result = asyncio.run(_leg())
    owner, produced_row, row = result["owner"], result["produced_row"], result["row"]

    assert owner["execution"].result.error_code is ErrorCode.TIMEOUT
    assert owner["execution"].result.ok is False
    assert owner["sentinel_after_call"] == 1, "副作用已落地 ⇒ 结论真的证不出来"
    # 产出时刻的行：未结清 + 带未证标记（谓词 4 与 5 都会读到它）
    assert produced_row.state is OperationState.UNKNOWN
    assert has_unproven_side_effect(produced_row) is True
    assert needs_reconcile(produced_row) is True
    # 读盘时刻的行：只经由 classify→adjudicate 变结清，裁决内容覆盖了未证标记
    assert row.state is OperationState.SUCCEEDED
    assert has_unproven_side_effect(row) is False
    assert "side_effect_confirmed" in (row.reconcile_meta or "")
    # 读数一：闸门会看到的 blocker 全在这条账本行上（摘掉账本就是空的 ⇒ 归因干净）
    assert result["before"]["kinds"] == ["unsettled_operation", "pending_reconcile"]
    assert result["before"]["kinds_from_events_only"] == []
    assert result["before"]["reason"] == "quiescence_blocked:pending_reconcile,unsettled_operation"
    # 读数二：分类进对账流程后只剩谓词 5（`03 §5` 的"对账优先"把"未定"换成了"欠对账"）
    assert result["classified"]["kinds"] == ["pending_reconcile"]
    # 读数三：裁决落地（覆盖未证标记）⇒ 静止
    assert result["settled"]["quiescent"] is True
    assert result["settled"]["kinds"] == []


# ── 端到端：三条腿在真 Session / 真账本 / 真沙箱上跑完 ────────────────────


def test_scenario_passes_end_to_end_with_the_provider_seam_stubbed(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    """三条腿连起来全绿；durable 面独立复读：三次执行、两条终态、owner 被裁决结清。"""
    models = _patch_provider(monkeypatch)
    ctx = _context(tmp_path)
    assert asyncio.run(SCENARIO.prepare(ctx)) == []

    outcome = _run_scenario(ctx)

    assert outcome.ok is True, _details(outcome.assertions)
    assert {item.name for item in outcome.assertions} == EXPECTED_ASSERTIONS
    assert outcome.session_id == ctx.session_id
    # 运行时真的把生产工具集绑给模型（"生产工具被用上"的接线前提，不只是类路径判据）。
    # 绑定的形状是 registry.export_model_definitions() 的 dict（name/description/parameters）。
    assert models, "场景必须真的造过模型客户端"
    bound = sorted({
        str(definition.get("name", ""))
        for model in models
        for definition in model.bound_tools or []
    })
    assert "write" in bound, f"模型拿到的是 {bound}"

    # durable 面（不看场景给的读数，自己重读一遍）：三次执行、两条终态、被挡的那次没有终态。
    events = JsonlSessionStore(root=ctx.session_root).read_events(ctx.session_id)
    started = [event for event in events if event.type == "run/started"]
    completed = [event for event in events if event.type == "run/completed"]
    terminals = [
        event for event in events
        if event.type in ("run/completed", "run/failed", "run/interrupted")
    ]
    assert len(started) == 3 and len(terminals) == 2, [e.type for e in events]
    assert [str(event.run_id) for event in completed] == [
        str(started[0].run_id), str(started[2].run_id),
    ]
    assert not [
        event for event in events
        if event.type in ("run/paused", "operation/reconcile-required")
    ]

    # owner 行最后是被**裁决**结清的（未证标记被裁决内容覆盖，不是留着标记说"没事"）。
    ledger = SqliteOperationLedger(Path(ctx.session_root.parent) / "harness.db")
    rows = {row.tool_call_id: row for row in asyncio.run(ledger.list_for_session(ctx.session_id))}
    assert rows[OWNER_CALL_ID].state is OperationState.SUCCEEDED
    assert has_unproven_side_effect(rows[OWNER_CALL_ID]) is False
    # 只钉"裁决内容覆盖了标记"，不钉 json.dumps 的空格（那属于落盘格式，改格式不是回归）
    assert "side_effect_confirmed" in (rows[OWNER_CALL_ID].reconcile_meta or "")
    # 三条腿各自的产物都在（含被挡的那次：拒绝的是收口，不是工作）
    sandbox = ctx.sandbox
    assert sandbox.read_text(POSITIVE_ARTIFACT) == POSITIVE_CONTENT
    assert sandbox.read_text(REFUSED_ARTIFACT) == REFUSED_CONTENT
    assert sandbox.read_text(RESUMED_ARTIFACT) == RESUMED_CONTENT
    assert _sentinel_count(sandbox) == 1


def test_scenario_is_red_when_the_completion_gate_always_passes(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    """把完成闸门弄成恒通过（谓词聚合恒报静止）⇒ 必须判红，且红的落点要对得上。

    这条是"本场景到底证明了什么"的诚实性检查：闸门不挡时，被挡那条腿会照常收口 ⇒
    "拒绝收口"与"单终态"两条判据都要红，而**场景侧的独立重算**会发现产品给的理由
    （空）与 durable 事实给出的理由对不上（`gate_readout_blocked_by_the_ledger_row`）。
    没有这一条，"3/3 全绿"可能只是"断言根本没在看完成闸门"。
    """
    _patch_provider(monkeypatch)
    # 只换 Runtime 命名空间里那一个引用：场景侧 `_gate_readout` 用的是**真**函数
    # ⇒ 产品恒通过、独立重算照旧读出 blocker，两者对不上就是本用例要的证据。
    monkeypatch.setattr(
        "agent_harness.agent.runtime.collect_quiescence_report",
        lambda **kwargs: QuiescenceReport(),
    )
    ctx = _context(tmp_path)
    assert asyncio.run(SCENARIO.prepare(ctx)) == []

    outcome = _run_scenario(ctx)

    assert outcome.ok is False
    red = _red(outcome.assertions)
    assert {
        "refused_leg_blocked_with_stable_reason",
        "one_terminal_per_completed_run_no_duplicates",
        "gate_readout_blocked_by_the_ledger_row",
    } <= red, _details(outcome.assertions)
    assert "reason=''" in _details(outcome.assertions)["refused_leg_blocked_with_stable_reason"]


def test_scenario_is_red_when_the_model_does_not_do_the_work(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    """模型只回答不落产物（道具失效的一种）⇒ 产物 / 工具面 / 工作已结清三条判红。

    这条钉住"判据不依赖模型措辞"的另一半：模型不干活时场景不会照样绿。
    """
    _patch_provider(monkeypatch, write=False)
    ctx = _context(tmp_path)
    assert asyncio.run(SCENARIO.prepare(ctx)) == []

    outcome = _run_scenario(ctx)

    assert outcome.ok is False
    red = _red(outcome.assertions)
    assert {
        "positive_leg_artifact_on_disk",
        "refused_leg_work_still_settled",
        "ledger_rows_for_every_call",
        "production_tools_used",
    } <= red, _details(outcome.assertions)


# ── 取证纪律 ──────────────────────────────────────────────────────────────


def test_failure_text_names_the_failing_line():
    """失败读数必须**指得出哪一行**，不只是异常类型（真实运行只有这一次机会）。"""

    def _inner() -> None:
        payload: list[str] | None = None
        for _ in payload:
            pass

    try:
        _inner()
    except TypeError as error:
        text = _failure_text(error)
    else:  # pragma: no cover - 上面必然抛 TypeError
        raise AssertionError("预期 TypeError")

    assert text.startswith("TypeError: 'NoneType' object is not iterable")
    assert "test_completion_scenario.py" in text, text
    assert "for _ in payload" in text, text


def test_runtime_dirs_are_redirected_into_the_attempt_root(tmp_path):
    """`workspace_dir` / `artifact_dir` 落在一次性根内；其余字段逐字沿用部署配置。"""
    original = _settings()
    ctx = _context(tmp_path, settings=original)
    effective = _scenario_settings(ctx)
    root = ctx.session_root.parent

    assert Path(effective.workspace_dir) == root
    assert Path(effective.workspace_dir) / "sessions" == ctx.session_root
    assert Path(effective.artifact_dir).is_relative_to(root)
    assert effective.model_provider == original.model_provider
    assert effective.model_name == original.model_name
    assert effective.capabilities == original.capabilities
    assert original.workspace_dir == Settings(_env_file=None).workspace_dir, "不得改到原对象"


def test_scenario_id_and_task_shape_are_stable():
    """场景 id / 三条腿的任务形状：产物契约是替身与断言共同的锚点，改了要一起改。"""
    assert SCENARIO.id == SCENARIO_ID
    assert SCENARIO.version >= 1
    for artifact, content in (
        (POSITIVE_ARTIFACT, POSITIVE_CONTENT),
        (REFUSED_ARTIFACT, REFUSED_CONTENT),
        (RESUMED_ARTIFACT, RESUMED_CONTENT),
    ):
        task = _task(artifact, content)
        assert artifact in task and content in task
        assert OWNER_TOOL_NAME == "bash"
