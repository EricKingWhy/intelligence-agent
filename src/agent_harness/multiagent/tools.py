"""delegate 工具：supervisor 的委派入口（Phase 13 T2, #83, ADR-0015 决策 4）。

DelegationDecision 即 delegate 工具调用参数（spec §4）——编排决策由模型
做出（agentic），工具经统一 ToolExecutor（不变量 #7，零旁路）。V1 阻塞
串行：调用 → child 跑完 → SubAgentResult 回填。

语义：
- child status=completed → ToolResult.success（payload = result JSON）；
- child 非 completed → ToolResult.failure（payload 仍带 result，retryable
  =False）——失败事实如实呈现，重试/放弃由 supervisor 决策（决策 16）；
- 未激活 / 未知 target → 明确失败，绝不静默伪装。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, Field

from agent_harness.context.tokens import estimate_tokens
from agent_harness.multiagent.provider import InProcessSubagentProvider
from agent_harness.session.context import current_session_var
from agent_harness.tooling import Tool, ToolResult, ToolRuntimeSignal, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode

#: 外置后父窗保留的摘要行长度（字符）。依据（2026-09-29 调研）：Anthropic 工程博客
# 对子代理回传的口径是 "a condensed, distilled summary ... (often 1,000-2,000
# tokens)"，Claude Code 的 TASK_MAX_OUTPUT_LENGTH 到 32_000 字符才落盘——200 字符
# （≈50-100 token）低于一切公开先例，父窗往往被迫多一轮 read-back。1500 字符
# ≈中文 1K token / 英文 ~375 token，落在该区间内且仍是有界载荷。
# 只作用于**超阈值外置**路径；未超阈值路径全文回传，不经此常量（逐字节不变）。
_EXTERNALIZED_SUMMARY_HEAD_CHARS = 1500

# output_schema（#530 IMP-17）：schema 进 child prompt、每次重试都复读，必须有界。
_MAX_OUTPUT_SCHEMA_BYTES = 4096


def _check_output_schema(schema: Any) -> str | None:
    """output_schema 参数校验（零副作用，fuse 纪律）：非法 / 超 4KB → 错误消息。"""
    if not isinstance(schema, dict):
        return "output_schema 必须是 JSON Schema 对象（dict）"
    serialized = json.dumps(schema, ensure_ascii=False)
    size = len(serialized.encode("utf-8"))
    if size > _MAX_OUTPUT_SCHEMA_BYTES:
        return (f"output_schema 序列化后 {size} 字节，超过上限 "
                f"{_MAX_OUTPUT_SCHEMA_BYTES} 字节；请精简 schema 后重试。")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        return f"output_schema 不是合法的 JSON Schema（Draft 2020-12）：{error.message}"
    return None


def _schema_instruction(schema: dict[str, Any]) -> str:
    """给 child 的输出指令（设计 §3.3 原文），拼接到 task。"""
    schema_json = json.dumps(schema, ensure_ascii=False)
    return (
        "你的最终回答的末尾必须是且仅是一个 JSON 对象，符合以下 JSON Schema：\n"
        f"{schema_json}\n"
        "不要在 JSON 之外解释该对象（解释写在 JSON 之前）。"
    )


def _schema_errors(text: str, schema: dict[str, Any]) -> tuple[Any | None, list[str]]:
    """从 child 最终回答提取并校验 JSON。

    提取顺序：最后一个 ```json 围栏 → 全文 json.loads 兜底 → iter_errors。
    返回 (对象, 错误列表)：对象非 None ⇒ 错误列表为空；提取失败归类
    missing_json（PORT DESIGN←dotai missing_tool_call）。
    """
    obj: Any = None
    fences = re.findall(r"```json\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fences:
        try:
            obj = json.loads(fences[-1])
        except ValueError:
            obj = None
    if obj is None:
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
    if obj is None:
        return None, ["missing_json: 最终回答中未找到可解析的 JSON 对象"]
    validator = Draft202012Validator(schema)
    errors = [
        f"schema 校验失败 {error.json_path}: {error.message}"
        for error in sorted(validator.iter_errors(obj),
                            key=lambda e: list(e.absolute_path))
    ]
    return (obj, []) if not errors else (None, errors)


class _DelegateArgs(BaseModel):
    target: str = Field(
        ...,
        description="委派目标的 profile 名（可选值见工具描述；只能是预定义角色）",
    )
    task: str = Field(
        ...,
        min_length=1,
        description=(
            "给子代理的完整自洽任务描述——子代理看不到你们的对话历史，任务"
            "描述必须包含它需要的全部上下文与验收标准"
        ),
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="可选约束清单（如：只改某目录、必须先写测试）",
    )
    output_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "可选：要求子代理最终输出必须符合的 JSON Schema（dict）。"
            "提供时，任务描述会自动追加输出格式指令；子代理结果经校验后回填，"
            "不符则自动重试一次。缺省 None = 现有行为逐字节不变。"
        ),
    )


class DelegateTool(Tool):
    is_subagent_dispatch = True
    """委派工具：multiagent capability 贡献的编排入口（可装卸插件）。"""

    def __init__(
        self,
        provider: InProcessSubagentProvider,
        *,
        max_delegations: int = 8,
        artifact_store: Any | None = None,
        summary_overflow_tokens: int = 0,
    ) -> None:
        self._provider = provider
        # 树级默认上限（#287）：持久化计数由 provider 的共享 tree ledger 负责。
        self._max_delegations = max_delegations
        # 结论 ref 化（W-31.3 / #415，不变量 #15）：summary 超阈值时全文外置
        # ArtifactStore，父窗只收截断摘要行 + summary_ref。默认关（store=None 或
        # 阈值 0）⇒ 现有行为逐字节不变；wiring 的 wire 期构造不传即关。
        self._artifact_store = artifact_store
        self._summary_overflow_tokens = summary_overflow_tokens

    @property
    def name(self) -> str:
        return "delegate"

    @property
    def description(self) -> str:
        available = ", ".join(sorted(self._provider._profiles)) or "（无可用 profile）"
        return (
            "把一个 scoped task 委派给专门的子代理执行（阻塞：返回时任务已完"
            f"成或失败）。可选 target：{available}。子代理拥有独立上下文，"
            "task 描述必须自洽；返回结构化结果（status/summary 等）。重探索"
            "类工作（全仓搜索、大文件分析、多源检索）默认走 delegate，在子代"
            "理的干净上下文里执行，父窗口只收结论；琐碎小事请亲自完成，不要"
            "为委派而委派。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _DelegateArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        # child 可写共享 workspace——委派本身就是有副作用的编排动作。
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WORKSPACE_WRITE

    @property
    def timeout_seconds(self) -> float:
        # child 是一次完整 run（多轮模型调用），父工具超时必须给足余量；
        # 真正的卡流治理由 child 继承的 stall 看门狗承担（秒级）。
        return 1800.0

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=True,
            suggested_action="重复同一委派会得到新 child 会话；换措辞或亲自验证。",
        )

    @property
    def prompt_guidance(self) -> str:
        """委派机制的操作事实（ADR-0023 D11）——不进 tool schema，它不是参数说明。

        写的是**代码里的既有事实**：预算默认值、child 的可见性、失败如何回填。
        这些只在 delegate 确实注册时才该出现（工具缺席 → 说明缺席）。
        """
        return (
            f"委派须知：整棵委派树最多委派 {self._max_delegations} 次，超出会明确失败，"
            "请预留收尾余量；重探索（全仓搜索、大文件分析、多源检索）默认走 delegate，"
            "你只接收它的结论（超长结论会以外置产物 ref 提供，可按需读回全文），"
            "不要在父窗口亲自重复执行这类检索；子代理看不到你们的对话历史，task 描述必须自洽；"
            "子代理失败会原样回填（含它的结构化结果），是否重试由你决定。"
        )

    async def execute(self, args: _DelegateArgs) -> ToolResult:
        # 零副作用参数校验（fuse 纪律）：schema 非法/超限时在 reserve 之前拒绝，
        # 不占委派预算、不启动 child。
        output_schema = args.output_schema
        if output_schema is not None:
            schema_error = _check_output_schema(output_schema)
            if schema_error is not None:
                return ToolResult.failure(
                    message=schema_error, error_code=ErrorCode.INVALID_ARGUMENT,
                )
        task = args.task
        if output_schema is not None:
            task = task + "\n\n" + _schema_instruction(output_schema)
        tree_id = self._provider.tree_id()
        fingerprint = self._fingerprint(args.target, args.task, args.constraints)
        try:
            reservation = await self._provider.reserve_delegation(
                tree_id, max_delegations=self._max_delegations,
            )
        except Exception:  # noqa: BLE001 - a missing durable reservation must fail closed
            return ToolResult.failure(
                message="无法确认委派树预算；本次未启动子代理，请稍后重试或直接收尾。",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )
        if not reservation.accepted:
            # 预算拒绝**不**进树守卫：熔断器（#88）数的是"子代理执行了且失败"；
            # 配额拒绝里子代理从未启动（`#318` 起 session 级拒绝还可能先于本 run
            # 的树行存在——observe 一个不存在的树只会拿到 KeyError）。拒绝消息
            # 自己带 used/limit，模型据此收尾；反复空转由 turns 预算兜底。
            return ToolResult.failure(
                message=(f"delegation 预算耗尽（已用 {reservation.used}/{reservation.limit}）。"
                         "请综合已有结果直接收尾，或改变策略，不要再委派。"),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        try:
            result = await self._provider.run(
                target=args.target, task=task, constraints=args.constraints,
            )
        except ValueError as error:
            failed = ToolResult.failure(
                message=str(error), error_code=ErrorCode.INVALID_ARGUMENT,
            )
            return await self._with_tree_guard(failed, tree_id, fingerprint)
        except RuntimeError as error:
            failed = ToolResult.failure(
                message=str(error), error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )
            return await self._with_tree_guard(failed, tree_id, fingerprint)
        if output_schema is None:
            # None = 现有路径逐字节不变（payload / 事件组装经 _assemble 共用）。
            payload, pending_events = await self._assemble(
                result, target=args.target, task=args.task,
            )
            if result.status == "completed":
                completed = ToolResult.success(
                    message=f"子代理 '{result.agent_id}' 完成：{result.summary[:200]}",
                    data={"output": json.dumps(payload, ensure_ascii=False)},
                    pending_events=pending_events,
                )
                return await self._with_tree_guard(completed, tree_id, fingerprint)
            # failure 无 data 参数：结构化 payload 走 metadata（内容仍经
            # model_dump_json 全量回灌给 supervisor 模型）。
            failed = ToolResult.failure(
                message=f"子代理 '{result.agent_id}' 未能完成（status={result.status}）："
                        f"{result.summary[:200] or '（无输出）'}",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
                retryable=False,
                metadata={"output": json.dumps(payload, ensure_ascii=False)},
                pending_events=pending_events,
            )
            return await self._with_tree_guard(failed, tree_id, fingerprint)
        return await self._finish_with_schema(
            result, args, output_schema, task, tree_id, fingerprint,
        )

    async def _assemble(
        self, result: Any, *, target: str, task: str,
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        """payload 与 pending_events 组装（None 路径与 schema 路径共用）。

        None 路径的输出与本特性引入前逐字节一致。
        """
        payload: dict[str, Any] = {
            "agent_id": result.agent_id,
            "status": result.status,
            "summary": result.summary,
        }
        # 结论 ref 化（W-31.3 / #415）：成功与失败两条回传路径共用本 payload，
        # 在分支前统一处理；未超阈值/特性关/外置失败时 payload 原样返回。
        payload, externalize_event = await self._externalize_summary_if_overflowed(
            result, payload,
        )
        # 真实来源字段：无则省略（绝不伪造/补零，ADR-0015 决策 12）
        for field_name in ("citations", "artifacts", "changed_files", "unresolved"):
            value = getattr(result, field_name, [])
            if value:
                payload[field_name] = value
        # 白盒透明（ADR-0015 决策 6/8）：委派事实以 pending_events 经 executor
        # 落盘（tool/call 之后、tool/result 之前）。started/finished 的落盘时点
        # 都是委派完成时——阻塞串行模型下无观察者可见差。
        pending_events = [
            ("agent/delegation-started", {
                "target": target, "task": task,
                "child_session_id": result.child_session_id,
            }),
            ("agent/delegation-finished", {
                "target": target,
                "child_session_id": result.child_session_id,
                "status": result.status, "summary": result.summary,
            }),
        ]
        # 外置事实走 overflow 同款 artifact/externalized 白盒事件（第二遍复审
        # P2：web 工件面靠它可见）；未外置时为 None 不发。
        if externalize_event is not None:
            pending_events.append(externalize_event)
        return payload, pending_events

    async def _finish_with_schema(
        self,
        result: Any,
        args: _DelegateArgs,
        output_schema: dict[str, Any],
        task: str,
        tree_id: str,
        fingerprint: str,
    ) -> ToolResult:
        """output_schema 非 None 的收尾：校验 → 不符则重试一次（同 reserve 路径
        占预算）→ 两次都不符按 schema_retry_exhausted 诚实失败（设计 §3.2/§3.6）。"""
        obj, errors = _schema_errors(result.summary, output_schema)
        payload, pending_events = await self._assemble(
            result, target=args.target, task=task,
        )
        if obj is not None:
            payload["structured"] = obj
            completed = ToolResult.success(
                message=f"子代理 '{result.agent_id}' 完成：{result.summary[:200]}",
                data={"output": json.dumps(payload, ensure_ascii=False)},
                metadata={"attempts": 1},
                pending_events=self._mark_finished_events(pending_events, "passed"),
            )
            return await self._with_tree_guard(completed, tree_id, fingerprint)
        # 第一次校验失败：重试 = 新 child（provider.run 每次开新 session），
        # 走同一 reserve_delegation 路径占委派树预算（spec 10 §5.1）。
        pending_events = self._mark_finished_events(pending_events, "failed_retry")
        retry_task = (
            task
            + "\n\n上次输出不符合 schema，错误如下：\n"
            + "\n".join(f"- {e}" for e in errors)
            + "\n请只输出修正后的 JSON。"
        )
        budget_note: str | None = None
        try:
            reservation = await self._provider.reserve_delegation(
                tree_id, max_delegations=self._max_delegations,
            )
            if not reservation.accepted:
                budget_note = (
                    "retry_not_started: delegation 预算耗尽"
                    f"（已用 {reservation.used}/{reservation.limit}），"
                    "第二次尝试未能启动"
                )
        except Exception:  # noqa: BLE001 - a missing durable reservation must fail closed
            budget_note = "retry_not_started: 无法确认委派树预算，第二次尝试未能启动"
        if budget_note is None:
            try:
                result2 = await self._provider.run(
                    target=args.target, task=retry_task,
                    constraints=args.constraints,
                )
            except ValueError as error:
                budget_note = f"retry_not_started: {error}"
            except RuntimeError as error:
                budget_note = f"retry_not_started: {error}"
        if budget_note is not None:
            # 预算/启动失败按"第二次失败"同口径（设计 §3.6）。
            return await self._with_tree_guard(self._schema_exhausted(
                errors + [budget_note], payload,
                self._mark_finished_events(pending_events, "retry_exhausted"),
            ), tree_id, fingerprint)
        obj2, errors2 = _schema_errors(result2.summary, output_schema)
        payload2, pending_events2 = await self._assemble(
            result2, target=args.target, task=retry_task,
        )
        if obj2 is not None:
            payload2["structured"] = obj2
            completed = ToolResult.success(
                message=f"子代理 '{result2.agent_id}' 完成：{result2.summary[:200]}",
                data={"output": json.dumps(payload2, ensure_ascii=False)},
                metadata={"attempts": 2},
                pending_events=pending_events
                + self._mark_finished_events(pending_events2, "passed"),
            )
            return await self._with_tree_guard(completed, tree_id, fingerprint)
        return await self._with_tree_guard(self._schema_exhausted(
            errors + errors2, payload2,
            pending_events
            + self._mark_finished_events(pending_events2, "retry_exhausted"),
        ), tree_id, fingerprint)

    @staticmethod
    def _mark_finished_events(
        pending_events: list[tuple[str, dict[str, Any]]], validation: str,
    ) -> list[tuple[str, dict[str, Any]]]:
        """delegation-finished 事件加 output_schema/schema_validation 字段（§3.5）。"""
        return [
            (event_type, {**data, "output_schema": True, "schema_validation": validation}
             if event_type == "agent/delegation-finished" else data)
            for event_type, data in pending_events
        ]

    @staticmethod
    def _schema_exhausted(
        errors: list[str],
        payload: dict[str, Any],
        pending_events: list[tuple[str, dict[str, Any]]],
    ) -> ToolResult:
        head = errors[0] if errors else ""
        return ToolResult.failure(
            message=(f"子代理输出连续两次不符合 output_schema（attempts=2）：{head}"
                     f"（共 {len(errors)} 条错误，见 metadata.errors）"),
            error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            retryable=False,
            metadata={
                "output": json.dumps(payload, ensure_ascii=False),
                "code": "schema_retry_exhausted",
                "attempts": 2,
                "errors": errors,
            },
            pending_events=pending_events,
        )

    async def _externalize_summary_if_overflowed(
        self, result: Any, payload: dict[str, Any],
    ) -> tuple[dict[str, Any], tuple[str, dict[str, Any]] | None]:
        """summary 超阈值时把全文外置 ArtifactStore，payload 只留摘要行 + ref。

        返回 (payload, externalize_event)；event 非 None 时由调用方并入
        pending_events（overflow 同款 artifact/externalized，含 size/mime_type）。

        fail-open 三级（W-31.3 / #415）：特性关（store=None 或阈值 ≤0）、执行期
        无会话上下文、save 抛异常——任一发生都回退现有行为（payload 带全文），
        绝不因外置失败丢结论。session_id 取执行期的父会话：装配层给本工具的
        artifact store 绑定的是根会话命名空间（LocalArtifactStore 校验
        session_id 必须等于命名空间），child_session_id 会被它拒收。深层委派
        （depth ≥ 2）时执行期会话本身已不在根命名空间 ⇒ save 被同一校验拒收、
        同口径 fail-open——本特性在嵌套委派内层有意不生效（结论不丢，provider
        侧工具输出截断托底）。
        """
        if (self._artifact_store is None or self._summary_overflow_tokens <= 0
                or estimate_tokens(result.summary) <= self._summary_overflow_tokens):
            return payload, None
        session = current_session_var.get()
        if session is None:
            # Tool.execute 协议不带 session；直呼工具（无 run 上下文）时拿不到
            # 会话命名空间——fail-open 不外置，与 store=None 同口径。
            return payload, None
        try:
            artifact = await self._artifact_store.save(
                session.session_id, result.summary,
                mime_type="text/plain", source_tool=self.name,
                # execute 期拿不到 tool_call_id（executor 在 execute 返回后才绑定
                # 调用 id）；今日无任何消费方读 Artifact.tool_call_id，空串 =
                # 如实"无值"。委派事实的关联走 pending_events 里的 child_session_id。
                tool_call_id="",
            )
        except Exception:  # noqa: BLE001 - 外置失败必须回退全文，不丢结论
            return payload, None
        deferred = ("artifact/externalized", {
            "artifact_id": artifact.artifact_id,
            "session_id": session.session_id,
            "source_tool": self.name, "tool_call_id": "",
            "size": artifact.size, "mime_type": artifact.mime_type,
        })
        return {
            **payload,
            "summary": result.summary[:_EXTERNALIZED_SUMMARY_HEAD_CHARS],
            "summary_ref": artifact.artifact_id,
            "summary_truncated": True,
        }, deferred

    @staticmethod
    def _fingerprint(target: str, task: str, constraints: list[str]) -> str:
        request = json.dumps(
            {"task": task, "constraints": constraints},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(request.encode("utf-8")).hexdigest()
        return f"{target}:{request_hash}"

    async def _with_tree_guard(
        self, result: ToolResult, tree_id: str, fingerprint: str,
    ) -> ToolResult:
        signal = await self._provider.observe_delegation_result(
            tree_id, fingerprint, ok=result.ok,
        )
        return result.model_copy(update={
            "runtime_signal": ToolRuntimeSignal(
                level=signal.level.name.lower(),
                tool_name=signal.tool_name,
                fingerprint=signal.fingerprint,
                consecutive_failures=signal.consecutive_failures,
            ),
        })
