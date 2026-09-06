"""AgentProfile/AgentSpec：Multi-Agent 的核心域对象（ADR-0015 决策 2/9）。

Profile 是预定义的 agent 角色（核心域对象，不进插件）；Spec 是运行时
实例化描述——动态创建 = 实例化 AgentSpec，绝不生成代码（spec §2）。

字段设计参照 pi/oh-my-pi 的 AgentDefinition（PORT DESIGN：name/description/
systemPrompt/tools 必备形状 + per-agent 工具收窄与深度上限），裁剪到 V1：
- tool_scope 显式声明、无隐式默认——缺席的 optional capability 工具由
  AgentFactory 降级丢弃，存在的越权申请由 Factory 拒绝（防权限提升）；
- model_policy V1 只有 "inherit"（继承主模型链，fallback/看门狗自动生效）；
- max_depth V1 全线 1（child 无 delegate）；max_delegations 只对 supervisor
  角色有意义，默认挂在 main 上。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentSpec:
    """一个可实例化 agent 的完整描述（角色、边界、预算）。"""

    name: str
    description: str
    system_prompt: str
    tool_scope: frozenset[str] = field(default_factory=frozenset)
    max_steps: int = 20
    max_depth: int = 1
    model_policy: str = "inherit"
    max_delegations: int = 0

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("AgentSpec.name 不能为空")
        if self.max_steps < 1:
            raise ValueError(f"AgentSpec.max_steps 必须 ≥ 1：{self.max_steps}")
        if self.max_depth < 1:
            raise ValueError(f"AgentSpec.max_depth 必须 ≥ 1：{self.max_depth}")


#: coding 角色的工具集（spec §8：read/write/edit/apply_patch/bash/grep/glob，
#: 加 git_status/git_diff；默认不开放 web）。
_CODING_TOOLS = frozenset({
    "read", "write", "edit", "apply_patch", "bash", "grep", "glob",
    "git_status", "git_diff",
})

#: research/review 角色的只读工具集（spec §8：Knowledge/Web/MCP 只读，无
#: write/bash；本地 read/grep/glob 是 review 看共享 workspace 真实文件的最小需要）。
#: MCP 工具名运行时动态发现，V1 静态集合不含——capability 接线时按需扩展。
_RESEARCH_TOOLS = frozenset({
    "read", "grep", "glob",
    "retrieve_knowledge", "read_knowledge_source", "web_search",
})

#: supervisor（main）：亲自查证用全量 + delegate。max_steps 对齐单代理现状。
_MAIN_TOOLS = _CODING_TOOLS | _RESEARCH_TOOLS | {"delegate", "inspect_artifact"}

#: 三内置 profile（出厂设定，非用户自定义面——文件发现机制 DEFER）。
BUILTIN_PROFILES: dict[str, AgentSpec] = {
    "main": AgentSpec(
        name="main",
        description="Supervisor：理解目标、拆解委派、必要时亲自查证与综合。",
        system_prompt=(
            "你是主协调 agent。简单任务直接完成；需要并行/专项深入时用 "
            "delegate 工具把 scoped task 派给合适的子代理（coding=写代码，"
            "research_review=调研与审查），并综合它们的结构化结果。委派时给"
            "出完整自洽的任务描述——子代理看不到你们的对话历史。"
        ),
        tool_scope=_MAIN_TOOLS,
        max_steps=20,
        max_depth=1,
        max_delegations=8,
    ),
    "coding": AgentSpec(
        name="coding",
        description="编码子代理：在共享 workspace 内读写代码并运行验证。",
        system_prompt=(
            "你是编码 agent，在给定 workspace 内完成 scoped task：读写文件、"
            "运行命令、验证结果。结束时给出简明总结：做了什么、改了哪些文件、"
            "验证结果，以及任何未解决事项。"
        ),
        tool_scope=_CODING_TOOLS,
        max_steps=10,
    ),
    "research_review": AgentSpec(
        name="research_review",
        description="调研/审查子代理：只读检索本地文件、知识库与网络证据。",
        system_prompt=(
            "你是调研审查 agent，只读地收集证据（本地文件、知识语料、网络）"
            "并给出带引用的结论。结束时给出简明总结：结论、引用（citation）、"
            "以及任何未解决事项。你没有写权限。"
        ),
        tool_scope=_RESEARCH_TOOLS,
        max_steps=10,
    ),
}
