"""Skill 目录只读展示（#529 §6.3 第一版：catalog 列表，无管理按钮）。

治理面第一版只做**只读**：skill 目录列表 + 待审草稿列表 + 发现阶段错误/冲突。
文件即真相（§6.3/不变量 #22）：编辑/删除走文件系统（registry 是 discover 的
投影），本路由不提供任何写操作——管理按钮 DEFER。

未装配 skills capability → 503 逐原因说法（#225 同款：不假装能力存在）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from agent_harness.host_service import (
    HOST_SKILLS_RESPONSE_PROOF_HEADER,
    host_skills_response_proof,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


#: 票面 §5：description 按 1536 字符截断（Claude Code skill 描述的展示上限）。
MAX_SKILL_DESCRIPTION_CHARS = 1536


def register_skill_routes(app: FastAPI) -> None:
    """把 skill 只读路由挂到既有 app（`create_app` 里一行调用的接入面）。"""

    @app.get("/api/skills")
    async def list_skills(request: Request) -> dict:
        _, wiring = await app.state.agent.get_wiring()
        capability = getattr(wiring, "skills", None)
        if capability is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "not_configured",
                    "message": "skills capability 未启用：请在 CAPABILITIES 中配置 skills。",
                },
            )
        # #1200：管理面列表要含禁用项（禁用≠删除）；旧替身无 all_entries 时回落
        # catalog（其条目默认 enabled=True）。
        all_entries = getattr(capability, "all_entries", None)
        entries = all_entries() if all_entries is not None else capability.catalog()
        promoter = getattr(capability, "promoter", None)
        drafts = (
            [{"name": d.name, "status": d.status} for d in promoter.drafts()]
            if promoter is not None else []
        )
        # #1200 / 裁决 D1：`enabled` 以 plugin-settings.json 的持久选择态为准
        # （管理窗口在重启前就要看到"已禁用"的意图；真正不投影给模型发生在下次
        # 装配）。settings 读不出时回落目录条目的 enabled（不把只读列表打成 500）。
        from agent_harness.plugins.settings import (
            PluginSettings,
            PluginSettingsError,
            load_plugin_settings,
        )

        # getattr 防御：宿主挑战等测试用替身 agent 可能没有 settings；缺失时
        # 不叠加选择态（条目自带的 enabled 仍是权威的目录投影）。
        agent_settings = getattr(app.state.agent, "settings", None)
        workspace_dir = getattr(agent_settings, "workspace_dir", None)
        if workspace_dir is not None:
            try:
                plugin_settings = load_plugin_settings(workspace_dir)
            except PluginSettingsError:
                plugin_settings = PluginSettings()
        else:
            plugin_settings = None
        payload = {
            # 文件即真相的投影（只读）；正文经 load_skill / 文件系统按需读。
            "skills": [
                _skill_payload(
                    entry,
                    enabled=(
                        plugin_settings.skill_enabled(entry.name)
                        if plugin_settings is not None
                        else None
                    ),
                )
                for entry in entries
            ],
            "drafts": drafts,
            "errors": capability.errors(),
            "conflicts": capability.conflicts(),
        }
        nonce = getattr(request.state, "host_skills_challenge_nonce", None)
        service_uuid = getattr(request.state, "host_skills_service_uuid", None)
        timestamp = getattr(request.state, "host_skills_proof_timestamp", None)
        token = getattr(request.app.state, "host_service_token", None)
        if nonce and service_uuid and timestamp and token:
            response = JSONResponse(payload)
            response.headers[HOST_SKILLS_RESPONSE_PROOF_HEADER] = host_skills_response_proof(
                token,
                nonce,
                service_uuid,
                timestamp,
                response.status_code,
                response.body,
            )
            return cast(dict, response)
        return payload


def _skill_payload(entry: object, *, enabled: bool | None = None) -> dict:
    """单个 skill 的投影（#1200 增量字段，全部 getattr 防御以兼容旧替身）。"""
    description = str(getattr(entry, "description", ""))
    payload = {
        "name": getattr(entry, "name", ""),
        "description": description[:MAX_SKILL_DESCRIPTION_CHARS],
        "when_to_use": getattr(entry, "when_to_use", ""),
        "source": str(getattr(entry, "source_path", "")),
        "scope": getattr(entry, "scope", "workspace"),
        "origin": getattr(entry, "origin", "personal"),
        "enabled": bool(getattr(entry, "enabled", True)) if enabled is None else enabled,
    }
    plugin = getattr(entry, "plugin", None)
    if plugin is not None:
        payload["plugin"] = plugin
    return payload
