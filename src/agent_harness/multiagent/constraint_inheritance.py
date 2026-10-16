"""子 agent 约束/授权继承（#1192 / #1176 Phase 3）。

照抄 Claude Code 的 subagent 模型（一手来源 `https://code.claude.com/docs/en/sub-agents`，
2026-10-16 读取；本仓调研 `~/workspace/research/codebuddy-1176-scope-research.md` §问题 3）：

- 非 fork subagent **默认继承持续约束**（项目级 + 全局级 active）；
- **不继承会话态**（本会话专属事实由父在委派任务描述里显式转述）；
- 当前有效 **authorization**（授权边界）随行——会话级 active 授权 + 项目/全局级 active 授权，
  保证不出现"父被允许、子被拦"的错位；
- 提供 `omit_constraints` 干净启动逃生门（对齐 `omitClaudeMd`）。

与 #1191 的关系：项目/全局级 active 约束的取用**复用** `scoped_context_facts`（精确取用不检索、
覆盖过滤同源）；本模块只负责"把父会话可见的继承集按 active 投影出来"，不新造第二套序列化。

注入与审计分离（票面 §四.3 / §五.5）：继承集序列化后经 `inherited_protected_facts` 进子 agent
上下文组装；同一份快照以**独立事件** `agent/constraint-inheritance` 落子会话事件流（可审计、可展示），
**不改写子 agent 的事实来源事件**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_harness.session.derive import ProtectedFact, derive_protected_facts
from agent_harness.session.event import AGENT_CONSTRAINT_INHERITANCE
from agent_harness.session.scoped_constraints import (
    ScopedConstraintStore,
    scoped_context_facts,
)

#: 子会话里承载"继承集快照"的事件类型（独立事件，非 fact 来源事件）。
#: 单一事实源在 `session/event.py`（与其他事件类型同处一处）。
CONSTRAINT_INHERITANCE_EVENT = AGENT_CONSTRAINT_INHERITANCE

#: authorization 是可继承的会话态**唯一例外**（票面 §三.2）。
_AUTHORIZATION_TYPE = "authorization"


def _row(fact: ProtectedFact, scope: str) -> dict[str, Any]:
    """一条继承集/未继承集条目的序列化（快照与展示共用）。

    除展示所需的 `fact_id/type/value/scope`，还保留 `source_event_id`/`source_seq`——
    注入侧（`inherited_protected_facts` → `serialize_protected_facts`）要求来源指针与
    父会话投影**逐字段一致**（尤其会话级授权：`source_event_id` 是父会话的真实事件 id，
    审计可回溯到父哪条事件；丢了它就只能拿到合成值）。
    """
    return {
        "fact_id": fact.fact_id,
        "type": fact.type,
        "value": fact.value,
        "scope": scope,
        "source_event_id": fact.source_event_id,
        "source_seq": fact.source_seq,
    }


@dataclass(frozen=True, slots=True)
class InheritanceSnapshot:
    """一次委派的约束继承集快照（子会话构建时定，落子事件流）。

    - ``inherited``：实际注入子 agent 上下文的条目（项目/全局 active + 会话级 active 授权）；
    - ``not_inherited``：父会话专属、**不**注入的条目（会话级非 authorization active facts）；
    - ``omit_constraints``：干净启动开关的取值。
    """

    omit_constraints: bool
    inherited: tuple[dict[str, Any], ...]
    not_inherited: tuple[dict[str, Any], ...]

    def to_event_data(self) -> dict[str, Any]:
        return {
            "omit_constraints": self.omit_constraints,
            "inherited": [dict(row) for row in self.inherited],
            "not_inherited": [dict(row) for row in self.not_inherited],
        }

    @classmethod
    def from_event_data(cls, data: dict[str, Any]) -> InheritanceSnapshot:
        """从事件 data 还原快照（展示层只读消费；坏数据尽量容错、不 throw）。"""
        inherited = data.get("inherited")
        not_inherited = data.get("not_inherited")
        return cls(
            # 严格 is True：非 bool 真值（如字符串）不当作开启，防坏数据静默放大
            omit_constraints=data.get("omit_constraints") is True,
            inherited=tuple(
                row for row in inherited if isinstance(row, dict)
            ) if isinstance(inherited, list) else (),
            not_inherited=tuple(
                row for row in not_inherited if isinstance(row, dict)
            ) if isinstance(not_inherited, list) else (),
        )


def compute_inheritance(
    parent_events: list[Any],
    *,
    store: ScopedConstraintStore,
    project_key: str | None,
    session_id: str,
    omit_constraints: bool = False,
) -> InheritanceSnapshot:
    """由父会话事件 + 规则层算出继承集快照。

    只投影 **active** facts（`serialize_protected_facts` 同口径）。`omit_constraints=True`
    时 `inherited` 为空（干净启动：不继承任何约束与授权）；`not_inherited` 仍如实列出父会话
    专属事实（展示层据它渲染 NOT INHERITED 组）。

    覆盖候选只取**会被继承**的会话行（authorization）：#1191 的同文覆盖是会话＞项目＞
    全局，若让「不会随继承传播的会话级非授权行」当覆盖候选，会出现父上下文有该文本
    （session 版）、子上下文却两头落空——项目行被覆盖跳过、session 行又不继承（审查
    Correctness Finding-1 的同文覆盖空洞）。会话授权行会被继承，覆盖它对应的项目/全局
    行仍成立。
    """
    parent_facts = derive_protected_facts(parent_events)
    active = [fact for fact in parent_facts if fact.status == "active"]
    session_authorizations = [f for f in active if f.type == _AUTHORIZATION_TYPE]
    session_other = [f for f in active if f.type != _AUTHORIZATION_TYPE]

    not_inherited = tuple(_row(f, "session") for f in session_other)
    if omit_constraints:
        return InheritanceSnapshot(
            omit_constraints=True, inherited=(), not_inherited=not_inherited,
        )

    scoped = scoped_context_facts(
        store, session_authorizations, session_id=session_id, project_key=project_key,
    )
    inherited = tuple(
        [_row(fact, fact.scope) for fact in scoped]
        + [_row(fact, "session") for fact in session_authorizations]
    )
    return InheritanceSnapshot(
        omit_constraints=False, inherited=inherited, not_inherited=not_inherited,
    )


def inherited_protected_facts(
    snapshot: InheritanceSnapshot, *, session_id: str,
) -> list[ProtectedFact]:
    """快照 → 可直接注入子 agent 上下文的 `ProtectedFact` 列表。

    `session_id` = **子会话** id（注入体的归属会话，与 #1191 注入口径一致）。项目/全局级
    条目用合成来源（`source_event_id="scoped:<fact_id>"`、`source_seq=0`，与 #1191
    `scoped_context_facts` 同款"无会话来源事件"表达）；会话级授权保留父会话的真实来源 id
    （审计可回溯到父会话哪条事件）。坏数据容错：缺 `fact_id` 的行跳过（与 web 解析器
    `parseRow` 同口径，不注入来源不可辨的条目）；会话级条目缺来源指针时以
    `unresolved:<fact_id>` 显式标记不可回溯（**不**伪装成真实事件 id——`fact_id` 是内容
    寻址哈希，不是事件 id）。
    """
    facts: list[ProtectedFact] = []
    for row in snapshot.inherited:
        scope = str(row.get("scope", "session"))
        fact_id = str(row.get("fact_id", ""))
        if not fact_id:
            continue  # 来源不可辨的行不注入（web parseRow 同口径跳过）
        source_event_id = row.get("source_event_id")
        if not isinstance(source_event_id, str) or not source_event_id:
            # 容错：坏数据/旧快照缺来源指针时按 scope 显式合成——项目/全局走 #1191
            # 口径；会话级无既有口径，用 unresolved: 前缀如实表达"不可回溯"。
            source_event_id = (
                f"scoped:{fact_id}" if scope in ("project", "global")
                else f"unresolved:{fact_id}"
            )
        source_seq = row.get("source_seq", 0)
        if not isinstance(source_seq, int) or isinstance(source_seq, bool):
            source_seq = 0
        facts.append(
            ProtectedFact(
                fact_id=fact_id,
                type=str(row.get("type", "")),
                value=row.get("value"),
                source_event_id=source_event_id,
                source_seq=source_seq,
                status="active",
                session_id=session_id,
                scope=scope,
            )
        )
    return facts


__all__ = [
    "CONSTRAINT_INHERITANCE_EVENT",
    "InheritanceSnapshot",
    "compute_inheritance",
    "inherited_protected_facts",
]
