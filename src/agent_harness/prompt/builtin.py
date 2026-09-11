"""内置 prompt section 声明——本项目 prompt 正文的唯一集散地（ADR-0023 D1）。

改 profile / 辅助 / 框架 prompt 的文案，只需编辑本文件。

【禁止】本模块不得 import `agent_harness.agent.*`——`agent.profiles` 反向依赖
本模块取内置文案，反向 import 会形成循环依赖。
"""

from __future__ import annotations

from agent_harness.prompt.registry import PromptRegistry, run_self_check
from agent_harness.prompt.section import SECTION_ORDERS, PromptSection, Target

__all__ = ["DEFAULT_REGISTRY", "build_registry"]

#: P0 的三条 profile section。正文与迁移前 `agent/profiles.py` 内联字符串
#: **逐字节相同**（标点、空格、全角半角一律原样），由
#: `tests/prompt/test_profiles_migration.py` 的逐字节断言锁死。
_BUILTIN_SECTIONS: tuple[PromptSection, ...] = (
    PromptSection(
        name="profile:main:identity",
        order=SECTION_ORDERS["profile:identity"],
        scopes=frozenset({"profile:main"}),
        target=Target.SYSTEM,
        text=(
            "你是主协调 agent。简单任务直接完成；需要并行/专项深入时用 delegate "
            "工具把 scoped task 派给合适的子代理（coding=写代码，research_review="
            "调研与审查），并综合它们的结构化结果。委派时给出完整自洽的任务描述"
            "——子代理看不到你们的对话历史。"
        ),
        description="主协调 agent 的角色定义",
    ),
    PromptSection(
        name="profile:coding:identity",
        order=SECTION_ORDERS["profile:identity"],
        scopes=frozenset({"profile:coding"}),
        target=Target.SYSTEM,
        text=(
            "你是编码 agent，在给定 workspace 内完成 scoped task：读写文件、运行命令、"
            "验证结果。结束时给出简明总结：做了什么、改了哪些文件、验证结果，以及"
            "任何未解决事项。"
        ),
        description="编码子代理的角色定义",
    ),
    PromptSection(
        name="profile:research_review:identity",
        order=SECTION_ORDERS["profile:identity"],
        scopes=frozenset({"profile:research_review"}),
        target=Target.SYSTEM,
        text=(
            "你是调研审查 agent，只读地收集证据（本地文件、知识语料、网络）并给出"
            "带引用的结论。结束时给出简明总结：结论、引用（citation）、以及任何未"
            "解决事项。你没有写权限。"
        ),
        description="调研/审查子代理的角色定义",
    ),
)

#: 注册表需要预声明的变量（R3b：section.requires ⊆ declared_variables()）。
#: P0 为空——三条 profile 正文都不含 `{{...}}`；T4 会在此追加 `tail_text` 等。
#: 【约束】任何新 section 只要正文含 `{{var}}`，就必须先在此登记，否则 `register()`
#: 抛 `undefined_variable`（import 期即崩）。
_DECLARED_VARIABLES: tuple[tuple[str, str], ...] = ()


def build_registry() -> PromptRegistry:
    """构建内置注册表（无参 → 确定性，不读环境）。

    **公开**：T4/T5 在其上扩展；测试用 `build_registry()` 拿确定性实例。
    T5 的 persona 走 `build_registry(persona=…)` 这个装配点注入，**不改**
    `DEFAULT_REGISTRY`（§4.4：默认注册表永不读环境）。

    顺序不可颠倒：先 `variable()` 再 `register()`——`register` 会校验
    `requires ⊆ declared_variables()`，反过来第一个带变量的 section 就会抛
    `undefined_variable`。
    """
    registry = PromptRegistry()
    for name, description in _DECLARED_VARIABLES:
        registry.variable(name, description=description)
    for section in _BUILTIN_SECTIONS:
        registry.register(section)
    return registry


def _declared_scopes(registry: PromptRegistry) -> list[str]:
    """自检的 scope 集 = 注册表里所有**非 `*`** 的 scope（去重 + 排序）。

    刻意不写死 `profile:` 前缀：T4 会加 `aux:*` scope，本函数届时自动覆盖，无需
    修改（PRD §10.8：自检规则无例外——注册表里出现的每个 scope 都必须能组装成功）。
    `*` 被排除：它不是可组装的 scope。
    """
    scopes = {
        scope for section in registry.available() for scope in section.scopes if scope != "*"
    }
    return sorted(scopes)


#: 内置注册表（**不含**任何环境派生内容）。交接文档 §4.4：本对象**永不读环境**——
#: persona 由装配点调 `build_registry(persona=…)` 注入，不在这里变形。
#: 因此断言"注册表组成"的测试用它或用 `build_registry()` 都安全。
DEFAULT_REGISTRY = build_registry()

# 启动自检（PRD §10.8）：覆盖注册表里全部非 `*` scope，配置错误在 import 期暴露。
# 因为本注册表不含环境派生 section，这段 import 期自检只校验项目自带文本，
# 不会被用户配置（AGENT_PERSONA 等）的语法错误拖崩。
run_self_check(DEFAULT_REGISTRY, _declared_scopes(DEFAULT_REGISTRY))
