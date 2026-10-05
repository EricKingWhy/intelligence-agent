"""W-03 (#347)：可回读 Artifact 前提下，裁剪 Runtime Context 中被取代的旧 Tool Result。

规格约束（SPEC_ROOT/06_CONTEXT_ARTIFACT_MEMORY.md §5/§8 + 票面 2026-09-27 修订）：

- **只改 Runtime 投影**：落点是 ``ToolMessage.content`` 的原位替换（骨架行），
  tool_call_id 不变、消息数不变、原事件 seq（source_range）保留；持久
  SessionEvent 与 Artifact 一字不动（不变量 #3/#5：append-only、完整保存 ≠ 完整注入）。
- **不拆 AI(tool_calls) 与 ToolResult 配对**：原位替换在结构上不可能切断
  call/result 块（compactor 的 ``_validate_tool_blocks`` 仍可通过）。
- **不伪造 artifact_ref**（§8）：只有执行期已被 overflow 外置（ToolResult 带
  ``artifact_ref``）**且** store 读回校验通过的结果才可裁；校验失败保持原样，
  把压力交给 W-04 硬护栏。

supersession 只设一条规则 **R1（同源重复）**，宁可窄而确定、无启发式：
两条 tool/result 均 ok=true + tool_name 相同 + args JSON canonical 序列化相同
+ 内容指纹相同（外置结果即 artifact_ref = 原文 sha256）⇒ 较旧的可裁、保留最新。
等价类只在**当前投影可见**（未被 compaction bracket / ``message/superseded``
shadow）的成员上构建（#643 T15/P-1）：可见性直接取自 derive 的有效投影输出，
不在此另写区间算法——被 shadow 的事件本就不在 Runtime Context（不变量 #5/#6），
对它们下裁决策既无投影效果、又会把收益门读数算进不存在于上下文的 token。
不裁清单全部确定性：指纹不同 / ok=false（失败诊断保留）/ 等价类可见成员中最新一条 /
被 ``source_event_ids`` 引用（W-02 保护事实接缝，main 上恒空）/ 无 artifact_ref
（已保存裁决 (A)：pruner 不现场 store.save，未外置结果不参与）/ ref 校验失败 /
最近 K 条 tool 结果窗口内（#414 W-31.2 ``keep_recent_tool_results``）/
一轮收益低于收益门（#414 W-31.2 ``clear_at_least_tokens``）。

零新事件类型、零模型调用、零工具执行——纯投影决策，供重启后按事件流重建解释。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AnyMessage, ToolMessage

from agent_harness.context.tokens import estimate_tokens
from agent_harness.session.event import (
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    SessionEvent,
)
from agent_harness.storage.artifact import ArtifactStore

logger = logging.getLogger("agent_harness.context.pruner")

__all__ = [
    "SKIP_BELOW_CLEAR_FLOOR",
    "SKIP_PROTECTED_REFERENCE",
    "SKIP_RECENT_WINDOW",
    "SKIP_UNREADABLE_REF",
    "PruneRecord",
    "PruneReport",
    "PruneSkip",
    "ToolResultPruner",
]

#: 骨架行 summary 的截断上限（票面定名：结论 + 精确 ID + ref 的语义骨架）。
_SUMMARY_MAX_CHARS = 120

#: 豁免原因：候选结果被某事件的 ``source_event_ids`` 引用（W-02 保护事实接缝）。
#: main 上该集合恒空（唯一写入者 session.resume 的 dangling 修复指向 tool_call_id），
#: 行为不受影响；W-02 落地后零改动生效。
SKIP_PROTECTED_REFERENCE = "protected_reference"

#: 豁免原因：artifact_ref 读回校验失败（含 KeyError 与其他 store 异常）。
SKIP_UNREADABLE_REF = "unreadable_ref"

#: 豁免原因：候选落在最近 K 条 tool 结果窗口内（#414 W-31.2；窗口按全量
#: tool/result 事件序计、含失败结果——保守口径）。
SKIP_RECENT_WINDOW = "recent_window"

#: 豁免原因：一轮 planned 的总收益估算（原文 − 骨架，token）低于收益下限门
#: （#414 W-31.2 ``clear_at_least_tokens``；门拦下也照记 planned_freed_tokens）。
SKIP_BELOW_CLEAR_FLOOR = "below_clear_floor"


@dataclass(frozen=True)
class PruneRecord:
    """一条被裁结果的解释记录：重启后按事件流可逐条重建（票面验收）。

    ``skeleton`` 是替换进投影的单行骨架 JSON——builder 缓存它供同步重放
    （``usage_snapshot`` 走同一份真相，不重算决策）。
    """

    seq: int
    tool_call_id: str
    tool_name: str
    artifact_ref: str
    superseded_by_seq: int
    skeleton: str


@dataclass(frozen=True)
class PruneSkip:
    """一条"本可裁但被确定性豁免"的记录，供测试断言与重启解释。"""

    seq: int
    tool_call_id: str
    artifact_ref: str
    reason: str


@dataclass(frozen=True)
class PruneReport:
    """prune 决策的完整账目：裁了什么、为什么、什么被豁免。

    逐条含原因（``superseded_by_seq`` + artifact_ref 指纹 / 豁免 reason），
    供重启可解释与测试断言。
    """

    pruned: tuple[PruneRecord, ...] = ()
    skipped: tuple[PruneSkip, ...] = ()
    #: #414 W-31.2：本轮 planned（窗口/豁免/校验之后本会裁掉）的总收益估算
    #: （原文 − 骨架，token）。门拦下时同样照记——观测口径与门判定同源。
    planned_freed_tokens: int = 0

    @property
    def pruned_seqs(self) -> frozenset[int]:
        return frozenset(record.seq for record in self.pruned)


@dataclass(frozen=True)
class _Candidate:
    """一条满足 R1 全部前置条件的 tool/result（等价类成员，等待豁免/校验）。

    ``content``（#414 W-31.2）是被替换 ToolMessage 的完整 content（ToolResult
    JSON）——收益门的估算基准（原文 − 骨架），与 builder memo 对账同源。
    """

    seq: int
    event_id: str
    tool_call_id: str
    tool_name: str
    artifact_ref: str
    message: str
    args: Any
    args_key: str
    content: str


class ToolResultPruner:
    """R1 判定 + store 读回校验 + 骨架行渲染；除校验缓存外无跨调用状态。

    ``validation`` 缓存按 artifact_id 记录可读性——内容寻址不可变（存在即永在，
    store ABC 无删除语义），False（校验异常）同样缓存：读回失败是保守方向，
    缓存它避免远端 store 每次 build 重复探测。两向缓存 ⇒ 决策在事件 append-only
    + 校验粘滞下单调收敛，不会回退。

    #414 W-31.2 增加两个确定性护栏（语义见 PRD §4.2 对照笔）：
    ``keep_recent_tool_results`` = 最近 K 条 tool 结果窗口豁免（含失败结果）；
    ``clear_at_least_tokens`` = 收益下限门（planned 总收益低于阈值 ⇒ 本轮整体
    不裁）。两者 0 = 关闭，回落 W-03 冻结行为。
    """

    def __init__(
        self,
        artifact_store: ArtifactStore,
        read_tool_name: str,
        *,
        keep_recent_tool_results: int = 3,
        clear_at_least_tokens: int = 5000,
    ) -> None:
        if artifact_store is None:
            raise ValueError("artifact_store is required")
        if not read_tool_name:
            # #186 教训：骨架行的回读提示必须点名装配期真实读回工具，
            # 名字不得写死、也不得静默缺省。
            raise ValueError("read_tool_name is required (skeleton note names the real tool)")
        if keep_recent_tool_results < 0 or clear_at_least_tokens < 0:
            # #414 W-31.2：负值 = 配错，构造处响亮失败；0 合法 = 关闭该护栏。
            raise ValueError(
                "keep_recent_tool_results / clear_at_least_tokens must be >= 0 (0 = off)")
        self._store = artifact_store
        self._read_tool_name = read_tool_name
        self._keep_recent_tool_results = keep_recent_tool_results
        self._clear_at_least_tokens = clear_at_least_tokens
        self._validation: dict[str, bool] = {}

    async def prune(
        self,
        pairs: list[tuple[AnyMessage, tuple[int, int] | None]],
        events: list[SessionEvent],
    ) -> tuple[list[AnyMessage], PruneReport]:
        """对 derive 产物做投影级裁剪，返回 (裁剪后 messages, 决策账目)。

        ``pairs`` 是 ``derive_messages_with_source_ranges`` 的输出；裁剪只按
        source seq 原位替换 ToolMessage.content，消息数与顺序不变。

        #643（T15/P-1）：带来源范围的 ToolMessage 即「当前投影可见」的
        tool/result——被 bracket / supersede shadow 的事件在 ``pairs`` 里没有
        对应消息，于是不进等价类。可见性口径因此与 derive 逐字同源（规则只有
        derive 一处实现），pruner 不重写 bracket 有效性判断。
        """
        visible_seqs = frozenset(
            source_range[0]
            for message, source_range in pairs
            if isinstance(message, ToolMessage) and source_range is not None
        )
        skeleton_by_seq, report = await self._decide(events, visible_seqs)
        return self.apply(pairs, skeleton_by_seq), report

    def apply(
        self,
        pairs: list[tuple[AnyMessage, tuple[int, int] | None]],
        skeleton_by_seq: Mapping[int, str],
    ) -> list[AnyMessage]:
        """把 seq → 骨架行 映射原位套到 derive 产物上（同步重放路径）。

        仅替换 source_range 已知的 ToolMessage——compaction summary（HumanMessage）
        与 dangling 合成（range=None）天然排除，seq 碰撞（bracket 起点恰为某
        tool/result seq）也不会误伤。
        """
        if not skeleton_by_seq:
            return [message for message, _source_range in pairs]
        messages: list[AnyMessage] = []
        for message, source_range in pairs:
            skeleton = (
                skeleton_by_seq.get(source_range[0])
                if source_range is not None else None
            )
            if skeleton is not None and isinstance(message, ToolMessage):
                messages.append(message.model_copy(update={"content": skeleton}))
            else:
                messages.append(message)
        return messages

    async def _decide(
        self, events: list[SessionEvent], visible_seqs: frozenset[int],
    ) -> tuple[dict[int, str], PruneReport]:
        calls = _collect_calls(events)
        groups: dict[tuple[str, str, str], list[_Candidate]] = {}
        for event in events:
            if event.type != TOOL_RESULT or event.seq not in visible_seqs:
                # #643：只对当前投影可见的结果下决策——被 shadow 的成员不在
                # Runtime Context 里，既不可能是 survivor，也不该进裁剪账目。
                continue
            candidate = _candidate_from(event, calls)
            if candidate is None:
                continue
            key = (candidate.tool_name, candidate.args_key, candidate.artifact_ref)
            groups.setdefault(key, []).append(candidate)
        protected = {
            ref for event in events for ref in (event.source_event_ids or [])
            if isinstance(ref, str)
        }
        # #414 W-31.2 keep_recent_tool_results：最近 K 条 tool 结果窗口（按全量
        # 事件序计、**含失败结果**——失败结果占窗口名额，口径保守）。
        window: frozenset[int] = frozenset()
        if self._keep_recent_tool_results > 0:
            result_seqs = [e.seq for e in events if e.type == TOOL_RESULT]
            window = frozenset(result_seqs[-self._keep_recent_tool_results:])
        planned: list[tuple[_Candidate, PruneRecord]] = []
        skipped: list[PruneSkip] = []
        for members in groups.values():
            # 等价类**可见**成员中最新一条永远保留；较旧者逐条过豁免与校验。
            survivor = members[-1]
            for candidate in members[:-1]:
                if candidate.event_id in protected:
                    skipped.append(PruneSkip(
                        seq=candidate.seq, tool_call_id=candidate.tool_call_id,
                        artifact_ref=candidate.artifact_ref,
                        reason=SKIP_PROTECTED_REFERENCE,
                    ))
                    continue
                if candidate.seq in window:
                    # 纯事件推导的豁免先于 store I/O：窗口命中不做读回探测。
                    skipped.append(PruneSkip(
                        seq=candidate.seq, tool_call_id=candidate.tool_call_id,
                        artifact_ref=candidate.artifact_ref,
                        reason=SKIP_RECENT_WINDOW,
                    ))
                    continue
                if not await self._readable(candidate.artifact_ref):
                    skipped.append(PruneSkip(
                        seq=candidate.seq, tool_call_id=candidate.tool_call_id,
                        artifact_ref=candidate.artifact_ref,
                        reason=SKIP_UNREADABLE_REF,
                    ))
                    continue
                planned.append((candidate, PruneRecord(
                    seq=candidate.seq,
                    tool_call_id=candidate.tool_call_id,
                    tool_name=candidate.tool_name,
                    artifact_ref=candidate.artifact_ref,
                    superseded_by_seq=survivor.seq,
                    skeleton=self._render_skeleton(candidate),
                )))
        # 第二遍裁决（#414 W-31.2 clear_at_least_tokens 收益下限门）：planned 的
        # 总收益（原文 − 骨架，token）低于阈值 ⇒ 本轮 planned 整体转豁免；收益量
        # 无论门开与否都照记（观测口径与门判定同源）。严格 < 才拦，恰好等于放行。
        planned_freed_tokens = sum(
            estimate_tokens(candidate.content) - estimate_tokens(record.skeleton)
            for candidate, record in planned
        )
        if (self._clear_at_least_tokens > 0
                and planned_freed_tokens < self._clear_at_least_tokens):
            records: list[PruneRecord] = []
            skipped.extend(PruneSkip(
                seq=record.seq, tool_call_id=record.tool_call_id,
                artifact_ref=record.artifact_ref,
                reason=SKIP_BELOW_CLEAR_FLOOR,
            ) for _candidate, record in planned)
        else:
            records = [record for _candidate, record in planned]
        records.sort(key=lambda record: record.seq)
        skipped.sort(key=lambda skip: skip.seq)
        report = PruneReport(
            pruned=tuple(records), skipped=tuple(skipped),
            planned_freed_tokens=planned_freed_tokens,
        )
        if report.pruned:
            logger.debug(
                "Tool result pruner removed %d superseded projection(s)",
                len(report.pruned),
            )
        return {record.seq: record.skeleton for record in report.pruned}, report

    async def _readable(self, artifact_ref: str) -> bool:
        verdict = self._validation.get(artifact_ref)
        if verdict is not None:
            return verdict
        try:
            await self._store.inspect(artifact_ref, max_lines=1)
            verdict = True
        except Exception:  # noqa: BLE001 — 读回失败=不可裁，保守方向不区分异常
            verdict = False
        self._validation[artifact_ref] = verdict
        return verdict

    def _render_skeleton(self, candidate: _Candidate) -> str:
        summary = candidate.message[:_SUMMARY_MAX_CHARS]
        skeleton = {
            "pruned": True,
            "tool": candidate.tool_name,
            # args 原样保留：等价类的判据之一，也是模型理解"哪次读取被裁"的
            # 精确标识（path / command / url……）。不做字段名启发式筛选——
            # args 本就随未裁的 AIMessage(tool_calls) 在场，骨架复述不引入
            # 新的无界增量。
            "args": candidate.args,
            "ok": True,
            "summary": summary,
            "artifact_ref": candidate.artifact_ref,
            "note": (f"重复旧结果已裁剪；完整原文已外置，"
                     f"可用 {self._read_tool_name}({candidate.artifact_ref}) 回读"),
        }
        return json.dumps(skeleton, ensure_ascii=False, separators=(",", ":"))


def _candidate_from(
    event: SessionEvent, calls: Mapping[str, tuple[str, Any]],
) -> _Candidate | None:
    """从 TOOL_RESULT 事件提取 R1 候选；任何前置条件不满足即返回 None（不裁）。"""
    payload = _result_payload(event)
    if payload is None or payload.get("ok") is not True:
        # ok=false（失败诊断）与坏形状事件一律保留。
        return None
    artifact_ref = payload.get("artifact_ref")
    if not isinstance(artifact_ref, str) or not artifact_ref:
        # 已保存裁决 (A)：只裁执行期已被 overflow 外置的结果；未外置结果
        # 不参与（pruner 不现场 store.save，不写死 (B) 路径）。
        return None
    call = calls.get(event.data.get("tool_call_id"))
    if call is None:
        return None
    tool_name, args = call
    args_key = _canonical_args(args)
    if args_key is None:
        return None
    message = payload.get("message")
    content = event.data.get("content")
    return _Candidate(
        seq=event.seq,
        event_id=event.event_id,
        tool_call_id=str(event.data.get("tool_call_id", "")),
        tool_name=tool_name,
        artifact_ref=artifact_ref,
        message=message if isinstance(message, str) else "",
        args=args,
        args_key=args_key,
        # #414 W-31.2：原文全文——收益门的估算基准（原文 − 骨架）。
        content=content if isinstance(content, str) else "",
    )


def _result_payload(event: SessionEvent) -> dict[str, Any] | None:
    """解析 TOOL_RESULT content（ToolResult JSON）；坏形状降级为 None。"""
    content = event.data.get("content")
    if not isinstance(content, str) or not content:
        return None
    try:
        payload = json.loads(content)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _collect_calls(events: list[SessionEvent]) -> dict[str, tuple[str, Any]]:
    """tool_call_id → (tool_name, args)，取 TOOL_CALL 与 MODEL_COMPLETED 双源。"""
    calls: dict[str, tuple[str, Any]] = {}
    for event in events:
        if event.type == TOOL_CALL:
            tc_id = event.data.get("tool_call_id")
            name = event.data.get("tool_name")
            if isinstance(tc_id, str) and tc_id and isinstance(name, str) and name:
                calls.setdefault(tc_id, (name, event.data.get("args")))
        elif event.type == MODEL_COMPLETED:
            for tc in event.data.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                name = tc.get("name")
                if isinstance(tc_id, str) and tc_id and isinstance(name, str) and name:
                    calls.setdefault(tc_id, (name, tc.get("args")))
    return calls


def _canonical_args(args: Any) -> str | None:
    """args 的 canonical JSON（键排序）；不可序列化 → None（该候选不参与分组）。"""
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
