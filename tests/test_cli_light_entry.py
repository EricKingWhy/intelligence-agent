"""CLI/Web 轻入口不加载 provider SDK 的导入图闸门（#1527）。

## 它守什么

`cli --help` 与无模型子命令（`approvals policy list` 等）是 CLI 的**轻入口**；
`import agent_harness.web.app` 是 Web 侧同位入口。它们不需要任何 provider SDK，
本不该为打印几行文本支付 openai / langchain_openai / langsmith 的导入成本
（本机 warm 实测 openai ≈1.7s + langchain_openai ≈0.7s，占轻入口导入墙钟一半
以上）。#1527 修掉了三条把 SDK 拖进轻入口的链：`assembly` 顶层
`import model.provider`；`model.failure → endpoint_guard` 模块级
`from openai import …`（守卫客户端基类移入按需工厂）；web 层为
`ModelClientConstructionError` 的 except 子句导入重模块（owner 迁至
`model.config`，provider 侧 re-export）。本闸门把「轻入口导入图里没有
provider SDK」钉成回归红线。

## 判定口径

- 子进程 import（同 `tests/test_package_boundary.py` 形态），避免被测试进程已
  加载的模块掩蔽。PYTHONPATH 必须用**绝对** `_SRC`：相对 `src` 在子进程换了
  cwd 后会解析失败、回落到 venv editable install 指向的另一棵工作树，判的就
  不是本仓代码（实测踩过）。
- 判 `sys.modules` 而非源码文本：对 import 语句写法与位置不敏感。
  `langchain_core` **允许**出现——`AnyMessage` 等消息类型是 session 层的数据
  模型（derive / fork / 事件重建都要用），不属于 provider 构造栈；本票只钉
  SDK 三件套。
- 反向对照（mutation 反控）：import `agent_harness.model.provider` 必须能观察
  到 openai 被加载——证明断言机制在 SDK 真被拖入时确实会红，不是恒真断言。
- 代表性无模型子命令在**执行后**再判：`approvals policy list` 纯读项目根下的
  approve-policy.json（cwd 钉到 tmp_path，零副作用），证明轻入口不止 import
  干净，跑起来也不在半路拉起 provider。
- `ModelClientConstructionError` 迁 owner 后两条导入路径必须是**同一对象**，
  web except 子句的类型匹配不能出现两个类。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_ROOT / "src")
_PROVIDER_SDK = ("openai", "langchain_openai", "langsmith")


def _run(code: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    # 子进程输出钉 UTF-8 + 容错解码：Windows 下子进程 stderr 默认走 ANSI 代码页，
    # 含中文的报错会在父进程解码线程里炸成 UnicodeDecodeError（stderr 变 None）。
    # 环境继承全量再覆写：provider 链（platformdirs 等）依赖 HOME/USERPROFILE，
    # 最小 env 会让反控用例假红在"Could not determine home directory"。
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd or _ROOT),
        env=env,
        timeout=120,
        check=False,
    )


_LEAK_PROBE = """
import sys
import {entry}
leaked = sorted({{m for m in sys.modules if m.split(".")[0] in {sdk}}})
print("|".join(leaked))
"""


@pytest.mark.parametrize(
    ("entry", "label"),
    [
        pytest.param("agent_harness.cli", "CLI", id="cli"),
        pytest.param("agent_harness.web.app", "Web", id="web"),
    ],
)
def test_light_entry_import_does_not_load_provider_sdk(entry: str, label: str) -> None:
    result = _run(_LEAK_PROBE.format(entry=entry, sdk=repr(set(_PROVIDER_SDK))))
    assert result.returncode == 0, result.stderr[-2000:]
    leaked = result.stdout.strip()
    assert leaked == "", f"{label} 轻入口导入图里出现 provider SDK：{leaked}"


_SUBCOMMAND_PROBE = """
import sys
sys.argv = ["agent-harness", "approvals", "policy", "list"]
from agent_harness import cli
cli._main_dispatch()
leaked = sorted(m for m in sys.modules if m.split(".")[0] in {sdk})
print("LEAKED|" + "|".join(leaked))
"""


def test_no_model_subcommand_runs_without_provider_sdk(tmp_path: Path) -> None:
    result = _run(_SUBCOMMAND_PROBE.format(sdk=repr(set(_PROVIDER_SDK))), cwd=tmp_path)
    assert result.returncode == 0, result.stderr[-2000:]
    marker = [line for line in result.stdout.splitlines() if line.startswith("LEAKED|")]
    assert marker, f"探针未产出泄漏判定行：stdout={result.stdout!r}"
    leaked = marker[-1][len("LEAKED|"):]
    assert leaked == "", f"无模型子命令执行期加载了 provider SDK：{leaked}"


def test_cli_help_entry_runs_and_exits_zero() -> None:
    """AC① 的字面用例：真实 `--help` 入口在新进程里跑通（exit 0 + 用法输出）。"""
    result = subprocess.run(
        [sys.executable, "-m", "agent_harness.cli", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_ROOT,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": _SRC},
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "usage" in result.stdout.lower(), (
        f"--help 未输出用法（前 200 字符）：{result.stdout[:200]!r}"
    )


def test_provider_import_still_loads_sdk_as_counter_evidence() -> None:
    result = _run(
        "import sys; import agent_harness.model.provider; print('openai' in sys.modules)"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "True", (
        "provider 模块必须仍加载 openai——否则本文件的轻入口断言失去反控效力"
    )


def test_model_client_construction_error_single_identity() -> None:
    result = _run(
        "from agent_harness.model.provider import ModelClientConstructionError"
        " as via_provider\n"
        "from agent_harness.model.config import ModelClientConstructionError"
        " as via_config\n"
        "print(via_provider is via_config)"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "True", (
        "owner 迁移后 provider 侧 re-export 与 model.config 必须是同一个类"
    )
