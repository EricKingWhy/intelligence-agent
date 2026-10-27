"""#1538：零引用符号的处置钉子（删/留各有机械判据）。

本票对 Q17/Q19/Q20 逐符号核过定义、全部仓内消费面、动态加载（`getattr`/字符串
取属性）、entry point、脚本与发布面。结论只有两类，各自在这里钉一条：

* **已删**：仓内零引用，且证明不存在动态取用与对外契约（见各断言上方的理由）。
* **保留**：零静态引用但存在具体合同理由——删了会破坏 gate 缝、动态调用或票面
  明文保留的决定。这些名字是**故意**留下的，重审后要删请同步改本文件。

反向纪律：本文件的删除断言在改动前是红的（符号还在），改动后转绿——它就是本票的
反控证据；保留断言在误删时会红，并把理由与归属票写在失败信息里。
"""

from __future__ import annotations

import importlib

import pytest

# ── 已删（3 个 Python 符号）─────────────────────────────────────────────────


def test_forwarding_facade_is_gone() -> None:
    """`SessionBudgetHandle.resolve_empty_model_request_accounting` 是纯转发。

    真正干活的是 `SqliteDelegationTreeLedger.resolve_empty_session_model_request_accounting`，
    由 `recovery/scan.py` 直接调用；handle 上另外两个同类 facade
    （`begin_*` / `resolve_unsettled_*`）有调用方，只有这一个从未接线。
    """
    handle_cls = importlib.import_module(
        "agent_harness.storage.delegation_tree"
    ).SessionBudgetHandle
    assert not hasattr(handle_cls, "resolve_empty_model_request_accounting")


def test_progress_bullets_helper_is_gone() -> None:
    """`session/progress.py::_bullets` 是未使用的私有渲染助手。

    同模块的 8 处"（无）"输出都直接写 `f"- {_NONE_LINE}"`，没有一处走它。
    """
    progress = importlib.import_module("agent_harness.session.progress")
    assert not hasattr(progress, "_bullets")


def test_store_utc_now_helper_is_gone() -> None:
    """`memory/v2/store.py::_utc_now` 未被使用（`storage/sqlite.py::_utc_now_iso`
    是另一个模块的独立缝，测试 monkeypatch 的是后者）。"""
    store = importlib.import_module("agent_harness.memory.v2.store")
    assert not hasattr(store, "_utc_now")


# ── 保留（有具体合同理由，不是漏删）─────────────────────────────────────────


@pytest.mark.parametrize(
    ("module", "owner", "name", "reason"),
    [
        (
            "agent_harness.memory.v2.markdown_store",
            "MarkdownMemoryStore",
            "reset_consolidation_failures",
            "dynamic: memory/v2/runner.py 用 getattr(markdown, ...) 取用，静态 rg 看不见",
        ),
        (
            "agent_harness.knowledge.milvus_store",
            "MilvusKnowledgeVectorStore",
            "drop_created_collection",
            "gate seam: 真实 collection 清理面，票面点名可能被 gate 使用",
        ),
        (
            "agent_harness.tooling.approval_queue",
            "PendingApprovalQueue",
            "request_for",
            "intentional: 前端审批面板的查询面，票面明示刻意保留",
        ),
        (
            "agent_harness.memory.v2.index",
            "MemoryV2IndexRelay",
            "rebuild_from_authority",
            "defer: memory/index 候选按记忆步骤处置，本票不抢跑",
        ),
    ],
)
def test_retained_symbol_still_present(
    module: str, owner: str, name: str, reason: str,
) -> None:
    owner_obj = getattr(importlib.import_module(module), owner, None)
    assert owner_obj is not None, f"{module}.{owner} 本身被删了——{reason}"
    assert hasattr(owner_obj, name), (
        f"{module}.{owner}.{name} 被删了，但它有保留理由——{reason}。"
        "要删请先改 #1538 的处置结论（见本文件）。"
    )
