"""decision_hooks 模块的单元测试（TDD）。

覆盖设计文档 §8 AC-1~AC-6 中模块级可断言的部分；
Executor 集成级断言（零开销、DENY 零执行、REWRITE 重校验）在
tests/tooling/test_executor_decision_hook.py 中覆盖。
"""

import asyncio

import pytest

from agent_harness.tooling.contract import ToolPermission, ToolSideEffect
from agent_harness.tooling.decision_hooks import (
    DEFAULT_DECISION_HOOK_TIMEOUT_SECONDS,
    BeforeToolDecision,
    DecisionFailPolicy,
    DecisionHookRunner,
    DecisionRequest,
    DecisionResolution,
    DecisionVerdict,
)


def _request() -> DecisionRequest:
    return DecisionRequest(
        tool_call_id="call_1",
        tool_name="bash",
        args={"command": "ls"},
        permission=ToolPermission.DANGER,
        side_effect=ToolSideEffect.MUTATING,
    )


def _runner(hook=None, **kwargs) -> DecisionHookRunner:
    return DecisionHookRunner(hook, **kwargs)


class TestDecisionVerdict:
    def test_allow_deny_rewrite(self):
        assert BeforeToolDecision.allow().verdict is DecisionVerdict.ALLOW
        d = BeforeToolDecision.deny("nope")
        assert d.verdict is DecisionVerdict.DENY
        assert d.reason == "nope"
        r = BeforeToolDecision.rewrite({"command": "pwd"})
        assert r.verdict is DecisionVerdict.REWRITE
        assert r.args == {"command": "pwd"}

    def test_rewrite_rejects_non_dict(self):
        with pytest.raises(TypeError):
            BeforeToolDecision.rewrite("not-a-dict")  # type: ignore[arg-type]

    def test_default_timeout(self):
        assert DEFAULT_DECISION_HOOK_TIMEOUT_SECONDS == 5.0
        assert _runner()._timeout_seconds == 5.0

    def test_default_fail_open(self):
        assert _runner()._on_error is DecisionFailPolicy.FAIL_OPEN

    def test_non_positive_timeout_rejected(self):
        with pytest.raises(ValueError):
            DecisionHookRunner(timeout_seconds=0)
        with pytest.raises(ValueError):
            DecisionHookRunner(timeout_seconds=-1.0)


class TestActive:
    def test_no_hook_not_active(self):
        assert _runner().active is False

    def test_with_hook_active(self):
        async def _hook(_req: DecisionRequest) -> BeforeToolDecision:
            return BeforeToolDecision.allow()

        assert _runner(_hook).active is True


class TestAllowDeny:
    @pytest.mark.asyncio
    async def test_allow_passthrough(self):
        async def _hook(_req: DecisionRequest) -> BeforeToolDecision:
            return BeforeToolDecision.allow()

        res = await _runner(_hook).decide(
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": "ls"},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert isinstance(res, DecisionResolution)
        assert res.decision.verdict is DecisionVerdict.ALLOW
        assert res.degraded is False

    @pytest.mark.asyncio
    async def test_deny_carries_reason(self):
        async def _hook(_req: DecisionRequest) -> BeforeToolDecision:
            return BeforeToolDecision.deny("policy says no")

        res = await _runner(_hook).decide(
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": "rm -rf /"},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res.decision.verdict is DecisionVerdict.DENY
        assert res.decision.reason == "policy says no"
        assert res.degraded is False

    @pytest.mark.asyncio
    async def test_hook_sees_deep_copied_args(self):
        seen = {}

        async def _hook(req: DecisionRequest) -> BeforeToolDecision:
            seen["args"] = req.args
            req.args["command"] = "MUTATED"  # 就地改 hook 看到的副本
            return BeforeToolDecision.allow()

        raw = {"command": "ls"}
        await _runner(_hook).decide(
            tool_call_id="c1",
            tool_name="bash",
            raw_args=raw,
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        # hook 的就地修改回流不到调用方的 raw_args（§4.5：改写未完成不部分应用）
        assert raw == {"command": "ls"}
        assert seen["args"] == {"command": "MUTATED"}


class TestRewrite:
    @pytest.mark.asyncio
    async def test_rewrite_returns_new_args(self):
        async def _hook(_req: DecisionRequest) -> BeforeToolDecision:
            return BeforeToolDecision.rewrite({"command": "pwd"})

        res = await _runner(_hook).decide(
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": "ls"},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res.decision.verdict is DecisionVerdict.REWRITE
        assert res.decision.args == {"command": "pwd"}
        assert res.degraded is False

    @pytest.mark.asyncio
    async def test_rewrite_result_is_isolated_snapshot(self):
        async def _hook(_req: DecisionRequest) -> BeforeToolDecision:
            return BeforeToolDecision.rewrite({"command": "pwd"})

        runner = _runner(_hook)
        res = await runner.decide(
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": "ls"},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res.decision.args is not None
        res.decision.args["command"] = "MUTATED_AFTER"
        # 第二次 decide 拿到的是新的快照，不受上次篡改影响
        res2 = await runner.decide(
            tool_call_id="c2",
            tool_name="bash",
            raw_args={"command": "ls"},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res2.decision.args == {"command": "pwd"}


class TestUncopyableArgs:
    """M-1：deepcopy 失败（不可拷贝对象）同样走 fail 策略，不冒泡。"""

    @pytest.mark.asyncio
    async def test_uncopyable_raw_args_fail_open(self):
        async def _hook(_req: DecisionRequest) -> BeforeToolDecision:
            return BeforeToolDecision.allow()

        class _Uncopyable:
            def __deepcopy__(self, memo):
                raise RuntimeError("cannot deepcopy me")

        res = await _runner(_hook).decide(
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": _Uncopyable()},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res.decision.verdict is DecisionVerdict.ALLOW
        assert res.degraded is True

    @pytest.mark.asyncio
    async def test_uncopyable_raw_args_fail_closed(self):
        async def _hook(_req: DecisionRequest) -> BeforeToolDecision:
            return BeforeToolDecision.allow()

        class _Uncopyable:
            def __deepcopy__(self, memo):
                raise RuntimeError("cannot deepcopy me")

        res = await _runner(_hook, on_error=DecisionFailPolicy.FAIL_CLOSED).decide(
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": _Uncopyable()},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res.decision.verdict is DecisionVerdict.DENY
        assert res.degraded is True


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_fail_open(self):
        async def _slow(_req: DecisionRequest) -> BeforeToolDecision:
            await asyncio.sleep(10)
            return BeforeToolDecision.deny("too late")

        res = await _runner(_slow, timeout_seconds=0.05).decide(
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": "ls"},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res.decision.verdict is DecisionVerdict.ALLOW
        assert res.degraded is True

    @pytest.mark.asyncio
    async def test_timeout_fail_closed(self):
        async def _slow(_req: DecisionRequest) -> BeforeToolDecision:
            await asyncio.sleep(10)
            return BeforeToolDecision.allow()

        res = await _runner(
            _slow, timeout_seconds=0.05, on_error=DecisionFailPolicy.FAIL_CLOSED
        ).decide(
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": "ls"},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res.decision.verdict is DecisionVerdict.DENY
        assert res.degraded is True


class TestHookError:
    @pytest.mark.asyncio
    async def test_exception_fail_open(self):
        async def _boom(_req: DecisionRequest) -> BeforeToolDecision:
            raise RuntimeError("hook exploded")

        res = await _runner(_boom).decide(
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": "ls"},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res.decision.verdict is DecisionVerdict.ALLOW
        assert res.degraded is True

    @pytest.mark.asyncio
    async def test_exception_fail_closed(self):
        async def _boom(_req: DecisionRequest) -> BeforeToolDecision:
            raise RuntimeError("hook exploded")

        res = await _runner(_boom, on_error=DecisionFailPolicy.FAIL_CLOSED).decide(
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": "ls"},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res.decision.verdict is DecisionVerdict.DENY
        assert res.degraded is True

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        # run 取消必须原样传播，绝不能被吞成一次 ALLOW（与 executor 取消臂一致）
        async def _cancel(_req: DecisionRequest) -> BeforeToolDecision:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await _runner(_cancel).decide(
                tool_call_id="c1",
                tool_name="bash",
                raw_args={"command": "ls"},
                permission=ToolPermission.DANGER,
                side_effect=ToolSideEffect.MUTATING,
            )

    @pytest.mark.asyncio
    async def test_bare_string_verdict_degrades(self):
        # M-2：BeforeToolDecision(verdict="deny") 裸字符串在 executor 侧
        # `is DecisionVerdict.DENY` 不命中，会静默变无标记 ALLOW——
        # 必须判非法走 fail 策略（fail-closed 下是安全语义）。
        async def _bad(_req: DecisionRequest):  # type: ignore[no-untyped-def]
            return BeforeToolDecision(verdict="deny")  # type: ignore[arg-type]

        res = await _runner(_bad).decide(  # type: ignore[arg-type]
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": "ls"},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res.decision.verdict is DecisionVerdict.ALLOW
        assert res.degraded is True

        res_closed = await _runner(
            _bad,
            on_error=DecisionFailPolicy.FAIL_CLOSED,  # type: ignore[arg-type]
        ).decide(
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": "ls"},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res_closed.decision.verdict is DecisionVerdict.DENY
        assert res_closed.degraded is True

    @pytest.mark.asyncio
    async def test_illegal_return_value_degrades(self):
        async def _bad(_req: DecisionRequest):  # type: ignore[no-untyped-def]
            return "not-a-decision"

        res = await _runner(_bad).decide(  # type: ignore[arg-type]
            tool_call_id="c1",
            tool_name="bash",
            raw_args={"command": "ls"},
            permission=ToolPermission.DANGER,
            side_effect=ToolSideEffect.MUTATING,
        )
        assert res.decision.verdict is DecisionVerdict.ALLOW
        assert res.degraded is True
