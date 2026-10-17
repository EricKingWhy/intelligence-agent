"""#1200 插件管理写端点：MCP server / 工具 / skill 的启停、重连、调用前确认。

定位（裁决 C1）：只写 `<workspace_dir>/plugin-settings.json`——**运行时选择态**，
不是配置源（ADR-0010 禁止第二份 YAML 配置）。生效时机是下次装配（裁决 D1）：
本模块不做热重载，UI 明示「重启后生效」。

来源闸：全部 POST 走 `require_trusted_origin`（ADR-0025 D1，与 disconnect 同款）。
只读的 `GET /api/mcp/servers` 仍留在 `app.py`（既有端点原地扩展，A1/B1）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from agent_harness.plugins.settings import (
    PluginSettings,
    PluginSettingsError,
    load_plugin_settings_tolerant,
    set_require_confirm,
    set_server_enabled,
    set_skill_enabled,
    set_tool_enabled,
)
from agent_harness.skills.discovery import is_valid_skill_name, single_line
from agent_harness.web.projects import require_trusted_origin

#: 展示上限与 `GET /api/skills` 共用同一常量（两处各写一份 1536 会静默漂移，
#: 同一 skill 在插件面板与 `/` 菜单下发的描述长度会不一致）。
from agent_harness.web.skills import MAX_SKILL_DESCRIPTION_CHARS

if TYPE_CHECKING:
    from fastapi import FastAPI

#: 票面 §5：不合规的 skill 名 422，不做静默规范化。判据与发现/写入侧同一份
#: （`is_valid_skill_name`），不另起一套正则。

#: `/` 菜单的内置命令（#1195 定稿命令表，D2=A 单一注册表）。
#: 元数据由后端提供，前端只保留按 name 查表的 `run` 回调（#1200 Phase 3）。
BUILTIN_SLASH_COMMANDS: tuple[dict[str, str], ...] = (
    {"name": "goal", "description": "设置或查看长期任务目标"},
    {"name": "plan", "description": "进入或退出计划模式"},
    {"name": "compact", "description": "压缩以上对话内容"},
    {"name": "permission", "description": "切换权限预设（沙箱模式与审批策略）"},
    {"name": "model", "description": "选择本会话使用的模型"},
)


class RequireConfirmBody(BaseModel):
    """`require_confirm` 只接受布尔值；字符串/缺键由 pydantic 给 422。

    strict=True：pydantic 默认 lax 模式会把 "yes"/"no" 也解析成布尔，管理端点
    不接受这种隐式转换（客户端只该发 true/false）。
    """

    model_config = ConfigDict(strict=True)

    require_confirm: bool


def _mcp_capability(wiring):
    from agent_harness.mcp.capability import find_mcp_capability

    return find_mcp_capability(wiring)


def _known_servers(capability) -> set[str]:
    return set(capability.all_server_names()) if capability is not None else set()


def _settings(app) -> PluginSettings:
    """当前选择态（文件即真相，每次请求现读，不缓存第二份）。

    读用容错版：坏字段只丢该字段，同文件里合法的禁用项仍生效。写路径的
    read-modify-write 也走容错读，用户能靠 UI 的开关把坏字段归一化修好。
    """
    settings, _errors = load_plugin_settings_tolerant(app.state.agent.settings.workspace_dir)
    return settings


def register_plugin_routes(app: FastAPI) -> None:
    @app.get("/api/plugins/slash-registry")
    async def slash_registry() -> dict:
        """`/` 菜单的唯一注册表（#1200 §5 / 裁决 D2=A）。

        内置命令 + 启用中的 skill 同处一个列表。禁用项的排除有**两层**：装配期
        的 `catalog()`（按 plugin-settings.json 快照过滤）+ 本端点的现读叠加
        （见下方 `live_settings`）——后者让刚在面板里禁用的 skill 立刻从菜单消失，
        而不必等下次装配。MCP 工具**永不进入**（它们是模型工具，不是用户命令）。
        """
        commands: list[dict] = [dict(cmd, kind="builtin") for cmd in BUILTIN_SLASH_COMMANDS]
        _, wiring = await app.state.agent.get_wiring()
        capability = getattr(wiring, "skills", None)
        if capability is not None:
            entries = capability.catalog() if hasattr(capability, "catalog") else []
            # `catalog()` 只过滤**装配期**读到的禁用项（裁决 D1：模型面下次装配生效）。
            # 菜单是只读投影，必须与插件面板同一时刻的答案一致——否则用户刚在面板里
            # 禁用的 skill，敲 `/` 仍列得出来（面板说已禁用、菜单说可用）。
            live_settings = _settings(app)
            for entry in entries:
                name = getattr(entry, "name", "")
                if not name:
                    continue
                if not live_settings.skill_enabled(name):
                    continue
                item = {
                    "name": name,
                    "kind": "skill",
                    "description": single_line(
                        str(getattr(entry, "description", ""))
                    )[:MAX_SKILL_DESCRIPTION_CHARS],
                }
                # 参数提示来自 frontmatter `argument-hint`（票面 §5；插件 mockup 注⑤）。
                meta = getattr(entry, "meta", None) or {}
                hint = meta.get("argument-hint") if isinstance(meta, dict) else None
                if hint:
                    item["argumentHint"] = single_line(str(hint))
                # 插件来源 skill 加 namespace，前端渲染 `/plugin:skill` 消歧。
                # namespace 与 name 相同时省略：托管安装的 plugin 名就是 skill 名，
                # 否则会渲染出 `/code-review:code-review` 这种自指前缀。
                plugin = getattr(entry, "plugin", None)
                if plugin and plugin != name:
                    item["namespace"] = plugin
                commands.append(item)
        return {"commands": commands}

    @app.get("/api/mcp/servers/{server_name}/tools")
    async def list_mcp_server_tools(server_name: str) -> dict:
        """该 server 的发现结果（含工具级禁用项——管理面要看全量并给开关）。"""
        _, wiring = await app.state.agent.get_wiring()
        capability = _mcp_capability(wiring)
        if server_name not in _known_servers(capability):
            raise HTTPException(status_code=404, detail=f"未知 MCP server: {server_name}")
        settings = _settings(app)
        return {
            "tools": [
                {
                    "name": getattr(tool, "remote_name", getattr(tool, "name", "")),
                    "description": getattr(tool, "description", ""),
                    "enabled": settings.tool_enabled(
                        server_name,
                        getattr(tool, "remote_name", getattr(tool, "name", "")),
                    ),
                }
                for tool in capability.tools_for(server_name)
            ]
        }

    async def _require_server(server_name: str):
        _, wiring = await app.state.agent.get_wiring()
        capability = _mcp_capability(wiring)
        if server_name not in _known_servers(capability):
            raise HTTPException(status_code=404, detail=f"未知 MCP server: {server_name}")
        return capability

    def _workspace() -> str:
        return app.state.agent.settings.workspace_dir

    def _set_server(server_name: str, enabled: bool) -> dict:
        try:
            set_server_enabled(_workspace(), server_name, enabled)
        except PluginSettingsError as error:
            raise HTTPException(
                status_code=500,
                detail={"code": "settings_write_failed", "message": str(error)},
            ) from error
        return {"name": server_name, "enabled": enabled}

    def _reject_config_disabled(capability, server_name: str) -> None:
        """`enabled:false` 的 server：选择态开关对它无效（禁用源胜出）。

        两个来源都走这里（`configs()` = CAPABILITIES 配置层 ∪ 所选包，两边都含
        禁用项）：不拦的话端点会 200 + 落盘一个永远不生效的选择——UI 靠
        `configOff` 挡住了，裸 API 调用则会拿到"成功"的假回执。禁用源不同、
        改法不同，故文案两种可能都点明（不猜来源，避免给出不可执行的指引）。
        """
        config = next(
            (item for item in capability.configs() if item.name == server_name), None
        )
        if config is not None and not getattr(config, "enabled", True):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "config_disabled",
                    "message": (
                        f"MCP server {server_name} 被标记为禁用（enabled: false）——"
                        "来自 CAPABILITIES 配置层或所选插件包，两者都优先于插件管理的"
                        "选择态，因此这里的启停/确认开关对它无效。请在对应来源"
                        "（CAPABILITIES 配置或包源）里启用后重启。"
                    ),
                },
            )

    @app.post(
        "/api/mcp/servers/{server_name}/enable",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def enable_mcp_server(server_name: str) -> dict:
        capability = await _require_server(server_name)
        _reject_config_disabled(capability, server_name)
        return _set_server(server_name, True)

    @app.post(
        "/api/mcp/servers/{server_name}/disable",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def disable_mcp_server(server_name: str) -> dict:
        capability = await _require_server(server_name)
        _reject_config_disabled(capability, server_name)
        return _set_server(server_name, False)

    @app.post(
        "/api/mcp/servers/{server_name}/reconnect",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def reconnect_mcp_server(server_name: str) -> dict:
        """重连（不重放历史调用）。未装配的连接（如已禁用）→ 404 说清原因。"""
        capability = await _require_server(server_name)
        connection = capability.connection(server_name)
        if connection is None:
            raise HTTPException(
                status_code=404,
                detail=f"MCP server {server_name} 未连接（可能已被禁用或装配失败）",
            )
        try:
            await connection.reconnect()
        except Exception as error:  # 连接失败按 server 降级，不 500
            raise HTTPException(
                status_code=502,
                detail={"code": "reconnect_failed", "message": str(error)},
            ) from error
        return {"name": server_name, "connected": bool(connection.connected)}

    def _set_tool(server_name: str, tool_name: str, enabled: bool) -> dict:
        try:
            set_tool_enabled(_workspace(), server_name, tool_name, enabled)
        except PluginSettingsError as error:
            raise HTTPException(
                status_code=500,
                detail={"code": "settings_write_failed", "message": str(error)},
            ) from error
        return {"name": server_name, "tool": tool_name, "enabled": enabled}

    async def _require_tool(server_name: str, tool_name: str):
        capability = await _require_server(server_name)
        known = {
            getattr(tool, "remote_name", getattr(tool, "name", ""))
            for tool in capability.tools_for(server_name)
        }
        if tool_name not in known:
            raise HTTPException(
                status_code=404,
                detail=f"未知工具 {tool_name}（server {server_name}）",
            )
        return capability

    @app.post(
        "/api/mcp/servers/{server_name}/tools/{tool_name}/enable",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def enable_mcp_tool(server_name: str, tool_name: str) -> dict:
        await _require_tool(server_name, tool_name)
        return _set_tool(server_name, tool_name, True)

    @app.post(
        "/api/mcp/servers/{server_name}/tools/{tool_name}/disable",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def disable_mcp_tool(server_name: str, tool_name: str) -> dict:
        await _require_tool(server_name, tool_name)
        return _set_tool(server_name, tool_name, False)

    @app.post(
        "/api/mcp/servers/{server_name}/require-confirm",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def set_mcp_require_confirm(server_name: str, body: RequireConfirmBody) -> dict:
        capability = await _require_server(server_name)
        # 与 enable/disable 同一类"假回执"：禁用源胜出的 server 永不装配 ⇒ 永远
        # 没有 MCPTool 带上 require_confirmation，落盘的开关一辈子不会触发。
        _reject_config_disabled(capability, server_name)
        try:
            set_require_confirm(_workspace(), server_name, body.require_confirm)
        except PluginSettingsError as error:
            raise HTTPException(
                status_code=500,
                detail={"code": "settings_write_failed", "message": str(error)},
            ) from error
        return {"name": server_name, "require_confirm": body.require_confirm}

    def _set_skill(name: str, enabled: bool) -> dict:
        try:
            set_skill_enabled(_workspace(), name, enabled)
        except PluginSettingsError as error:
            raise HTTPException(
                status_code=500,
                detail={"code": "settings_write_failed", "message": str(error)},
            ) from error
        return {"name": name, "enabled": enabled}

    async def _require_skill_known(name: str):
        if not is_valid_skill_name(name):
            raise HTTPException(
                status_code=422,
                detail=f"非法 skill 名：{name!r}（允许小写字母/数字/连字符/下划线）",
            )
        _, wiring = await app.state.agent.get_wiring()
        capability = getattr(wiring, "skills", None)
        entries = (
            capability.all_entries()
            if capability is not None and hasattr(capability, "all_entries")
            else (capability.catalog() if capability is not None else [])
        )
        if name not in {getattr(entry, "name", "") for entry in entries}:
            raise HTTPException(status_code=404, detail=f"未知 skill: {name}")

    # `{name:path}`：skill 名白名单在端点内校验（非法名 422，不做静默规范化）。
    # 用 path 转换器才能让 `a/b` 这类带分隔符的非法名走到 422，而不是路由不匹配
    # 变成 404——"名字不合法"与"名字不存在"必须是两个可区分的答案。
    @app.post(
        "/api/skills/{name:path}/enable",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def enable_skill(name: str) -> dict:
        await _require_skill_known(name)
        return _set_skill(name, True)

    @app.post(
        "/api/skills/{name:path}/disable",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def disable_skill(name: str) -> dict:
        await _require_skill_known(name)
        return _set_skill(name, False)
