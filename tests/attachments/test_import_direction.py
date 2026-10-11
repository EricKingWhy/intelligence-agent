"""`config` ↔ `attachments` 的**依赖方向**闸门（#939 M-15）。

**问题**：`config.Settings` 的字段校验（启动期 fail-loud）调
`attachments.types.parse_allowed_media_types`，而 `attachments.types.resolve_image_limits`
又在运行期读 `Settings` 的字段——两个模块概念上互相依赖。审计（MM A-F14）核到的实际形态是：
反向边由 `@field_validator` **函数体内**的延迟 import 提供，故 **import 期无环**；但这层保护
只写在注释里，**没有任何机械守卫**——把 import 提到模块顶部、或把解析函数搬个位置，全门禁照绿，
而环会在某个 import 顺序下突然闭合成 `ImportError`（症状离改动很远）。

**为什么把解析搬到中性模块而不是延迟 import**：`config.py` 已有大量想留在 `Settings` 旁边的
非附件字段校验；把**每一种附属领域**的规则都搬进 `config` 才是真正的耦合增长。中性模块
`attachments.validation` 让方向单向 `config → validation → types`，`config` 的 validator 里
那句延迟 import 随之**删除**（不再是"靠延迟导入躲环"）。

判定口径：本用例在**子进程**里 import（避免被测试进程已加载的模块掩蔽），断言
① `attachments` 侧不在 import 期拉进 `config`；② 子模块**不许反向依赖** `config`；
③ `config` 侧的解析函数住中性模块（真身不在 `attachments.types`）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parents[2] / "src")

#: 领域叶子模块：它们构成 `attachments` 的**被依赖面**，不许反向 import `config`。
_DOMAIN_LEAVES = (
    "agent_harness.attachments.filenames",
    "agent_harness.attachments.errors",
    "agent_harness.attachments.probe",
    "agent_harness.attachments.normalize",
    "agent_harness.attachments.validation",
    "agent_harness.attachments.types",
)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_SRC,
        env={"PYTHONPATH": _SRC, "PATH": "/usr/bin:/bin"},
        timeout=60,
        check=False,
    )


def test_importing_the_domain_does_not_pull_in_config() -> None:
    """`import attachments.types` 不得连带加载 `config`（反向边必须保持"用到才加载"）。"""
    proc = _run(
        "import sys, agent_harness.attachments.types as t;"
        "print('agent_harness.config' in sys.modules)"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", (
        "import 领域类型时就拉进了 `agent_harness.config`——反向边从「函数体内延迟 import」"
        "退化成「import 期依赖」，一旦 config 侧也 import 领域包，环即刻闭合"
    )


@pytest.mark.parametrize("module", _DOMAIN_LEAVES)
def test_domain_leaf_modules_never_import_config_at_runtime(module: str) -> None:
    """领域叶子模块在 import 期**不得**依赖 `config`（单向：config → 领域）。"""
    proc = _run(
        f"import sys, {module};"
        "print('agent_harness.config' in sys.modules)"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", (
        f"{module} 在 import 期拉进了 `agent_harness.config`——领域包反向依赖配置包，"
        "`config → attachments → config` 成环（M-15）"
    )


def test_config_side_media_type_validation_lives_in_the_neutral_module() -> None:
    """`config` 侧调用的解析函数住中性模块，真身不在 `attachments.types`。"""
    import agent_harness.attachments.types as types_module
    import agent_harness.attachments.validation as validation_module

    assert validation_module.parse_allowed_media_types is types_module.parse_allowed_media_types
    # `Settings` 的 validator 调的就是它——两个名字必须是同一个对象。
    from agent_harness.config import Settings

    assert Settings.model_fields["attachment_allowed_media_types"].metadata is not None
    assert callable(validation_module.parse_allowed_media_types)
