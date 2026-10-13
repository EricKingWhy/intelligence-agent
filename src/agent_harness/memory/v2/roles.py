"""#298 / MEM-V2-2：记忆作业的模型角色解析（R9 / PRD §5.3 第 3 条）。

两个别名由 PRD 固定：`memory.primary`、`memory.fallback`。"固定"指**别名**固定，别名到
provider 的映射则**运营者可配**（#1039 / PRD §5.3 第 3 条修订）：默认仍是
`memory.primary` → `senseaudio`、`memory.fallback` → `qwen`（`MEMORY_PRIMARY_PROVIDER` /
`MEMORY_FALLBACK_PROVIDER`），但部署者可用 `MEMORY_PRIMARY_PROVIDER` /
`MEMORY_FALLBACK_PROVIDER` 两个应用级 Settings 把任一角色改指到：
- 任意**内置 preset**（`PROVIDER_PRESETS` 的键），或
- 任意 #203 注册的**自定义供应商**模型，写法是 `provider:model` 命名空间
  （复用 `ModelConfig.from_custom_provider` 的寻址，不另造一套）。

`senseaudio` / `qwen` 是 `PROVIDER_PRESETS` 里既有的键，不是本模块发明的字符串。这条有
判别性：拼错一个字母会让角色**永远**解析不出，而运行时这只表现为"记忆作业静默降级"——最
难发现的那种坏。默认值现在由 `test_the_role_mapping_defaults_to_the_providers_the_prd_names`
钉住；一个**既非已知 preset、也不带命名空间**的映射值按"配错了"响亮抛 `ConfigError`。

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
from typing import TYPE_CHECKING

from agent_harness.config import Settings
from agent_harness.model.config import (
    PROVIDER_PRESETS,
    ConfigError,
    ModelConfig,
    parse_model_catalog,
)

if TYPE_CHECKING:
    from agent_harness.model.provider_store import ProviderStore

#: PRD §5.3 第 3 条固定的两个别名（对外名字，会出现在配置与文档里）。
MEMORY_PRIMARY_ALIAS = "memory.primary"
MEMORY_FALLBACK_ALIAS = "memory.fallback"
#: 别名**默认**指向的 provider（`PROVIDER_PRESETS` 的键）；运营者可经 Settings 改指。
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

    def targets_are_distinct(self) -> bool:
        """主/备是否解析到**不同**目标（provider + 端点 + 模型名）。

        #1039：面板降级开关打开时，"确定性 provider 错 ⇒ 落一次备用"只在这两者**不是
        同一个目标**时才有意义——落到同一个坏端点等于把一次 400 换成再一次 400。判据含
        `base_url`：#203 的同 id override 可以换端点而不换 provider 名，只比 provider 名
        会漏掉这种"其实已经换了目标"的形态。
        """
        primary, fallback = self.primary, self.fallback
        if primary is None or fallback is None:
            return False
        return (primary.provider, primary.base_url, primary.model_name) != (
            fallback.provider, fallback.base_url, fallback.model_name)


def resolve_configured_memory_roles(settings: Settings) -> MemoryModelRoles:
    """按设置解析记忆角色，并带上 #203 自定义供应商 store（便捷入口）。

    `provider:model` 命名空间的映射解析需要 store；store 构造零 I/O
    （`ProviderStore.for_settings`），只有命名空间形态才真正消费它。生产装配
    （`capability/wiring.py`）与 `assembly.build_memory_formation` 的直调回退共用本入口，
    避免"造 store 再解析"的样板各写一遍。
    """
    from agent_harness.model.provider_store import ProviderStore

    return resolve_memory_roles(settings, store=ProviderStore.for_settings(settings))


def resolve_memory_roles(
    settings: Settings, store: ProviderStore | None = None,
) -> MemoryModelRoles:
    """按 PRD §5.3 的别名解析记忆作业的两个模型（顺序见模块 docstring）。

    `store`：#203 自定义供应商存储。仅当某个角色映射是 `provider:model` 命名空间时才需要；
    该形态下缺失即配置错误，响亮抛 `ConfigError`（不静默成"未配置"）。
    """
    return MemoryModelRoles(
        primary=_resolve_role(
            settings, settings.memory_primary_provider, main_chain=True, store=store),
        fallback=_resolve_role(
            settings, settings.memory_fallback_provider, main_chain=False, store=store),
    )


def _resolve_role(
    settings: Settings, mapping: str, *, main_chain: bool, store: ProviderStore | None,
) -> ModelConfig | None:
    """找一个角色。`mapping` = 内置 preset 名或 `provider:model` 命名空间。

    `main_chain` = 该角色也接受 `.env` 上的对应那一级链（仅 preset 形态适用）。
    """
    if ":" in mapping:
        return _resolve_custom_role(settings, mapping, store=store)

    if mapping not in PROVIDER_PRESETS:
        raise ConfigError(
            f"memory 角色映射 {mapping!r} 既不是内置 provider"
            f"（可选 {sorted(PROVIDER_PRESETS)}），也不是 `provider:model` 命名空间"
        )

    for entry in parse_model_catalog(settings):
        if entry.provider == mapping:
            return ModelConfig.from_catalog(settings, entry.name)

    configured_on_chain = (
        settings.model_provider if main_chain else settings.fallback_model_provider
    )
    if configured_on_chain != mapping:
        return None

    chain = ModelConfig.from_settings(settings)
    resolved = chain if main_chain else chain.fallback
    if resolved is not None:
        # 记忆作业的备用由 MemoryModelRoles.fallback 决定，不借默认链的（见模块 docstring）。
        resolved.fallback = None
    return resolved


def _resolve_custom_role(
    settings: Settings, mapping: str, *, store: ProviderStore | None,
) -> ModelConfig:
    """`provider:model` 命名空间的角色映射（#203 自定义供应商）。"""
    if store is None:
        raise ConfigError(
            f"memory 角色映射 {mapping!r} 需要自定义供应商存储（ProviderStore），当前不可用"
        )
    provider_id, _, model_id = mapping.partition(":")
    resolved = ModelConfig.from_custom_provider(settings, provider_id, model_id, store)
    # 记忆作业的备用由 MemoryModelRoles.fallback 决定，不借默认链的（见模块 docstring）。
    resolved.fallback = None
    return resolved
