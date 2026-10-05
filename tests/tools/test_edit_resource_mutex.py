"""EditTool 同文件编辑跨批互斥（#525 一期 / IMP-14）。

2026-10-05 用户裁决节：一期只做同文件编辑互斥——resource 键声明 + 执行域锁
消费，同一文件的编辑跨 Tool call 批间串行（含并发 SubAgent 各自 executor 实例
的场景）；不同文件不受影响；Spec05 冻结的 exact 0/1/multiple 命中合同不变。

可观察性说明：当前 EditTool.execute 内部无 await（单事件循环内 read→write
原子执行），纯内存替身造不出真实交错。竞态症状因此用【带挂起点】的 MUTATING
测试工具（模拟未来非原子后端）命中执行域锁消费点；EditTool 本身验证 resource
键声明形状与三态合同不回归。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.tooling import (
    ErrorCode,
    Tool,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolSideEffect,
)
from agent_harness.tooling.resource_locks import ResourceLockRegistry
from agent_harness.tools import EditTool


class _SuspendArgs(BaseModel):
    path: str = Field(..., description="内存文件键")
    old_string: str = Field(..., min_length=1)
    new_string: str = Field(...)


class _SuspendingEditTool(Tool):
    """内存版 edit：read 后 await（真实挂起点）再 write。

    无互斥时两个并发调用都从旧内容读到同一快照，后提交者命中 lost-update
    （0 匹配失败）；互斥生效时严格串行，链式替换成功。
    """

    def __init__(self, files: dict[str, str], delay: float = 0.05) -> None:
        self._files = files
        self._delay = delay

    @property
    def name(self) -> str:
        return "suspend_edit"

    @property
    def description(self) -> str:
        return "测试用：带挂起点的同文件编辑"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _SuspendArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    def resource_keys(self, args: _SuspendArgs) -> list[str]:
        return [f"workspace-file:{args.path}"]

    async def execute(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, _SuspendArgs)
        content = self._files.get(args.path)
        if content is None:
            return ToolResult.failure(
                message=f"文件 '{args.path}' 不存在。",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )
        # 真实挂起点：让出事件循环，另一个并发调用得以进入读改写临界区。
        await asyncio.sleep(self._delay)
        count = content.count(args.old_string)
        if count == 0:
            return ToolResult.failure(
                message=f"在 '{args.path}' 中未找到匹配的字符串。",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )
        self._files[args.path] = content.replace(args.old_string, args.new_string, 1)
        return ToolResult.success(message="replaced")


def _tool_call(name: str, call_id: str, **args: str) -> dict:
    return {"id": call_id, "name": name, "args": args}


def _pair_executors(
    tool: Tool, locks: ResourceLockRegistry,
) -> tuple[ToolExecutor, ToolExecutor]:
    """两个共享同一 resource 锁注册表的 executor（模拟父 + 并发 SubAgent）。"""

    def _one() -> ToolExecutor:
        reg = ToolRegistry()
        reg.register(tool)
        return ToolExecutor(reg, resource_locks=locks)

    return _one(), _one()


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(workspace_root=tmp_path)


@pytest.fixture
def executor(sandbox: LocalSubprocessSandbox) -> ToolExecutor:
    reg = ToolRegistry()
    reg.register(EditTool(sandbox))
    return ToolExecutor(reg)


class _InFlightProbe(_SuspendingEditTool):
    """统计并发在途数的探针版：验证互斥没有误伤不同文件的真并行。"""

    def __init__(self, files: dict[str, str], delay: float = 0.05) -> None:
        super().__init__(files, delay)
        self.in_flight = 0
        self.max_in_flight = 0

    async def execute(self, args: BaseModel) -> ToolResult:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            return await super().execute(args)
        finally:
            self.in_flight -= 1


class _ExplodingEditTool(_SuspendingEditTool):
    """只炸第一次（挂起点后抛 RuntimeError），之后正常执行。

    执行域把工具异常映射成 ToolResult failure（不向调用方传播）。
    用于验证锁在异常/失败路径仍被释放（hold() 的 finally 分支）：
    若锁未释放，第二次调用将永远拿不到锁（饿死/超时），而非成功。
    """

    def __init__(self, files: dict[str, str], delay: float = 0.05) -> None:
        super().__init__(files, delay)
        self.exploded = False

    async def execute(self, args: BaseModel) -> ToolResult:
        if not self.exploded:
            self.exploded = True
            await asyncio.sleep(self._delay)
            raise RuntimeError("boom")
        return await super().execute(args)


class TestSameFileCrossBatchMutex:
    @pytest.mark.asyncio
    async def test_same_file_two_executors_serialize_chained_edit(self):
        """同文件两批 edit（各自 executor 实例，模拟父 + 并发 SubAgent）串行。

        链式编辑：c1 把 1→2，c2 把 2→3。无互斥时 c2 与 c1 同一快照读起
        （lost-update）→ 0 匹配失败；互斥生效时严格串行，双双成功。
        """
        files = {"a.py": "x = 1"}
        tool = _SuspendingEditTool(files)
        ex1, ex2 = _pair_executors(tool, ResourceLockRegistry())

        call1 = _tool_call("suspend_edit", "c1", path="a.py",
                           old_string="x = 1", new_string="x = 2")
        call2 = _tool_call("suspend_edit", "c2", path="a.py",
                           old_string="x = 2", new_string="x = 3")

        # c1 先持锁并进入挂起点（0.01 << delay 0.05），c2 再并发提交。
        task1 = asyncio.create_task(ex1.execute(call1))
        await asyncio.sleep(0.01)
        task2 = asyncio.create_task(ex2.execute(call2))
        exec1, exec2 = await asyncio.gather(task1, task2)

        assert exec1.result.ok is True
        assert exec2.result.ok is True
        assert files["a.py"] == "x = 3"

    @pytest.mark.asyncio
    async def test_same_file_repeated_key_single_executor_serial(self):
        """同一 executor 连续两批对同一文件编辑：第二读取到第一的写入。"""
        files = {"a.py": "x = 1"}
        tool = _SuspendingEditTool(files)
        ex1, _ex2 = _pair_executors(tool, ResourceLockRegistry())

        first = await ex1.execute(_tool_call(
            "suspend_edit", "c1", path="a.py",
            old_string="x = 1", new_string="x = 2"))
        second = await ex1.execute(_tool_call(
            "suspend_edit", "c2", path="a.py",
            old_string="x = 2", new_string="x = 3"))

        assert first.result.ok is True
        assert second.result.ok is True
        assert files["a.py"] == "x = 3"

    @pytest.mark.asyncio
    async def test_lock_released_on_tool_exception(self):
        """工具抛异常（非 ToolResult failure）后锁必须释放，后续同 key 调用不被饿死。"""
        files = {"a.py": "x = 1"}
        tool = _ExplodingEditTool(files)
        ex1, ex2 = _pair_executors(tool, ResourceLockRegistry())

        # 执行域把工具异常映射成 failure ToolResult（不向调用方传播）；
        # 关键断言在下一步：锁已释放，同 key 后续调用不被饿死。
        first = await ex1.execute(_tool_call("suspend_edit", "c1", path="a.py",
                                             old_string="x = 1", new_string="x = 2"))
        assert first.result.ok is False

        result = await ex2.execute(_tool_call(
            "suspend_edit", "c2", path="a.py",
            old_string="x = 1", new_string="x = 2"))
        assert result.result.ok is True


    @pytest.mark.asyncio
    async def test_opposite_order_multi_key_no_deadlock(self):
        """一次调用声明多 key、两次调用 key 顺序相反：排序 acquire 保证无环。

        布景：主任务先占住 a——t1（顺序 [a, gate]）在 a 上阻塞、t2（顺序
        [gate, a]）已持有 gate 在 a 上阻塞；若无排序，main 释放 a 后
        t1 持 a 等 gate、t2 持 gate 等 a ⇒ 死锁。排序后两调用同为
        [a, gate] 升序 ⇒ 先来先走，双双完成。
        """
        reg = ResourceLockRegistry()
        held = await reg.acquire_all(["workspace-file:a"])

        async def _grab(keys: list[str], tag: str) -> str:
            locks = await reg.acquire_all(keys)
            try:
                return tag
            finally:
                reg.release_all(locks)

        t1 = asyncio.create_task(_grab(
            ["workspace-file:a", "workspace-file:gate-x"], "t1"))
        await asyncio.sleep(0.01)  # t1 已阻塞在 a 上
        t2 = asyncio.create_task(_grab(
            ["workspace-file:gate-x", "workspace-file:a"], "t2"))
        await asyncio.sleep(0.01)  # t2 已持有 gate-x、阻塞在 a 上
        reg.release_all(held)  # 解锁：无环 ⇒ 两者都应完成而非互等

        done = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=3)
        assert sorted(done) == ["t1", "t2"]


class TestDifferentFilesUnaffected:
    @pytest.mark.asyncio
    async def test_different_files_still_run_concurrently(self):
        """不同文件：互斥不生效（key 不同），真并行保留（票面 AC）。"""
        files = {"a.py": "x = 1", "b.py": "y = 1"}
        tool = _InFlightProbe(files, delay=0.05)
        ex1, ex2 = _pair_executors(tool, ResourceLockRegistry())

        call_a = _tool_call("suspend_edit", "c1", path="a.py",
                            old_string="x = 1", new_string="x = 2")
        call_b = _tool_call("suspend_edit", "c2", path="b.py",
                            old_string="y = 1", new_string="y = 2")

        exec1, exec2 = await asyncio.gather(
            ex1.execute(call_a), ex2.execute(call_b),
        )

        assert exec1.result.ok is True
        assert exec2.result.ok is True
        assert files == {"a.py": "x = 2", "b.py": "y = 2"}
        # 两个调用真实重叠在途（未串行化）——锁没有把不同文件误伤成排队。
        assert tool.max_in_flight == 2

    @pytest.mark.asyncio
    async def test_no_shared_registry_no_mutex(self):
        """不共享锁注册表的两个 executor：行为与改造前一致（各自独立执行）。"""
        files = {"a.py": "x = 1"}
        tool = _SuspendingEditTool(files, delay=0.01)

        reg = ToolRegistry()
        reg.register(tool)
        # 两个 executor 各自持有不同的 ResourceLockRegistry（或不传）。
        ex1 = ToolExecutor(reg)
        ex2 = ToolExecutor(reg)

        results = await asyncio.gather(
            ex1.execute(_tool_call("suspend_edit", "c1", path="a.py",
                                   old_string="x = 1", new_string="x = 2")),
            ex2.execute(_tool_call("suspend_edit", "c2", path="a.py",
                                   old_string="x = 1", new_string="x = 2")),
        )
        # 各自从同一快照读起、各自成功——不共享注册表就没有互斥承诺。
        assert results[0].result.ok is True
        assert results[1].result.ok is True


class TestEditResourceKeyShape:
    def _args(self, sandbox: LocalSubprocessSandbox, path: str):
        tool = EditTool(sandbox)
        return tool, tool.args_schema(
            path=path, old_string="a", new_string="b")

    def test_key_normalized_dedupes_dot_segments(
        self, sandbox: LocalSubprocessSandbox
    ):
        """同一文件的 ./ 与 prefix 写法归一到同一 key（互斥不因路径写法漏配）。"""
        tool, args = self._args(sandbox, "a/f.py")
        assert tool.resource_keys(args) == ["workspace-file:a/f.py"]

        _, args_dot = self._args(sandbox, "./a/f.py")
        assert tool.resource_keys(args_dot) == ["workspace-file:a/f.py"]

        _, args_inner = self._args(sandbox, "a/./f.py")
        assert tool.resource_keys(args_inner) == ["workspace-file:a/f.py"]

    def test_different_paths_get_different_keys(
        self, sandbox: LocalSubprocessSandbox
    ):
        tool, args_a = self._args(sandbox, "a.py")
        tool2, args_b = self._args(sandbox, "b.py")
        keys_a = tool.resource_keys(args_a)
        keys_b = tool2.resource_keys(args_b)
        assert keys_a != keys_b
        assert keys_a[0].startswith("workspace-file:")

    def test_base_tool_default_is_no_keys(self):
        """Tool 基类默认不声明 resource key（安全默认：不参与互斥）。"""

        class _PlainTool(Tool):
            @property
            def name(self) -> str:
                return "plain"

            @property
            def description(self) -> str:
                return "plain"

            @property
            def args_schema(self) -> type[BaseModel]:
                return _SuspendArgs

            @property
            def side_effect(self) -> ToolSideEffect:
                return ToolSideEffect.READ_ONLY

            async def execute(self, args: BaseModel) -> ToolResult:
                return ToolResult.success(message="ok")

        assert _PlainTool().resource_keys(_SuspendArgs(
            path="p", old_string="a", new_string="b")) == []

    @pytest.mark.asyncio
    async def test_multi_hit_contract_not_regressed(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """Spec05 冻结合同回归：>1 命中 → AMBIGUOUS（本票不改三态语义）。"""
        sandbox.write_text("f.py", "return 1\nreturn 1\n")
        result = await executor.execute(
            _tool_call("edit", "c1", path="f.py", old_string="return 1",
                       new_string="return 2"))
        assert result.result.ok is False
        # >1 命中 → AMBIGUOUS（映射 TOOL_EXECUTION_ERROR，见 tools/edit.py 头注）。
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
