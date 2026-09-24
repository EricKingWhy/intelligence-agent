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
from typing import Any

from agent_harness.prompt import DEFAULT_REGISTRY


def _builtin_prompt(name: str) -> str:
    """内置 profile 的 system prompt **唯一来源 = 注册表**（ADR-0023 D1/D12）。

    只在这一处接线：parent 走 `assembly.py` 的 `profile_spec.system_prompt`，
    child 走 `factory.py` 的 `spec.system_prompt`——两条路径因此零改动即一致，
    不存在两个调用点漂移的可能。

    刻意**不**改成"按 `spec.name` 查注册表"：`AgentSpec.system_prompt` 必须继续
    是普通字段，否则自定义 spec（以及 `multiagent/provider.py` 传入的自定义
    profiles 字典）的 prompt 会被内置文案覆盖。
    """
    return DEFAULT_REGISTRY.assemble(f"profile:{name}").system_text


@dataclass(frozen=True)
class AgentSpec:
    """一个可实例化 agent 的完整描述（角色、边界、预算）。"""

    name: str
    description: str
    system_prompt: str
    tool_scope: frozenset[str] = field(default_factory=frozenset)
    #: 档位的 local turn fuse 声明（#308 / ADR-0044 D1）。``None`` = **继承上层**
    #: （Deployment 默认 500，`config.Settings.local_max_agent_turns`）——三个内置档位
    #: 就是 None：出厂设定不写死数字，operator 下调 Deployment ceiling 时它们自然跟随，
    #: 不会出现"档位声明 500 撞上 deployment 100"这种必然失败的组合（低层只能收窄，
    #: 判定见 `agent/budget.py`）。自定义档位要收窄时显式给正整数，且必须 ≤ Deployment。
    max_agent_turns: int | None = None
    max_depth: int = 1
    model_policy: str = "inherit"
    max_delegations: int = 0

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("AgentSpec.name 不能为空")
        if self.max_agent_turns is not None and self.max_agent_turns < 1:
            raise ValueError(
                f"AgentSpec.max_agent_turns 必须 ≥ 1 或 None（继承上层）："
                f"{self.max_agent_turns}"
            )
        if self.max_depth < 1:
            raise ValueError(f"AgentSpec.max_depth 必须 ≥ 1：{self.max_depth}")


#: coding 角色的工具集（spec §8：read/write/edit/apply_patch/bash/grep/glob，
#: 加 git_status/git_diff；默认不开放 web）。
#: #202 / ADR-0031 D8：记忆检索与显式写入工具对 coding 开放（写入源是模型自己）。
_CODING_TOOLS = frozenset({
    "read", "write", "edit", "apply_patch", "bash", "grep", "glob",
    "git_status", "git_diff", "retrieve_memory", "remember_this", "forget_memory",
})

#: research/review 角色的只读工具集（spec §8：Knowledge/Web/MCP 只读，无
#: write/bash；本地 read/grep/glob 是 review 看共享 workspace 真实文件的最小需要）。
#: #202：retrieve_memory 是只读检索，对 research 开放；remember_this（写）不进。
_RESEARCH_TOOLS = frozenset({
    "read", "grep", "glob",
    "retrieve_knowledge", "read_knowledge_source", "web_search",
    "retrieve_memory",
})

#: supervisor（main）：亲自查证用全量 + delegate。
#: #202：`forget_memory`（DANGER + 审批）补进 scope——修它此前不在任何 tool_scope、
#: 被非 main 档位 `registry.filtered()` 静默剔除的既有缺陷。
_MAIN_TOOLS = _CODING_TOOLS | _RESEARCH_TOOLS | frozenset({
    "delegate", "inspect_artifact", "forget_memory",
})

#: **加工具的约束**（#238）：新增的内置工具必须至少归属一个非-main 档位，否则要登记进
#: `tests/agent/test_tool_scope_reconciliation.py` 的带理由白名单——那边的对账机械枚举
#: `src/` 下全部 `Tool` 子类与 `assembly.BUILTIN_LOCAL_TOOLS`，漏登记就是红灯（不靠人记得）。
#: 三个 scope 是**声明面**：本部署实际注册了什么由 capability wiring 决定，两者会双向不一致
#: （`tool_scope_summary` 的 docstring 有实测数字），所以不要把这里的集合读成"能用的工具"。

#: 三内置 profile（出厂设定，非用户自定义面——文件发现机制 DEFER）。
#: `system_prompt` 的正文在 `agent_harness.prompt.builtin`（改文案开那一个文件）。
#: 三档位**都不声明** `max_agent_turns`（= 继承 Deployment 默认 500）：旧的低位数字
#: （main 20 / child 10）是"另一个低位上限"，正是 #305 决策 2 要取代的东西——档位要
#: 收窄 local fuse 时才写数字，且必须 ≤ Deployment ceiling。
BUILTIN_PROFILES: dict[str, AgentSpec] = {
    "main": AgentSpec(
        name="main",
        description="Supervisor：理解目标、拆解委派、必要时亲自查证与综合。",
        system_prompt=_builtin_prompt("main"),
        tool_scope=_MAIN_TOOLS,
        max_depth=1,
        max_delegations=8,
    ),
    "coding": AgentSpec(
        name="coding",
        description="编码子代理：在共享 workspace 内读写代码并运行验证。",
        system_prompt=_builtin_prompt("coding"),
        tool_scope=_CODING_TOOLS,
    ),
    "research_review": AgentSpec(
        name="research_review",
        description="调研/审查子代理：只读检索本地文件、知识库与网络证据。",
        system_prompt=_builtin_prompt("research_review"),
        tool_scope=_RESEARCH_TOOLS,
    ),
}


def declared_tool_universe() -> frozenset[str]:
    """全部内置档位声明工具面的**并集**——`tool_scope_summary` 的 `total`。

    取并集而不是取 main 的 scope：并集对"将来新增一个档位、其 scope 不在 main 里"
    这个改动是自洽的（那时 total 仍然成立），取 main 会静默漏掉它。
    """
    return frozenset().union(*(spec.tool_scope for spec in BUILTIN_PROFILES.values()))


def tool_scope_summary(profile: str) -> dict[str, Any]:
    """某个内置档位的工具面披露（#201：档位收窄提示的数据面）。

    ``open`` = 该档位 ``tool_scope`` 的条目数；``total`` = 所有内置档位声明工具面的
    并集大小；``excluded`` = 并集里**不在**该 scope 的名字（升序）。

    ## 口径：这是**声明面**，不是本部署的工具清单

    两个方向都会差（最小 harness 实测：`CAPABILITIES=""`、无 session_store、
    无 Tavily key，`build_runtime` 后数 registry）：

    - **声明 ⊃ 注册**：`scope` 里有名字，本部署可能没注册它。同一 harness 实测
      注册数 = main/None **10**、coding **9**、research_review **3**（声明分别是
      17/12/7）：knowledge/websearch/memory 等 capability 未启用（或缺运行期前置，
      如 `TAVILY_API_KEY` / session_store）时不注册，`retrieve_knowledge` /
      `web_search` / `retrieve_memory` / `delegate` 一类就不在 registry 里，此时
      "开放 7 个"是**高报**。这个数**不是常量**：它随 wiring 与运行期前置变化
      （缺 key / 缺 session_store 都会按 optional 降级缺席）——所以"本部署 = N 个
      工具"这种说法本身就不稳，不该写进对用户的话里。
    - **注册 ⊄ 声明**：`assembly.py` 在收窄**前**注册的工具若不在任何 scope 里，
      它会被 filter 静默剔除，却**不在** `excluded` 里。同一实测：coding 丢掉的
      唯一工具是 `read_artifact`（本地 artifact 存储注册的），而声明面只声明了
      `inspect_artifact`——前端 tooltip 因此**列不出**这个名字。

    所以这两个数**只能读作"声明的工具面"**，读作"你现在有 N 个工具"就是假的。
    要算真值得在会话上下文里数**收窄前后**的 registry（`assembly.py:277-287` 已经
    算过一次：`pre_filter_names - filtered` = 运行时 `dropped_tools`，随
    `run/started` 落盘）——catalog 端点没有 session，也不会为了数数去
    `build_runtime`（要 sandbox/workspace，MCP 还会建连接）。同一取舍在
    `capability/manifest.py` 的 core 条目里写过一次：**部署级/声明级的实话
    > 按会话猜**。前端文案据此写成"声明开放"（见
    `web/src/lib/agentProfileScope.ts`），不写成"你开放了多少个"。
    """
    spec = BUILTIN_PROFILES[profile]
    universe = declared_tool_universe()
    return {
        "open": len(spec.tool_scope),
        "total": len(universe),
        "excluded": sorted(universe - spec.tool_scope),
    }
