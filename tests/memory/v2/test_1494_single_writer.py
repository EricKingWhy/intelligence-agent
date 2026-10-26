"""#1494 AC5：当前内容的写者只有一处——静态图 + 真实入口的运行时配对。

两条独立证据：

1. **静态**：`src/agent_harness` 的 AST 里，`mutate_now` 的调用点只出现在
   `commit.py`；没有任何模块直接调用 `_write_file` 或直接给 `memory_entries` /
   `user_entries` 赋值；`MarkdownMemoryStore` 不再暴露 add/replace/remove 旧写面。
2. **运行时**：把引擎的落盘函数包一层，跑完 service / 工具 / 审批 / 被拦放行 /
   formation 全部入口后，**每一次落盘的 (target, digest) 都能在某条 COMMITTED 回执的
   `after_digest` 里找到**——即没有任何一次内容变更绕过回执（因此也绕过 commit）。

静态只能证明"当前代码没有第二条路"，运行时只能证明"跑过的路径都配对了回执"；
两者一起才排除了"静态漏看某个动态分发"和"图上干净但实际旁路"两种失败。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from agent_harness.config import Settings
from agent_harness.memory.v2 import assembly
from agent_harness.memory.v2.markdown_store import (
    MarkdownMemoryStore,
    entries_digest,
    entry_digest,
)
from agent_harness.memory.v2.memory_tool import MemoryTool, _MemoryArgs
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.memory.v2.write_approval import _identity
from tests.memory.v2._records import make_draft, make_record

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "src" / "agent_harness"
LOCAL = TrustedMemoryIdentity(tenant_id="local", user_id="local")


# --------------------------------------------------------------------------------------
# 静态图
# --------------------------------------------------------------------------------------


def _call_sites(method: str) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == method
            ):
                found.setdefault(str(path.relative_to(REPO_ROOT)).replace("\\", "/"), []).append(
                    node.lineno)
    return found


def _entry_assignments() -> set[str]:
    """给活条目赋值的模块：直接赋值、增量赋值、注解赋值、以及 ``setattr`` 动态写。"""
    assigned: set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in {"memory_entries", "user_entries"}
                ):
                    assigned.add(relative)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and str(node.args[1].value) in {"memory_entries", "user_entries"}
            ):
                assigned.add(relative)
    return assigned


def test_only_commit_calls_the_single_mutation_entry() -> None:
    sites = _call_sites("mutate_now")
    assert set(sites) == {"src/agent_harness/memory/v2/commit.py"}, sites
    assert len(sites["src/agent_harness/memory/v2/commit.py"]) >= 3
    # 落盘函数只在引擎自己的 mutate_now 临界区里被调用
    assert list(_call_sites("_write_file")) == [
        "src/agent_harness/memory/v2/markdown_store.py"]
    assert list(_call_sites("_set_entries")) == [
        "src/agent_harness/memory/v2/markdown_store.py"]


def test_live_entries_are_only_assigned_inside_the_engine() -> None:
    assert _entry_assignments() == {"src/agent_harness/memory/v2/markdown_store.py"}


def test_store_exposes_no_second_write_surface() -> None:
    public = {
        name for name in vars(MarkdownMemoryStore)
        if not name.startswith("_")
    }
    assert "mutate_now" in public
    assert not public & {
        "add", "replace", "remove", "apply_batch", "batch", "save", "save_to_disk",
        "write", "write_entries",
    }, "旧写面不得残留：所有变更必须经 mutate_now"


# --------------------------------------------------------------------------------------
# 运行时配对：每一次落盘都能在 COMMITTED 回执的 after_digest 里找到
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_runtime_write_is_paired_with_a_committed_receipt(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path / "workspace"), model_api_key="sk-test")
    service = await assembly.build_memory_v2_service(settings)
    engine = service.markdown
    assert engine is not None

    writes: list[tuple[str, str]] = []
    real_write = engine._write_file

    def _record(path: Path, entries: list[str]) -> None:
        target = "user" if path.name == "USER.md" else "memory"
        writes.append((target, entries_digest(entries)))
        real_write(path, entries)

    engine._write_file = _record  # type: ignore[method-assign]

    created = await service.create(make_draft(content="服务创建"), LOCAL)
    updated = await service.update(
        created.id, make_draft(content="服务更新", source_event_ids=["event-2"]), LOCAL)
    await service.invalidate(updated.id, LOCAL)
    doomed = await service.create(
        make_draft(content="服务删除", source_event_ids=["event-3"]), LOCAL)
    await service.delete(doomed.id, LOCAL)

    tool = MemoryTool(service.commit, service=service)
    assert (await tool.execute(
        _MemoryArgs(action="add", target="memory", content="工具写入"))).ok is True

    # formation 的提交前发布（executor 走的就是这个入口，确定性 jobp_ 前缀）
    await service.mirror_committed(
        [make_record(memory_id="mem-formation-writer", content="formation 写入")],
        operation_prefix="jobp_single_writer",
    )
    # 双目标按 digest 清理（治理删除的墓碑-only 分支）
    await service.commit.remove_by_digest(
        [entry_digest("工具写入")], operation_id="del_single_writer_digest")

    staged = service.staged
    record = await staged.stage(
        {"target": "memory", "action": "add", "content": "审批写入"}, None, LOCAL)
    decision, _response = await staged.approve(record.id, LOCAL)
    assert decision.allow is True

    blocked_id = await service.blocked.insert(
        job_id="job-single-writer", identity=_identity(LOCAL), project_id=None,
        payload={"actions": [{
            "action": "add", "scope": "user_global",
            "draft": make_draft(
                content="放行写入", source_event_ids=["event-blocked"]).model_dump(mode="json"),
            "pin": None,
        }]},
    )
    assert blocked_id is not None
    assert (await service.blocked.promote(blocked_id, LOCAL)).applied is True

    assert writes, "本用例必须真的发生内容落盘"

    planned: set[tuple[str, str]] = set()
    prefixes: set[str] = set()
    for path in service.state_root.receipts_dir.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["state"] == "COMMITTED", path.name
        prefixes.add(payload["operation_id"].split("_", 1)[0] + "_")
        for target, plan in payload["targets"].items():
            if (
                plan.get("planned")
                and plan.get("after_digest")
                and plan["after_digest"] != plan.get("before_digest")
            ):
                planned.add((target, plan["after_digest"]))
    assert {"mcf_", "del_", "stgp_", "blkp_", "jobp_"} <= prefixes

    unpaired = [item for item in writes if item not in planned]
    assert unpaired == [], f"这些落盘没有回执计划（说明绕过了 commit）：{unpaired}"

    # 反向：每一条计划也确实发生过一次落盘（没有"记了回执但没写"的假提交）
    for target, digest in planned:
        assert (target, digest) in writes, (target, digest)
