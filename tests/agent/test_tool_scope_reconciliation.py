"""#238：profile→`tool_scope` 的机械对账（声明面 × 注册面 × 收窄面）。

## 这个文件在防什么

`profiles.py` 的 `tool_scope` 是**手写**集合，runtime registry 由 capability wiring 动态
装配，而 `ToolRegistry.filtered()` 对未注册名静默跳过、对"注册了但没声明"的工具静默剔除。
三者之间过去没有任何机械绑定——`forget_memory` 就是这么被非 main 档位静默剔除的（#202）。

## 三条判据（本文件的骨架）

1. **覆盖**：`src/` 下每个 `Tool` 子类（除动态命名的 `MCPTool`）必须落在**非-main 档位的
   声明面**里，或在 `_UNSCOPED_TOOL_REASONS` 里有一条理由。两条都不占 = 有人加了工具却
   没人决定它归谁 → 红。判据本身写成可注入的纯函数 `unscoped()`，所以"变异会红"是被
   测试证明的（`test_new_unscoped_tool_is_reported`），不是读一遍代码觉得对。
2. **无死条目**：白名单里不该出现已被非-main 声明的名字（那条理由已经过期）→ 红。
3. **枚举来源是代码，不是这张表**：`test_source_tool_classes_are_all_inventoried` 扫
   `src/` 的 AST，发现新类而清单没登记就红——否则"新工具"永远进不了第 1 条判据。

## 白名单为什么在测试里

它是**登记表**，不是运行时授权：没有任何生产代码读它，唯一消费方就是本文件的对账。
放进 `profiles.py` 会变成一份没有消费方的生产数据（§9.2），也会让人把它误读成"登记 =
授权"。权限本体仍是 `profiles.py` 的三个 scope，本票**不动**它们（#238 Scope：先建立
机械对账，不改变声明面与实际部署面的分工）。

## 边界（不在本文件的判据里）

- **MCP 工具**：名字来自 MCP 服务器（运行时才知道），静态枚举不到，也不属"内置"。
- **本部署实际注册了什么**：那是 wiring 与运行期前置（API key / session_store）决定的，
  会与声明面双向不一致。本文件只对账**声明**与**源码里的工具类**；"声明 ⊃ 注册"由
  `tests/test_assembly_agent_profile.py` 的降级用例覆盖。
- **扫描根是 `src/agent_harness`**：`evaluation/support.py` 的 `AddTool`（由
  `evaluation/runner.py` 注册进真实 registry）在扫描根之外——它是评测支撑工具，不属
  内置工具面。生产侧新增注册机制时，这里要跟着扩，否则新工具对判据不可见。
- **基类判定是"名字以 `Tool` 结尾"**（`source_tool_classes`）：不是 MRO/import 判定。
  两个方向都不是静默的——基类名不以 `Tool` 结尾的真子类会**漏掉**（判据看不见它），
  继承自某个 `*Tool` 命名的非工具基类会被**多收**（进清单，红，需登记）。实测当前
  `src/` 下无两类误判；真出现时按 `_NOT_STATICALLY_ENUMERABLE` 的先例写明理由。
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
from collections.abc import Iterable

import pytest

from agent_harness.agent.profiles import BUILTIN_PROFILES
from agent_harness.assembly import BUILTIN_LOCAL_TOOLS

_SRC_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "agent_harness"

#: 非-main 档位（对账的"声明面"）。main 不过滤，所以"能不能用"对 main 恒真，
#: 收窄只发生在这些档位上——对账要问的正是"子代理被剔掉的东西有没有人决定过"。
_SUB_AGENT_PROFILES: tuple[str, ...] = tuple(
    sorted(name for name in BUILTIN_PROFILES if name != "main")
)

#: capability 侧的工具类（本地工具由 `assembly.BUILTIN_LOCAL_TOOLS` 提供，不在这里重复）。
#: 来源模块写全，方便 `importlib` 取类读 `.name`（构造器只存依赖、无副作用，可传 None）。
_CAPABILITY_TOOL_CLASSES: tuple[tuple[str, str], ...] = (
    # storage/artifact_select：与 store 配对的读回工具（两个 Provider 各一个）
    ("ReadArtifactTool", "agent_harness.tools.read_artifact"),
    ("InspectArtifactTool", "agent_harness.tools.inspect_artifact"),
    # knowledge / memory / websearch / skills / multiagent / ticker（demo）
    ("RetrieveKnowledgeTool", "agent_harness.knowledge.tools"),
    ("ReadKnowledgeSourceTool", "agent_harness.knowledge.tools"),
    ("IngestDocumentTool", "agent_harness.knowledge.tools"),
    ("RetrieveMemoryTool", "agent_harness.memory.tools"),
    ("RememberThisTool", "agent_harness.memory.tools"),
    ("ForgetMemoryTool", "agent_harness.memory.tools"),
    ("WebSearchTool", "agent_harness.websearch.tools"),
    ("LoadSkillTool", "agent_harness.skills.tool"),
    ("DelegateTool", "agent_harness.multiagent.tools"),
    ("TickTool", "agent_harness.capability.demo"),
)

#: 静态枚举不到的 `Tool` 子类 → 理由。AST 覆盖闸用：命中的类**不**参与内置工具对账。
_NOT_STATICALLY_ENUMERABLE: dict[str, str] = {
    "MCPTool": "名字来自 MCP 服务器的工具清单（运行时才知道），不属内置工具面",
}

#: 未归属任何非-main 档位的**内置工具** → 理由（#238 的显式白名单）。
#: 判据：`unscoped(builtin_tool_names())` 必须为空；每条都必须是**理由**，
#: 不是"以后再说"——登记在这里等于"已知且被接受"，不是"没看见"。
_UNSCOPED_TOOL_REASONS: dict[str, str] = {
    "delegate": (
        "只给 main：`max_depth=1` ⇒ child 无 delegate（ADR-0015 决策 2/13 防递归委派），"
        "属**设计如此**"
    ),
    "inspect_artifact": (
        "S3 配对的产物读回工具，只在 main 声明——与 `read_artifact` 同族（见下），"
        "子代理的读回面待决"
    ),
    "read_artifact": (
        "MinIO/Local 配对的产物读回工具，未在任何档位声明：审计 §5.7 实测它是 coding "
        "收窄后**唯一**被剔除的工具，而前端 tooltip 因此列不出这个名字。**只登记不改 scope**"
        "——收窄面变更会牵动跨端手工镜像的 17/12/7（#238 Scope）"
    ),
    "ingest_document": (
        "知识库**写入**工具，未在任何档位声明（子代理走只读检索 retrieve_knowledge / "
        "read_knowledge_source）"
    ),
    "load_skill": (
        "skills 能力工具（`skills/tool.py`），未在任何档位声明：技能包由 main 加载"
    ),
    "tick": (
        "Phase 7 Gate 的 demo 能力工具（`capability/demo.py` 自述无业务价值），"
        "仅 `CAPABILITIES` 显式配 ticker 时存在"
    ),
}


def _declared_sub_agent_scope() -> frozenset[str]:
    """非-main 档位声明面的**并集**（对账里的"声明面"）。"""
    return frozenset().union(
        *(BUILTIN_PROFILES[name].tool_scope for name in _SUB_AGENT_PROFILES)
    )


def unscoped(names: Iterable[str]) -> list[str]:
    """返回"既不在任何非-main 档位、也不在带理由白名单"的工具名（升序）。空 = 通过。

    刻意收成纯函数：判据要能被喂**假名字**（`test_new_unscoped_tool_is_reported`），
    否则"新增工具会红"只是声称，不是证据。
    """
    return sorted(set(names) - _declared_sub_agent_scope() - set(_UNSCOPED_TOOL_REASONS))


def _name_of(class_name: str, module_name: str) -> str:
    """读工具类**自己**报的名字（不复制字面量：复制一份就等于给漂移留门）。"""
    cls = getattr(importlib.import_module(module_name), class_name)
    signature = inspect.signature(cls.__init__)
    positional = [
        p for p in signature.parameters.values()
        if p.name != "self" and p.default is p.empty
    ]
    # 构造器只存依赖（已核对全部 12 个 capability/artifact 工具类），传 None 足够读 `.name`。
    return cls(*([None] * len(positional))).name


def builtin_tool_names() -> set[str]:
    """全部内置工具名：本地（`assembly.BUILTIN_LOCAL_TOOLS`）+ capability 侧清单。"""
    local = {cls(None).name for cls in BUILTIN_LOCAL_TOOLS}
    capability = {_name_of(cls_name, mod) for cls_name, mod in _CAPABILITY_TOOL_CLASSES}
    return local | capability


def source_tool_classes() -> dict[str, list[str]]:
    """`src/agent_harness` 下全部 `Tool` 子类：类名 → 定义它的模块路径（可多个）。

    名字是**唯一**的对账键（清单登记的是名字），所以同名类不能互相覆盖：两个模块各
    定义一个 `FooTool` 时只留一条，另一个就逃过全部判据。返回列表、由调用方对"同名"
    直接判红（`test_source_tool_classes_are_all_inventoried`）。
    """
    found: dict[str, list[str]] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name == "Tool":
                continue
            bases = [
                base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                for base in node.bases
            ]
            if any(base.endswith("Tool") for base in bases):
                rel = str(path.relative_to(_SRC_ROOT.parent.parent)).replace("\\", "/")
                found.setdefault(node.name, []).append(rel)
    return {name: sorted(set(paths)) for name, paths in found.items()}


def _inventoried_classes() -> set[str]:
    return {cls.__name__ for cls in BUILTIN_LOCAL_TOOLS} | {
        cls_name for cls_name, _ in _CAPABILITY_TOOL_CLASSES
    }


# ── 判据 3：枚举来源是代码 ─────────────────────────────────────


def test_source_tool_classes_are_all_inventoried():
    """源码里每个 `Tool` 子类都必须在本文件的清单里（否则第 1 条判据永远看不见它）。

    这是"新增 builtin tool 而未更新对账表时测试变红"（#238 AC2）的机械部分：
    加类 → 这里红 → 加进清单 → 第 1 条判据接着问它归谁。
    """
    discovered_all = source_tool_classes()
    # 同名类：按名字对账会让其中一个静默逃过**全部**判据（清单/白名单都只认名字）。
    duplicates = sorted(name for name, paths in discovered_all.items() if len(paths) > 1)
    assert not duplicates, (
        f"同名 Tool 子类出现在多个模块，按名字对账会漏掉其中一个："
        f"{ {name: discovered_all[name] for name in duplicates} }"
    )
    discovered = set(discovered_all) - set(_NOT_STATICALLY_ENUMERABLE)
    missing = sorted(discovered - _inventoried_classes())
    assert not missing, (
        f"这些 Tool 子类不在内置工具清单里，工具面因此无人对账：{missing}"
        "（加进 _CAPABILITY_TOOL_CLASSES，或按 MCPTool 的先例写进 _NOT_STATICALLY_ENUMERABLE）"
    )
    # 反向：清单里写了但源码里没有的类（改名/删除后的残骸）——同样红。
    stale = sorted(_inventoried_classes() - set(discovered_all))
    assert not stale, f"清单里有源码中不存在的工具类（已改名或删除？）：{stale}"


# ── 判据 1：覆盖 ───────────────────────────────────────────────


def test_every_builtin_tool_is_declared_or_whitelisted():
    """#238 AC1：内置工具 ⊆ 非-main 声明面 ∪ 带理由白名单。"""
    assert unscoped(builtin_tool_names()) == []


def test_new_unscoped_tool_is_reported():
    """#238 AC2：喂一个未归属的假工具名 → 判据必须点名它（变异会红，不是声称）。"""
    fake = builtin_tool_names() | {"fake_new_tool"}

    assert unscoped(fake) == ["fake_new_tool"]


def test_whitelist_entries_carry_a_reason():
    """白名单的每条都必须是理由：空串 / 空白 = 没登记。"""
    empty = sorted(name for name, reason in _UNSCOPED_TOOL_REASONS.items() if not reason.strip())
    assert not empty, f"白名单条目缺理由：{empty}"


# ── 判据 2：无死条目 ───────────────────────────────────────────


def test_whitelist_has_no_dead_entries():
    """已被非-main 档位声明的名字不该再躺在白名单里（理由过期 = 误导后来人）。"""
    dead = sorted(set(_UNSCOPED_TOOL_REASONS) & _declared_sub_agent_scope())
    assert not dead, (
        f"这些名字已被非-main 档位声明，白名单条目应删除：{dead}"
    )


def test_whitelist_only_covers_real_tools():
    """白名单不该出现源码里根本没有的工具名（抄错 / 工具已删）。"""
    unknown = sorted(set(_UNSCOPED_TOOL_REASONS) - builtin_tool_names())
    assert not unknown, f"白名单里有不存在的工具名：{unknown}"


# ── 声明面本身：子代理档位非空（exact 集合见 test_profiles_factory）──


@pytest.mark.parametrize("profile", _SUB_AGENT_PROFILES)
def test_sub_agent_scopes_are_non_empty(profile: str):
    """子代理档位的 scope 非空——空集等于"这个档位一个工具都没有"。

    "子代理 ⊆ main"**不在这里断言**：main 的 scope 定义为
    `_MAIN_TOOLS = _CODING_TOOLS | _RESEARCH_TOOLS | frozenset({...})`，子代理档位又
    复用这两个常量，所以那是**结构上**成立的性质，写出来只会是一条永远绿的断言
    （审查实测：只有把 main 改成不派生的字面量才可能红，而那种改动由
    `test_profiles_factory.py::test_declared_scope_is_exact` 的 exact set 表先抓住）。
    """
    assert BUILTIN_PROFILES[profile].tool_scope, f"{profile} 的 tool_scope 为空"
