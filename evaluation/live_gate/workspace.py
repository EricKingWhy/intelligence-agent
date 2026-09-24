"""一次性工作区：建、用、核实销毁（`#307` R4 / AC「一次性工作区销毁或隔离后开发仓库没有副作用」）。

## 为什么不是直接 `shutil.rmtree`

Windows 上 `git` 把 `.git/objects/**` 写成**只读**（实测 `-r--r--r--`），`shutil.rmtree`
遇到它会失败 —— `LocalSubprocessSandbox.delete()` 用的正是 `rmtree(ignore_errors=True)`，
于是删不掉却**不报错**（原型实测：`workspace/.git` 整棵树留在临时目录里）。本模块因此：
先走 sandbox 自己的 `delete()`（契约不变），失败后按"清只读位 + 重试"补一次，**最后核实**
目录是否真的消失 —— `deleted` 是核实过的结论，不是"我调用过删除"。

## 工作区身份不落本机绝对路径

`docs/live_gate/**` 是**入库**证据，把宿主的用户目录路径写进去没有收益（还泄漏本机信息）
⇒ 只记 `sha256(绝对路径)[:16]` + 删除结论：一次性身份可核对，路径不外泄。
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_harness.sandbox.base import Sandbox
from agent_harness.sandbox.local import LocalSubprocessSandbox
from evaluation.live_gate.schema import SandboxRecord

#: 本票的证据只用生产默认后端（`local`）。Docker 后端**另有**覆盖（见 scope.does_not_cover）：
#: 一个没有 git 的镜像（`python:3-slim`）会让 git 工具场景直接失败，把"镜像缺工具"混进
#: "实现有问题"的归因面里 —— 本票不做，登记边界。
SUPPORTED_BACKENDS = ("local",)


def _identity(path: Path | str) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


def _force_remove(path: Path) -> None:
    """清只读位后自底向上删除（Windows 上 `.git/objects` 是只读的，见模块 docstring）。

    刻意不用 `shutil.rmtree(onerror=/onexc=)`：那对钩子的名字在 3.12 换过（`onexc` 是 3.12+
    才有、`onerror` 在 3.12 起弃用），而本项目 `requires-python = ">=3.11"`。自己走一遍
    `os.walk(topdown=False)` 反而没有版本分叉。逐项失败**不在这里报** —— 外层 `teardown()`
    会核实目录是否真的消失，那才是判据（本函数只负责尽力删干净）。
    """
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            target = os.path.join(root, name)
            try:
                os.chmod(target, stat.S_IWRITE)
                os.remove(target)
            except OSError:
                pass
        for name in dirs:
            target = os.path.join(root, name)
            try:
                os.chmod(target, stat.S_IWRITE)
                os.rmdir(target)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


@dataclass
class DisposableWorkspace:
    """一次尝试的工作区。`record` 在销毁后才是最终值（`deleted` / `teardown`）。"""

    sandbox: Sandbox
    root: Path
    record: SandboxRecord

    def teardown(self) -> SandboxRecord:
        """销毁工作区并**核实**。返回更新后的记录（`teardown` = 真走过的步骤，`deleted` = 核实结论）。

        ⚠ 一次性根目录（`root`）里**不只有** sandbox 的工作子目录：会话轨迹落在
        `root/sessions`（`ScenarioContext.session_root`），它不在 `sandbox.delete()` 的负责范围内
        ⇒ 只调 sandbox 的删除会**留下轨迹目录**，`root.exists()` 仍为真（实测：机制用例里
        `deleted=False`，被 runner 判成取证卫生失败）。故这里是两步：先走 sandbox 自己的契约，
        再清掉剩下的部分，最后核实整个根目录是否真的消失。
        """
        steps = ["sandbox.delete"]
        raised = ""
        try:
            self.sandbox.delete()
        except Exception as error:  # noqa: BLE001 - 删除失败不是异常面，是**要记录的事实**
            raised = type(error).__name__
        if self.root.exists():
            steps.append("force-remove")
            _force_remove(self.root)
        self.record.teardown = "+".join(steps) + (f"({raised})" if raised else "")
        self.record.deleted = not self.root.exists()
        return self.record


def create_workspace(*, prefix: str = "live-gate-") -> DisposableWorkspace:
    """建一个一次性工作区（临时目录**在仓库之外**，仓库内不留任何路径）。"""
    root = Path(tempfile.mkdtemp(prefix=prefix))
    sandbox = LocalSubprocessSandbox(workspace_root=root / "workspace")
    record = SandboxRecord(
        backend="local",
        disposable=True,
        created=True,
        deleted=False,
        env_allowlisted=True,
        workspace_ids=[_identity(root)],
    )
    _assert_env_allowlisted(sandbox)
    return DisposableWorkspace(sandbox=sandbox, root=root, record=record)


def _assert_env_allowlisted(sandbox: Sandbox) -> None:
    """拒绝"把宿主 env 整份透传给模型可执行命令"的构造（那等于把 `.env` 交给模型）。

    `LocalSubprocessSandbox(passthrough_env=True)` 是显式的本地调试逃生门；Live Gate 的
    证据要是采自那条路径，`echo $MODEL_API_KEY` 就会被模型读走 —— 这不是可选项。
    """
    if getattr(sandbox, "_env", None) is None:  # pragma: no cover - 只在误用时触发
        raise RuntimeError("sandbox 未启用 env 白名单（passthrough_env=True）——Live Gate 拒绝在此构造下取证")
