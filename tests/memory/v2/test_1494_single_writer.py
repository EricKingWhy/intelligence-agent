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
from collections import Counter
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
    "copy", "copyfile", "copy2", "move", "atomic_write_text",
})
_WRITE_OPEN_FLAGS = frozenset({
    "O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND",
})
_ALLOWED_WRITE_MODULES = frozenset({
    # 引擎自身与底层原子写原语；其余模块只能经 mutate_now / MemoryCommit。
    "src/agent_harness/memory/v2/markdown_store.py",
    "src/agent_harness/memory/v2/file_state.py",
    # migration 清单是操作状态，不是 memories/ 内容文件。
    "src/agent_harness/memory/v2/identity_mapping.py",
})


def _engine_aliases(tree: ast.AST) -> set[str]:
    """`md = engine` / `store = markdown` 这类别名，静态图里也必须跟着走。"""
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            is_engine = (
                isinstance(value, ast.Name)
                and (value.id in _ENGINE_HINTS or value.id in aliases)
            ) or (
                isinstance(value, ast.Attribute) and value.attr in _ENGINE_HINTS
            )
            if not is_engine:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    return aliases


def _is_engine_ref(node: ast.AST, aliases: set[str]) -> bool:
    return any(
        (isinstance(part, ast.Name)
         and (part.id in _ENGINE_HINTS or part.id in aliases))
        or (isinstance(part, ast.Attribute) and part.attr in _ENGINE_HINTS)
        for part in ast.walk(node)
    )


def _references_content(
    node: ast.AST, tainted: set[str], aliases: set[str] | None = None,
) -> bool:
    engine_aliases = aliases or set()
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            if child.attr in _CONTENT_PATH_HINTS:
                return True
            if child.attr == "path_for" and _is_engine_ref(child.value, engine_aliases):
                return True
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "getattr"
            and len(child.args) >= 2
            and isinstance(child.args[1], ast.Constant)
            and child.args[1].value == "path_for"
            and _is_engine_ref(child.args[0], engine_aliases)
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


def _write_mode(call: ast.Call) -> ast.expr | None:
    if len(call.args) > 1:
        return call.args[1]
    return next((kw.value for kw in call.keywords if kw.arg == "mode"), None)


def _open_for_write(call: ast.Call) -> bool:
    """``open(..., "w")`` 与 ``os.open(..., os.O_WRONLY | os.O_CREAT)`` 都算写。"""
    mode = _write_mode(call)
    if mode is None:
        return False
    if isinstance(mode, ast.Constant):
        return isinstance(mode.value, str) and any(flag in mode.value for flag in "wax+")
    return any(
        (isinstance(node, ast.Attribute) and node.attr in _WRITE_OPEN_FLAGS)
        or (isinstance(node, ast.Name) and node.id in _WRITE_OPEN_FLAGS)
        for node in ast.walk(mode)
    )


def _scan_tree(relative: str, tree: ast.AST) -> list[int]:
    """扫一棵 AST：直接对 memories/ 内容文件落笔的调用点行号。"""
    aliases = _engine_aliases(tree)
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if _references_content(node.value, tainted, aliases):
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in tainted:
                        tainted.add(target.id)
                        changed = True

    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else None
        name = func.id if isinstance(func, ast.Name) else None
        suspicious = False
        if attr in _WRITE_ATTRS or name == "atomic_write_text":
            receiver = func.value if isinstance(func, ast.Attribute) else None
            suspicious = (
                (receiver is not None
                 and _references_content(receiver, tainted, aliases))
                # 目标也可能在参数里（module-qualified atomic_write_text、
                # shutil.copy(src, engine.path_for(...))）。
                or any(
                    _references_content(argument, tainted, aliases)
                    for argument in node.args)
            )
        elif attr == "open" or name == "open":
            path_arg = node.args[0] if node.args else None
            suspicious = bool(
                _open_for_write(node) and (
                    (path_arg is not None
                     and _references_content(path_arg, tainted, aliases))
                    or (isinstance(func, ast.Attribute)
                        and _references_content(func.value, tainted, aliases))
                )
            )
        elif attr == "fdopen" or name == "fdopen":
            mode = _write_mode(node)
            suspicious = bool(
                isinstance(mode, ast.Constant)
                and isinstance(mode.value, str)
                and any(flag in mode.value for flag in "wax+")
                and _references_content(func, tainted, aliases)
            )
        if suspicious and relative not in _ALLOWED_WRITE_MODULES:
            found.append(node.lineno)
    return found


def _content_write_sites() -> dict[str, list[int]]:
    """直接对 memories/ 内容文件落笔的调用点（不经过 `_write_file` / `mutate_now`）。

    先做模块内简单污点传播：`path = engine.path_for(target)` 之后 `path.write_text(...)`
    同样算旁路。读原语（`read_text` / `open(..., "r")`）不在扫描面内。
    """
    found: dict[str, list[int]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = _scan_tree(relative, tree)
        if hits:
            found[relative] = hits
    return found


def test_no_module_opens_a_second_write_handle_on_content_files() -> None:
    offenders = _content_write_sites()
    assert offenders == {}, (
        "这些模块直接对 memories/ 内容文件落笔，绕过 mutate_now/MemoryCommit："
        f"{offenders}"
    )


def test_static_scan_catches_alias_keyword_and_atomic_write() -> None:
    """静态图必须覆盖别名/关键字 mode/atomic_write_text/getattr 这些旁路写法。"""
    source = (
        "engine = object()\n"
        "md = engine\n"
        "path = md.path_for('memory')\n"
        "path.open(mode='w')\n"
        "atomic_write_text(engine.path_for('user'), 'x')\n"
        "getattr(engine, 'path_for')('memory').write_text('x')\n"
        "file_state.atomic_write_text(engine.path_for('memory'), 'x')\n"
        "os.open(engine.path_for('memory'), os.O_WRONLY | os.O_CREAT)\n"
        "shutil.copy('x', engine.path_for('user'))\n"
        "engine.path_for('memory').read_text()\n"
    )

    assert _scan_tree("src/fake.py", ast.parse(source)) == [4, 5, 6, 7, 8, 9]


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
    editable = await service.create(
        make_draft(content="服务编辑", source_event_ids=["event-edit"]), LOCAL)
    await service.edit(
        editable.id, LOCAL, expected_version=1, content="服务编辑修订",
        payload=make_draft(content="服务编辑修订").payload)
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
    # bulk_delete 也必须是同一条 seam（治理批量删除）
    assert await service.bulk_delete(LOCAL, kind=None)

    assert writes, "本用例必须真的发生内容落盘"

    planned: list[tuple[str, str, str]] = []
    for path in service.state_root.receipts_dir.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["state"] == "COMMITTED", path.name
        for target, plan in payload["targets"].items():
            if (
                plan.get("planned")
                and plan.get("after_digest")
                and plan["after_digest"] != plan.get("before_digest")
            ):
                planned.append(
                    (payload["operation_id"], target, plan["after_digest"]))

    prefixes = {operation_id.split("_", 1)[0] + "_" for operation_id, _, _ in planned}
    assert {"mcf_", "del_", "stgp_", "blkp_", "jobp_"} <= prefixes

    # 每一次落盘恰好对应一条计划（数量也相等，集合相等只能证明包含关系）。
    assert Counter(writes) == Counter(
        (target, digest) for _, target, digest in planned
    ), "落盘与回执计划必须一一配对（多写或无回执写都会在这里暴露）"

    # 每个入口自己的 operation id 必须真的对上一次落盘，而不是只有前缀出现过。
    by_operation = {operation_id: (target, digest)
                    for operation_id, target, digest in planned}
    assert by_operation["jobp_single_writer_apply"][0] == "memory"
    assert by_operation[f"stgp_{record.id}"][0] == "memory"
    assert by_operation[f"blkp_{blocked_id}_apply"][0] == "memory"
    assert by_operation["del_single_writer_digest"][0] == "memory"
    assert any(operation_id.startswith("mcf_") for operation_id in by_operation)
    assert any(operation_id.startswith("del_") and operation_id != "del_single_writer_digest"
               for operation_id in by_operation)
