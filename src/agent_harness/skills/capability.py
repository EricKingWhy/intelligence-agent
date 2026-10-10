"""SkillCapability（spec 09 §2）：目录 + 按需加载的 Provider，注册进 CapabilityRegistry。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_harness.capability.base import CapabilityError
from agent_harness.skills.discovery import SkillCatalogEntry, SkillDiscovery


class SkillCapability:
    """Skill 作为 Context Capability 的 Provider（不等于 Tool，ADR-0011 Q3）。

    "加载动作"是模型可调用的工具：本类实现 ContributesTools，把 load_skill
    交给 wire_capabilities 统一收集进 ToolRegistry（零旁路）。

    #529：本类持 SkillDiscovery **引用**（不再是装配期静态 catalog）——沉淀闭环
    写入 skill 文件后由 discovery 内嵌刷新 registry，capability 的目录投影随
    discover() 收敛，不存在"写了但不可见"的漂移态（oh-my-pi 教训，§6.1）。

    #529 T-529-5：装配传入 SkillPromoter 时，沉淀工具（promote_skill /
    register_skill）一并经 contributes_tools 进统一收集循环；未装配 promoter
    （如旧测试/极简装配）= 无沉淀写入面，只读不变。
    """

    def __init__(self, discovery: SkillDiscovery, promoter: Any = None) -> None:
        self._discovery = discovery
        self._promoter = promoter
        #: discover diff 基线（skill/removed 事件，§10-3）：上次 poll_removals
        #: 看到的 name 集合；None = 尚未建立基线。
        self._known_names: frozenset[str] | None = None

    @property
    def promoter(self):
        """沉淀状态机（未装配时 None = 本进程无沉淀写入面）。"""
        return self._promoter

    def catalog(self) -> list[SkillCatalogEntry]:
        """目录条目（只有 name/description/meta，不含正文）。"""
        return list(self._discovery.catalog().entries)

    def errors(self) -> list[str]:
        """发现阶段的解析/边界错误，以及受管包选择失效（可观察，不静默）。"""
        return list(self._discovery.catalog().errors)

    def conflicts(self) -> list[str]:
        """同名冲突标注（先到先得，被 shadow 的一方显式可见）。"""
        return list(self._discovery.catalog().conflicts)

    def poll_removals(self) -> list[str]:
        """discover diff（§10-3 推荐）：重新扫描并与上次已知 name 集合对比。

        返回消失的 skill 名（外部删除/文件即真相的编辑收敛结果）并更新基线；
        首次调用只建基线（无历史可比，返回空）。调用方负责发 skill/removed 事件。
        """
        current = frozenset(e.name for e in self._discovery.discover().entries)
        known = self._known_names
        self._known_names = current
        if known is None:
            return []
        return sorted(known - current)

    def sync_known_names(self) -> None:
        """把 diff 基线对齐到当前 catalog（不经 diff 留痕的合法 name 变更——
        即闭环自己的 register/update/remove——之后调用，避免下次 poll 把自己
        刚写入的 skill 误报为"消失/新增"）。"""
        self._known_names = frozenset(e.name for e in self._discovery.catalog().entries)

    def load(self, name: str) -> str:
        """按名加载 skill 全文；未知名显式报错，不伪造内容。

        读盘失败（含 load_body 的路径边界重验证拒绝与超大文件拦截）统一映射为
        CapabilityError：LoadSkillTool 只捕获 CapabilityError，裸异常会以未分类
        形态漏到 Executor 兜底，丢失"边界被改动/内容漂移"的语义。覆盖 OSError 与
        UnicodeDecodeError（后者是 ValueError 子类——发现后文件被换成非 UTF-8
        字节正是本防线针对的漂移形态，与 discovery.py 的捕获面一致）。
        """
        for entry in self._discovery.catalog().entries:
            if entry.name == name:
                try:
                    return entry.load_body()
                except (OSError, UnicodeDecodeError) as error:
                    raise CapabilityError(
                        f"skill '{name}' body unreadable: {error}", code="io",
                    ) from error
        raise CapabilityError(f"skill '{name}' is not in the catalog", code="not_found")

    def load_with_resource_root(self, name: str) -> tuple[str, Path]:
        """Load one Skill body with the root for resolving its relative resources."""
        body = self.load(name)
        for entry in self._discovery.catalog().entries:
            if entry.name == name:
                return body, entry.source_path.parent
        raise CapabilityError(f"skill '{name}' is not in the catalog", code="not_found")

    def contributes_tools(self) -> list:
        from agent_harness.skills.tool import LoadSkillTool

        tools = [LoadSkillTool(self)]
        if self._promoter is not None:
            from agent_harness.skills.promote_tool import (
                PromoteSkillTool,
                RegisterSkillTool,
            )

            tools.extend([PromoteSkillTool(self), RegisterSkillTool(self)])
        return tools
