"""Live Gate 运行器：3 次真实尝试 + 机器证据（`#307` 的核心）。

## 判定规则（全部 fail-closed，逐条都有"否则会怎样"）

| 情形 | 判定 | 为什么不是别的 |
| --- | --- | --- |
| `--skip <reason>` | `SKIPPED` | 操作者显式跳过；**不得**产出 PASS |
| 缺凭证 / 端点全不可用 / **第 1 次尝试**的 prepare 就不成立 | `BLOCKED` | "没跑"与"跑了没通过"必须分开归因 |
| 3 次全过（且无替身缝） | `PASS` | 唯一能产出 PASS 的路径 |
| 任一次不过 / 尝试数不足 | `FAIL` | 失败尝试**全部保留**（R2：第 1、2 次失败后仍跑完计划） |
| 注入了受控失败或存在替身缝 | `FAIL`（上限） | 假模型 / 假工具 / 替身场景不得计入 Live Gate（`#305` 明文） |
| 凭证扫描命中（轨迹 / 错误 / 证据字段） | `FAIL` + **立即停** | 安全边界优先于"跑完计划"；命中时轨迹**不落盘** |
| 工作区没被真删掉 / 开发仓库被改动 | `FAIL` | 这两条是"证据本身不可信"的形状 |

**`BLOCKED` 少发请求 ≠ 一个请求都不发**（措辞要准）：凭证不足时确实一个请求都不发
（`capability.check_capability` 在构造配置链之前就返回）；端点不可用那条路**必然已经发过**
探测请求（`probe_endpoint`，`max_tokens=1`）—— 说得准确些是"**不发 run、不跑那 3 次尝试**"。
本票的 R1 关心的是后者（真实调用次数），不要把它读成"网络层零字节"。

**替身缝有两类**（`GateOptions.seams`，出现即把判定上限锁到 `FAIL`）：`capability`（显式传入
了替身探测函数）与 `scenario`（场景不是随 harness 发布的内置场景）。后者是**机械核实**的
（`registry.is_builtin`：定义文件必须落在 `evaluation/live_gate/scenarios/` 下）—— 注册表是
进程级可写单例，只靠"调用方声明"的话，一个替身场景就能写出一份 `PASS / seams={}` 的证据。

## 为什么工作区在仓库外、证据在仓库内

工作区在系统临时目录（`workspace.py`，跑完销毁并核实）；证据默认落 `docs/live_gate/`
（**入库**，供 `#319` 与集成前重车道引用）。销毁前把事件轨迹**复制进证据目录** ——
否则"证据可由独立命令复核"就落空（临时目录一删，轨迹就没了）。

## ⚠ 与 Gate-0 的先后次序（实测踩过）

证据文件是**未跟踪的 `*.json`**，而 `docs/gate/` 之外任何未跟踪的 `.json` 都是 Gate-0
`worktree_divergence()` 里的 `risky`（`LANE_INPUT_SUFFIXES` 含 `.json`）⇒ **Live Gate 跑完
但证据未提交时，Gate-0 会拒绝落盘读数**（拒绝是对的：那份读数会指到一棵被"未跟踪的车道输入"
影响的树）。正确次序：跑 Live Gate → `git add docs/live_gate/...` 提交 → 再跑 Gate-0。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluation.live_gate import repo
from evaluation.live_gate.capability import ProviderCapability, check_capability
from evaluation.live_gate.registry import (
    AttemptOutcome,
    ScenarioContext,
    get_scenario,
    is_builtin,
)
from evaluation.live_gate.schema import (
    GATE_ATTEMPTS,
    AttemptRecord,
    LiveEvidence,
    ProviderRecord,
    RunnerRecord,
    SandboxRecord,
    SecretScanRecord,
    Verdict,
    WorktreeProof,
    decide_verdict,
    load_evidence,
    now_utc,
)
from evaluation.live_gate.secrets import (
    SecretFinding,
    credential_values,
    mask_text,
    scan_payload,
)
from evaluation.live_gate.workspace import DEFAULT_BACKEND, create_workspace

#: 单次尝试的墙钟上限（秒）。模型调用自身有 `REQUEST_TIMEOUT`（180s），但 20 步上限叠起来
#: 仍可能把一次 Gate 拖成小时级 —— 那等于没有闸门。到点判 FAIL（不是"跳过"）。
ATTEMPT_TIMEOUT = 900.0

#: 受控失败注入的前缀（**验证专用**）：`attempt:<n>` 让第 n 次尝试在不调用场景的情况下失败。
INJECT_ATTEMPT_PREFIX = "attempt:"

#: 证据里如实登记的替身清单（键 = 面，值 = `injected` / `not_builtin`）。出现即判 FAIL 上限。
SEAM_CAPABILITY = "capability"
SEAM_SCENARIO = "scenario"

#: 落盘前把一次性工作区的宿主绝对路径替成 `<workspace>`（`workspace.py` 的对外承诺：
#: 入库证据不带本机用户目录）。事实登记在 `AttemptRecord.redactions` 上。
WORKSPACE_PLACEHOLDER = "<workspace>"
WORKSPACE_PATH_RULE = "workspace_path"

#: 本票的证据边界（`scope.does_not_cover`，照 gate0 的如实登记）。
SCOPE_DOES_NOT_COVER = (
    "预算 / 暂停恢复 / 卡死 / deadline / 委派树五类场景（后续票的场景，本票只给注册入口）",
    "Docker 后端（本票证据取自生产默认的 local；docker 后端的覆盖在 tests/sandbox/）",
    "模型语义质量（本 runner 只做结构性断言，不评文本好坏）",
)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S")


def _tool_versions() -> dict[str, str]:
    git_version = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    ).stdout.strip()
    return {"git": git_version, "python": platform.python_version(), "platform": sys.platform}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _relative_to_repo(path: Path) -> str:
    """证据引用一律写**仓库相对路径**（可核对、不泄漏本机目录）；不在仓库内则退回文件名。"""
    try:
        return path.resolve().relative_to(repo.REPO_ROOT).as_posix()
    except ValueError:
        return path.name


@dataclass
class GateOptions:
    """一次 invocation 的输入。

    **没有尝试数开关**：次数写死 `GATE_ATTEMPTS`（3）。这里曾经有个 `attempts: int` 字段
    —— `GateOptions(attempts=0)` 能一路走到 `decide_verdict` 并产出 `PASS`（"3/3 不可放宽"
    是 `#307` 的硬边界，一个构造参数就绕过去了）。放松的入口不留（`__init__.py` 同款措辞）。

    `capability_fn` 是**替身缝**（测试用）：注入即不得 PASS（见 `seams`）。
    """

    scenario_id: str
    out_dir: Path = field(default_factory=lambda: repo.REPO_ROOT / repo.OUTPUT_DIR)
    settings: Any | None = None
    timeout: float | None = None
    skip_reason: str = ""
    injected_failure: str = ""
    capability_fn: Callable[[Any], Awaitable[ProviderCapability]] = check_capability
    write: bool = True

    @property
    def seams(self) -> dict[str, str]:
        """本次运行的替身缝：出现即把判定上限锁到 `FAIL`（判定表见模块 docstring）。

        两类都**核实**、都不靠调用方声明：`capability` 比的是函数对象（`is`），`scenario`
        问的是注册表（定义文件是否真的在 `scenarios/` 下 —— 注册表可写，声明不足为凭）。
        """
        seams: dict[str, str] = {}
        if self.capability_fn is not check_capability:
            seams[SEAM_CAPABILITY] = "injected"
        if not is_builtin(self.scenario_id):
            seams[SEAM_SCENARIO] = "not_builtin"
        return seams


@dataclass
class GateResult:
    """运行结果：证据本体 + 落盘路径（`None` = 未落盘）+ 凭证命中（已脱敏）。"""

    evidence: LiveEvidence
    evidence_path: Path | None = None
    findings: list[SecretFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.evidence.verdict is Verdict.PASS


@dataclass
class _AttemptRun:
    """`_run_attempt` 的返回包（记录 + 凭证命中 + 未满足前置 + 工作区事实 + 取证卫生）。

    `findings` **只装凭证命中**（触发立即停）；取证卫生问题（工作区没删干净）走 `hygiene`：
    两者都会让总判定 FAIL，但只有前者是"安全边界要求立即停止"的形状（R2）。
    """

    record: AttemptRecord
    findings: list[SecretFinding]
    problems: list[str]
    sandbox: SandboxRecord
    hygiene: list[str] = field(default_factory=list)


def _path_variants(root: str) -> set[str]:
    """一个宿主路径在落盘文本里可能出现的样子（JSON 转义 / 正斜杠 / 解析后形态）。

    **轨迹是 JSONL**：`C:\\Users\\…` 在文件里写成 `C:\\\\Users\\\\…`，逐字比对必然落空——
    实测踩过：替换静默没发生，证据里留着宿主用户目录，而现场只看到 `redactions == []`。
    两种形态都要替换。
    """
    candidates: set[str] = set()
    for base in {root, str(Path(root).resolve())}:
        for form in (base, base.replace("\\", "/")):
            if not form:
                continue
            candidates.add(form)
            candidates.add(json.dumps(form, ensure_ascii=False)[1:-1])
    return candidates


def _redact_host_paths(text: str, *, roots: tuple[str, ...]) -> tuple[str, bool]:
    """把一次性工作区的**宿主绝对路径**替成 `<workspace>`（`workspace.py` 的对外承诺）。

    `session/started` 的 `cwd` 与工具错误正文都可能带工作区路径；`docs/live_gate/**` 是**入库**
    证据，"把本机用户目录写进版本库"没有收益（用户名校验、机器相关噪音）。替换是**落盘前**的
    确定性变换（不是事后改文件）：写进证据的字节与 `events_sha256` 同源，复核照常对得上。

    候选**跨根合起来按长度倒序**再替换：否则短根（`…\\live-gate-x`）先吃掉前缀，长的那个
    （`…\\live-gate-x\\workspace`）就只剩尾巴，落盘成 `<workspace>\\workspace`。
    """
    variants = {variant for root in roots if root for variant in _path_variants(root)}
    replaced = False
    for variant in sorted(variants, key=len, reverse=True):
        if variant in text:
            text = text.replace(variant, WORKSPACE_PLACEHOLDER)
            replaced = True
    return text, replaced


def _mask_outcome(
    outcome: AttemptOutcome, *, values: tuple[str, ...], where: str, roots: tuple[str, ...] = (),
) -> tuple[AttemptOutcome, list[SecretFinding], bool]:
    """把一次尝试的文字面过一遍脱敏（错误原文可能回显请求头 / URL query / 工作区路径）。

    第三个返回值是"替换过宿主路径"，与轨迹那份一起并进 `AttemptRecord.redactions`。
    """
    findings: list[SecretFinding] = []
    replaced = False

    def _clean(text: str, target: str) -> str:
        nonlocal replaced
        masked, hits = mask_text(text, values=values, where=target)
        findings.extend(hits)
        masked, hit = _redact_host_paths(masked, roots=roots)
        replaced = replaced or hit
        return masked

    masked_tools = [
        _clean(name, f"{where}.tool_calls[{index}]")
        for index, name in enumerate(outcome.tool_calls)
    ]
    masked_assertions = [
        assertion.model_copy(
            update={"detail": _clean(assertion.detail, f"{where}.assertions.{assertion.name}")},
        )
        for assertion in outcome.assertions
    ]
    return (
        AttemptOutcome(
            ok=outcome.ok,
            session_id=outcome.session_id,
            run_id=outcome.run_id,
            run_status=outcome.run_status,
            steps=outcome.steps,
            tool_calls=masked_tools,
            assertions=masked_assertions,
            error=_clean(outcome.error, f"{where}.error"),
            event_count=outcome.event_count,
            output_tail=_clean(outcome.output_tail, f"{where}.output_tail"),
        ),
        findings,
        replaced,
    )


def _copy_events(
    *, session_root: Path, session_id: str, run_dir: Path, index: int,
    values: tuple[str, ...], roots: tuple[str, ...] = (), persist: bool = True,
) -> tuple[str, str, int, list[SecretFinding], bool]:
    """把一次尝试的事件轨迹写进证据目录（脱敏 + 路径替换后再落盘）。

    返回 `(events_ref, sha256, 行数, findings, 是否替换过宿主路径)`。**扫出凭证就不落地**：
    轨迹里出现真凭证时，唯一正确的动作是"不把它写进仓库 + 把这次 Gate 判 FAIL"，而不是
    "掩掉再存"（掩码规则一旦漏一种形状，泄漏就已经发生了）。

    `persist=False`（`--no-write`）时**一个字节都不写**（连目录都不建）：但**扫描照做** ——
    "只看结论"是少落盘，不是少一层安全边界。
    """
    source = session_root / session_id / "events.jsonl"
    if not source.exists():
        return "", "", 0, [], False
    text = source.read_text(encoding="utf-8")
    _, findings = mask_text(text, values=values, where=f"attempt[{index}].events")
    if findings:
        return "", "", text.count("\n"), findings, False
    if not persist:
        return "", "", text.count("\n"), [], False
    text, replaced = _redact_host_paths(text, roots=roots)
    target = run_dir / f"attempt-{index}.jsonl"
    target.write_text(text, encoding="utf-8", newline="")
    return _relative_to_repo(target), _sha256_text(text), text.count("\n"), [], replaced


async def _run_attempt(
    *,
    scenario: Any,
    options: GateOptions,
    settings: Any,
    values: tuple[str, ...],
    run_dir: Path,
    index: int,
) -> _AttemptRun:
    """一次尝试：建工作区 → prepare → 跑 → 脱敏 → 存档轨迹 → 核实销毁。"""
    started_at = now_utc()
    started_monotonic = asyncio.get_running_loop().time()
    workspace = create_workspace()
    session_id = f"{options.scenario_id}-a{index}-{uuid.uuid4().hex[:8]}"
    outcome = AttemptOutcome(ok=False, session_id=session_id)
    problems: list[str] = []
    hygiene: list[str] = []
    findings: list[SecretFinding] = []
    error = ""
    redactions: list[str] = []
    events_ref, events_sha, line_count = "", "", 0
    # 宿主路径替换的判据对象：一次性根目录、sandbox 的工作子目录、会话轨迹目录
    workspace_roots = (
        str(workspace.root), str(workspace.sandbox.workspace_root), str(workspace.root / "sessions"),
    )
    try:
        ctx = ScenarioContext(
            settings=settings,
            sandbox=workspace.sandbox,
            session_root=workspace.root / "sessions",
            session_id=session_id,
            attempt_index=index,
            injected_failure=options.injected_failure,
        )
        try:
            problems = await scenario.prepare(ctx)
        except Exception as crash:  # noqa: BLE001 - prepare 抛异常**不是**"前置不成立"这一种可能
            # 场景的 prepare 自己崩了：早先这里没兜底 ⇒ 异常穿透 `run_gate`，整次 Gate 直接炸掉
            # （连一份"没跑成"的证据都不留）。归因文案必须说清"这也可能是场景缺陷"：否则一个
            # 有 bug 的场景会被读成"环境不具备"，把实现问题洗成 BLOCKED。
            message, _ = mask_text(
                f"{type(crash).__name__}: {crash}",
                values=values, where=f"attempt[{index}].prepare",
            )
            message, _ = _redact_host_paths(message, roots=workspace_roots)
            problems = [f"prepare 抛异常：{message}（也可能是场景实现缺陷）"]
        if not problems:
            try:
                outcome = await asyncio.wait_for(scenario.run(ctx), timeout=ATTEMPT_TIMEOUT)
            except TimeoutError:
                outcome = AttemptOutcome(
                    ok=False, session_id=session_id,
                    error=f"AttemptTimeout: 超过 {ATTEMPT_TIMEOUT:.0f}s 未结束",
                )
            except Exception as crash:  # noqa: BLE001 - runner 兜底；场景内应自行处理
                outcome = AttemptOutcome(
                    ok=False, session_id=session_id,
                    error=f"{type(crash).__name__}: {crash}",
                )
        # 脱敏先于落盘：轨迹 / 错误 / 工具名 / 断言细节都过一遍
        # （顺序不可换，见 secrets.mask_text 的说明）
        outcome, findings, path_replaced = _mask_outcome(
            outcome, values=values, where=f"attempt[{index}]", roots=workspace_roots,
        )
        events_ref, events_sha, line_count, event_findings, trace_replaced = _copy_events(
            session_root=workspace.root / "sessions", session_id=session_id,
            run_dir=run_dir, index=index, values=values, roots=workspace_roots,
            persist=options.write,
        )
        findings.extend(event_findings)
        if path_replaced or trace_replaced:
            redactions.append(WORKSPACE_PATH_RULE)
        if event_findings:
            error = "凭证扫描命中事件轨迹（轨迹未落盘）"
    finally:
        sandbox_record = workspace.teardown()

    if not sandbox_record.deleted:
        # 取证卫生失败（不等于"扫出凭证"）：**记进这次尝试的 error 并判它 FAIL**，
        # 于是总判定由 `decide_verdict` 自然落到 FAIL，validator 的重算也一致。
        # 早先这里用 `SecretFinding` 承载它会污染 `secret_scan_rules`（把卫生问题说成凭证命中），
        # 而且会触发"立即停"，与 R2（跑完计划）相冲。
        hygiene.append(
            f"一次性工作区未被删除（teardown={sandbox_record.teardown or '未销毁'}，"
            f"workspace={_workspace_id(sandbox_record) or '未知'}）"
        )
    if problems:
        error = "；".join(problems)
    duration_ms = int((asyncio.get_running_loop().time() - started_monotonic) * 1000)
    record = AttemptRecord(
        index=index,
        started_at=started_at,
        ended_at=now_utc(),
        status=Verdict.PASS if (outcome.ok and not hygiene) else Verdict.FAIL,
        duration_ms=duration_ms,
        session_id=outcome.session_id,
        run_id=outcome.run_id,
        run_status=outcome.run_status,
        steps=outcome.steps,
        event_count=outcome.event_count or line_count,
        tool_calls=outcome.tool_calls,
        events_ref=events_ref,
        events_sha256=events_sha,
        assertions=outcome.assertions,
        error="；".join(part for part in (*hygiene, error or outcome.error) if part),
        secret_scan_rules=sorted({finding.rule for finding in findings}),
        output_tail=outcome.output_tail,
        workspace_id=_workspace_id(sandbox_record),
        redactions=sorted(set(redactions)),
    )
    return _AttemptRun(
        record=record, findings=findings, problems=problems, sandbox=sandbox_record, hygiene=hygiene,
    )


async def run_gate(options: GateOptions) -> GateResult:
    """跑一次 Live Gate：场景查名 → 能力面 → 3 次尝试 → 汇总 → 落盘证据。"""
    from agent_harness.config import Settings

    scenario = get_scenario(options.scenario_id)  # 未知名场景：fail-fast，不发任何请求
    settings = options.settings if options.settings is not None else Settings()
    before = repo.worktree_proof()
    values = tuple(credential_values(settings))
    capability = ProviderCapability(verdict="BLOCKED", reason="未探测")
    attempts: list[AttemptRecord] = []
    findings: list[SecretFinding] = []
    sandboxes: list[SandboxRecord] = []
    missing: list[str] = []
    reason = ""
    verdict = Verdict.FAIL  # 默认取最保守的终点：跑了没通过
    run_dir: Path | None = None
    stopped_early = False

    if options.skip_reason:
        verdict = Verdict.SKIPPED
        reason = options.skip_reason
    else:
        capability = await options.capability_fn(settings)
        if not capability.ready:
            verdict = Verdict.BLOCKED
            missing = [capability.reason]
            reason = capability.reason

    if not options.skip_reason and capability.ready:
        run_dir = options.out_dir / f"{_stamp()}-{before['head_sha'][:12]}-{options.scenario_id}"
        if options.write:
            # `--no-write` 是"不落盘"的承诺，不是"少落一个文件"：目录不建、轨迹不复制
            # （早先目录与轨迹无条件落盘，于是 `--no-write` 仍然在 out_dir 留了一整套轨迹）。
            run_dir.mkdir(parents=True, exist_ok=True)
        statuses: list[Verdict] = []
        for index in range(1, GATE_ATTEMPTS + 1):
            if stopped_early:
                break
            if options.injected_failure == f"{INJECT_ATTEMPT_PREFIX}{index}":
                # 受控失败注入：**不调用场景**（验证专用，不必再烧一次真实调用）
                attempts.append(AttemptRecord(
                    index=index, started_at=now_utc(), ended_at=now_utc(), status=Verdict.FAIL,
                    duration_ms=0, error=f"injected-failure: attempt {index}（验证专用注入）",
                ))
                statuses.append(Verdict.FAIL)
                continue
            run = await _run_attempt(
                scenario=scenario, options=options, settings=settings, values=values,
                run_dir=run_dir, index=index,
            )
            findings.extend(run.findings)
            sandboxes.append(run.sandbox)
            if run.problems and index == 1:
                # 第 1 次尝试连 prepare 都没过 ⇒ 外部条件不具备，判 BLOCKED（不是 FAIL）。
                # 取证卫生一并写进前置清单：BLOCKED 不产出 attempt 记录，早先这里直接 break，
                # teardown 失败那行字连同被丢弃的 record 一起消失（"没跑成"的运行看起来无痕）。
                verdict = Verdict.BLOCKED
                missing = [*run.problems, *run.hygiene]
                reason = "；".join(missing)
                stopped_early = True
                break
            attempts.append(run.record)
            statuses.append(run.record.status)
            if run.findings:
                # 安全边界：扫出凭证立即停（R2 的"除非安全边界要求立即停止"）
                verdict = Verdict.FAIL
                reason = "凭证扫描命中（轨迹未落盘 / 相关字段已脱敏），按安全边界立即停止"
                stopped_early = True
        if not stopped_early:
            verdict = decide_verdict(
                attempt_statuses=statuses,
                attempts_planned=GATE_ATTEMPTS,
                injected_failure=options.injected_failure,
                injected_seams=tuple(options.seams),
            )
            reason = "" if verdict is Verdict.PASS else _failure_reason(
                attempts, seams=tuple(options.seams), injected_failure=options.injected_failure,
            )

    after = repo.worktree_proof()
    unchanged = repo.repo_unchanged(before, after)
    if not unchanged:
        verdict = Verdict.FAIL
        reason = (reason + " " if reason else "") + "开发仓库的工作树在本次运行前后发生了偏离"

    evidence = LiveEvidence(
        scenario_id=options.scenario_id,
        scenario_version=scenario.version,
        verdict=verdict,
        reason=reason,
        missing_preconditions=missing,
        created_at=now_utc(),
        sha=before["head_sha"],
        tree=before["tree"],
        worktree=WorktreeProof(
            head_sha=before["head_sha"],
            tree=before["tree"],
            tracked_matches_head=before["tracked_matches_head"],
            untracked=before["untracked"],
            hidden=before["hidden"],
            risky=before["risky"],
        ),
        provider=capability.provider or ProviderRecord(provider_id="", model_name="", base_url_host=""),
        sandbox=_sandbox_summary(sandboxes),
        capability=capability.capability_record(),
        attempts=attempts,
        seams=options.seams,
        injected_failure=options.injected_failure,
        secret_scan=SecretScanRecord(
            exact_value_scan="ran" if values else "unavailable",
            scanned=_scanned_targets(attempts),
            findings=[],
        ),
        runner=RunnerRecord(
            tool_versions=_tool_versions(), argv=list(sys.argv), attempts_planned=GATE_ATTEMPTS,
        ),
        scope={"does_not_cover": list(SCOPE_DOES_NOT_COVER)},
    )
    # 最后一道网：整份证据再扫一次（前面逐字段扫过，这里兜"漏了某个字段"）
    payload, payload_findings = scan_payload(evidence.model_dump(mode="json"), values=values)
    findings.extend(payload_findings)
    if payload_findings:
        payload["verdict"] = Verdict.FAIL.value
        payload["reason"] = "凭证扫描命中证据字段（已脱敏落盘）：" + "；".join(
            f"{finding.rule}@{finding.where}" for finding in payload_findings
        )
        evidence = load_evidence(payload)
    evidence.secret_scan.findings = [
        {"rule": finding.rule, "where": finding.where, "sample": finding.sample}
        for finding in findings
    ]
    evidence_path: Path | None = None
    if options.write:
        if run_dir is None:
            run_dir = options.out_dir / f"{_stamp()}-{before['head_sha'][:12]}-{options.scenario_id}"
            run_dir.mkdir(parents=True, exist_ok=True)
        evidence_path = run_dir / "evidence.json"
        evidence_path.write_text(evidence.to_json() + "\n", encoding="utf-8", newline="\n")
    return GateResult(evidence=evidence, evidence_path=evidence_path, findings=findings)


def _workspace_id(record: SandboxRecord) -> str:
    """一次尝试的工作区身份（`workspace_ids` 是本票的单元素列表，见 `workspace.create_workspace`）。

    取不到时返回空串而不是抛异常：身份缺失会由 `sandbox.deleted` / validator 那边暴露，
    不该让一次已经跑完的尝试因为记录字段而崩掉。
    """
    return record.workspace_ids[0] if record.workspace_ids else ""


def _sandbox_summary(sandboxes: list[SandboxRecord]) -> SandboxRecord:
    """证据级的 sandbox 块：配置面 + 全部一次性身份 + 是否都删干净（逐次身份在 attempt 上）。"""
    if not sandboxes:
        return SandboxRecord(
            backend=DEFAULT_BACKEND, disposable=True, created=False, deleted=False,
            env_allowlisted=True, workspace_ids=[],
        )
    return SandboxRecord(
        backend=sandboxes[0].backend,
        disposable=all(item.disposable for item in sandboxes),
        created=all(item.created for item in sandboxes),
        deleted=all(item.deleted for item in sandboxes),
        env_allowlisted=all(item.env_allowlisted for item in sandboxes),
        teardown=sandboxes[-1].teardown,
        workspace_ids=[wid for item in sandboxes for wid in item.workspace_ids],
    )


def _failure_reason(
    attempts: list[AttemptRecord], *, seams: tuple[str, ...] = (), injected_failure: str = "",
) -> str:
    """未通过原因：把**注入/替身**与**失败尝试**分开说，缺一不可。

    全 PASS 但带着替身缝时，旧版只回一句"尝试数不足"——那句话对不上事实（三次都在），
    会让复核者去找一个不存在的缺口。注入/替身是非 PASS 的另一条来源，必须自己说出来。
    """
    notes: list[str] = []
    if injected_failure:
        notes.append(f"注入了受控失败（{injected_failure}）——注入运行不得计入 Live Gate")
    if seams:
        notes.append(f"存在替身缝（{', '.join(sorted(seams))}）——替身结果不得计入 Live Gate")
    failed = [attempt for attempt in attempts if attempt.status is not Verdict.PASS]
    if failed:
        notes.append("；".join(
            f"attempt {attempt.index}: {attempt.error or _first_failed_assertion(attempt)}"
            for attempt in failed
        ))
    elif len(attempts) != GATE_ATTEMPTS:
        notes.append("尝试数不足（计划 3 次）——缺的尝试不得当成成功")
    return "；".join(notes) or "未通过（无进一步细节）"


def _first_failed_assertion(attempt: AttemptRecord) -> str:
    for assertion in attempt.assertions:
        if not assertion.ok:
            return f"{assertion.name} 未满足（{assertion.detail}）"
    return "未通过（无进一步细节）"


def _scanned_targets(attempts: list[AttemptRecord]) -> list[str]:
    targets = ["evidence"]
    for attempt in attempts:
        targets.append(f"attempt[{attempt.index}].error")
        targets.append(f"attempt[{attempt.index}].output_tail")
        if attempt.events_ref:
            targets.append(attempt.events_ref)
    return targets
