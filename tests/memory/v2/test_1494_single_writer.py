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


#: 能打开/替换内容文件的原语。静态图不追踪 `_write_file` 的名字，而是按「写原语 +
#: 内容路径」判定：改个函数名或直接 `engine.path_for(...).write_text(...)` 都会被抓住。
_CONTENT_PATH_HINTS = frozenset({
    "memory_file", "user_file", "memories", "MEMORY.md", "USER.md",
    "MEMORIES_DIRECTORY_NAME",
})
#: `path_for` 在各状态 store 上也有同名方法（receipt/staged/job/tombstone）；
#: 只有经过 markdown 引擎的 `path_for` 才指向 memories/ 内容文件。
_ENGINE_HINTS = frozenset({"engine", "_engine", "markdown", "markdown_store"})
_WRITE_ATTRS = frozenset({
    "write_text", "write_bytes", "replace", "rename",
    "copyfile", "copy2", "move",
})
_ALLOWED_WRITE_MODULES = frozenset({
    # 引擎自身与底层原子写原语；其余模块只能经 mutate_now / MemoryCommit。
    "src/agent_harness/memory/v2/markdown_store.py",
    "src/agent_harness/memory/v2/file_state.py",
    # migration 清单是操作状态，不是 memories/ 内容文件。
    "src/agent_harness/memory/v2/identity_mapping.py",
})


def _references_content(node: ast.AST, tainted: set[str]) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            if child.attr in _CONTENT_PATH_HINTS:
                return True
            if child.attr == "path_for" and any(
                (isinstance(part, ast.Name) and part.id in _ENGINE_HINTS)
                or (isinstance(part, ast.Attribute) and part.attr in _ENGINE_HINTS)
                for part in ast.walk(child.value)
            ):
                return True
        if (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and child.value in _CONTENT_PATH_HINTS
        ):
            return True
        if isinstance(child, ast.Name) and child.id in tainted:
            return True
    return False


def _content_write_sites() -> dict[str, list[int]]:
    """直接对 memories/ 内容文件落笔的调用点（不经过 `_write_file` / `mutate_now`）。

    先做模块内简单污点传播：`path = engine.path_for(target)` 之后 `path.write_text(...)`
    同样算旁路。读原语（`read_text` / `open(..., "r")`）不在扫描面内。
    """
    found: dict[str, list[int]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        tainted: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if _references_content(node.value, tainted):
                    tainted.update(
                        target.id for target in targets
                        if isinstance(target, ast.Name)
                    )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else None
            name = func.id if isinstance(func, ast.Name) else None
            suspicious = False
            if attr in _WRITE_ATTRS:
                receiver = func.value
                suspicious = _references_content(receiver, tainted)
                if attr in {"replace", "rename", "copyfile", "copy2", "move"}:
                    suspicious = suspicious or any(
                        _references_content(argument, tainted) for argument in node.args)
            elif attr == "open" or name == "open":
                path_arg = node.args[0] if node.args else None
                mode = node.args[1] if len(node.args) > 1 else None
                writes = (
                    isinstance(mode, ast.Constant)
                    and isinstance(mode.value, str)
                    and any(flag in mode.value for flag in "wax+")
                )
                suspicious = bool(
                    writes and path_arg is not None
                    and _references_content(path_arg, tainted)
                )
            elif attr == "fdopen" or name == "fdopen":
                mode = node.args[1] if len(node.args) > 1 else None
                suspicious = bool(
                    isinstance(mode, ast.Constant)
                    and isinstance(mode.value, str)
                    and any(flag in mode.value for flag in "wax+")
                    and _references_content(func, tainted)
                )
            if suspicious and relative not in _ALLOWED_WRITE_MODULES:
                found.setdefault(relative, []).append(node.lineno)
    return found


def test_no_module_opens_a_second_write_handle_on_content_files() -> None:
    offenders = _content_write_sites()
    assert offenders == {}, (
        "这些模块直接对 memories/ 内容文件落笔，绕过 mutate_now/MemoryCommit："
        f"{offenders}"
    )


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
