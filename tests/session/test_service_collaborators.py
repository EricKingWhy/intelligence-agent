"""#248：领域服务的构造契约 + 单点适配守卫（AC1–AC5 的证据位）。

四条证据分别对应：

- **AC1**：构造参数的**名字集合**与 ADR-0040 §3 的字段清单逐字一致——清单在文档里
  是表格，在这里是 frozenset，两边任一漂移这条就红。
- **AC2/AC4**：构造不经过容器——普通 duck-typed 对象（不是 `AppState`、不是
  `MagicMock`）即可；少一个 collaborator 属性就是 `AttributeError`，没有魔法兜底；
  domain 源码（`session/service.py` / `session/projects.py`）不再在代码或注解里
  命名 `AppState`。
- **AC3**：两个领域服务的**构造点都只允许 `web/app.py`**（传输侧组合根）；
  各自的定义模块只定义类，不构造实例。
- **AC5**：删掉组合根就会重新造成跨层耦合，所以这里锁住"import domain 不 import
  `agent_harness.web.app`"（子进程实测，不受本进程已导入的模块影响）。
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import ClassVar

import pytest

from agent_harness.session.projects import ProjectService
from agent_harness.session.service import SessionService
from agent_harness.web.app import project_service, session_service

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
REPO_ROOT = SRC_ROOT.parent

#: AC1 的字段清单（与 ADR-0040 §3 表格同源；改这里必须同时改文档）。
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


def callee_name(func: ast.expr) -> str:
    """被调用者的**最后一段名字**：`SessionService(...)` 与 `svc.SessionService(...)` 同判。"""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def imported_modules(node: ast.Import | ast.ImportFrom, package: str) -> list[str]:
    """import 语句**涉及的全部**模块名（用于精确判断是否属 `agent_harness.web`）。

    只取第一个别名、或只看 `node.module` 都会漏两种等价写法（审查 findings）：
    `from agent_harness import web`（web 在 names 里、不在 module 里）与
    `import a, b` 里 web 排在第二个。
    `package`（必填，无默认值）= 被扫文件所在包的模块名（如 `agent_harness.session`），
    相对导入（`from .. import web`）按它解成绝对模块名——否则 `level` 被忽略、整类逃过
    扫描（审查 findings）。**刻意不给默认值**：将来新增调用点忘传时应当 `TypeError`
    报出来，而不是静默退回修复前的弱判据（审查 findings 的 P3）。
    """
    if isinstance(node, ast.ImportFrom):
        if node.level:
            parts = package.split(".") if package else []
            prefix = ".".join(parts[: max(len(parts) - (node.level - 1), 0)])
            base = f"{prefix}.{node.module}" if node.module else prefix
        else:
            base = node.module or ""
        return [base, *(f"{base}.{alias.name}" for alias in node.names)]
    return [alias.name for alias in node.names]


def assembly_type_imports(node: ast.Import | ast.ImportFrom, package: str) -> list[str]:
    """import 语句从组合层 `agent_harness.assembly` 带进领域层的**白名单外**名字。

    复用 `imported_modules`：`from agent_harness.assembly import X`、`from agent_harness
    import assembly`、相对导入（`from ..assembly import X` / `from .. import assembly`）、
    多别名 `import` 四种等价写法**一起收**——只看 `node.module` 或 `alias.name` 时，
    后三种能整类绕过守卫（审查 findings 的 P2；本文件 88-107 行的同类漏判第一次审查
    也提过，所以这里必须复用同一个 helper，不另写一遍）。
    白名单只有 `build_runtime`：本票改造**之前**就有的运行时依赖，且被
    `tests/web/test_web_phase5_permission.py` 的 `monkeypatch.setattr(service_module,
    "build_runtime", …)` 钉在模块级名字上，动它属 Scope 外。
    """
    allowed = {"build_runtime"}
    modules = imported_modules(node, package)
    if isinstance(node, ast.Import):
        return [m for m in modules if m.startswith("agent_harness.assembly")]
    offenders = []
    for leaf in modules[1:]:
        if not leaf.startswith("agent_harness.assembly"):
            continue
        if leaf == "agent_harness.assembly" or leaf.rsplit(".", 1)[-1] not in allowed:
            offenders.append(leaf)
    return offenders


def is_web_module(module: str) -> bool:
    """是否属 `agent_harness.web` 包**本身**。

    按完整模块路径判，不用子串：`agent_harness.websearch` 是另一个模块（能力层），
    `"agent_harness.web" in module` 会把它误当传输层。
    """
    return module == "agent_harness.web" or module.startswith("agent_harness.web.")


def is_type_checking_test(test: ast.expr) -> bool:
    """`if TYPE_CHECKING:` 的**正向**形态。

    子串判据（`"TYPE_CHECKING" in ast.unparse(test)`）会把 `if not TYPE_CHECKING:`
    也算进去，那等于把一条**真运行时** import 当成类型引用豁免掉（审查 findings）。
    只认裸名字 `TYPE_CHECKING` 与 `typing.TYPE_CHECKING` 两种正向写法：任意
    `X.TYPE_CHECKING` 属性（如 `settings.TYPE_CHECKING`）一律不算——那同样会让
    一条真运行时 import 落进豁免块（审查 findings 的 P3）。
    """
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return (
            test.attr == "TYPE_CHECKING"
            and isinstance(test.value, ast.Name)
            and test.value.id == "typing"
        )
    return False


class TestConstructionContract:
    def test_session_service_takes_exactly_the_documented_collaborators(self):
        params = set(inspect.signature(SessionService.__init__).parameters) - {"self"}
        assert params == SESSION_SERVICE_COLLABORATORS, (
            "构造契约变了：ADR-0040 §3 的字段清单必须同步"
        )

    def test_project_service_takes_exactly_the_documented_collaborators(self):
        params = set(inspect.signature(ProjectService.__init__).parameters) - {"self"}
        assert params == PROJECT_SERVICE_COLLABORATORS, (
            "构造契约变了：ADR-0040 §3 的字段清单必须同步"
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
        属性形式（`svc.SessionService(...)`）与裸名字形式**都**算构造点：本仓两种写法
        并存（`artifacts.build_read_artifact_store(...)` 是属性形式）。
        """
        composition_root = "agent_harness/web/app.py"
        targets = {"SessionService", "ProjectService"}
        found: dict[str, set[str]] = {name: set() for name in targets}

        for py in sorted(SRC_ROOT.rglob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            rel = str(py.relative_to(SRC_ROOT)).replace("\\", "/")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = callee_name(node.func)
                if called in targets:
                    found[called].add(rel)

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

    #: 领域文件的 web 类型引用**登记表**（ADR-0040 §4 R1）：集合必须恰等于这里。
    #: 2026-09-22 用户裁决「搬到 `session/`」后 R1 已闭合 ⇒ 三份域文件**全为空集**，
    #: 即"领域层（代码 + 注解）零 web 引用"；任一条重新出现都会让集合不等而变红。
    #: 多一条 = 残余复活；改这里必须同时改 ADR 与裁决，不能顺手改守卫。
    EXPECTED_TYPE_ONLY_WEB_IMPORTS: ClassVar[dict[str, set[str]]] = {
        "agent_harness/session/service.py": set(),
        "agent_harness/session/projects.py": set(),
        # R1 的产物本身也是域文件：`RunManager` 搬进来后不得反向 import web
        # （它被 `web.app` 在模块级 import，反向依赖会当场成环）。
        "agent_harness/session/runmanager.py": set(),
    }

    def test_types_only_reference_to_web_is_the_documented_residual(self):
        """**残余已归零**：三份域文件（代码 + TYPE_CHECKING）都不得引用 `web`。

        命名保留（ADR-0040 §5 的红证表按此名留痕）；语义随 2026-09-22 用户裁决更新：
        R1 闭合后判据从"残余恰等于登记值"变成"登记值全为空集"——`RunManager` 的家已搬到
        `agent_harness/session/runmanager.py`，`service.py` 里那条 TYPE_CHECKING 引用随之删除。
        `import agent_harness.web.app` 与 `from agent_harness.web import app` 语义等价，
        两种写法一起收（`ast.Import` / `ast.ImportFrom` + 全部别名），否则强度就取决于
        写法；相对导入（`from .. import web`）按被扫文件所在包解成绝对模块名；
        模块名按完整路径判（`is_web_module`），兄弟模块 `agent_harness.websearch`
        不算；`if TYPE_CHECKING:` 只认正向形态（`is_type_checking_test`），
        `if not TYPE_CHECKING:` 里的 import 仍是运行时 import。
        """
        assert set(self.EXPECTED_TYPE_ONLY_WEB_IMPORTS) == {
            "agent_harness/session/service.py",
            "agent_harness/session/projects.py",
            "agent_harness/session/runmanager.py",
        }, "登记表被清空或改名 ⇒ 这条守卫会静默变成空转（审查 findings）"

        for rel, residual in self.EXPECTED_TYPE_ONLY_WEB_IMPORTS.items():
            tree = ast.parse((SRC_ROOT / rel).read_text(encoding="utf-8"))
            package = str(PurePosixPath(rel).parent).replace("/", ".")

            # 先收 TYPE_CHECKING 块内的 import 节点（含嵌套），运行时 import 是剩下的那些。
            type_checking_nodes: set[int] = set()
            type_checking_imports: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.If) and is_type_checking_test(node.test):
                    for inner in ast.walk(node):
                        type_checking_nodes.add(id(inner))
                        if isinstance(inner, (ast.Import, ast.ImportFrom)) and any(
                            is_web_module(m) for m in imported_modules(inner, package)
                        ):
                            type_checking_imports.add(ast.unparse(inner))

            runtime_web_imports = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                and any(is_web_module(m) for m in imported_modules(node, package))
                and id(node) not in type_checking_nodes
            ]
            assert runtime_web_imports == [], f"{rel} 出现运行时 web import"

            assert type_checking_imports == residual, (
                f"{rel} 的 TYPE_CHECKING web 引用集合变了：{sorted(type_checking_imports)}"
                f"（登记值 {sorted(residual)}；增减都要先更新 ADR-0040 §4 R1 / 裁决，"
                "不能顺手改守卫）"
            )

    def test_run_manager_home_is_the_session_package(self):
        """R1（用户裁决 2026-09-22）：`RunManager` 的家 = `session/runmanager.py`。

        纯移位，运行时零影响；这条钉住"家在哪"，免得后来者把它挪回传输层
        （挪回去会让上一条守卫立刻红，但那是间接信号——这里给直接的判据）。
        """
        moved = SRC_ROOT / "agent_harness/session/runmanager.py"
        assert moved.is_file(), (
            "RunManager 的家应是 agent_harness/session/runmanager.py"
            "（ADR-0040 §4 R1 已按用户裁决闭合）"
        )
        assert not (SRC_ROOT / "agent_harness/web/runmanager.py").exists(), (
            "旧路径 web/runmanager.py 不得复活——RunManager 是领域模块"
        )

    def test_domain_store_port_is_satisfied_by_composition_bundle(self, tmp_path: Path):
        """R2（用户裁决 2026-09-22）：组合层造的真实束必须满足领域端口。

        端口 = `RecoveryStoreBundle`（`session/service.py` 自建，只声明装配层真正读的
        四个成员）。这条是**结构兼容**判据：`assembly.RecoveryStores` 改成员名/删成员
        会让它红——那时要么改装配层，要么改端口，不能让它悄悄漂移。
        """
        from agent_harness.assembly import recovery_stores
        from agent_harness.session.service import RecoveryStoreBundle

        bundle = recovery_stores(tmp_path / "harness.db")
        assert isinstance(bundle, RecoveryStoreBundle), (
            "assembly 造的 RecoveryStores 不再满足领域端口 RecoveryStoreBundle"
        )

    def test_domain_files_import_no_composition_types(self):
        """R2：三份域文件都不得 import 组合层**类型**；唯一允许的是 `build_runtime`。

        `build_runtime` 是既有耦合（改造前就在，且
        `tests/web/test_web_phase5_permission.py` 用 `monkeypatch.setattr(service_module,
        "build_runtime", …)` 把它钉在模块级名字上），不在本票范围。本票收口的是
        `RecoveryStores` 那一条——它现在是领域自建的端口，不该再出现在 import 里。
        扫**三份**域文件而不是只扫 `service.py`：`projects.py` / `runmanager.py` 今天
        都没有这类 import，但只守一处等于把另两处的缺口留给下一个人（审查 findings P3）。
        判据本体在 `assembly_type_imports`（收全四种等价写法）。
        """
        offenders: list[str] = []
        for rel in sorted(self.EXPECTED_TYPE_ONLY_WEB_IMPORTS):
            package = str(PurePosixPath(rel).parent).replace("/", ".")
            tree = ast.parse((SRC_ROOT / rel).read_text(encoding="utf-8"))
            offenders += [
                f"{rel}:{node.lineno}: {ast.unparse(node)}"
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                and assembly_type_imports(node, package)
            ]
        assert offenders == [], (
            f"领域文件又 import 了组合层类型：{offenders}"
            "（R2 已改为领域自建端口 RecoveryStoreBundle：装配层造束、领域只认形状）"
        )

    def test_composition_import_forms_are_all_considered(self):
        """钉住上一条判据的**强度**：四种等价写法一起收，否则强度取决于写法。

        审查 findings 的 P2：最初的实现只看 `node.module` 与 `alias.name`，于是
        `from agent_harness import assembly`、`from ..assembly import RecoveryStores`、
        `from .. import assembly` 三种写法**整类绕过**守卫（同文件 88-107 行的
        `imported_modules` 早已为 web 守卫处理过同一类漏判）。
        """
        package = "agent_harness.session"
        cases = {
            "from agent_harness.assembly import RecoveryStores": True,
            "from agent_harness.assembly import build_runtime": False,  # 白名单
            "from agent_harness.assembly import build_runtime, RecoveryStores": True,
            "import agent_harness.assembly": True,
            "import agent_harness.assembly as a": True,
            "from agent_harness import assembly": True,
            "from ..assembly import RecoveryStores": True,
            "from .. import assembly": True,
            "from agent_harness.storage.operation import OperationLedger": False,
        }
        for source, expected in cases.items():
            node = ast.parse(source).body[0]
            got = bool(assembly_type_imports(node, package))
            assert got is expected, f"{source!r} 判成 {got}，应为 {expected}"

    def test_web_module_match_is_not_a_substring_test(self):
        """钉住判据的**精度**：`agent_harness.websearch` 是兄弟模块，不是传输层。

        子串判据（`"agent_harness.web" in module`）会给它误报——那时守卫的"绿"
        就不再等于"领域没碰传输层"，而是"领域没碰名字里带 web 的东西"。
        """
        assert is_web_module("agent_harness.web")
        assert is_web_module("agent_harness.web.app")
        assert not is_web_module("agent_harness.websearch")
        assert not is_web_module("agent_harness.websearch.tool")
        assert not is_web_module("my_agent_harness.web")

    def test_import_statement_forms_are_all_considered(self):
        """两种等价写法都要算 web 引用：`from agent_harness import web` 与多别名 `import`。

        只取 `names[0]`（`import a, b` 里 web 排第二）或只看 `node.module`
        （`from agent_harness import web`）都会漏（审查 findings 的 P3）。
        相对导入段（`package=` 那几行）同理：`level` 被忽略时整类逃过扫描。
        """
        cases = {
            "import agent_harness.web.app": True,
            "import agent_harness.websearch, agent_harness.web.app": True,
            "import agent_harness.web.app, agent_harness.websearch": True,
            "from agent_harness import web": True,
            "from agent_harness.web import app": True,
            "from .. import web": True,
            "from ..web.app import AppState": True,
            "import agent_harness.websearch": False,
            "from agent_harness import websearch": False,
            "from agent_harness.websearch import protocol": False,
            "from . import web": False,
            "from .web import app": False,
        }
        package = "agent_harness.session"  # 被扫的两个领域文件都在这个包下
        for source, expected in cases.items():
            node = ast.parse(source).body[0]
            assert isinstance(node, (ast.Import, ast.ImportFrom))
            got = any(is_web_module(m) for m in imported_modules(node, package))
            assert got is expected, f"{source!r} 判成 {got}，应为 {expected}"

    def test_type_checking_test_must_be_positive(self):
        """`if TYPE_CHECKING:` 才豁免；`if not TYPE_CHECKING:` 与任意 `X.TYPE_CHECKING` 都不是。

        子串判据把前者一并豁免 ⇒ 那条 import 是真运行时 import 却逃过扫描；
        只看 `attr` 的宽判据同样会被 `settings.TYPE_CHECKING` 那类写法钻（审查 findings）。
        """
        assert is_type_checking_test(ast.parse("TYPE_CHECKING").body[0].value)
        assert is_type_checking_test(ast.parse("typing.TYPE_CHECKING").body[0].value)
        assert not is_type_checking_test(ast.parse("not TYPE_CHECKING").body[0].value)
        assert not is_type_checking_test(ast.parse("settings.TYPE_CHECKING").body[0].value)
        assert not is_type_checking_test(ast.parse("True").body[0].value)
