"""#244 AC5：Bash 执行预算的**配置入口**与非法值的启动期响亮失败（B-37）。

契约正本：`docs/adr/0039-tool-executor-owns-absolute-deadline.md`（D1 唯一 owner）。
本文件钉三件事：

1. **单一冻结值**：配置默认（`Settings.bash_timeout_seconds`）、工具模块默认
   （`DEFAULT_BASH_TIMEOUT_SECONDS`）与 #244 冻结值 **60 秒**三者同值——任何一处
   字面量单独漂移就红（#256 AC1「不存在 10/60 双真相」在配置面的机械钉子）。
2. **非法值在构造期响亮失败**：0 / 负数 / nan / ±inf / 非数字一律 `ValidationError`；
   `Settings()` 构造就是启动期，所以"没有预算"或"无穷预算"进不了运行时。
3. **运行面第二道闸**：绕过 Settings 直接构造工具（测试替身、仓外调用方）时，
   `BashTool` 自己拒绝非法预算——Executor 是唯一 deadline owner，但只应拿到合法数值。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_harness.config import Settings
from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.tools import BashTool
from agent_harness.tools.bash import DEFAULT_BASH_TIMEOUT_SECONDS

#: #244 冻结的 Bash 默认有效预算（秒）。写死在这里是**故意的**：本文件的存在意义
#: 就是让"冻结值被悄悄改掉"这件事变红，而不是跟着改动一起漂。
FROZEN_BASH_TIMEOUT_SECONDS = 60.0

INVALID_BUDGETS = [0, -1, -0.5, float("nan"), float("inf"), float("-inf")]


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(workspace_root=tmp_path)


def _settings(**overrides) -> Settings:
    """`_env_file=None` 关掉仓内 .env；其余优先级不变（init 参数 > 环境变量）。"""
    return Settings(
        _env_file=None, workspace_dir=".", model_api_key="sk-test", **overrides
    )


def test_default_budget_has_a_single_source(sandbox: LocalSubprocessSandbox):
    """配置默认 == 工具模块默认 == 冻结值；默认构造的工具读到的就是这个值。"""
    assert DEFAULT_BASH_TIMEOUT_SECONDS == FROZEN_BASH_TIMEOUT_SECONDS
    assert _settings().bash_timeout_seconds == FROZEN_BASH_TIMEOUT_SECONDS
    assert BashTool(sandbox).timeout_seconds == FROZEN_BASH_TIMEOUT_SECONDS


def test_configured_budget_becomes_the_effective_budget(sandbox: LocalSubprocessSandbox):
    """注入的预算就是 `Tool.timeout_seconds`（Executor 用的那一个数）。"""
    assert _settings(bash_timeout_seconds=12.5).bash_timeout_seconds == 12.5
    assert BashTool(sandbox, timeout_seconds=12.5).timeout_seconds == 12.5


def test_budget_is_configurable_through_the_environment(monkeypatch):
    """部署面入口：环境变量（`.env`）能改预算——这是配置入口存在的意义。"""
    monkeypatch.setenv("BASH_TIMEOUT_SECONDS", "5")
    assert _settings().bash_timeout_seconds == 5.0
    monkeypatch.setenv("BASH_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValidationError):
        _settings()


@pytest.mark.parametrize("value", INVALID_BUDGETS + ["abc"])
def test_invalid_configured_budget_fails_loudly_at_construction(value):
    """非法配置 = 构造期抛错（= 启动即崩），**不**静默回落到默认值。

    `gt=0` 挡 0 / 负数，`allow_inf_nan=False` 挡 nan / inf——两个约束各挡一半，
    缺任何一个都会让"无预算/无穷预算"悄悄生效。
    """
    with pytest.raises(ValidationError):
        _settings(bash_timeout_seconds=value)


@pytest.mark.parametrize("value", INVALID_BUDGETS)
def test_bash_tool_refuses_an_invalid_budget(sandbox: LocalSubprocessSandbox, value):
    """绕过 Settings 的构造路径也不许把非法预算交给 Executor。"""
    with pytest.raises(ValueError):
        BashTool(sandbox, timeout_seconds=value)
