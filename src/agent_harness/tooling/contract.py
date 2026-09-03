"""Tool Contract：Tool 的身份、Schema、执行入口与策略元数据。

为什么用 ABC（抽象基类）而不是 Pydantic BaseModel：
- Tool 有【行为】（execute 方法），不只是数据。Pydantic 模型装不下抽象方法。
- Tool 是"接口契约"，子类填实现；ABC 表达这层意图最直接。
- 对比 ToolResult：ToolResult 是纯数据 + 序列化，所以用 Pydantic。

为什么 execute 收的是【已校验的 Pydantic 实例】而不是 dict：
- 主链是 args_schema.model_validate(args) → execute(validated_args)。
- 校验发生在执行前（Task 2 实现），所以 execute 拿到的必定是合法实例，
  无需在 execute 内再判一次参数。
- 今天（Task 1）execute 只是"被定义"，Executor 还不会调它。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from enum import Enum

from pydantic import BaseModel

from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ToolResult


class ToolSideEffect(str, Enum):
    """Tool 副作用分类。

    为什么是 str Enum：JSON 序列化与日志可读性，与 ErrorCode 同理。

    为什么只两类，不做 READ/WRITE/DELETE 细分：
    - 批次调度的可解释规则只依赖"是否改变外部状态"这一条。
    - 更细的读写冲突分析属复杂 DAG，Day04 明确不做（V1 Backlog）。
    """

    READ_ONLY = "READ_ONLY"  # 不改外部状态 → 批次可并发（Task 4）
    MUTATING = "MUTATING"  # 改外部状态 → 整批串行（Task 4）


class PermissionPolicy(str, Enum):
    """Session 级权限策略——Agent 在这个 Session 里的最大权限边界。

    05_SANDBOX_CODING_TOOLS.md §6 的三层 Permission Policy（参考 DeepSeek Harness）。
    与 ToolSideEffect 正交：side_effect 驱动调度（并发/串行），
    PermissionPolicy 驱动授权（允许/审批/拒绝）。
    """

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class ToolPermission(str, Enum):
    """单个 Tool 的授权级别——这个工具需要什么级别的权限才能执行。

    与 PermissionPolicy 对齐但不完全相同：Tool 声明自己需要什么级别，
    ToolExecutor 检查 Session 的 PermissionPolicy 是否覆盖该级别。
    """

    READ_ONLY = "read-only"  # 读操作：read/grep/glob/git_status/git_diff
    WORKSPACE_WRITE = "workspace-write"  # workspace 内写：write/edit/apply_patch
    DANGER = "danger"  # 高风险：bash（可配网络/系统副作用）


class Tool(ABC):
    """Tool Contract：模型 Schema 与 Runtime Tool 的共同来源。

    子类必须实现：name / description / args_schema / execute。
    可选覆写：timeout_seconds / side_effect（有安全默认值）。
    """

    # —— 必填字段（身份 + Schema + 行为） ——
    @property
    @abstractmethod
    def name(self) -> str:
        """Tool 的稳定标识。Runtime 按它查找；模型按它发起 tool_call。"""

    @property
    @abstractmethod
    def description(self) -> str:
        """给模型看的行为说明。

        【行为控制】不是装饰：它影响模型【何时】选这个 Tool、【怎么】填参数。
        应写清三要素：做什么、什么时候用、参数含义。今天学会写清即可。
        """

    @property
    @abstractmethod
    def args_schema(self) -> type[BaseModel]:
        """Tool 参数的 Pydantic 类（注意是类本身，不是实例）。

        【单一事实源根】：
        - Registry 用 args_schema.model_json_schema() 导出给模型的 JSON Schema；
        - Executor 用 args_schema.model_validate(args) 校验模型回的参数；
        - 同一个类两边复用，避免"模型按 A 参数调用、执行端期待 B 参数"的漂移。
        """

    @abstractmethod
    async def execute(self, args: BaseModel) -> ToolResult:
        """执行 Tool，返回结构化 ToolResult。

        参数是【已校验】的 Pydantic 实例（由 Executor 在 Task 2 先 validate），
        所以本方法内不再判参数合法性——拿到即合法。

        本方法只负责"做这件事"并把结果包成 ToolResult；
        重试、超时、分类、调度都不在这里（Executor 的职责）。
        """

    # —— 可选元数据（有安全默认值，子类按需覆写） ——
    @property
    def timeout_seconds(self) -> float:
        """执行超时上限。默认 10s；Task 3 的 Executor 用它做 timeout 边界。"""
        return 10.0

    @property
    def side_effect(self) -> ToolSideEffect:
        """副作用分类。默认 READ_ONLY（安全默认，避免误并发执行 mutating 工具）。"""
        return ToolSideEffect.READ_ONLY

    @property
    def permission(self) -> ToolPermission:
        """授权级别。默认 WORKSPACE_WRITE（安全偏高，避免新工具默认 DANGER）。

        与 side_effect 正交：side_effect 驱动批次调度（并发/串行），
        permission 驱动授权关卡（ToolExecutor 的 approval gate）。
        """
        return ToolPermission.WORKSPACE_WRITE

    @property
    def reconcile_hint(self) -> ReconcileHint:
        """崩溃恢复时的可验证性提示。默认 unverifiable（安全默认即 NEED_RECONCILE）。

        hint 只是给 ReconcileCallback 的建议数据——Runtime 永不据此自动验证
        或自动重跑（不变量 #14）。副作用可事后核验的工具（read/write/edit/
        glob/grep/git_status/git_diff）覆写；bash 保持默认（各命令副作用彼此
        不同，不允许统一假装可验证）。
        """
        return ReconcileHint(verifiable=False)

    def args_identity(self, args: dict[str, object]) -> str:
        """Return the stable identity persisted for one Operation's arguments."""
        return json.dumps(args, sort_keys=True, ensure_ascii=False)
