"""#298 / MEM-V2-2：记忆作业的模型角色解析（R9 / PRD §5.3 第 3 条）。

两个别名由 PRD **固定**：`memory.primary` = 本地配置的 `senseaudio`，`memory.fallback` = `qwen`。
"固定"指**别名到 provider 的映射**固定；`senseaudio` / `qwen` 是 `PROVIDER_PRESETS` 里既有的
键，不是本模块发明的字符串。这条有判别性：拼错一个字母会让角色**永远**解析不出，而运行时
这只表现为"记忆作业静默降级"——最难发现的那种坏。用例
`test_each_alias_names_a_provider_the_repository_knows` 钉着它。

# 从哪读"本地配置"（本模块唯一的实质决策）

沿用既有的模型配置面，**不新增一套 env 键**（ticket：must use the existing model-provider
abstraction）。每个角色按同一顺序找：

1. **显式目录**：`AGENT_MODELS` 里第一条 `provider` 匹配的条目。这是本仓既有的"本地配置了
   某个 provider 的模型名 + 凭据"的表达方式（`ModelCatalogEntry`，凭据持有形态是 `SecretStr`），
   也是唯一能把 `senseaudio` 配到一个**具体模型名**的地方——它的 preset 没有默认模型名，
   走 provider 预设会因缺 `model_name` 而响亮失败。
2. **默认链恰好就是该 provider**：`.env` 只配了一条链时，`MODEL_PROVIDER` 是 `senseaudio`
   就是 `memory.primary`，`FALLBACK_MODEL_PROVIDER` 是 `qwen` 就是 `memory.fallback`。
   这一支让"主上游本来就是 senseaudio"的部署**零新增配置**即可启用记忆作业。

两条都没有 ⇒ 该角色返回 `None`（**未配置**）。这与"配错了"刻意分开：目录不是合法 JSON、
provider 名非法、provider 命中但缺 `MODEL_API_KEY`，都按既有约定响亮抛 `ConfigError`
（`ModelConfig.from_catalog` / `from_settings` 的既有行为），**不在这里被吞成 `None`**——
静默把"配错了"降级成"没配"，会让一个错误配置表现为"记忆永远不形成"，而没有任何地方报错。

# 两个刻意的细节

- **同 provider 多条时取声明顺序第一条**。目录是给交互式选择器用的，"同一个 provider 两个
  模型"是真实存在的形态（候选列表），所以这里不能因为歧义就抛错；取第一条是确定的规则。
- **默认链的 `.fallback` 不被借用**。`from_settings` 返回的 `ModelConfig` 自带 `.fallback`
  （ADR-0014 的两级链）。作为记忆角色返回前把它置 `None`——记忆作业的备用只由
  `MemoryModelRoles.fallback` 决定，否则"`memory.fallback` 是 qwen"这句话会在主链
  配了别的 fallback 时不成立。

解析备用角色时顺带要求主链可解析（`from_settings` 的既有约束）。这不是新增约束：装配层
本来就要构造主链（`create_chat_model(ModelConfig.from_settings(settings))`），主链坏了
应用起不来。

# 凭据

解析结果的持有形态是 `ModelConfig`（`api_key` 为 `SecretStr`）：`repr` / `str` / `vars`
里一律是 `**********`，明文只能从 `get_secret_value()` 取（唯一出口）。本模块不打印
任何 provider、端点或凭据值。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_harness.config import Settings
from agent_harness.model.config import ModelConfig, parse_model_catalog

#: PRD §5.3 第 3 条固定的两个别名（对外名字，会出现在配置与文档里）。
MEMORY_PRIMARY_ALIAS = "memory.primary"
MEMORY_FALLBACK_ALIAS = "memory.fallback"
#: 别名指向的 provider（`PROVIDER_PRESETS` 的键）。
MEMORY_PRIMARY_PROVIDER = "senseaudio"
MEMORY_FALLBACK_PROVIDER = "qwen"


@dataclass(frozen=True, slots=True)
class MemoryModelRoles:
    """一次解析的产物：两个角色各自的模型配置，未配置的角色是 `None`。"""

    primary: ModelConfig | None
    fallback: ModelConfig | None

    @property
    def has_fallback(self) -> bool:
        """是否配了备用角色——`budget.next_attempt` 的 `has_fallback` 输入就是它。"""
        return self.fallback is not None


def resolve_memory_roles(settings: Settings) -> MemoryModelRoles:
    """按 PRD §5.3 的别名解析记忆作业的两个模型（顺序见模块 docstring）。"""
    return MemoryModelRoles(
        primary=_resolve_role(settings, MEMORY_PRIMARY_PROVIDER, main_chain=True),
        fallback=_resolve_role(settings, MEMORY_FALLBACK_PROVIDER, main_chain=False),
    )


def _resolve_role(settings: Settings, provider: str, *, main_chain: bool) -> ModelConfig | None:
    """找一个角色。`main_chain` = 该角色也接受 `.env` 上的对应那一级链。"""
    for entry in parse_model_catalog(settings):
        if entry.provider == provider:
            return ModelConfig.from_catalog(settings, entry.name)

    configured_on_chain = (
        settings.model_provider if main_chain else settings.fallback_model_provider
    )
    if configured_on_chain != provider:
        return None

    chain = ModelConfig.from_settings(settings)
    resolved = chain if main_chain else chain.fallback
    if resolved is not None:
        # 记忆作业的备用由 MemoryModelRoles.fallback 决定，不借默认链的（见模块 docstring）。
        resolved.fallback = None
    return resolved
