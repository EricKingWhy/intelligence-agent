"""Phase 16 Final Full E2E 链路 helper（ADR-0019 D7/D10）。

E2E 链路专用：ScriptedModel 关键路径剧本构造 + fake provider 组合套件。
同 `_kill_child.py` 先例——phase gate 文件依赖的共享 helper 放这里，不在
gate 文件里重复。混合 fixture 策略（D10）：单元级 fake provider 直接 import
复用，E2E 编排剧本（多轮预设回复）新建在本 helper。

薄编排层（D1）的关键路径剧本共 4 轮：
1. retrieve_knowledge（KB 证据不足 is_sufficient=false）
2. web_search（外部检索，带 citation）
3. add（coding 代理工具，走统一 ToolExecutor）
4. 模型给最终回答，引用 web 来源

deterministic + 零网络 + 零 token（同 Phase 15 ScriptedModel + fake provider 模式）。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage

from agent_harness.identity import IdentityContext, set_identity_context
from agent_harness.knowledge.registry import SqliteKnowledgeSourceRegistry
from agent_harness.knowledge.service import KnowledgeService
from agent_harness.knowledge.store import FakeKnowledgeVectorStore
from agent_harness.knowledge.tools import RetrieveKnowledgeTool
from agent_harness.model.scripted import ScriptedModel
from agent_harness.tooling import ToolRegistry
from agent_harness.websearch.fake import FakeWebSearchProvider
from agent_harness.websearch.tools import WebSearchTool
from evaluation.support import AddTool

# E2E 链路使用的固定身份（同 tests/knowledge/test_tools.py ALICE 风格）。
# 用固定 tenant/user 让 KB 检索的 fake store 命中预置 chunk（不依赖真
# IdentityContext 绑定——只在调 service.retrieve 时经 get_identity_context 取到）。
PHASE16_TENANT = "phase16"
PHASE16_USER = "e2e"
PHASE16_IDENTITY = IdentityContext(PHASE16_TENANT, PHASE16_USER, ["user", "session"])

# 关键路径引用的 web URL（剧本里 citation 引用、断言里校验格式）。
PHASE16_WEB_URL = "https://docs.python.org/3/"


def critical_path_scripted_model() -> ScriptedModel:
    """薄编排层用的 ScriptedModel（4 轮简化链路，ADR-0019 D1）。

    第 1 轮：模型判断需要 KB 证据 → 调 retrieve_knowledge（fake store 返回不足）
    第 2 轮：KB 不足 → 模型按 Retrieval Fallback affordance 调 web_search（带 citation）
    第 3 轮：模型顺带做一次 coding 代理计算 → 调 add（统一 ToolExecutor 路径）
    第 4 轮：模型给最终回答，文本里含 web citation（引用来源可追溯）
    """
    return ScriptedModel([
        AIMessage(
            content="",
            tool_calls=[{
                "id": "phase16-kb",
                "name": "retrieve_knowledge",
                "args": {"query": "python typing 类型标注"},
                "type": "tool_call",
            }],
        ),
        AIMessage(
            content="",
            tool_calls=[{
                "id": "phase16-web",
                "name": "web_search",
                "args": {"query": "Python asyncio 官方文档"},
                "type": "tool_call",
            }],
        ),
        AIMessage(
            content="",
            tool_calls=[{
                "id": "phase16-add",
                "name": "add",
                "args": {"first_number": 1, "second_number": 2},
                "type": "tool_call",
            }],
        ),
        AIMessage(
            content=f"答案参见 web:{PHASE16_WEB_URL}；合计 3。",
        ),
    ])


async def build_critical_path_registry(tmp_path: Path) -> ToolRegistry:
    """组装关键路径用的 ToolRegistry（KB + Web + Add 三工具，全 fake provider）。

    KB 走 FakeKnowledgeVectorStore（空语料 → is_sufficient=False，编排层据此
    证明 Retrieval Fallback affordance 链路）；Web 走 FakeWebSearchProvider（
    默认 demo 文档命中 Python 官方文档，citation=web:<url>）；Add 是评测域
    确定性求和工具。registry 初始化期绑定身份到 ContextVar，让 KB retrieve
    工具经 get_identity_context() 取到本 E2E 的 tenant。
    """
    # 绑定身份 ContextVar：Knowledge 工具的 _identity() 取这个值。
    # token 不 reset——本 helper 是测试体内一次性 setup，进程级隔离。
    set_identity_context(PHASE16_IDENTITY)

    registry = ToolRegistry()
    # KB：空 fake store（不预 ingest）→ retrieve 返回空 hits → is_sufficient=False
    kb_registry = SqliteKnowledgeSourceRegistry(tmp_path / "phase16_harness.db")
    await kb_registry.initialize()
    kb_service = KnowledgeService(
        store=FakeKnowledgeVectorStore(),
        registry=kb_registry,
        min_score=0.6,
    )
    registry.register(RetrieveKnowledgeTool(kb_service))
    # Web：默认 demo 文档（含 Python 官方文档，substring 命中评分）
    registry.register(WebSearchTool(FakeWebSearchProvider()))
    # Coding 代理：评测域确定性求和工具
    registry.register(AddTool())
    return registry
