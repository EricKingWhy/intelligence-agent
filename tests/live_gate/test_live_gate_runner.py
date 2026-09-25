"""runner 的判定与停止语义（`#307` R1 / R2 / R5）。

⚠ **本文件跑的不是 Live Gate 证据**：场景是替身、能力面是替身、证据只落 `tmp_path`。
它测的是"什么时候判 PASS、失败之后还跑不跑、注入/替身能不能换来 PASS、扫出凭证停不停"。
真实模型 + 生产工具那条路径的证据由 `scripts/live_gate.py run` 产出并入库（`docs/live_gate/`）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, SecretStr

from agent_harness.session import JsonlSessionStore, Session
from evaluation.live_gate import registry as scenario_registry
from evaluation.live_gate.capability import READY, ProviderCapability
from evaluation.live_gate.registry import (
    AttemptOutcome,
    ScenarioContext,
    register_scenario,
    unregister_scenario,
)
from evaluation.live_gate.runner import (
    SEAM_CAPABILITY,
    SEAM_SCENARIO,
    GateOptions,
    run_gate,
)
from evaluation.live_gate.schema import (
    GATE_ATTEMPTS,
    AssertionResult,
    ProviderRecord,
    Verdict,
)
from evaluation.live_gate.validator import validate_evidence
from tests import live_model_guard
from tests.live_gate._evidence_factory import RUN_ID, write_session_trace
from tests.live_model_guard import REASON_RATE_LIMITED, EndpointProbe

SCENARIO_ID = "mechanism-test-scenario"
#: 泄漏用例用的凭证值（只活在内存里；出现即判 FAIL）。
SECRET = "configured-provider-key-for-leak-test"
#: 能力面用例的"在场凭证"（不参与泄漏判据，只需非空即可让能力面进入探测分支）。
STUB_CREDENTIAL = "stub-credential-value-0001"

#: 本模块全是用例都是协程（runner 是 async）——`addopts` 没开 `asyncio_mode=auto`，
#: 故在模块级统一打标（与 `tests/evaluation/test_experiment_gate.py` 的逐条打标同效）。
pytestmark = pytest.mark.asyncio


class _StubSettings(BaseModel):
    model_api_key: SecretStr = SecretStr("")


def _recorded_trace(root: Path, session_id: str, *, secret: str = "") -> Path:
    """用生产 Session 写轨迹；`secret` 非空时再追加一条带该值的模型事件（泄漏注入）。"""
    path = write_session_trace(root, session_id=session_id, tool_calls=("write",))
    if secret:
        session = Session.load(JsonlSessionStore(root=root), session_id)
        session.append("model/completed", {"text": f"echo {secret}"}, run_id=RUN_ID)
    return path


class _ScriptedScenario:
    """可控场景替身：按 `prepare_failures` / `run_failures` 决定第 N 次尝试的结局。"""

    id = SCENARIO_ID
    version = 7
    description = "机制测试替身（不产生 Live Gate 证据）"

    def __init__(
        self, *, prepare_failures: tuple[int, ...] = (), run_failures: tuple[int, ...] = (),
        trace: bool = True, trace_secret: str = "", prepare_raises: tuple[int, ...] = (),
    ) -> None:
        self.prepare_failures = prepare_failures
        self.run_failures = run_failures
        self.trace = trace
        self.trace_secret = trace_secret
        self.prepare_raises = prepare_raises
        self.prepared: list[int] = []
        self.ran: list[int] = []

    async def prepare(self, ctx: ScenarioContext) -> list[str]:
        self.prepared.append(ctx.attempt_index)
        if ctx.attempt_index in self.prepare_raises:
            raise RuntimeError("场景自己崩了（机制测试）")
        if ctx.attempt_index in self.prepare_failures:
            return [f"前置不成立（attempt {ctx.attempt_index}）"]
        return []

    async def run(self, ctx: ScenarioContext) -> AttemptOutcome:
        self.ran.append(ctx.attempt_index)
        if self.trace:
            _recorded_trace(ctx.session_root, ctx.session_id, secret=self.trace_secret)
        ok = ctx.attempt_index not in self.run_failures
        return AttemptOutcome(
            ok=ok,
            session_id=ctx.session_id,
            run_id=RUN_ID,
            run_status="completed" if ok else "failed",
            steps=1,
            tool_calls=["write"],
            assertions=[AssertionResult(name="run_completed", ok=ok)],
            error="" if ok else "脚本化失败",
        )


@pytest.fixture
def scenario():
    """注册替身场景并在用例结束时摘掉（注册表是进程级单例）。"""
    holder: dict[str, _ScriptedScenario] = {}

    def _register(**kwargs) -> _ScriptedScenario:
        stub = register_scenario(_ScriptedScenario(**kwargs))
        holder["stub"] = stub
        return stub

    yield _register
    unregister_scenario(SCENARIO_ID)


@pytest.fixture
def builtin_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """把替身场景标成**内置**（仅此一处豁免），用来覆盖"3/3 全过 ⇒ PASS"这条路径。

    生产代码拿不到这个豁免：`register_scenario(builtin=True)` 要求场景类的定义文件**真的**
    落在 `evaluation/live_gate/scenarios/` 下（`registry._is_shipped`，机制用例的替身定义在
    `tests/` 里 ⇒ 过不了）。要覆盖 PASS 分支就只能在测试里显式改这个集合；而"替身场景默认
    判 FAIL + 记 seam"由 `test_stub_scenario_is_a_seam_and_forbids_pass` 单独钉住。
    """
    monkeypatch.setattr(scenario_registry, "_BUILTIN_IDS", {SCENARIO_ID})


def _capability(*, verdict: str = READY, reason: str = "stub") -> ProviderCapability:
    return ProviderCapability(
        verdict=verdict, reason=reason,
        provider=ProviderRecord(provider_id="stub", model_name="stub-model", base_url_host="localhost"),
    )


#: 能力面判定的**依赖**替身（不是能力面本身的替身）：
#: `check_capability` 是**真实实现**，被换掉的是它内部的 `chain_from_settings` / `probe_endpoint`。
#: 这样 `GateOptions.seams` 仍为空 —— "PASS 只有 3/3 一条路" 才能被真实地测到：
#: 若改成替换 `check_capability` 本身，`seams` 非空 ⇒ 判定上限被锁到 FAIL，PASS 路径无法覆盖。
_STUB_BASE_URL = "https://stub.local/v1"


def _chain():
    return [("primary", SimpleNamespace(
        provider="stub-provider", model_name="stub-model", base_url=_STUB_BASE_URL, fallback=None,
    ))]


def _settings(**overrides) -> _StubSettings:
    values = {"model_api_key": SecretStr(STUB_CREDENTIAL)}
    values.update(overrides)
    return _StubSettings(**values)


async def _probe_ready(config, *, label, timeout):
    return EndpointProbe(
        label=label, provider="stub-provider", model_name="stub-model",
        base_url=_STUB_BASE_URL, ok=True,
    )


async def _probe_blocked(config, *, label, timeout):
    return EndpointProbe(
        label=label, provider="stub-provider", model_name="stub-model",
        base_url=_STUB_BASE_URL, ok=False, reason=REASON_RATE_LIMITED, error_type="RateLimitError",
    )


@pytest.fixture
def ready_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """能力面 READY，但**不引入替身缝**（见 `_chain` 上方说明）。"""
    monkeypatch.setattr(live_model_guard, "chain_from_settings", lambda settings: _chain())
    monkeypatch.setattr(live_model_guard, "probe_endpoint", _probe_ready)


@pytest.fixture
def blocked_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """能力面 BLOCKED（端点全不可用），同样不引入替身缝。"""
    monkeypatch.setattr(live_model_guard, "chain_from_settings", lambda settings: _chain())
    monkeypatch.setattr(live_model_guard, "probe_endpoint", _probe_blocked)


def _options(tmp_path: Path, **overrides) -> GateOptions:
    options = {
        "scenario_id": SCENARIO_ID,
        "out_dir": tmp_path,
        "settings": _settings(),
    }
    options.update(overrides)
    return GateOptions(**options)


async def test_unknown_scenario_fails_fast_without_writing(tmp_path: Path) -> None:
    """未知名场景：一个请求都不发、一份文件都不落（对照 `--scenario` 写错时的常见形状）。"""
    with pytest.raises(KeyError, match="未知场景"):
        await run_gate(GateOptions(scenario_id="no-such-scenario", out_dir=tmp_path))
    assert list(tmp_path.iterdir()) == []


async def test_three_passes_produce_pass_and_evidence_under_out_dir(
    tmp_path: Path, scenario, ready_capability, builtin_stub,
) -> None:
    stub = scenario()
    result = await run_gate(_options(tmp_path))
    assert result.evidence.verdict is Verdict.PASS
    assert result.evidence.reason == ""
    assert [attempt.status for attempt in result.evidence.attempts] == [Verdict.PASS] * 3
    assert stub.ran == [1, 2, 3]
    assert result.evidence.seams == {}
    assert result.evidence.scenario_version == 7
    assert result.passed is True
    # 证据只落 out_dir（替身证据不得进 docs/live_gate/）
    assert result.evidence_path is not None
    assert tmp_path in result.evidence_path.parents
    assert result.evidence_path.name == "evidence.json"
    assert result.evidence_path.read_text(encoding="utf-8").endswith("}\n")
    # 工作区销毁事实进证据
    assert result.evidence.sandbox.deleted is True
    assert len(result.evidence.sandbox.workspace_ids) == 3
    assert all(attempt.secret_scan_rules == [] for attempt in result.evidence.attempts), (
        "卫生问题不得被记成凭证规则"
    )


async def test_injected_failure_caps_verdict_at_fail_and_keeps_other_attempts(
    tmp_path: Path, scenario, ready_capability,
) -> None:
    """R2 + R5：注入一次受控失败 ⇒ 总判定 FAIL，其余计划尝试照跑，失败记录不被覆盖。"""
    stub = scenario()
    result = await run_gate(_options(tmp_path, injected_failure="attempt:2"))
    assert result.evidence.verdict is Verdict.FAIL
    assert result.evidence.injected_failure == "attempt:2"
    assert [attempt.status for attempt in result.evidence.attempts] == [
        Verdict.PASS, Verdict.FAIL, Verdict.PASS,
    ]
    assert "injected-failure" in result.evidence.attempts[1].error
    assert stub.ran == [1, 3], "被注入的那次不该调用场景（不烧真实调用）"
    assert "注入" in result.evidence.reason


async def test_explicit_capability_seam_forbids_pass(tmp_path: Path, scenario, builtin_stub) -> None:
    """替身缝（显式传入的 `capability_fn`）必须在证据里留痕，且判 FAIL。"""
    scenario()
    result = await run_gate(_options(tmp_path, capability_fn=lambda settings: _await(_capability())))
    assert result.evidence.seams == {SEAM_CAPABILITY: "injected"}
    assert result.evidence.verdict is Verdict.FAIL
    assert "替身缝" in result.evidence.reason


async def test_stub_scenario_is_a_seam_and_forbids_pass(
    tmp_path: Path, scenario, ready_capability,
) -> None:
    """P1：替身场景（定义文件不在 `scenarios/` 下）不得产出 `PASS / seams={}`。

    三次尝试全过、能力面是真的 —— 唯一的问题是"这不是随 harness 发布的场景"，
    判定必须落到 FAIL 并在证据里写下 `scenario` 这一条缝，否则一份假证据看起来完全正常。
    """
    stub = scenario()
    result = await run_gate(_options(tmp_path))
    assert [attempt.status for attempt in result.evidence.attempts] == [Verdict.PASS] * 3
    assert result.evidence.seams == {SEAM_SCENARIO: "not_builtin"}
    assert result.evidence.verdict is Verdict.FAIL
    assert "替身缝" in result.evidence.reason and SEAM_SCENARIO in result.evidence.reason
    assert stub.ran == [1, 2, 3], "判定变了，但尝试照跑完（不是靠少跑来判 FAIL）"


async def test_no_attempt_count_knob_can_shorten_the_gate(
    tmp_path: Path, scenario, ready_capability, builtin_stub,
) -> None:
    """P1：`GateOptions.attempts` 曾经是可写构造参数 ⇒ `attempts=0` 能一路走到 PASS。

    现在次数写死 `GATE_ATTEMPTS`：没有旋钮、证据里的计划数恒为该常量、场景被调用 3 次。
    """
    assert "attempts" not in GateOptions.__dataclass_fields__
    with pytest.raises(TypeError):
        GateOptions(scenario_id=SCENARIO_ID, out_dir=tmp_path, attempts=1)
    stub = scenario()
    result = await run_gate(_options(tmp_path))
    assert result.evidence.runner.attempts_planned == GATE_ATTEMPTS
    assert [attempt.index for attempt in result.evidence.attempts] == [1, 2, 3]
    assert stub.ran == [1, 2, 3]


async def test_no_write_leaves_out_dir_untouched(tmp_path: Path, scenario, ready_capability) -> None:
    """`--no-write` 是"一个字节都不落"：目录都不建（轨迹复制更是不能发生）。

    早先 run_dir 与轨迹是无条件落盘的，于是 `--no-write` 只在最后少写了一行 evidence.json。
    """
    stub = scenario()
    result = await run_gate(_options(tmp_path, write=False))
    assert result.evidence_path is None
    assert list(tmp_path.iterdir()) == [], "只看结论的运行不得在证据目录里留任何东西"
    assert stub.ran == [1, 2, 3]
    assert result.evidence.attempts[0].events_ref == ""
    # 扫描照做：解出来的行数仍写进记录（只是没有可复核的轨迹引用）
    assert result.evidence.attempts[0].event_count > 0


async def test_no_write_still_stops_on_a_credential_in_the_trace(
    tmp_path: Path, scenario, ready_capability,
) -> None:
    """`--no-write` 省的是落盘，**不是**安全边界：轨迹里扫出凭证仍要立即停、判 FAIL。"""
    stub = scenario(trace_secret=SECRET)
    result = await run_gate(
        _options(tmp_path, write=False, settings=_settings(model_api_key=SecretStr(SECRET))),
    )
    assert stub.ran == [1], "安全边界优先于「跑完计划」"
    assert "configured_credential_value" in result.evidence.attempts[0].secret_scan_rules
    assert list(tmp_path.iterdir()) == []


async def test_run_failure_keeps_running_the_remaining_attempts(
    tmp_path: Path, scenario, ready_capability,
) -> None:
    """R2：第 1 次就失败也**不**提前收工（只有安全边界才允许停）。"""
    stub = scenario(run_failures=(1,))
    result = await run_gate(_options(tmp_path))
    assert result.evidence.verdict is Verdict.FAIL
    assert stub.ran == [1, 2, 3]
    assert [attempt.status for attempt in result.evidence.attempts] == [
        Verdict.FAIL, Verdict.PASS, Verdict.PASS,
    ]
    assert "attempt 1" in result.evidence.reason


async def test_skip_is_skipped_not_pass(tmp_path: Path, scenario) -> None:
    scenario()
    result = await run_gate(_options(tmp_path, skip_reason="操作者显式跳过：维护窗口"))
    assert result.evidence.verdict is Verdict.SKIPPED
    assert result.evidence.attempts == []
    assert "维护窗口" in result.evidence.reason
    assert result.evidence_path is not None, "跳过也要留档（否则无从审计）"


async def test_blocked_capability_sends_no_attempts(
    tmp_path: Path, scenario, blocked_capability, builtin_stub,
) -> None:
    stub = scenario()
    result = await run_gate(_options(tmp_path))
    assert result.evidence.verdict is Verdict.BLOCKED
    assert result.evidence.attempts == []
    # 前置清单装的是能力面给出的**理由原文**（这里来自 `live_model_guard.skip_reason`）
    assert len(result.evidence.missing_preconditions) == 1
    assert "没有任何可用端点" in result.evidence.missing_preconditions[0]
    assert result.evidence.sandbox.created is False
    assert stub.ran == [], "BLOCKED 时一次都不该跑"
    assert result.evidence.seams == {}


async def test_first_attempt_prepare_failure_blocks(tmp_path: Path, scenario, ready_capability) -> None:
    """"环境缺 git"这类前置不成立 ⇒ BLOCKED（不是 FAIL）：没跑与跑了没通过必须分开归因。"""
    stub = scenario(prepare_failures=(1,))
    result = await run_gate(_options(tmp_path))
    assert result.evidence.verdict is Verdict.BLOCKED
    assert result.evidence.attempts == []
    assert result.evidence.missing_preconditions == ["前置不成立（attempt 1）"]
    assert stub.ran == []


async def test_prepare_crash_is_a_problem_not_a_runner_crash(
    tmp_path: Path, scenario, ready_capability,
) -> None:
    """场景的 `prepare` 抛异常不得让整次 Gate 崩掉（那就连"没跑成"的证据都不留）。

    归因文案必须点出"也可能**是场景自己的缺陷**"：否则一个有 bug 的场景会被读成"环境不具备"，
    把实现问题洗成 BLOCKED（第 1 次尝试 ⇒ 判 BLOCKED 的口径不变）。
    """
    stub = scenario(prepare_raises=(1,))
    result = await run_gate(_options(tmp_path))
    assert result.evidence.verdict is Verdict.BLOCKED
    assert result.evidence.attempts == []
    assert len(result.evidence.missing_preconditions) == 1
    problem = result.evidence.missing_preconditions[0]
    assert "prepare 抛异常" in problem and "RuntimeError" in problem
    assert "场景实现缺陷" in problem
    assert stub.ran == []
    # 证据仍然落盘（"没跑成"也是一条要审计的事实）
    assert result.evidence_path is not None


async def test_blocked_keeps_teardown_hygiene_in_the_precondition_list(
    tmp_path: Path, scenario, ready_capability, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLOCKED 不产出 attempt 记录 ⇒ 取证卫生必须写进前置清单，否则它就此消失。

    早先这条路径直接 `break`，teardown 失败那行字随被丢弃的记录一起不见（"没跑成"的运行
    看起来无痕）。这里只**篡改结论**（真删除照走，不留临时垃圾），判的是"那行字会不会被带出来"。
    """
    from evaluation.live_gate.workspace import DisposableWorkspace

    real_teardown = DisposableWorkspace.teardown

    def _fake_teardown(self):
        record = real_teardown(self)  # 真删干净；只是把"核实结论"改成失败
        record.deleted = False
        return record

    monkeypatch.setattr(DisposableWorkspace, "teardown", _fake_teardown)
    stub = scenario(prepare_failures=(1,))
    result = await run_gate(_options(tmp_path))
    assert result.evidence.verdict is Verdict.BLOCKED
    assert result.evidence.attempts == []
    joined = "；".join(result.evidence.missing_preconditions)
    assert "一次性工作区未被删除" in joined
    assert "一次性工作区未被删除" in result.evidence.reason
    assert result.evidence.sandbox.deleted is False, "证据级的删除结论也要如实为假"
    assert stub.ran == []


async def test_later_prepare_failure_is_fail_not_blocked(
    tmp_path: Path, scenario, ready_capability,
) -> None:
    """第 1 次已经跑过 ⇒ 后面再缺前置是 FAIL（BLOCKED 只留给"一次都没跑起来"）。"""
    stub = scenario(prepare_failures=(2,))
    result = await run_gate(_options(tmp_path))
    assert result.evidence.verdict is Verdict.FAIL
    assert len(result.evidence.attempts) == 3
    assert result.evidence.attempts[1].status is Verdict.FAIL
    assert "前置不成立" in result.evidence.attempts[1].error
    assert result.evidence.missing_preconditions == []
    assert stub.ran == [1, 3], "第 2 次没跑起来，第 3 次仍要跑完计划"


async def test_credential_in_trace_stops_immediately_and_fails(
    tmp_path: Path, scenario, ready_capability,
) -> None:
    """R6 安全边界：轨迹里扫出已配置凭证 ⇒ 立即停、判 FAIL、**轨迹不落盘**。"""
    stub = scenario(trace_secret=SECRET)
    settings = _settings(model_api_key=SecretStr(SECRET))
    result = await run_gate(_options(tmp_path, settings=settings))
    assert result.evidence.verdict is Verdict.FAIL
    assert stub.ran == [1], "安全边界优先于「跑完计划」"
    attempt = result.evidence.attempts[0]
    assert attempt.events_ref == ""
    assert "configured_credential_value" in attempt.secret_scan_rules
    assert result.evidence.secret_scan.exact_value_scan == "ran"
    run_dir = result.evidence_path.parent
    assert list(run_dir.glob("*.jsonl")) == [], "命中时不得把轨迹复制进证据目录"
    assert SECRET not in result.evidence_path.read_text(encoding="utf-8")


async def test_evidence_records_worktree_and_runner_context(
    tmp_path: Path, scenario, ready_capability,
) -> None:
    scenario()
    result = await run_gate(_options(tmp_path))
    evidence = result.evidence
    assert evidence.sha and evidence.tree
    assert evidence.worktree.head_sha == evidence.sha
    assert evidence.runner.attempts_planned == 3
    assert evidence.runner.tool_versions["python"]
    assert evidence.provider.provider_id == "stub-provider"
    assert evidence.scope["does_not_cover"]


async def test_attempt_events_are_copied_for_independent_review(
    tmp_path: Path, scenario, ready_capability,
) -> None:
    scenario()
    result = await run_gate(_options(tmp_path))
    for attempt in result.evidence.attempts:
        assert attempt.events_ref.endswith(f"attempt-{attempt.index}.jsonl")
        assert attempt.event_count > 0
        assert len(attempt.events_sha256) == 64
    assert result.evidence.secret_scan.scanned


async def test_host_paths_are_redacted_and_traces_still_validate(
    tmp_path: Path, scenario, ready_capability,
) -> None:
    """入库证据不带宿主路径（`workspace.py` 的承诺），且替换不破坏独立复核。

    轨迹里真的带着工作区路径（`session/started` 的 `cwd`），所以这条既有正控（替换发生了）
    也有闭环比对（`sha256` / 行数 / 工具序列仍与落盘字节一致 ⇒ 复核通过）。
    """
    scenario()
    result = await run_gate(_options(tmp_path))
    assert result.evidence_path is not None
    for attempt in result.evidence.attempts:
        assert attempt.redactions == ["workspace_path"]
        stored = (result.evidence_path.parent / Path(attempt.events_ref).name).read_text(
            encoding="utf-8",
        )
        assert "<workspace>" in stored
        assert "live-gate-" not in stored, "一次性工作区的宿主路径不得写进入库证据"
        assert attempt.events_sha256 == hashlib.sha256(stored.encode("utf-8")).hexdigest()
    report = validate_evidence(
        json.loads(result.evidence_path.read_text(encoding="utf-8")),
        evidence_path=result.evidence_path,
    )
    # `worktree_clean` 反映的是**当前开发树**的状态（跑测试时树本来就是脏的），与"这份证据
    # 自洽吗"无关 ⇒ 只对它放行，其余任何 FAIL 都不接受。
    assert [check.name for check in report.failures] in ([], ["worktree_clean"])


async def _await(value):
    return value
