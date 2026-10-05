"""SkillCapability（spec 09 §2）：目录 + 按需加载的 Provider，注册进 CapabilityRegistry。"""

from __future__ import annotations

from agent_harness.capability.base import CapabilityError
from agent_harness.skills.discovery import SkillCatalogEntry, SkillDiscovery


class SkillCapability:
    """Skill 作为 Context Capability 的 Provider（不等于 Tool，ADR-0011 Q3）。

    "加载动作"是模型可调用的工具：本类实现 ContributesTools，把 load_skill
    交给 wire_capabilities 统一收集进 ToolRegistry（零旁路）。

    #529：本类持 SkillDiscovery **引用**（不再是装配期静态 catalog）——沉淀闭环
    写入 skill 文件后由 discovery 内嵌刷新 registry，capability 的目录投影随
    discover() 收敛，不存在"写了但不可见"的漂移态（oh-my-pi 教训，§6.1）。
    """

    def __init__(self, discovery: SkillDiscovery) -> None:
        self._discovery = discovery

    def catalog(self) -> list[SkillCatalogEntry]:
        """目录条目（只有 name/description/meta，不含正文）。"""
        return list(self._discovery.catalog().entries)

    def errors(self) -> list[str]:
        """发现阶段的解析/边界错误（可观察，不静默）。"""
        return list(self._discovery.catalog().errors)

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

    def contributes_tools(self) -> list:
        from agent_harness.skills.tool import LoadSkillTool

        return [LoadSkillTool(self)]
