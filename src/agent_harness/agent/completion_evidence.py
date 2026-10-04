"""EvidenceCompletionPolicy：完成门上的声明式证据策略（`#524`）。

设计定稿见 `docs/tickets/524-evidence-completion-policy-design.md`（用户裁决：
选项 A——既有 CompletionPolicy seam 上的**可选**域策略，默认关闭；装配点传本类
实例即开启，Core 运行路径不 import 本模块）。ADR 归宿：`docs/adr/0047` 的
`#524` 增补节。

- 证据 = durable 事实：`tool/call(tool_name)` 与 `tool/result`（同 call_id，
  content JSON `ok=true`）在**会话作用域**配对（与六条谓词同域，ADR-0047 D1）。
  `final_text` 里的"已完成 / 通过"永远不是证据——claim_pattern 只决定"要不要
  查证据"，不参与"证据成立"。
- 拒绝理由是稳定串 `evidence_missing:<rule_id>`（同 `QUIESCENCE_BLOCKED_PREFIX`
  的纪律：同输入同输出，客户端与用例都能断言）；`correction_feedback` 据此组装
  纠正文案（片段 `corrective:completion_evidence`），显式携带"引用既有结果、
  勿重复执行"——证据判定接受会话内已有结果，纠正循环从不需要重跑 mutating tool。
- 有界性（裁决：不造第二台 stuck 机器）：本策略不带计数器 / 闩 / 暂停路径；
  纠正循环的收敛由既有 run 预算与 stuck 检测器逐轮治理。
- `events=None`（拿不到 durable 事实）按"证据不存在"处理——拒绝而非放行，
  与"不可得 ≠ 0"同一纪律（seam 契约见 `agent.completion.CompletionPolicy`）。
- V1 非目标（已登记）：Executor 层同参 mutating 重跑硬阻断；证据新鲜度窗口；
  规则表的 settings / HTTP 配置面。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_harness.agent.completion import (
    CompletionDecision,
    CompletionPolicy,
    QuiescenceReport,
)
from agent_harness.model.config import ConfigError
from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.session.event import TOOL_CALL, TOOL_RESULT

if TYPE_CHECKING:
    from agent_harness.session.event import SessionEvent

#: 证据缺失的稳定拒绝前缀；`<rule_id>` 由规则表给出（构造期已校验非空）。
EVIDENCE_MISSING_PREFIX = "evidence_missing"


@dataclass(frozen=True)
class EvidenceRule:
    """一条证据规则：最终回答命中 `claim_pattern` ⇒ 必须存在 `required_tool_name`
    的成功执行事实（durable 配对），否则拒绝并报 `evidence_missing:<rule_id>`。

    `claim_pattern` 是 `re` 语法字符串；构造期编译校验（坏规则响亮失败，
    不静默放行——与 `model.config.ConfigError` 的既有配置纪律同源）。
    """

    rule_id: str
    claim_pattern: str
    required_tool_name: str

    def __post_init__(self) -> None:
        if not self.rule_id:
            raise ConfigError("evidence 规则的 rule_id 不能为空")
        if not self.required_tool_name:
            raise ConfigError(
                f"evidence 规则 {self.rule_id} 的 required_tool_name 不能为空"
            )
        try:
            re.compile(self.claim_pattern)
        except re.error as error:
            raise ConfigError(
                f"evidence 规则 {self.rule_id} 的 claim_pattern 非法：{error}"
            ) from error


def _successful_tool_calls(events: Sequence[SessionEvent]) -> dict[str, str]:
    """会话作用域里"执行成功"的 call_id → tool_name 映射。

    配对规则：`tool/call` 的 `tool_call_id` ↔ `tool/result` 的同 id，且 result
    content JSON 的 `ok` 为 true。腐烂数据（缺键 / content 非法 JSON / 非对象）
    只让该条失去判据，不让扫描抛错——`session.derive.detect_dangling` 同一纪律。
    """
    names: dict[str, str] = {}
    succeeded: set[str] = set()
    for event in events:
        call_id = event.data.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if event.type == TOOL_CALL:
            name = event.data.get("tool_name")
            if isinstance(name, str) and name:
                names.setdefault(call_id, name)
        elif event.type == TOOL_RESULT:
            try:
                ok = json.loads(event.data.get("content", "")).get("ok")
            except (AttributeError, TypeError, ValueError):
                continue
            if ok is True:
                succeeded.add(call_id)
    return {call_id: names[call_id] for call_id in succeeded if call_id in names}


class EvidenceCompletionPolicy(CompletionPolicy):
    """声明式证据策略（`#524`）：命中主张 ⇒ 会话里必须有对应工具的成功事实。

    规则表在构造期校验（含 rule_id 去重）；`decide` 只做"主张 → 证据"的存在性
    检查——不重跑工具、不读参数值、不评判文本质量（`02 §7` 的主观判据禁令同源）。
    """

    def __init__(self, rules: Sequence[EvidenceRule]) -> None:
        seen: set[str] = set()
        for rule in rules:
            if rule.rule_id in seen:
                raise ConfigError(f"evidence rule_id 重复：{rule.rule_id}")
            seen.add(rule.rule_id)
        self._rules = tuple(rules)

    @property
    def rules(self) -> tuple[EvidenceRule, ...]:
        return self._rules

    async def decide(
        self, *, report: QuiescenceReport, final_text: str, run_id: str,
        events: Sequence[SessionEvent] | None = None,
    ) -> CompletionDecision:
        if not report.quiescent:
            return CompletionDecision(accepted=False, reason=report.refusal_reason())
        succeeded: dict[str, str] = (
            _successful_tool_calls(events) if events is not None else {}
        )
        for rule in self._rules:
            if re.search(rule.claim_pattern, final_text) is None:
                continue
            if rule.required_tool_name not in succeeded.values():
                return CompletionDecision(
                    accepted=False,
                    reason=f"{EVIDENCE_MISSING_PREFIX}:{rule.rule_id}",
                )
        return CompletionDecision(accepted=True)

    def correction_feedback(self, decision: CompletionDecision) -> str | None:
        """证据缺失拒绝 ⇒ 组装纠正片段；其余拒绝（含 quiescence 自检）走既有 blocked 臂。

        未知 `rule_id`（理论不可达：reason 由本策略自己的规则表产生）不注入
        猜测性反馈，返回 `None` 交给 blocked 臂——宁可少一条消息，不编造内容。
        """
        prefix = f"{EVIDENCE_MISSING_PREFIX}:"
        if (
            decision.accepted
            or not decision.reason
            or not decision.reason.startswith(prefix)
        ):
            return None
        rule_id = decision.reason[len(prefix):]
        for rule in self._rules:
            if rule.rule_id == rule_id:
                return DEFAULT_REGISTRY.assemble(
                    "corrective:completion_evidence",
                    {
                        "rule_id": rule.rule_id,
                        "required_tool_name": rule.required_tool_name,
                    },
                ).fragment_text
        return None
