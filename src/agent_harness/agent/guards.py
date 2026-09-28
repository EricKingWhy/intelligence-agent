"""循环护栏：同错熔断（ADR-0014）+ 多模式 stuck 检测（`02 §5.3` / ADR-0048）。

**责任域唯一**（`02 §5.3`/`§6`）：本模块是 Runtime 的**唯一** loop guard。ADR-0014 的
`RepeatedToolFailureGuard`（同动作 + 同错误连续失败：3 次软 → 纠正消息，6 次硬）在这里被
**扩展**成五个模式，而不是另起一个护栏：

| 模式 | 事件源上的连续量 | 首达 T（replan） | 再达（2T，暂停） |
| --- | --- | ---: | ---: |
| ① `stuck.tool_failure_loop` | 同动作 + 同错误的失败（`RepeatedToolFailureGuard`） | 3 | 6 |
| ② `stuck.repeated_observation` | 同动作 + 同观察的**成功**结果 | 4 | 8 |
| ③ `stuck.no_progress_monologue` | 无工具调用的决策，连续同一签名 | 3 | 6 |
| ④ `stuck.alternating_loop` | 决策在恰好两个签名间严格交替 | 6 | 12 |
| ⑤ `stuck.project_no_progress` | 连续决策内没有任何进展证据 | 4 | 8 |

阈值表在 `02 §5.3`（**它是唯一权威**，本文件只是登记；暂停点 = 2T 的读法与理由见
ADR-0048 D2）。失败结果里 `BUDGET_EXHAUSTED` / `DEADLINE_EXCEEDED` **不喂**护栏
（`#314`/`#315`：准入前被拒不是工具失败，喂进去会把"到线即停的暂停"收成护栏动作）。

**状态全部由 SessionEvent 派生**（ADR-0048 D1）：`StuckDetector.from_events()` 在每次执行
开始时重放该逻辑 run 的事件重建计数，随后 `advance()` 只吃新事件。因此进程重启与同 run
恢复**天然**得到同样的指纹与计数（`03 §3.4` 不变量），不需要任何额外存储；重放期间产生的
信号被**丢弃**（当时的 replan / 暂停已经落过盘，重放不等于重做）。

指纹规范化与脱敏见 ADR-0048 D3：动作参数**保守**规范化（key 排序、trim、换行统一——内部
空白在命令里是语义），观察文本**激进**规范化（ANSI 剥离、空白折叠、JSON 重排——这些是
"修饰性差异"）；凭证值先经 `redact_secret_values()` 再取**截断摘要**，明文永不进指纹、
永不进事件。

无上游蓝图：pi-mono / oh-my-pi / claude-code 都没有工具失败检测。
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any

from agent_harness.redaction import redact_secret_values
from agent_harness.session.event import (
    GUARD_STUCK,
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_FAILURE_GUARD,
    TOOL_RESULT,
    SessionEvent,
)

# ── 模式名与阈值（`02 §5.3` 的表是阈值唯一权威；暂停点 = 2T，ADR-0048 D2） ──────

STUCK_PATTERN_TOOL_FAILURE = "stuck.tool_failure_loop"
STUCK_PATTERN_OBSERVATION = "stuck.repeated_observation"
STUCK_PATTERN_MONOLOGUE = "stuck.no_progress_monologue"
STUCK_PATTERN_ALTERNATING = "stuck.alternating_loop"
STUCK_PATTERN_PROJECT = "stuck.project_no_progress"

STUCK_THRESHOLDS: dict[str, int] = {
    STUCK_PATTERN_TOOL_FAILURE: 3,
    STUCK_PATTERN_OBSERVATION: 4,
    STUCK_PATTERN_MONOLOGUE: 3,
    STUCK_PATTERN_ALTERNATING: 6,
    STUCK_PATTERN_PROJECT: 4,
}

#: 纠正性 replan 消息里的人类可读模式名（`corrective:stuck_pattern` 的 `pattern_label`）。
STUCK_PATTERN_LABELS: dict[str, str] = {
    STUCK_PATTERN_TOOL_FAILURE: "同一工具调用反复以同样的方式失败",
    STUCK_PATTERN_OBSERVATION: "同一工具调用反复得到完全相同的观察",
    STUCK_PATTERN_MONOLOGUE: "模型连续多轮没有调用工具、也没有带来新信息",
    STUCK_PATTERN_ALTERNATING: "在两个动作之间来回交替",
    STUCK_PATTERN_PROJECT: "连续多轮没有任何可以被证实的进展",
}

STUCK_LEVEL_REPLAN = "replan"
STUCK_LEVEL_PAUSED = "paused"

#: 准入前被拒的错误码（不是工具失败，也不是模型在打转）：不喂护栏，也不打断已有计数。
GUARD_EXEMPT_ERROR_CODES: frozenset[str] = frozenset(
    {"BUDGET_EXHAUSTED", "DEADLINE_EXCEEDED"}
)

#: "纠正已经发生过"的两个 durable 来源（ADR-0048 D5 的"恰好一次"由它们重建）：
#: `tool/failure-guard`（① 的既有形状，soft / hard 两个 level 都算——历史里的 hard 也是
#: 纠正）与 `guard/stuck(level=replan)`（②–⑤）。**`guard/stuck(level=paused)` 不在其中**：
#: 暂停不是纠正，它是"纠正没能解开"之后的那一步——见到它反而把闩**清零**（下一个 episode
#: 从"还没纠正过"重新开始；依据见 `_replan_episode_boundary`）。
_REPLAN_ACTION_TYPES: frozenset[str] = frozenset({TOOL_FAILURE_GUARD, GUARD_STUCK})

# ── 指纹：规范化 + 脱敏 + 截断摘要（ADR-0048 D3） ────────────────────────────

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_WHITESPACE = re.compile(r"\s+")
#: 摘要长度（hex 字符）。指纹只用于**相等比较**，不需要抗碰撞之外的任何性质。
_DIGEST_CHARS = 16
_SEPARATOR = "\x1f"


def _digest(*parts: str) -> str:
    """截断摘要：明文（含凭证）永不落盘，落盘的只有它。"""
    payload = _SEPARATOR.join(parts)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def canonical_observation(value: str) -> str:
    """观察文本：**激进**规范化——ANSI 剥离、空白折叠、JSON 重排。

    这些差异是"修饰性/格式"的（`02 §5.3` 明文：无关格式、修饰性文字变化 MUST NOT 构成
    进展），pretty-print 与 compact 的 JSON、带色与不带色的 stderr 都是同一个观察。
    """
    text = _ANSI_ESCAPE.sub("", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE.sub(" ", text).strip()
    if text[:1] in ("{", "["):
        try:
            parsed = json.loads(text)
        except ValueError:
            return text
        return json.dumps(parsed, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
    return text


def _canonical_payload(value: Any) -> Any:
    """递归规范化结构化 payload（工具结果的 `data`）：数值/字符串归一，容器保结构。"""
    if isinstance(value, str):
        return canonical_observation(value)
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, Mapping):
        return {str(key): _canonical_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_payload(item) for item in value]
    return str(value)


def action_fingerprint(tool_name: str, args: Mapping[str, Any] | None) -> str:
    """动作指纹 = 摘要(工具名 + 规范化参数)。call id 不在参数里，天然不参与。"""
    canonical = json.dumps(
        {str(key): _canonical_payload(item) for key, item in (args or {}).items()},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return _digest("action", tool_name, redact_secret_values(canonical))


def outcome_fingerprint(
    *,
    ok: bool,
    error_code: str | None,
    message: str,
    data: Any = None,
) -> str:
    """结果指纹：失败 = `error:<code>` + 规范化 message；成功 = 规范化 message + data。

    **不含 `metadata`**（ADR-0048 D3 残余 3）：`duration_ms` / `attempt` 这类每次必变的
    字段一旦进指纹，"同观察"永远不成立，"等价观察"就成了假命题。
    """
    kind = "ok" if ok else f"error:{error_code or ''}"
    payload = canonical_observation(message or "")
    if data is not None:
        payload += _SEPARATOR + json.dumps(
            _canonical_payload(data), sort_keys=True, ensure_ascii=False,
            separators=(",", ":"),
        )
    return _digest("outcome", kind, redact_secret_values(payload))


def decision_signature(*, content: str, calls: Sequence[tuple[str, str]]) -> str:
    """一次模型决策的签名：规范化正文 + 它请求的动作指纹（保序）。

    签名里**没有** call id —— 换 id 不是新决策（`02 §5.3`：call id 不构成进展）。
    """
    body = canonical_observation(content or "")
    joined = "\x1e".join(f"{name}:{fp}" for name, fp in calls)
    return _digest("decision", redact_secret_values(body), joined)


# ── ADR-0014 的两级护栏（模式 ① 的引擎） ─────────────────────────────────────


class GuardLevel(enum.IntEnum):
    """护栏动作级别（IntEnum 便于 max() 比较）。"""

    NONE = 0
    SOFT = 1
    HARD = 2


@dataclass(frozen=True)
class GuardSignal:
    """observe() 的返回：本轮观察到的最严重动作 + 诊断字段。"""

    level: GuardLevel
    tool_name: str
    fingerprint: str
    consecutive_failures: int


class RepeatedToolFailureGuard:
    """跟踪连续同动作失败，在阈值触发软/硬两级熔断（ADR-0014 决策 2-6；模式 ①）。

    状态机：动作指纹变化 → 重置计数 + soft_triggered；指纹不变的成功 → 只有"带来不同
    观察"时才复位（ADR-0048 D4 的收窄：Q12(b) 防的是"等价观察洗计数"，不是"状态真的
    变了"）；失败 → 计数++，按阈值判定。错误码（`02 §5.3` 的"同错误"）只在两侧都已知
    且不同时重新计数——历史事件 / 纯执行场景缺错误码时不细分错误种类，与 ADR-0014 的
    原始语义一致。
    """

    def __init__(self, *, soft_threshold: int = 3, hard_threshold: int = 3) -> None:
        self._soft_threshold = soft_threshold
        self._hard_threshold = hard_threshold
        self._fingerprint: str | None = None
        self._consecutive_failures = 0
        self._soft_triggered = False
        self._error_code: str | None = None
        self._last_failure_fp: str | None = None

    @staticmethod
    def make_fingerprint(tool_name: str, args: dict) -> str:
        """规范化指纹：工具名 + 规范化参数（key 顺序无关、Unicode 不转义）。

        `#317` 起它就是 `action_fingerprint`（同一份规范化，不再另写一份排序 JSON）；
        保留本方法是因为它是 ADR-0014 的公开形状（既有用例与 `ToolRuntimeSignal`
        的消费侧按它取值）。
        """
        return action_fingerprint(tool_name, args)

    def observe(self, tool_name: str, args: dict, ok: bool) -> GuardSignal:
        """喂入一次工具结果，返回本轮该不该触发动作（ADR-0014 的原始入口）。

        不传错误码 / 观察：等价于"不细分错误种类、没有可比观察"——纯状态机用例与
        注入型调用方走这条，语义逐字不变。
        """
        return self.observe_action(
            action_fp=self.make_fingerprint(tool_name, args),
            tool_name=tool_name, ok=ok, error_code=None, outcome_fp=None,
        )

    def observe_action(
        self,
        *,
        action_fp: str,
        tool_name: str,
        ok: bool,
        error_code: str | None = None,
        outcome_fp: str | None = None,
    ) -> GuardSignal:
        """喂入一次工具结果（带错误码 / 观察指纹），返回该不该触发动作。"""
        if action_fp != self._fingerprint:
            self._fingerprint = action_fp
            self._consecutive_failures = 0
            self._soft_triggered = False
            self._error_code = None
            self._last_failure_fp = None

        if ok:
            # 相关进展（ADR-0048 D4）：同动作这一次成功带来**不同**的观察 ⇒ 之前那条
            # "同错连续"的模式已经不成立，复位。观察相同（等价成功）不复位——Q12(b)
            # 防"振荡洗计数"的原意在等价这一侧保留。
            if (
                outcome_fp is not None
                and self._last_failure_fp is not None
                and outcome_fp != self._last_failure_fp
            ):
                self._consecutive_failures = 0
                self._soft_triggered = False
            self._last_failure_fp = None
            return GuardSignal(
                GuardLevel.NONE, tool_name, action_fp, self._consecutive_failures,
            )

        if (
            error_code is not None
            and self._error_code is not None
            and error_code != self._error_code
        ):
            # 同动作但换了错误种类：这是**另一个**模式（`02 §5.3` 的"同动作 + 同错误"）
            self._consecutive_failures = 0
            self._soft_triggered = False
        if error_code is not None:
            self._error_code = error_code
        if outcome_fp is not None:
            self._last_failure_fp = outcome_fp

        self._consecutive_failures += 1

        if not self._soft_triggered and self._consecutive_failures >= self._soft_threshold:
            self._soft_triggered = True
            return GuardSignal(
                GuardLevel.SOFT, tool_name, action_fp, self._consecutive_failures,
            )

        if (
            self._soft_triggered
            and self._consecutive_failures >= self._soft_threshold + self._hard_threshold
        ):
            return GuardSignal(
                GuardLevel.HARD, tool_name, action_fp, self._consecutive_failures,
            )

        return GuardSignal(
            GuardLevel.NONE, tool_name, action_fp, self._consecutive_failures,
        )

    def reset(self) -> None:
        """手动重置（如新一轮 run 开始时）。"""
        self._fingerprint = None
        self._consecutive_failures = 0
        self._soft_triggered = False
        self._error_code = None
        self._last_failure_fp = None

    @property
    def soft_threshold(self) -> int:
        """模式 ① 的 T（replan 线；暂停线是 `T + hard_threshold`）。

        公开只读是为了让 `StuckDetector` 报告的 `threshold` 与**真正触发**的那个计数
        同源：注入引擎时两者若各说一套，事件里的 `count=1, threshold=3` 就是一条
        自相矛盾的证据（调用方只注入引擎，没法同时告诉检测器阈值是多少）。
        """
        return self._soft_threshold


# ── 多模式检测（`02 §5.3` 的模式 ②–⑤ + 五模式的统一信号） ─────────────────────


@dataclass(frozen=True)
class StuckSignal:
    """一次被检出的 stuck 动作：`replan` 或 `paused`（ADR-0048 D5）。"""

    level: str
    pattern: str
    count: int
    threshold: int
    tool_name: str | None = None
    fingerprint: str | None = None

    @property
    def label(self) -> str:
        return STUCK_PATTERN_LABELS.get(self.pattern, self.pattern)

    def as_event_data(self) -> dict[str, Any]:
        """`guard/stuck` 的 data（缺席键不落，不用 `null` 冒充"没有"）。

        `replan_count` 是该模式**已发出**的纠正性 replan 次数：两种 level 都是 1
        ——这是"恰好一次"这条不变量的可读投影（replan 级就是那一次本身，paused 级
        表示它已经用掉了）。
        """
        data: dict[str, Any] = {
            "level": self.level,
            "pattern": self.pattern,
            "count": self.count,
            "threshold": self.threshold,
            "replan_count": 1,
        }
        if self.tool_name is not None:
            data["tool_name"] = self.tool_name
        if self.fingerprint is not None:
            data["fingerprint"] = self.fingerprint
        return data

    def as_pause_payload(self) -> dict[str, Any]:
        """`run/paused.data.stuck`（`03 §3.4`：pattern / threshold / replan count / counters）。"""
        return {
            "pattern": self.pattern,
            "threshold": self.threshold,
            "count": self.count,
            "replan_count": 1,
            "fingerprint": self.fingerprint,
        }


#: 同级别信号的固定优先级（`02 §5.3` 表的顺序）：同一条决策可能同时命中多个模式，
#: 取表中靠前的那一个做动作，避免同一次触发发两条 guard 事件。
_PATTERN_ORDER: tuple[str, ...] = (
    STUCK_PATTERN_TOOL_FAILURE,
    STUCK_PATTERN_OBSERVATION,
    STUCK_PATTERN_MONOLOGUE,
    STUCK_PATTERN_ALTERNATING,
    STUCK_PATTERN_PROJECT,
)


@dataclass
class _ToolObservation:
    """一次工具执行的判定输入（从 `tool/call` + `tool/result` 派生）。"""

    tool_name: str
    action_fp: str
    ok: bool
    error_code: str | None
    outcome_fp: str


@dataclass
class _OpenDecision:
    """一次模型决策的判定输入（从 `model/completed` + 它的结果派生）。"""

    signature: str
    has_tool_calls: bool
    pending_ids: set[str]
    observations: list[_ToolObservation]


class StuckDetector:
    """五个 stuck 模式的唯一判定点（ADR-0048 D1–D4）。

    用法（Runtime 的两个接入点）：构造时 ``StuckDetector.from_events(session.events,
    run_id)``，每轮结束 ``signals = detector.advance(session.events)``，再按
    `worst_stuck_signal(signals)` 最多执行**一个**动作（replan ⇒ 注入纠正消息继续
    循环；paused ⇒ `run/paused(reason=stuck)`）。
    """

    def __init__(
        self,
        *,
        run_id: str | None = None,
        thresholds: Mapping[str, int] | None = None,
        failure_guard: RepeatedToolFailureGuard | None = None,
    ) -> None:
        self._run_id = run_id
        self._thresholds: dict[str, int] = dict(STUCK_THRESHOLDS)
        if thresholds:
            self._thresholds.update(thresholds)
        # ① 的引擎：soft=T（replan），hard=2T（暂停）——ADR-0014 的 3/6 形状
        # （ADR-0048 D5 把 HARD 的动作从 `run/failed` 改成暂停）。
        # 注入的引擎**只贡献阈值**：状态一律由重放重建，所以先 reset 再喂事件——
        # 调用方残留的计数（例如跨 run 复用的实例）不得参与本 run 的判定。
        if failure_guard is None:
            failure_guard = RepeatedToolFailureGuard(
                soft_threshold=self._thresholds[STUCK_PATTERN_TOOL_FAILURE],
                hard_threshold=self._thresholds[STUCK_PATTERN_TOOL_FAILURE],
            )
        else:
            # 报告里的 threshold 必须与真正触发它的那个计数同源（见 soft_threshold）。
            self._thresholds[STUCK_PATTERN_TOOL_FAILURE] = failure_guard.soft_threshold
        self._failure = failure_guard
        self._failure.reset()
        # ② 同动作 + 同观察（仅成功结果；失败由 ① 更严格地覆盖）
        self._observation_key: str | None = None
        self._observation_count = 0
        self._observation_replanned = False
        # ③ 无进展独白
        self._monologue_signature: str | None = None
        self._monologue_count = 0
        self._monologue_replanned = False
        # ④ 两模式交替：保留 2T 个签名就够判"交替尾长"
        self._signatures: deque[str] = deque(
            maxlen=2 * self._thresholds[STUCK_PATTERN_ALTERNATING],
        )
        self._alternating_replanned = False
        # ⑤ 项目级无进展窗口
        self._window_count = 0
        self._window_replanned = False
        # 全局"已经纠正过一次"闩（ADR-0048 D5 的"恰好一次 replan"）：每个模式各有自己的
        # 首达闩（上面那些），但**整个 run** 只给一条纠正消息——五个模式在同一段历史里
        # 会互相重叠（一条反复失败的调用同时命中 ① 与 ⑤：第 3 次 ① 要 replan、第 4 次 ⑤
        # 又想要一条），若按模式各发一条，模型会在两轮里收到两条几乎同义的纠正，
        # 而契约写的是"exactly one corrective replan"。
        # 该闩也由事件重建（重放时命中的 replan 信号 + durable 的护栏事件），所以
        # 重启 / 恢复后不会把"已经纠正过"忘掉、又补发一条。
        self._replanned = False
        # 进展水位：本 run 见过的观察指纹（"新的观察" = 进展）
        self._seen_observations: set[str] = set()
        # 事件游标 + tool/call 待配对表（按 tool_call_id）+ 当前未收口的决策
        self._cursor = -1
        self._pending_calls: dict[str, _ToolObservation] = {}
        self._open: _OpenDecision | None = None

    @classmethod
    def from_events(
        cls,
        events: Iterable[SessionEvent],
        run_id: str | None,
        *,
        thresholds: Mapping[str, int] | None = None,
        failure_guard: RepeatedToolFailureGuard | None = None,
    ) -> StuckDetector:
        """重放既有事件重建状态（重启 / 同 run 恢复的唯一入口）。

        重放期间产生的信号**丢弃**：那些动作（replan / 暂停）在当时就已经落盘，
        重放只负责把计数与指纹恢复成同样的值（`03 §3.4` 不变量）。
        """
        detector = cls(
            run_id=run_id, thresholds=thresholds, failure_guard=failure_guard,
        )
        detector.advance(events)
        return detector

    # ── 对外：吃事件、出信号 ────────────────────────────────────────────

    def advance(
        self,
        events: Iterable[SessionEvent],
        *,
        externally_counted_call_ids: AbstractSet[str] = frozenset(),
    ) -> list[StuckSignal]:
        """处理游标之后的新事件，返回这次新产生的信号（按发生顺序）。

        传全量事件列表即可：函数自己按 `seq > cursor` 过滤，重复调用是幂等的。

        `externally_counted_call_ids`：这些 `tool_call_id` 的**失败计数由另一个 durable
        账本负责**（今天是委派树账本，见 `external_failure_signal`）——本检测器不在 ①
        上重复数它们，否则同一批失败会被记两遍（父 run 一次、树账本一次），阈值提前到顶。
        其余模式（②–⑤）照常吃它们的结果：那几条讲的是观察与进展，树账本不观测这些。
        这个集合是**调用方当场给出的**（它才知道哪几条执行带了 `runtime_signal`），
        不进 durable 事实——重放时那些调用在事件流里的样子与别的失败调用完全相同，
        检测器的 ① 会照着数；差别只在"当场那一轮"由谁计数，而结论（谁先到阈值）由
        调用方在 `worst_stuck_signal` 处一并比较。

        返回前做一次**全 run 去重**：已经纠正过一次时，任何模式的首达 replan 信号都
        不再给出（各模式自己的首达闩照旧置位——否则它会在下一次又冒出来）。这条规则
        的契约面是"exactly one corrective replan"（ADR-0048 D5），实现面见
        `self._replanned`。
        """
        signals: list[StuckSignal] = []
        for event in events:
            if event.seq <= self._cursor:
                continue
            if self._run_id is not None and event.run_id != self._run_id:
                continue
            self._cursor = max(self._cursor, event.seq)
            if event.type in _REPLAN_ACTION_TYPES:
                # durable 的"已经纠正过"事实（重启后重放的主要来源）：本 run 的历史里
                # 落过一条纠正性护栏事件 ⇒ 那次 replan 已经发生过。
                self._replanned = self._replan_episode_boundary(event)
            if event.type == MODEL_COMPLETED:
                signals.extend(self._on_model_completed(event))
            elif event.type == TOOL_CALL:
                self._on_tool_call(event)
            elif event.type == TOOL_RESULT:
                signals.extend(self._on_tool_result(
                    event, externally_counted=externally_counted_call_ids,
                ))
        fresh = [
            signal for signal in signals
            if signal.level != STUCK_LEVEL_REPLAN or not self._replanned
        ]
        if any(signal.level == STUCK_LEVEL_REPLAN for signal in fresh):
            # 给出的信号会被执行一次（两个接入点都无条件执行）——闩在这里置位，
            # 同一个 `advance` 里第二个模式的首达信号就已经被上面那条过滤掉了。
            self._replanned = True
        return fresh

    # ── 事件处理 ────────────────────────────────────────────────────────

    def _on_tool_call(self, event: SessionEvent) -> None:
        data = event.data or {}
        call_id = data.get("tool_call_id")
        tool_name = data.get("tool_name")
        if not isinstance(call_id, str) or not isinstance(tool_name, str):
            return
        args = data.get("args")
        self._pending_calls[call_id] = _ToolObservation(
            tool_name=tool_name,
            action_fp=action_fingerprint(
                tool_name, args if isinstance(args, Mapping) else {},
            ),
            ok=False, error_code=None, outcome_fp="",
        )

    def _on_tool_result(
        self, event: SessionEvent, *,
        externally_counted: AbstractSet[str] = frozenset(),
    ) -> list[StuckSignal]:
        data = event.data or {}
        call_id = data.get("tool_call_id")
        call = self._pending_calls.pop(call_id, None) if isinstance(call_id, str) else None
        parsed = _parse_result_payload(data.get("content"))
        if call is None or parsed is None:
            # 配不上 tool/call 的结果（异常窗口 / 恢复期合成结果）不进判定：判不出
            # "同动作"，猜一个动作指纹等于把两条不同的动作混成一条。
            return []
        ok, error_code, message, result_data = parsed
        if not ok and error_code in GUARD_EXEMPT_ERROR_CODES:
            # 准入前被拒（配额 / 到点）：不是工具失败，也不打断已有计数（#314/#315）
            return []
        observation = _ToolObservation(
            tool_name=call.tool_name, action_fp=call.action_fp, ok=ok,
            error_code=error_code,
            outcome_fp=outcome_fingerprint(
                ok=ok, error_code=error_code, message=message, data=result_data,
            ),
        )
        signals = self._feed_observation(
            observation,
            # ① 的计数归调用方点名的那个账本（见 advance 的 docstring）。
            count_failure=not (
                isinstance(call_id, str) and call_id in externally_counted
            ),
        )
        if self._open is not None and isinstance(call_id, str):
            self._open.pending_ids.discard(call_id)
            self._open.observations.append(observation)
            if not self._open.pending_ids:
                signals.extend(self._settle_decision())
        return signals

    def _on_model_completed(self, event: SessionEvent) -> list[StuckSignal]:
        data = event.data or {}
        raw_calls = data.get("tool_calls")
        requested: list[tuple[str, str]] = []
        pending_ids: set[str] = set()
        calls = raw_calls if isinstance(raw_calls, list) else []
        for raw in calls:
            if not isinstance(raw, Mapping):
                continue
            name = raw.get("name")
            if not isinstance(name, str):
                continue
            args = raw.get("args")
            requested.append(
                (name, action_fingerprint(name, args if isinstance(args, Mapping) else {})),
            )
            call_id = raw.get("id")
            if isinstance(call_id, str):
                pending_ids.add(call_id)
        # 上一轮若还开着（结果缺失 / 等不到下一轮），先按已有事实收口
        signals = self._settle_decision() if self._open is not None else []
        self._open = _OpenDecision(
            signature=decision_signature(
                content=str(data.get("content") or ""), calls=requested,
            ),
            has_tool_calls=bool(requested),
            pending_ids=pending_ids,
            observations=[],
        )
        if not requested:
            # 无工具调用的决策：进展与观察都不可能再有，当场收口（③ 的可达面就是它）
            signals.extend(self._settle_decision())
        return signals

    # ── 判定 ────────────────────────────────────────────────────────────

    def _feed_observation(
        self, observation: _ToolObservation, *, count_failure: bool = True,
    ) -> list[StuckSignal]:
        """①/② 的连续量（每次工具结果一条）。

        `count_failure=False`：这一条不喂 ①（它的失败计数在别处，见 `advance`）。
        ② 照常——"同动作 + 同观察"是本 run 自己的事实。
        """
        signals: list[StuckSignal] = []
        failure_threshold = self._thresholds[STUCK_PATTERN_TOOL_FAILURE]
        guard_signal = (
            self._failure.observe_action(
                action_fp=observation.action_fp, tool_name=observation.tool_name,
                ok=observation.ok, error_code=observation.error_code,
                outcome_fp=observation.outcome_fp,
            )
            if count_failure else None
        )
        if guard_signal is not None and guard_signal.level == GuardLevel.SOFT:
            signals.append(StuckSignal(
                level=STUCK_LEVEL_REPLAN, pattern=STUCK_PATTERN_TOOL_FAILURE,
                count=guard_signal.consecutive_failures, threshold=failure_threshold,
                tool_name=guard_signal.tool_name, fingerprint=guard_signal.fingerprint,
            ))
        elif guard_signal is not None and guard_signal.level == GuardLevel.HARD:
            signals.append(StuckSignal(
                level=STUCK_LEVEL_PAUSED, pattern=STUCK_PATTERN_TOOL_FAILURE,
                count=guard_signal.consecutive_failures, threshold=failure_threshold,
                tool_name=guard_signal.tool_name, fingerprint=guard_signal.fingerprint,
            ))

        if observation.ok:
            key = f"{observation.action_fp}{_SEPARATOR}{observation.outcome_fp}"
            if key == self._observation_key:
                self._observation_count += 1
            else:
                self._observation_key = key
                self._observation_count = 1
                self._observation_replanned = False
            signals.extend(self._observation_signals(
                observation.tool_name, observation.action_fp,
            ))
        else:
            # 失败不喂 ②，但**打断**它的连续（同动作这一次的观察已经不是同一个了）
            self._observation_key = None
            self._observation_count = 0
            self._observation_replanned = False
        return signals

    def _observation_signals(self, tool_name: str, action_fp: str) -> list[StuckSignal]:
        threshold = self._thresholds[STUCK_PATTERN_OBSERVATION]
        count = self._observation_count
        signal = StuckSignal(
            level="", pattern=STUCK_PATTERN_OBSERVATION, count=count,
            threshold=threshold, tool_name=tool_name, fingerprint=action_fp,
        )
        if count == threshold and not self._observation_replanned:
            self._observation_replanned = True
            return [with_level(signal, STUCK_LEVEL_REPLAN)]
        if count >= 2 * threshold:
            return [with_level(signal, STUCK_LEVEL_PAUSED)]
        return []

    def _replan_episode_boundary(self, event: SessionEvent) -> bool:
        """一条 durable 护栏事件之后，"本 episode 已纠正过"该记成什么。

        作用域是 **episode**（一次"检测 → 纠正 → 再达阈值则暂停"），不是整个 run：

        - `tool/failure-guard`（任意 level）⇒ `True`（纠正已经发生）；
        - `guard/stuck(level=replan)` ⇒ `True`；
        - `guard/stuck(level=paused)` ⇒ `False`——暂停是 episode 的结束。下一次执行
          （`run/resumed` 之后）从"还没纠正过"开始：若模型在**新的**模式下打转，
          它还能拿到一条纠正，而不是被上一段历史的静音一直捂住。同一条模式则拿不到
          ——它自己的首达闩在重放里已经置位，`count > T` 只能走暂停分支。
        """
        if event.type == TOOL_FAILURE_GUARD:
            return True
        return (event.data or {}).get("level") != STUCK_LEVEL_PAUSED

    def _settle_decision(self) -> list[StuckSignal]:
        """一次决策的结局已知（无工具调用 / 结果齐了 / 被下一轮取代）⇒ ③④⑤ 判定。"""
        decision = self._open
        self._open = None
        if decision is None:
            return []
        progress = self._decision_progress(decision)
        signals: list[StuckSignal] = []
        signals.extend(self._monologue_signals(decision))
        signals.extend(self._alternating_signals(decision))
        signals.extend(self._window_signals(progress))
        return signals

    def _decision_progress(self, decision: _OpenDecision) -> bool:
        """项目进展（ADR-0048 D4）：成功的**新**观察。失败不算（又学到一个错误不是进展）。"""
        fresh = [
            observation for observation in decision.observations
            if observation.ok and observation.outcome_fp not in self._seen_observations
        ]
        for observation in decision.observations:
            if observation.ok:
                self._seen_observations.add(observation.outcome_fp)
        return bool(fresh)

    def _monologue_signals(self, decision: _OpenDecision) -> list[StuckSignal]:
        threshold = self._thresholds[STUCK_PATTERN_MONOLOGUE]
        if decision.has_tool_calls:
            self._monologue_signature = None
            self._monologue_count = 0
            self._monologue_replanned = False
            return []
        if decision.signature == self._monologue_signature:
            self._monologue_count += 1
        else:
            self._monologue_signature = decision.signature
            self._monologue_count = 1
            self._monologue_replanned = False
        count = self._monologue_count
        signal = StuckSignal(
            level="", pattern=STUCK_PATTERN_MONOLOGUE, count=count,
            threshold=threshold, fingerprint=decision.signature,
        )
        if count == threshold and not self._monologue_replanned:
            self._monologue_replanned = True
            return [with_level(signal, STUCK_LEVEL_REPLAN)]
        if count >= 2 * threshold:
            return [with_level(signal, STUCK_LEVEL_PAUSED)]
        return []

    def _alternating_signals(self, decision: _OpenDecision) -> list[StuckSignal]:
        threshold = self._thresholds[STUCK_PATTERN_ALTERNATING]
        self._signatures.append(decision.signature)
        tail = _alternating_tail(list(self._signatures))
        if tail < threshold:
            self._alternating_replanned = False
            return []
        signal = StuckSignal(
            level="", pattern=STUCK_PATTERN_ALTERNATING, count=tail,
            threshold=threshold, fingerprint=decision.signature,
        )
        if tail == threshold and not self._alternating_replanned:
            self._alternating_replanned = True
            return [with_level(signal, STUCK_LEVEL_REPLAN)]
        if tail >= 2 * threshold:
            return [with_level(signal, STUCK_LEVEL_PAUSED)]
        return []

    def _window_signals(self, progress: bool) -> list[StuckSignal]:
        threshold = self._thresholds[STUCK_PATTERN_PROJECT]
        if progress:
            self._window_count = 0
            self._window_replanned = False
            return []
        self._window_count += 1
        count = self._window_count
        signal = StuckSignal(
            level="", pattern=STUCK_PATTERN_PROJECT, count=count, threshold=threshold,
        )
        if count == threshold and not self._window_replanned:
            self._window_replanned = True
            return [with_level(signal, STUCK_LEVEL_REPLAN)]
        if count >= 2 * threshold:
            return [with_level(signal, STUCK_LEVEL_PAUSED)]
        return []


def with_level(signal: StuckSignal, level: str) -> StuckSignal:
    """同一份计数事实换个 level（`StuckSignal` 是 frozen 的，重建而不是改）。"""
    return StuckSignal(
        level=level, pattern=signal.pattern, count=signal.count,
        threshold=signal.threshold, tool_name=signal.tool_name,
        fingerprint=signal.fingerprint,
    )


def worst_stuck_signal(signals: Sequence[StuckSignal]) -> StuckSignal | None:
    """一次 `advance` 里最多执行一个动作：暂停优先，其次按 `02 §5.3` 的表序。

    例外（操作约束）：**同一批里刚发出 replan 的那个模式，这一批不暂停**——`02 §5.3`
    的"replan **后**同一模式仍持续 ⇒ 暂停"要求至少一次后续观测，而一批事件里 T 与 2T
    可能同时到线。抑制是**按模式**的（跨模式同批仍会挑出暂停，ADR-0048 D5 / 残余 12）。
    """
    if not signals:
        return None
    replanned = {
        signal.pattern for signal in signals if signal.level == STUCK_LEVEL_REPLAN
    }
    pool = [
        signal for signal in signals
        if not (signal.level == STUCK_LEVEL_PAUSED and signal.pattern in replanned)
    ]
    paused = [signal for signal in pool if signal.level == STUCK_LEVEL_PAUSED]
    pool = paused or pool
    return min(pool, key=lambda signal: _PATTERN_ORDER.index(signal.pattern))


def external_failure_signal(
    *, level: str, tool_name: str, fingerprint: str, count: int,
    threshold: int | None = None,
) -> StuckSignal | None:
    """把**另一个 durable 计数器**给出的 ① 判定转成本 run 的 stuck 信号。

    用例只有一个：委派树的持久护栏（`10` 的 persistent guard / `#88`）。它对每个
    `delegate` 结果在**树账本**里计数（跨后代、跨进程），并把 SOFT / HARD 两级的结论
    随 `ToolResult.runtime_signal` 交回父 run。那个计数**不在本 run 的事件流里**——
    子会话的失败事件落在子会话的 JSONL 上，父 run 只能看到"这一条 delegate 失败了"。
    所以它不是"事件派生"能覆盖的一维：账本自己就是 durable 的（重启后仍在，这正是
    persistent 的含义），本函数只把它的结论翻译成与检测器**同一种**信号（ADR-0048 D5
    的统一收口：replan 一次 / 再达阈值就暂停）。

    `threshold` 缺省 = ① 的首达线（`STUCK_THRESHOLDS`）：树账本有它自己的 3/3 两级
    （`SqliteDelegationTreeLedger.observe_result` 的默认参数），与本 run 注入的引擎阈值
    无关——报告里的 `threshold` 必须与真正触发它的那个计数同源。

    三级之外的取值 / 未知 level ⇒ `None`（不知道就不动作；调用方据此不把它计入重复
    计数，见 `StuckDetector.advance` 的 `externally_counted_call_ids`）。
    """
    normalized = level.strip().lower()
    if normalized == "soft":
        signal_level = STUCK_LEVEL_REPLAN
    elif normalized == "hard":
        signal_level = STUCK_LEVEL_PAUSED
    else:
        return None
    return StuckSignal(
        level=signal_level, pattern=STUCK_PATTERN_TOOL_FAILURE,
        count=count,
        threshold=(
            STUCK_THRESHOLDS[STUCK_PATTERN_TOOL_FAILURE]
            if threshold is None else threshold
        ),
        tool_name=tool_name, fingerprint=fingerprint,
    )


def _alternating_tail(signatures: list[str]) -> int:
    """尾部"相邻两两不同"的最长后缀长度；只有恰好两个不同签名才算交替循环。"""
    if len(signatures) < 2:
        return 0
    length = 1
    while length < len(signatures) and signatures[-1 - length] != signatures[-length]:
        length += 1
    tail = signatures[-length:]
    if len(set(tail)) != 2:
        return 0
    return length


def _parse_result_payload(content: Any) -> tuple[bool, str | None, str, Any] | None:
    """`tool/result.content` → (ok, error_code, message, data)。

    形状权威是 `tooling/result.py::ToolResult`（`content` 是它的 JSON）；这里按**原始
    dict** 读而不是 import 那个类：事件派生代码与 `run_budget.consumed_from_events`
    同一条纪律（只依赖已落盘的事实形状），也避免 `agent → tooling` 的导入环。
    """
    if not isinstance(content, str) or not content:
        return None
    try:
        parsed = json.loads(content)
    except ValueError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    ok = bool(parsed.get("ok"))
    error_code = parsed.get("error_code")
    message = parsed.get("message")
    return (
        ok,
        error_code if isinstance(error_code, str) else None,
        message if isinstance(message, str) else "",
        parsed.get("data"),
    )
