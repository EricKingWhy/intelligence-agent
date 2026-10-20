"""#1353 AC6：不喊关键词也能记住——真实 harness run → 真实后台 review job → markdown 落盘。

票面判据（原文）：固定 fixture 对话（含 3 个可记忆事实：用户姓名 / 偏好 / 环境事实，
**不含"记住"关键词**）跑完一个真实 harness run（后台 review，N=10 turns 触发或压缩前
触发），MEMORY.md / USER.md 新增条目覆盖全部 3 个事实。

# "端到端"在这里的边界

真跑的部分：`AgentRuntime.run` × 10 轮、`JsonlSessionStore`、`memory_v2_jobs` SQLite、
`MemoryJobRunner` 的服务循环与切片（`run_slice_bounds`）、`projection` 的别名发放、
`formation`/`adjudication` 的契约校验、`policy` 的敏感面判定、以及**写入路径本身**
（`build_memory_v2_service` 装配出的 service，markdown 是权威）。

唯一的替身是**模型**：没有 API 凭据，且 AC6 判的是"编排有没有把一段日常对话交给 review
并落到文件"，不是模型提取质量。替身完全按真实契约说话——证据必须引用投影发出的
`ref`（`e1`…），引用不到就写不进——所以"候选能落盘"这件事本身就证明切片、投影、
校验、写入四段都真的跑过，而不是把断言架在一个旁路上。

# 为什么三条事实分散在不同轮次

runner 的切片是**当前这一轮**（`run_slice_bounds`），更早的消息只进
`earlier_messages`（`ref` 恒 `None`，按投影的规则不可引用）。三条事实放在第 2 / 5 / 9 轮，
所以"整段对话结束时三条都在文件里"成立的前提是：**每一轮都真的被 review 过**。
任何"只在第 N 轮拍一次、且只喂当轮事件"的实现都会让前两条事实缺席——这正是 AC6
要钉的东西，用例不做妥协。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentRuntime
from agent_harness.config import Settings
from agent_harness.memory.v2 import assembly
from agent_harness.memory.v2.executor import MemoryModelCall, MemoryModelStage
from agent_harness.memory.v2.roles import MemoryModelRoles
from agent_harness.model.config import ModelConfig
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

#: 票面 AC6 的 N（Hermes 默认 `_memory_nudge_interval = 10`）。
TURNS = 10


@dataclass(frozen=True)
class Fact:
    """一条可记忆事实：对话里的引子、期望落在 markdown 里的词、以及它该走的 V2 形状。"""

    marker: str
    needle: str
    tier: str
    category: str
    subject: str
    fact: str
    content: str


FACTS: tuple[Fact, ...] = (
    Fact(
        marker="我叫林之",
        needle="林之",
        tier="profile",
        category="profile",
        subject="用户姓名",
        fact="林之",
        content="用户叫林之",
    ),
    Fact(
        marker="冰美式",
        needle="冰美式",
        tier="profile",
        category="preference",
        subject="咖啡偏好",
        fact="只喝冰美式、不加糖",
        content="用户喝咖啡只喝冰美式、不加糖",
    ),
    Fact(
        marker="Debian 12",
        needle="Debian 12",
        tier="collection",
        category="constraint",
        subject="开发机环境",
        fact="Debian 12 + Python 3.12",
        content="开发机运行 Debian 12，Python 3.12",
    ),
)

#: 三条事实分别落在第 2 / 5 / 9 轮，其余轮次是日常对话（见模块 docstring 末段）。
_FACT_TURNS = {1: FACTS[0], 4: FACTS[1], 8: FACTS[2]}

_CHATTER = (
    "早上好，今天先把导出脚本跑一遍。",
    None,  # 第 2 轮：姓名
    "顺手把日志目录清一下。",
    "刚才那个 CSV 用逗号分隔就行。",
    None,  # 第 5 轮：偏好
    "帮我看下昨天的构建为什么慢。",
    "先把缓存目录挪到 /tmp 试试。",
    "跑完把结果贴给我。",
    None,  # 第 9 轮：环境事实
    "好，今天就到这里。",
)

#: 这些词一旦出现在 fixture 里，用例就可能退化成"命中关键词才记住"，判据失去意义。
_KEYWORD_MARKERS = ("记住", "记一下", "别忘", "帮我记", "remember", "memorize")


def _conversation() -> list[str]:
    turns: list[str] = []
    for index, chatter in enumerate(_CHATTER):
        if chatter is None:
            turns.append(_FACT_TURNS[index].marker + "。")
        else:
            turns.append(chatter)
    assert len(turns) == TURNS
    return turns


def _model_config() -> ModelConfig:
    """角色只需要"解析得出"；替身 invoker 从不读它（也不该读凭据）。"""
    return ModelConfig(
        provider="senseaudio",
        model_name="ac6-review-model",
        api_key="unit-test-key",
        base_url="http://localhost:1",
        temperature=0.0,
    )


def _excerpt(text: str, marker: str) -> str:
    """从被引用的事件里取一段真实存在的摘录（契约要求 excerpt 非空且不超 300 字符）。"""
    start = max(0, text.index(marker) - 20)
    return text[start:start + 120]


def _formation_reply(payload: Mapping[str, Any]) -> str:
    """按 payload 里的 `current_run` 取证：只记**本轮**说过的三条事实。

    真实模型的提取质量不在 AC6 的判据里；这里要钉的是"候选 → 校验 → 落盘"这条链。
    引用 `earlier_messages`（`ref` 为 `None`）会被运行时按 `unsupported_source` 拒掉，
    所以替身与真实模型守同一条规则。
    """
    current = payload.get("current_run") or []
    candidates: list[dict[str, Any]] = []
    for item in current:
        text = str(item.get("text") or "")
        ref = item.get("ref")
        if not ref:
            continue
        for fact in FACTS:
            if fact.marker not in text:
                continue
            candidates.append({
                "kind": "semantic",
                "tier": fact.tier,
                "scope": "user_global",
                "content": fact.content,
                "payload": {
                    "kind": "semantic",
                    "subject": fact.subject,
                    "fact": fact.fact,
                    "category": fact.category,
                },
                "importance": 0.9,
                "strength": 0.9,
                "evidence": [{
                    "event_id": ref,
                    "role": str(item.get("role") or "user"),
                    "excerpt": _excerpt(text, fact.marker),
                }],
                "sensitivity": "ordinary",
            })
    if not candidates:
        return json.dumps(
            {"decision": "NO_MEMORY", "candidates": [], "skip_reason": "no_durable_value"},
            ensure_ascii=False,
        )
    return json.dumps(
        {"decision": "CANDIDATES", "candidates": candidates, "skip_reason": None},
        ensure_ascii=False,
    )


def _adjudication_reply(payload: Mapping[str, Any]) -> str:
    """每个候选都 ADD；结果字段照抄候选（契约要求 kind/tier/scope 与候选一致）。"""
    results = []
    for candidate in payload.get("candidates") or []:
        results.append({
            "action": "ADD",
            "target_memory_id": None,
            "reason_code": "durable_new",
            "result": {
                "kind": candidate["kind"],
                "tier": candidate["tier"],
                "scope": candidate["scope"],
                "content": candidate["content"],
                "payload": candidate["payload"],
                "importance": candidate["importance"],
                "strength": candidate["strength"],
                "evidence": candidate["evidence"],
            },
        })
    return json.dumps({"results": results}, ensure_ascii=False)


class _FormingReviewModel:
    """确定性 review 模型：formation 按事实取证，adjudication 全 ADD。"""

    def __init__(self) -> None:
        self.calls: list[MemoryModelCall] = []

    async def __call__(self, call: MemoryModelCall) -> str:
        self.calls.append(call)
        if call.stage is MemoryModelStage.FORMATION:
            return _formation_reply(call.payload)
        if call.stage is MemoryModelStage.ADJUDICATION:
            return _adjudication_reply(call.payload)
        raise AssertionError(
            f"AC6 只该看到 formation / adjudication 两次阶段调用，收到 {call.stage!r}"
        )

    @property
    def formation_calls(self) -> int:
        return sum(1 for call in self.calls if call.stage is MemoryModelStage.FORMATION)


class _NoMemoryReviewModel:
    """反控：同一段对话、同一套编排，只把模型的结论换成 NO_MEMORY。"""

    def __init__(self) -> None:
        self.calls: list[MemoryModelCall] = []

    async def __call__(self, call: MemoryModelCall) -> str:
        self.calls.append(call)
        return json.dumps(
            {"decision": "NO_MEMORY", "candidates": [], "skip_reason": "no_durable_value"},
            ensure_ascii=False,
        )


async def _build_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review: Any):
    """按**生产装配**建管线，只把模型调用器换成替身。

    走 `assembly.build_memory_formation`（而不是手搓 jobs/executor/runner）的理由：它自己
    决定库位、writer/searcher、并发上限与启动恢复；手搓一份就等于让用例验一条生产里
    不存在的接线。`ChatModelInvoker` 是装配层构造执行器时唯一读模型的地方，替换它之后
    剩下的每一段都是真的。
    """
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path / "workspace"),
        model_api_key="sk-test",
    )
    sessions = JsonlSessionStore(root=tmp_path / "sessions")
    # 默认 raising=True：装配层换了注入点就当场红，绝不静默落回真实模型。
    monkeypatch.setattr(assembly, "ChatModelInvoker", lambda *args, **kwargs: review)
    runner = await assembly.build_memory_formation(
        settings,
        sessions=sessions,
        roles=MemoryModelRoles(primary=_model_config(), fallback=None),
    )
    assert runner is not None, (
        "记忆形成管线没有装配：角色已显式给出，返回 None 说明装配期闸门判错了"
    )
    return settings, sessions, runner


def _markdown_files(settings: Settings) -> tuple[Path, Path]:
    memories = Path(settings.workspace_dir) / "memories"
    return memories / "MEMORY.md", memories / "USER.md"


def _read_markdown(settings: Settings) -> str:
    """读两份 markdown（不存在的当空串）——反控要用它判"什么都没写"。"""
    return "\n".join(
        path.read_text(encoding="utf-8") for path in _markdown_files(settings)
        if path.exists()
    )


def _markdown_text(settings: Settings) -> str:
    """读 markdown 权威存储，并断言它真的被创建过。

    缺文件时把 workspace 里实际存在的文件列出来——"没写"与"写到别处"是两种完全不同的
    实现缺陷，失败信息必须能一眼分开。
    """
    paths = _markdown_files(settings)
    present = sorted(
        str(path.relative_to(settings.workspace_dir))
        for path in Path(settings.workspace_dir).rglob("*")
        if path.is_file()
    )
    assert any(path.exists() for path in paths), (
        "AC6：后台 review 跑完之后 memories/MEMORY.md 与 memories/USER.md 都不存在。"
        f"workspace 现有文件：{present[:20]}"
    )
    return _read_markdown(settings)


def _record_count(settings: Settings) -> int:
    """`memory_v2_records` 的行数——用来把"形成没到写入"与"写入没落 markdown"分开。"""
    database = Path(settings.workspace_dir) / "memory-v2.db"
    if not database.exists():
        return 0
    connection = sqlite3.connect(database)
    try:
        return int(
            connection.execute("SELECT COUNT(*) FROM memory_v2_records").fetchone()[0]
        )
    finally:
        connection.close()


async def _run_turns(runtime: AgentRuntime, session: Any, conversation: list[str]) -> None:
    for text in conversation:
        result = await runtime.run(session, text)
        assert result.status == "completed", (
            f"AC6 的 run 没有正常收口（{result.status}, reason={result.reason!r}）："
            "这一轮记忆不会被 review"
        )


@pytest.mark.asyncio
async def test_ten_turn_conversation_is_remembered_without_keywords(
    tmp_path, monkeypatch,
) -> None:
    """AC6 主判据：10 轮日常对话后，三条事实全部落进 MEMORY.md / USER.md。"""
    review = _FormingReviewModel()
    settings, _sessions, runner = await _build_pipeline(tmp_path, monkeypatch, review)
    registry = ToolRegistry()
    runtime = AgentRuntime(
        model=ScriptedModel([AIMessage(content="好的。") for _ in range(TURNS)]),
        registry=registry,
        executor=ToolExecutor(registry),
        max_agent_turns=5,
        memory_formation=runner,
    )
    session = make_session(tmp_path / "sessions")
    # 生产交付点（RunManager.launch）会标记这枚聚合并交给 runtime；标记后 append 撞号
    # 才在 store 的写锁内改号重落（#1055/#1062）。不标记时后台 review 的 `memory/updated`
    # 一旦抢在下一轮 USER_MESSAGE 之前落盘，前台就会以 SeqConflict 失败——那是
    # "聚合未走交付点"的测试假象，不是产品路径。
    session.deliver_to_runtime()
    try:
        await _run_turns(runtime, session, _conversation())
        await runner.drain()
    finally:
        await runner.aclose()

    assert review.formation_calls >= 3, (
        "AC6：只有 "
        f"{review.formation_calls} 次 formation 调用 —— 三条事实分散在第 2/5/9 轮，"
        "按轮触发的 review 至少应看到三次；少于三次说明有的轮次根本没被 review"
    )

    rows = _record_count(settings)
    assert rows >= 3, (
        f"AC6：SQLite 里只有 {rows} 条记录 —— 形成/裁决/写入这一段没有走到；"
        "这时文件缺失的原因不是 markdown 落盘，而是候选根本没被接受"
    )

    remembered = _markdown_text(settings)
    missing = [fact.needle for fact in FACTS if fact.needle not in remembered]
    assert missing == [], (
        f"AC6：下列事实没有进入 markdown 记忆：{missing}；实际内容：{remembered!r}"
    )

    # 画像类事实（姓名 / 偏好）走 USER.md，其余走 MEMORY.md——与 AC9 的迁移分拣同一条规则。
    user_text = (Path(settings.workspace_dir) / "memories" / "USER.md").read_text(
        encoding="utf-8"
    )
    assert "林之" in user_text and "冰美式" in user_text, (
        f"AC6：画像事实没有落到 USER.md（实际：{user_text!r}）"
    )


@pytest.mark.asyncio
async def test_without_forming_decision_nothing_reaches_the_files(
    tmp_path, monkeypatch,
) -> None:
    """反控（突变自证）：同一段对话 + 同一套编排，模型说 NO_MEMORY 时文件里什么都不该有。

    没有这一条，"三条事实出现在文件里"也可能只是对话文本被别处原样抄了一份——那样
    断言对"形成"没有鉴别力。
    """
    review = _NoMemoryReviewModel()
    settings, _sessions, runner = await _build_pipeline(tmp_path, monkeypatch, review)
    registry = ToolRegistry()
    runtime = AgentRuntime(
        model=ScriptedModel([AIMessage(content="好的。") for _ in range(TURNS)]),
        registry=registry,
        executor=ToolExecutor(registry),
        max_agent_turns=5,
        memory_formation=runner,
    )
    session = make_session(tmp_path / "sessions")
    session.deliver_to_runtime()
    try:
        await _run_turns(runtime, session, _conversation())
        await runner.drain()
    finally:
        await runner.aclose()

    assert review.calls, "反控没有真的触发 review —— 那样它证明不了任何事"
    assert _record_count(settings) == 0, (
        "AC6：模型判了 NO_MEMORY，SQLite 里却出现了记录 —— 形成结论没有约束写入"
    )
    remembered = _read_markdown(settings)
    leaked = [fact.needle for fact in FACTS if fact.needle in remembered]
    assert leaked == [], (
        "AC6：模型明确判了 NO_MEMORY，事实却出现在 markdown 里 —— "
        f"写入路径绕过了形成结论：{leaked}；实际内容：{remembered!r}"
    )


def test_fixture_conversation_contains_no_remember_keyword() -> None:
    """fixture 自检：三条事实必须靠"日常对话"触发，不能靠关键词。"""
    conversation = _conversation()
    hits = [
        (index, marker)
        for index, text in enumerate(conversation)
        for marker in _KEYWORD_MARKERS
        if marker in text.lower()
    ]
    assert hits == [], f"AC6 fixture 里出现了关键词提示：{hits}"
    lowered = "\n".join(conversation).lower()
    for fact in FACTS:
        assert fact.marker.lower() in lowered, f"事实引子 {fact.marker!r} 不在对话里"
