"""#1048 AC8：项目指令读取 / 文件工具路径解析 / 审批身份 / web workspace 文件路由
对 Docker workspace 一律走 Sandbox/POSIX 语义。

现状：`agent_harness.assembly` 与 web workspace 文件路由经
`uses_sandbox_path_semantics` 统一裁决（AC8）——`DockerSandbox` 与
`WindowsPsecSandbox` 均走 Sandbox/POSIX 语义，不再是红阶段的
「只对 WindowsPsecSandbox 走 sandbox 语义」。

本文件用一个最小「Docker-like」替身钉住「按 Sandbox 语义处理」的判据，
不依赖真实 daemon。
"""

from __future__ import annotations

from pathlib import PurePosixPath

from agent_harness.sandbox.local import LocalSubprocessSandbox


class _DockerLikeSandbox:
    """代表 DockerSandbox 的语义面：容器内 POSIX 工作区根。"""

    @property
    def workspace_root(self) -> PurePosixPath:
        return PurePosixPath("/workspace")

    def resolve_within_workspace(self, path: str) -> PurePosixPath:
        return self.workspace_root / path


class TestDockerWorkspaceUsesSandboxSemantics:
    def test_docker_workspace_uses_sandbox_path_semantics(self) -> None:
        """AC8：Docker workspace 走 Sandbox/POSIX 语义分支。"""
        from agent_harness.assembly import uses_sandbox_path_semantics

        assert uses_sandbox_path_semantics(_DockerLikeSandbox()) is True

    def test_local_workspace_does_not_use_sandbox_path_semantics(self, tmp_path) -> None:
        """AC8：local 后端仍是宿主目录语义（回归护栏）。"""
        from agent_harness.assembly import uses_sandbox_path_semantics

        local = LocalSubprocessSandbox(workspace_root=tmp_path)
        assert uses_sandbox_path_semantics(local) is False

    def test_workspace_semantics_helper_maps_docker_like_sandbox(self) -> None:
        """AC8 语义锚点：`workspace_semantics_for_sandbox` 委托 assembly 判据，对容器内
        工作区返回 "sandbox"。（web 文件路由本身经 Sandbox 抽象读写，不按后端分叉；本
        测试只钉住这个被保留的语义锚点的判据。）"""
        from agent_harness.web.workspace_files import workspace_semantics_for_sandbox

        assert workspace_semantics_for_sandbox(_DockerLikeSandbox()) == "sandbox"
