"""#248：领域服务的构造契约 + 单点适配守卫（AC1–AC5 的证据位）。

四条证据分别对应：

- **AC1**：构造参数的**名字集合**与 ADR-0040 §2 的字段清单逐字一致——清单在文档里
  是表格，在这里是 frozenset，两边任一漂移这条就红。
- **AC2/AC4**：构造不经过容器——普通 duck-typed 对象（不是 `AppState`、不是
  `MagicMock`）即可；少一个 collaborator 属性就是 `AttributeError`，没有魔法兜底；
  domain 源码（`session/service.py` / `session/projects.py`）不再在代码或注解里
  命名 `AppState`。
- **AC3**：`src` 全树只有**两个**领域服务构造点被允许——各自的定义模块与
  `web/app.py`（传输侧组合根）。
- **AC5**：删掉组合根就会重新造成跨层耦合，所以这里锁住"import domain 不 import
  `agent_harness.web.app`"（子进程实测，不受本进程已导入的模块影响）。
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from agent_harness.session.projects import ProjectService
from agent_harness.session.service import SessionService
from agent_harness.web.app import project_service, session_service

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
REPO_ROOT = SRC_ROOT.parent

#: AC1 的字段清单（与 ADR-0040 §2 表格同源；改这里必须同时改文档）。
SESSION_SERVICE_COLLABORATORS = frozenset({
    "store",
    "run_manager",
    "settings",
    "workspace_registry",
    "session_meta_store",
    "message_queues",
    "approval_queues",
    "workspaces_root",
    "workspace_index",
    "operation_ledger",
    "transport_ledger",
    "checkpoint_store",
    "harness_db",
    "stores",
    "ensure_stores",
    "get_wiring",
})

PROJECT_SERVICE_COLLABORATORS = frozenset({
    "store",
    "workspace_index",
    "ensure_stores",
})


class _DuckState:
    """普通对象：只有被构造器读到的那些属性。

    刻意**不**继承 `AppState`、不用 `MagicMock`——后者的 any-attribute 语义会说
    "少一个字段也照样成功"，正是本票要证明不成立的行为。
    """

    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


def _state_for(names: frozenset[str]) -> tuple[_DuckState, dict[str, object]]:
    """每个 collaborator 一个**可辨认**的哨兵对象（identity 可断言）。"""
    sentinels = {name: object() for name in sorted(names)}
    return _DuckState(**sentinels), sentinels


class TestConstructionContract:
    def test_session_service_takes_exactly_the_documented_collaborators(self):
        params = set(inspect.signature(SessionService.__init__).parameters) - {"self"}
        assert params == SESSION_SERVICE_COLLABORATORS, (
            "构造契约变了：ADR-0040 §2 的字段清单必须同步"
        )

    def test_project_service_takes_exactly_the_documented_collaborators(self):
        params = set(inspect.signature(ProjectService.__init__).parameters) - {"self"}
        assert params == PROJECT_SERVICE_COLLABORATORS, (
            "构造契约变了：ADR-0040 §2 的字段清单必须同步"
        )

    @pytest.mark.parametrize("cls", [SessionService, ProjectService])
    def test_all_collaborators_are_keyword_only(self, cls):
        """16 个同形而不同实体的 collaborator 靠位置传参必然错位——只允许关键字。"""
        kinds = {
            name: param.kind
            for name, param in inspect.signature(cls.__init__).parameters.items()
            if name != "self"
        }
        not_kwonly = {n for n, k in kinds.items() if k is not inspect.Parameter.KEYWORD_ONLY}
        assert not_kwonly == set(), f"{cls.__name__} 有非关键字参数：{sorted(not_kwonly)}"

    def test_session_service_builds_from_a_plain_object(self):
        """AC4：容器不是构造前提；每个字段都被真的搬进去了（没有静默丢字段）。"""
        state, sentinels = _state_for(SESSION_SERVICE_COLLABORATORS)

        service = session_service(state)

        for name, sentinel in sentinels.items():
            assert getattr(service, f"_{name}") is sentinel, f"{name} 没有被搬进服务"

    def test_project_service_builds_from_a_plain_object(self):
        state, sentinels = _state_for(PROJECT_SERVICE_COLLABORATORS)

        project = project_service(state)

        for name, sentinel in sentinels.items():
            assert getattr(project, f"_{name}") is sentinel, f"{name} 没有被搬进项目服务"

    def test_missing_collaborator_raises_instead_of_defaulting(self):
        """缺字段 = `AttributeError`（不是 `None` 兜底、不是静默降级）。"""
        fields = {name: object() for name in SESSION_SERVICE_COLLABORATORS - {"get_wiring"}}

        with pytest.raises(AttributeError):
            session_service(_DuckState(**fields))

    def test_factories_read_attributes_at_call_time(self):
        """每次调用现取属性：构造后替换 collaborator 仍然生效（与旧行为逐字一致）。"""
        state, _ = _state_for(SESSION_SERVICE_COLLABORATORS)
        replacement = object()

        state.run_manager = replacement

        assert session_service(state)._run_manager is replacement


class TestCompositionRoot:
    def test_factories_live_in_the_transport_composition_root(self):
        assert session_service.__module__ == "agent_harness.web.app"
        assert project_service.__module__ == "agent_harness.web.app"

    def test_services_are_constructed_only_in_the_composition_root(self):
        """AC3：`src` 全树只允许组合根 `web/app.py` 构造这两个服务。

        守卫的是"适配只有一处"：任何在 router / handler / 领域代码里就地 new 服务的
        写法都会在这里变红，而不是等到某天两份构造悄悄漂移。
        （定义模块自己不构造——只定义类；因此判据是"构造点 ⊆ {组合根}" + 组合根必须在。）
        """
        composition_root = "agent_harness/web/app.py"
        targets = {"SessionService", "ProjectService"}
        found: dict[str, set[str]] = {name: set() for name in targets}

        for py in sorted(SRC_ROOT.rglob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            rel = str(py.relative_to(SRC_ROOT)).replace("\\", "/")
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in targets
                ):
                    found[node.func.id].add(rel)

        for name, files in found.items():
            assert files == {composition_root}, (
                f"{name} 的构造点是 {sorted(files)}，只允许组合根 {composition_root}"
            )

    def test_domain_never_names_the_transport_container(self):
        """AC2：领域源码在**代码与注解**里不出现 `AppState`（注释/文档串不算）。

        用 AST 取标识符而不是文本匹配：docstring 可以正常提到"那个 state 容器"，
        但一旦有注解或代码真的引用了容器类型，这条就红。
        """
        offenders: list[str] = []
        for rel in (
            Path("agent_harness/session/service.py"),
            Path("agent_harness/session/projects.py"),
        ):
            tree = ast.parse((SRC_ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                named: list[str] = []
                if isinstance(node, ast.Name):
                    named.append(node.id)
                elif isinstance(node, ast.Attribute):
                    named.append(node.attr)
                # 注解是"代码里出现容器类型"的另一种写法（旧实现正是注解里写 AppState）。
                annotation = getattr(node, "annotation", None)
                if annotation is None and isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    annotation = node.returns
                if annotation is not None:
                    named.append(ast.unparse(annotation))
                if any("AppState" in name for name in named):
                    offenders.append(f"{rel}:{node.lineno}")

        assert offenders == [], f"领域层又命名了传输容器：{offenders}"


class TestRuntimeImportBoundary:
    def test_importing_the_domain_does_not_load_the_web_app(self):
        """AC3/AC5：`import agent_harness.session.service` 不把 `web.app` 拖进来。

        子进程跑：本进程里 pytest 早就导入过 `web.app`，`sys.modules` 已不可信。
        """
        code = (
            "import sys, agent_harness.session.service as m;"
            "print(m.__file__);"
            "loaded=[k for k in sys.modules if k.startswith('agent_harness.web')];"
            "print('LOADED:', loaded);"
            "sys.exit(1 if loaded else 0)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, (
            f"导入领域层时被拖入了 web 模块：\n{proc.stdout}\n{proc.stderr}"
        )

    def test_types_only_reference_to_web_is_the_documented_residual(self):
        """残余的**唯一**一条 `web` 引用必须仍是 TYPE_CHECKING 下的 `RunManager`。

        ADR-0040 §4 R1 记着它：`RunManager` 的模块家在 `web/`（无 HTTP 依赖，
        仅 4 个方法被用到）。哪天它多了新的 web 类型引用，或运行时真的被 import，
        这条守卫就红——那时应当先做决策，而不是让残余悄悄扩大。
        """
        text = (SRC_ROOT / "agent_harness/session/service.py").read_text(encoding="utf-8")
        tree = ast.parse(text)

        # 先收 TYPE_CHECKING 块内的 import 节点（含嵌套），运行时 import 是剩下的那些。
        type_checking_nodes: set[int] = set()
        type_checking_imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
                for inner in ast.walk(node):
                    type_checking_nodes.add(id(inner))
                    if isinstance(inner, ast.ImportFrom):
                        type_checking_imports.add(ast.unparse(inner))

        runtime_web_imports = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("agent_harness.web")
            and id(node) not in type_checking_nodes
        ]
        assert runtime_web_imports == [], "领域层出现运行时 web import"

        residual = "from agent_harness.web.runmanager import ManagedRun, RunManager, Subscriber"
        assert residual in type_checking_imports, "残余的 RunManager 类型引用不见了（先看 ADR-0040 §4 R1）"
