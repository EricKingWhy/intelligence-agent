"""#1084：`build` / `compact_now` 的保护事实与预算决策单源。

票面前提（已核）：两条路径的「事件投影 → 保护事实 → 运行时快照 → 清单/修改文件块
→ token 预留 → hard guard」决策曾经各写一份同式代码；`compact_now` 在
`protected_facts_for_context`（`build` 已用）之外**另行**实现了一份"只取最新
work_boundary"的筛选，且图片 token 注释还停在旧口径。这是**漂移风险**而非当前
报错——因此本文件不重写压缩算法、不改阈值，只钉住四件事：

1. 两个入口的事实选择走**同一个 seam**（消融：让共享选择返回全部 work_boundary，
   两侧"只取最新边界"断言同时转红）；
2. 两侧都**只**注入最新一条 work_boundary + 全部非边界事实（active/superseded 预算
   口径不变）；
3. 清单锚 / 最近修改文件块的**选择与 token 预留**只有一个 owner（消融：去掉共享
   函数让两侧各自重算即失去单源）；
4. `compact_now` 的 token 估算**按现行尺寸成本计入图片**（不因过时注释回退旧口径）。

红灯来源：refactor 前，入口 1（`compact_now` 自写筛选）不读共享 seam，故
`test_compact_now_fact_selection_flows_through_shared_selector` 与
`test_plan_and_paths_decision_is_single_source` 在 base 上为红。行为等价性基线由
`test_both_entry_points_select_only_latest_work_boundary` 等钉住（base 与改后皆绿，
消融转红）。
"""

from __future__ import annotations

import io
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from PIL import Image

import agent_harness.context.builder as builder_mod
from agent_harness.attachments.types import ImageAttachmentRef
from agent_harness.context.builder import (
    ContextBuilder,
)
from agent_harness.context.tokens import (
    estimate_message_tokens,
    image_tokens_for_size,
)
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
)
from agent_harness.session.derive import derive_modified_file_paths
from agent_harness.session.event import RUN_PAUSED
from agent_harness.session.plan import apply_plan_update, derive_plan
from agent_harness.storage.artifact import FakeArtifactStore, compute_byte_artifact_id
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


#: 模型撰写的四节（其余四节程序化生成）；第 5 节 = "(none)"（零未完成项）。
def _model_sections(section5: str) -> str:
    return (
        "## 已完成工作与关键决策\n已完成读取历史记录。\n\n"
        "## 失败方案\n(none)\n\n"
        f"## 当前进行中状态\n{section5}\n\n"
        "## Next Step\n等待当前请求继续。"
    )


def _facts_message(messages: list) -> HumanMessage | None:
    return next(
        (
            message for message in messages
            if isinstance(message, HumanMessage)
            and message.content.startswith("## Protected task facts\n")
        ),
        None,
    )


def _injected_facts(messages: list) -> list[dict]:
    message = _facts_message(messages)
    if message is None:
        return []
    return json.loads(message.content.rsplit("\n", 1)[1])


def _summary_facts(summary: str) -> list[dict]:
    section = summary.split("## 保护事实表\n", 1)[1].split("\n\n## ", 1)[0]
    return [] if section.strip() == "(none)" else json.loads(section)


def _facts_session(tmp_path):
    """代表性事件流：user_goal + 3×work_boundary + 授权/撤权（superseded/active）。

    结构对齐 #647 的可压缩形态（USER → 长 MODEL_COMPLETED → USER）——额外的事实
    事件（RUN_PAUSED / TASK_PROTECTED_FACT）不投影成消息，不改变投影计数。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "完成 W-02，保留订单号 ORD-84721。"})
    session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
    boundaries = [
        session.append(
            RUN_PAUSED,
            {"reason": f"Run {index}: " + "prior state " * 40},
            run_id=f"run-{index}",
        )
        for index in range(3)
    ]
    grant_event = session.append(USER_MESSAGE, {"content": "授权只运行测试。"})
    grant = session.register_protected_fact(
        fact_type="authorization",
        value="授权只运行测试。",
        source_event_id=grant_event.event_id,
    )
    revoke_event = session.append(USER_MESSAGE, {"content": "撤销刚才的运行授权。"})
    session.register_protected_fact(
        fact_type="authorization_revocation",
        value="撤销刚才的运行授权。",
        source_event_id=revoke_event.event_id,
        supersedes_fact_id=grant.data["fact_id"],
    )
    session.append(USER_MESSAGE, {"content": "current request"})
    return session, boundaries, grant.data["fact_id"]


def _manual_builder(provider=None) -> ContextBuilder:
    """手动压缩路径的 builder：阈值足够低，`compact_now` 必然产出 bracket。"""
    return ContextBuilder(
        ScriptedModel([AIMessage(content=_model_sections("(none)"))]),
        max_context_tokens=10_000,
        auto_compact_threshold=0.3,
        runtime_context_provider=provider,
    )


# ── 1. 事实选择：两入口只取最新 work_boundary（行为基线，消融敏感）─────────


@pytest.mark.asyncio
async def test_both_entry_points_select_only_latest_work_boundary(tmp_path):
    """同一 fixture：build（未压缩路径）与 compact_now（手动路径）都只取最新边界。

    绝对断言（不是两侧互比）：一条 work_boundary、且是最后一条——消融「共享选择
    返回全部 work_boundary」时两侧同时转红。active 撤权事实在场、superseded 授权
    不在场，证明预算口径（只序列化 active）不变。
    """
    session, boundaries, grant_fact_id = _facts_session(tmp_path)

    built = await ContextBuilder(ScriptedModel([]), max_context_tokens=200_000).build(session)
    injected = _injected_facts(built)
    build_boundaries = [fact for fact in injected if fact["type"] == "work_boundary"]
    assert len(build_boundaries) == 1, "build 只注入最新一条 work_boundary"
    assert build_boundaries[0]["source_event_id"] == boundaries[-1].event_id
    assert any(fact["type"] == "authorization_revocation" for fact in injected)
    assert all(fact["fact_id"] != grant_fact_id for fact in injected), "superseded 不注入"

    result = await _manual_builder().compact_now(session)
    assert result is not None and result.summary is not None
    compact_facts = _summary_facts(result.summary)
    compact_boundaries = [fact for fact in compact_facts if fact["type"] == "work_boundary"]
    assert len(compact_boundaries) == 1, "compact_now 摘要 §2 只含最新一条 work_boundary"
    assert compact_boundaries[0]["source_event_id"] == boundaries[-1].event_id
    assert any(fact["type"] == "authorization_revocation" for fact in compact_facts)
    assert all(fact["fact_id"] != grant_fact_id for fact in compact_facts)


# ── 2. 单源接线：compact_now 复用共享选择（refactor 前红）───────────────────


@pytest.mark.asyncio
async def test_compact_now_fact_selection_flows_through_shared_selector(tmp_path, monkeypatch):
    """把共享选择器换成空选择：build 与 compact_now 必须同时读到它。

    `build` 本就读 `protected_facts_for_context`；`compact_now` 若仍自写一份筛选，
    就会忽略这次替换、在摘要 §2 里照常吐出全部事实 ⇒ base 红、改后绿。这直接钉住
    票面「compact_now 复用已有 protected_facts_for_context」。
    """
    monkeypatch.setattr(builder_mod, "protected_facts_for_context", lambda events: [])
    session, _boundaries, _grant = _facts_session(tmp_path)

    built = await ContextBuilder(ScriptedModel([]), max_context_tokens=200_000).build(session)
    assert _facts_message(built) is None, "空选择 ⇒ build 不注入事实块"

    result = await _manual_builder().compact_now(session)
    assert result is not None and result.summary is not None
    assert _summary_facts(result.summary) == [], (
        "compact_now 必须经由被替换的共享选择器（自写筛选会忽略替换）"
    )


# ── 3. 清单锚 / 修改文件块：选择与 token 预留单源（refactor 前红）───────────


def _plan_paths_session(tmp_path):
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    session.append(MODEL_COMPLETED, {"content": "前情 " * 600})
    session.append(
        TOOL_CALL,
        {"tool_call_id": "c1", "tool_name": "write", "args": {"path": "src/a.py"}},
    )
    session.append(
        TOOL_RESULT,
        {"tool_call_id": "c1", "content": json.dumps({"ok": True, "path": "src/a.py"})},
    )
    outcome = apply_plan_update(
        session,
        [{"id": "1", "content": "梳理压缩规格", "activeForm": "梳理压缩规格中",
          "status": "completed", "source": "agent"}],
    )
    assert outcome.ok, outcome.reason
    session.append(USER_MESSAGE, {"content": "current request"})
    return session


def _compactable_plan_session(tmp_path):
    """可压缩的清单会话（无 tool 块，规避 compactor 的 orphan tool result 校验）。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
    outcome = apply_plan_update(
        session,
        [{"id": "1", "content": "梳理压缩规格", "activeForm": "梳理压缩规格中",
          "status": "completed", "source": "agent"}],
    )
    assert outcome.ok, outcome.reason
    session.append(USER_MESSAGE, {"content": "current request"})
    return session


@pytest.mark.asyncio
async def test_plan_and_paths_decision_is_single_source(tmp_path, monkeypatch):
    """两个入口都调用同一个 `_plan_and_paths_injection`（一个 owner）。

    base 无该符号 ⇒ 收集期即红；改后 build 与 compact_now 各调用一次。替换为记录
    型包装（内部仍委托真实实现）证明调用方不再各自内联同式选择。
    """
    calls: list[int] = []
    original = builder_mod._plan_and_paths_injection

    def spy(events, fallback_period):
        calls.append(len(calls))
        return original(events, fallback_period)

    monkeypatch.setattr(builder_mod, "_plan_and_paths_injection", spy)

    session = _compactable_plan_session(tmp_path)
    await ContextBuilder(ScriptedModel([]), max_context_tokens=200_000).build(session)
    assert len(calls) == 1, "build 前置决策走共享 helper"

    calls.clear()
    await _manual_builder().compact_now(session)
    assert len(calls) == 1, "compact_now 预算预留走同一 helper"


def test_plan_and_paths_helper_renders_expected_blocks(tmp_path):
    """helper 是纯决策：渲染与 token 成本都能由公开派生函数独立重算。"""
    session = _plan_paths_session(tmp_path)
    injection = builder_mod._plan_and_paths_injection(session.events, 6)

    assert injection.plan_text == builder_mod._render_plan_block(derive_plan(session.events))
    assert injection.paths_text == builder_mod._render_modified_paths_block(
        derive_modified_file_paths(session.events)
    )
    assert injection.plan_tokens == estimate_message_tokens(
        [SystemMessage(content=injection.plan_text)]
    )
    assert injection.paths_tokens == estimate_message_tokens(
        [SystemMessage(content=injection.paths_text)]
    )


# ── 4. 图像：compact_now 估算按现行尺寸成本计入（不因过时注释回退旧口径）───


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _image_session(tmp_path, *, width: int = 8, height: int = 6):
    session = make_session(tmp_path)
    data = _png(width, height)
    attachment_id = compute_byte_artifact_id(data)
    session.append(
        USER_MESSAGE,
        {
            "content": "看看这张图",
            "attachments": [
                {
                    "kind": "image",
                    "attachment_id": attachment_id,
                    "media_type": "image/png",
                    "bytes": len(data),
                    "width": width,
                    "height": height,
                }
            ],
        },
    )
    store = FakeArtifactStore()
    await store.save_bytes(session.session_id, data, mime_type="image/png")
    return session, store


@pytest.mark.asyncio
async def test_compact_now_counts_image_size_cost(tmp_path):
    """手动路径的 token 估算与 build 同口径：视觉口径多出这张图的尺寸相关成本。

    `_estimate_tokens_cached` 在 `compact_now` 内先于压缩阈值判定执行，故
    `_token_estimate_total` 直接反映图片增量——旧口径注释声称"图片不计入"，此断言
    在回退旧口径时会转红。
    """
    session, store = await _image_session(tmp_path)

    def _builder(vision: bool) -> ContextBuilder:
        return ContextBuilder(
            ScriptedModel([]),
            artifact_store=store,
            artifact_read_tool_name="read_artifact",
            model_supports_vision=vision,
        )

    vision = _builder(True)
    non_vision = _builder(False)
    await vision.compact_now(session)
    await non_vision.compact_now(session)

    assert (
        vision._token_estimate_total - non_vision._token_estimate_total
        >= image_tokens_for_size(8, 6)
    )


def test_image_tokens_via_standard_block_are_size_related():
    """口径锚点（独立于估算器）：标准图片块按 width/height 计费，随尺寸增长。"""
    from agent_harness.attachments.projection import image_content_block

    ref = ImageAttachmentRef(
        attachment_id="sha256:" + "a" * 64, media_type="image/png",
        bytes=10, width=2048, height=2048,
    )
    block = image_content_block(ref)
    message = HumanMessage(content=[{"type": "text", "text": "x"}, block])
    assert estimate_message_tokens([message]) >= image_tokens_for_size(2048, 2048)


# ── 5. 运行时快照：手动 unset 路径只渲染一次 ────────────────────────────────


@pytest.mark.asyncio
async def test_compact_now_renders_runtime_provider_once_on_unset_path(tmp_path):
    """`compact_now(runtime_context 缺省=哨兵)`：自行渲染且只渲染一次。"""
    calls: list[int] = []

    def provider() -> str:
        calls.append(len(calls))
        return "运行期快照"

    session, _boundaries, _grant = _facts_session(tmp_path)
    await _manual_builder(provider).compact_now(session)
    assert len(calls) == 1, "手动 unset 路径 provider 恰好渲染一次"


@pytest.mark.asyncio
async def test_build_auto_path_renders_runtime_provider_once(tmp_path):
    """自动路径（阈值命中→compact_now）复用 build 渲染结果，provider 也只一次。"""
    calls: list[int] = []

    def provider() -> str:
        calls.append(len(calls))
        return "运行期快照"

    session, _boundaries, _grant = _facts_session(tmp_path)
    await _manual_builder(provider).build(session)
    assert len(calls) == 1, "自动路径 provider 每次 build 只调用一次"
