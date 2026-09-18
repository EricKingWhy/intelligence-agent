"""Read-only model and capability catalogs.

The router depends on a small protocol-shaped state seam rather than the web
application's concrete ``AppState``.  ``create_app`` supplies the real state;
tests can override the dependency with a small fake.
"""

from __future__ import annotations

from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, Request

from agent_harness.agent.profiles import tool_scope_summary
from agent_harness.capability.base import CapabilityRegistry
from agent_harness.capability.manifest import (
    core_manifest_entry,
    descriptor_manifest_entry,
)
from agent_harness.capability.wiring import CapabilityWiring
from agent_harness.config import Settings
from agent_harness.model.config import (
    PROVIDER_PRESETS,
    ModelConfig,
    _pick_capabilities,
    parse_model_catalog,
)
from agent_harness.model.provider_store import ProviderStore
from agent_harness.session.service import resolve_model_target
from agent_harness.tooling.contract import PERMISSION_MODE_DESCRIPTIONS

REASONING_EFFORT_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "minimal": {
        "display_name": "轻量",
        "description": "最少推理开销，最快但不够深入。",
        "icon": "bolt",
    },
    "standard": {
        "display_name": "标准",
        "description": "平衡的推理深度，适用于常规任务（默认）。",
        "icon": "gauge",
    },
    "deep": {
        "display_name": "深度",
        "description": "较高推理开销，较慢但更深入。",
        "icon": "telescope",
    },
}


AGENT_PROFILE_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "main": {
        "display_name": "通用",
        "description": "通用编排 Agent（默认）。含全部工具（读写、执行、检索、网络、委派）。",
        "icon": "layers",
    },
    "coding": {
        "display_name": "编程",
        "description": "专精代码编辑、调试和构建任务。可读写与执行命令；不含网络检索。",
        "icon": "code",
    },
    "research_review": {
        "display_name": "研究审查",
        "description": "专精研究、检索和审查任务。只读：不含 write / edit / apply_patch / bash。",
        "icon": "search",
    },
}


CATALOG_ICON_NAMES: frozenset[str] = frozenset({
    "lock", "pencil", "unlock",
    "layers", "code", "search",
    "bolt", "gauge", "telescope",
})


CONTEXT_PROVIDER_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "memory": {
        "display_name": "记忆",
        "description": "注入与用户相关的召回记忆。",
    },
    "skills": {
        "display_name": "技能",
        "description": "注入可用技能目录（名称 + 描述）。",
    },
}


class CatalogState(Protocol):
    """Minimum state needed by this read-only router."""

    settings: Settings
    provider_store: ProviderStore

    async def get_wiring(self) -> tuple[CapabilityRegistry, CapabilityWiring]: ...


def get_catalog_state(request: Request) -> CatalogState:
    """Resolve the narrow catalog seam from the host application's state."""
    return request.app.state.agent


router = APIRouter()
CatalogStateDependency = Annotated[CatalogState, Depends(get_catalog_state)]


def _render_model_option(
    *, id: str, provider: str, model_name: str, is_default: bool,
    capabilities: dict[str, Any], metadata_source: str,
    is_available: bool = True, unavailable_reason: str | None = None,
) -> dict[str, Any]:
    """Render a ModelOption without exposing provider credentials."""
    option: dict[str, Any] = {
        "id": id,
        "provider": provider,
        "is_default": is_default,
        "is_available": is_available,
        "metadata_source": metadata_source,
        "name": id,
        "model": model_name,
        "default": is_default,
    }
    if unavailable_reason is not None:
        option["unavailable_reason"] = unavailable_reason
    option["display_name"] = capabilities.get("display_name", model_name)
    for cap_key in (
        "context_window", "speed_tier", "supports_tools",
        "supports_vision", "supports_reasoning_summary",
    ):
        if cap_key in capabilities:
            option[cap_key] = capabilities[cap_key]
    return option


@router.get("/api/models")
async def list_models(
    state: CatalogStateDependency,
) -> dict[str, Any]:
    """列出可选模型（ADR-0016 §5，C6 + SDD 03 §16 ModelOption）。

        绝不携带任何密钥字段；is_default=true 的条目 = 不传 model 参数时的链。
        思考能力不进元数据（D-B③ 事件驱动：模型真吐思考才有 reasoning 事件）。

        Phase 2 加法（SDD 03 §16）：每条返回能力位元数据，来源标注 metadata_source。
        - 默认链：能力位来自 PROVIDER_PRESETS → metadata_source="provider_preset"；
        - catalog 条目：条目显式声明的能力位优先，回落 preset；条目自身声明时
          metadata_source="agent_models"，否则（全靠 preset 回落）="provider_preset"。
        未知能力位省略（契约：「not guessed」）。旧字段 name/model/default 作为
        alias 保留（前端切换期间不破）。

        #203 / ADR-0032 D5：is_available 是**真实判定**（有凭据 ⇒ true）；
        自定义供应商的条目按 provider_store 的凭据状态过滤 unavailable_reason。
        默认链/内置 preset 条目仍走 .env（本票**不迁移**既有 key，行为不变）。
        """
    default_config = ModelConfig.from_settings(state.settings)
    default_provider = state.settings.model_provider
    default_caps = _pick_capabilities(PROVIDER_PRESETS.get(default_provider, {}))
    models: list[dict[str, Any]] = [_render_model_option(
        id=default_config.model_name,
        provider=default_provider,
        model_name=default_config.model_name,
        is_default=True,
        capabilities=default_caps,
        metadata_source="provider_preset",
    )]
    for entry in parse_model_catalog(state.settings):
        shadowed = resolve_model_target(state.settings, entry.provider, entry.name)
        if shadowed is not None and shadowed.model_id is None:
            continue
        declared = entry.declared_capabilities()
        preset_caps = _pick_capabilities(PROVIDER_PRESETS.get(entry.provider, {}))
        models.append(_render_model_option(
            id=entry.name,
            provider=entry.provider,
            model_name=entry.model_name,
            is_default=False,
            capabilities={**preset_caps, **declared},
            metadata_source="agent_models" if declared else "provider_preset",
        ))
    for provider_entry in state.provider_store.list_entries():
        for model in provider_entry["models"]:
            models.append(_render_model_option(
                id=f"{provider_entry['id']}:{model['model_id']}",
                provider=provider_entry["id"],
                model_name=model["model_id"],
                is_default=False,
                capabilities=(
                    {"display_name": model["label"]} if model.get("label") else {}
                ),
                metadata_source="custom_provider",
                is_available=provider_entry["is_available"],
                unavailable_reason=provider_entry["unavailable_reason"],
            ))
    return {"models": models}


@router.get("/api/permission-modes")
async def list_permission_modes(
    _state: CatalogStateDependency,
) -> dict[str, Any]:
    """列出后端能真实执行的权限模式（SDD 03 §10，Phase 2 加法）。

        返回 PermissionPolicy 全集 + 人类可读描述。诚实标注：当前 Web 层
        auto_approve 默认开（同步 callback），交互式审批是 Phase 5 的工作——
        这里只暴露「后端认识哪些 mode」，不假装审批已就绪。

        #214 加法：每条带 ``icon``（**语义名**，取值集 CATALOG_ICON_NAMES）——
        前端把它映射成行首字形；未知名/缺键 ⇒ 留空槽，两端都不编字形。
        """
    return {
        "modes": [
            {
                "id": policy.value,
                "display_name": desc["display_name"],
                "description": desc["description"],
                "icon": desc.get("icon"),
            }
            for policy, desc in PERMISSION_MODE_DESCRIPTIONS.items()
        ]
    }


@router.get("/api/capabilities")
async def list_capabilities(
    state: CatalogStateDependency,
) -> dict[str, Any]:
    """列出 capability manifest：**内置工具集（core）+ 已装配的插件 capability**（SDD 03 §17）。

        **core 条目恒在且排在最前**（`capability/manifest.py`）：`changes`（「文件/改动」）与
        `terminal`（「输出」）两个面由内置工具（`write`/`edit`/`apply_patch`/`bash`）产出，
        而它们**不由任何插件 capability 产出**——只投影插件 descriptor 时，未声明 `surfaces`
        的保守默认会让这两个面在**所有**真实部署里被前端 `centerTabs` 滤掉（#193）。

        插件条目：无 `surfaces` 声明 → 保守默认（`chat`/`timeline` = true，其余 false）；
        显式声明 → 以声明为准（局部声明**不补齐**，契约里 `actions` 是可选局部字典）。
        条目级**不做并集**——取并集是前端的事（`capabilities.ts::deriveSurfaces`），
        两处都算一遍就等于有两个口径。

        条目形状在**后端侧**只有 `capability/manifest.py` 一份（此前内联在本函数里，
        core 一加入就会变成两份）。说清楚边界，免得把"一份"当成跨仓保证：
        前端的键集与缺省（`web/src/lib/capabilities.ts::SURFACE_KEYS` / `DEFAULT_SURFACES`、
        e2e 的 `web/e2e/fixtures.ts::CORE_CAPABILITY`）是**手工镜像**——跨语言、跨仓，
        改这里不会自动同步过去。两端各有测试锁着同一份值
        （后端 `tests/web/test_web_phase2_endpoints.py::TestCapabilities`，
        前端 `web/src/lib/capabilities.test.ts` + `workspace-modes.spec.ts`），
        改声明时两边一起改。
        """
    registry, _wiring = await state.get_wiring()
    return {
        "capabilities": [
            core_manifest_entry(),
            *[descriptor_manifest_entry(d) for d in registry.available()],
        ],
    }


@router.get("/api/reasoning-efforts")
async def list_reasoning_efforts(
    _state: CatalogStateDependency,
) -> dict[str, Any]:
    """列出 reasoning_effort 可选档位（Ticket B1，SDD 03 §16 对齐 Phase 5）。

        reasoning_effort 已被运行时真实消费：harness 语义档位经
        create_chat_model 翻译为 provider 线格式枚举后注入（翻译表在
        model/provider.py）；本端点暴露「后端认识哪些档位」。
        字段与 /api/permission-modes 同模式（{id, display_name, description, icon}），
        单一事实源是模块级 REASONING_EFFORT_DESCRIPTIONS（validator 与清单
        引用同一份 → 永不漂移）。
        """
    return {
        "efforts": [
            {
                "id": effort_id,
                "display_name": desc["display_name"],
                "description": desc["description"],
                "icon": desc.get("icon"),
            }
            for effort_id, desc in REASONING_EFFORT_DESCRIPTIONS.items()
        ]
    }


@router.get("/api/agent-profiles")
async def list_agent_profiles(
    _state: CatalogStateDependency,
) -> dict[str, Any]:
    """列出 agent_profile 可选档位（Ticket B1，SDD 03 §16 对齐 Phase 5）。

        同 reasoning-efforts：Phase 5 staged 契约的清单投影，运行时 no-op 不变。
        字段与 /api/permission-modes 同模式（含 #214 的 `icon`），单一事实源是
        AGENT_PROFILE_DESCRIPTIONS。

        #201 加法：每条带 ``tool_scope``（档位收窄披露的数据面）——
        ``{open, total, excluded}``，值来自 `agent/profiles.py` 的
        ``tool_scope_summary``（口径写在那里的 docstring：**声明面**，不是运行时
        注册集）。前端据此在档位 picker 底部说一句「该档位只开放 N 个工具（共 M 个）」，
        被收窄掉的名字放 hover 提示——用户只看得到"选完档位后工具变少了"，看不到
        变少了什么，这条是那个缺口的唯一补法（#198 现象的缓解）。
        """
    return {
        "profiles": [
            {
                "id": profile_id,
                "display_name": desc["display_name"],
                "description": desc["description"],
                "icon": desc.get("icon"),
                "tool_scope": tool_scope_summary(profile_id),
            }
            for profile_id, desc in AGENT_PROFILE_DESCRIPTIONS.items()
        ]
    }


@router.get("/api/context-providers")
async def list_context_providers(
    state: CatalogStateDependency,
) -> dict[str, Any]:
    """列出已装配的 context provider 清单（ADR-0020b 运行时消费）。

        动态投影 ``wiring.context_providers`` 的 ``name`` 属性（与
        MemoryContextProvider.name / SkillCatalogContextProvider.name 对齐）。
        display_name / description 从 CONTEXT_PROVIDER_DESCRIPTIONS 取——
        清单端点与 POST /api/sessions 共用同一 id 集合。

        未装配任何 capability（bare 配置）→ wiring.context_providers 为空 →
        返 ``{"providers": []}``（未装配就不编条目，不伪造基础项）。前端据空列表自行 fallback。
        注意与 ``/api/capabilities`` 的区别：那边**恒有一条 core 条目**（内置工具集的声明，
        见 `capability/manifest.py` / #193），因为内置工具真的在每个会话里；这里没有对应的
        "内置 Context Provider"——没装配就是空。
        """
    _, wiring = await state.get_wiring()
    providers: list[dict[str, Any]] = []
    for provider in wiring.context_providers:
        name = getattr(provider, "name", None)
        if not isinstance(name, str) or not name:
            continue
        desc = CONTEXT_PROVIDER_DESCRIPTIONS.get(name)
        providers.append({
            "id": name,
            "display_name": desc["display_name"] if desc else name,
            "description": desc["description"] if desc else "",
        })
    return {"providers": providers}


def register_catalog_routes(app: FastAPI) -> None:
    """Mount the read-only catalog router on an existing application."""
    app.include_router(router)


__all__ = [
    "AGENT_PROFILE_DESCRIPTIONS",
    "CATALOG_ICON_NAMES",
    "CONTEXT_PROVIDER_DESCRIPTIONS",
    "REASONING_EFFORT_DESCRIPTIONS",
    "CatalogState",
    "get_catalog_state",
    "register_catalog_routes",
    "router",
]
