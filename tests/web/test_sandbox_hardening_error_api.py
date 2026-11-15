"""Web diagnostics for sandbox hardening failures."""

import ast
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agent_harness.sandbox.docker as sandbox_docker
from agent_harness.config import Settings
from agent_harness.sandbox.capabilities import SandboxUnavailableError
from agent_harness.sandbox.docker import (
    _HARDENING_REASONS,
    DockerSandbox,
    SandboxHardeningError,
)
from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.web.app import create_app


def test_create_reports_volume_hardening_failure(tmp_path, monkeypatch):
    def fail_create(self, *args, **kwargs):
        raise SandboxHardeningError(
            "token=secret123 /workspace/private-file",
            volume_name="test-volume", reason="migration_failed",
        )

    monkeypatch.setattr(WorkspaceRegistry, "create", fail_create)
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/sessions?launch=false", json={"sandbox_backend": "local"}
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["volume"] is None
    assert detail["reason"] == "migration_failed"
    assert "secret123" not in response.text
    assert "/workspace/private-file" not in response.text
    assert detail["remediation"]


def test_resume_reports_hardening_failure(tmp_path, monkeypatch):
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        created = client.post(
            "/api/sessions?launch=false", json={"sandbox_backend": "local"}
        )
        assert created.status_code == 200
        sid = created.json()["session_id"]

        def fail_get(self, *args, **kwargs):
            raise SandboxHardeningError(
                "private migration log", volume_name="resume-volume", reason="migration_failed"
            )

        monkeypatch.setattr(WorkspaceRegistry, "get", fail_get)
        response = client.post(f"/api/sessions/{sid}/resume", json={"task": "continue"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["volume"] is None
    assert detail["reason"] == "migration_failed"
    assert "private migration log" not in response.text
    assert detail["remediation"]


def test_unavailable_and_unexpected_errors_keep_existing_status(tmp_path, monkeypatch):
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        def unavailable(self, *args, **kwargs):
            raise SandboxUnavailableError("docker", "not running", remediation="start Docker")

        monkeypatch.setattr(WorkspaceRegistry, "create", unavailable)
        response = client.post("/api/sessions?launch=false", json={"sandbox_backend": "local"})
        assert response.status_code == 409
        assert response.json()["detail"]["reason"] == "not running"
        assert response.json()["detail"]["remediation"] == "start Docker"

        def unexpected(self, *args, **kwargs):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(WorkspaceRegistry, "create", unexpected)
        response = client.post("/api/sessions?launch=false", json={"sandbox_backend": "local"})
        assert response.status_code == 500
        assert response.json() == {"detail": "Internal Server Error"}


@pytest.mark.parametrize(
    ("phase", "expected_reason"),
    [("start", "migration_helper_start_failed"), ("wait", "migration_wait_failed"),
     ("exit", "migration_failed"), ("rollback", "migration_rollback_failed"),
     ("restore", "migration_backup_restore_failed")],
)
def test_real_migration_failure_is_safe_and_cleans_up(tmp_path, monkeypatch, phase, expected_reason):
    import docker

    removed = []
    main_container = SimpleNamespace(remove=lambda **kwargs: removed.append("main"))
    def wait(**kwargs):
        if phase == "wait":
            raise TimeoutError("token=secret123 /workspace/private-file")
        return {"StatusCode": {"rollback": 96, "restore": 89}.get(phase, 1)}

    helper = SimpleNamespace(
        wait=wait,
        logs=lambda **kwargs: b"MIGRATE_ERROR token=secret123 /workspace/private-file",
        remove=lambda **kwargs: removed.append("helper"),
        stop=lambda **kwargs: removed.append("stop"),
    )

    def absent(name):
        raise docker.errors.NotFound("not found")

    def run(*args, **kwargs):
        if phase == "start":
            raise RuntimeError("token=secret123 /workspace/private-file")
        return helper

    fake_client = SimpleNamespace(
        containers=SimpleNamespace(get=absent, create=lambda *a, **k: main_container, run=run, list=lambda **k: []),
        images=SimpleNamespace(get=lambda image: SimpleNamespace(attrs={"Config": {"User": "1000:1000"}})),
        volumes=SimpleNamespace(get=lambda name: object()),
    )
    monkeypatch.setattr(docker, "from_env", lambda **kwargs: fake_client)

    def fail_create(self, *args, **kwargs):
        sandbox = DockerSandbox(image="test-image", container_name="test-container", volume_name="test-volume")
        sandbox.ensure_started()

    monkeypatch.setattr(WorkspaceRegistry, "create", fail_create)
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/sessions?launch=false", json={"sandbox_backend": "local"})
    assert response.status_code == 409
    assert response.json()["detail"]["volume"] is None
    assert response.json()["detail"]["reason"] == expected_reason
    assert "secret123" not in response.text
    assert "/workspace/private-file" not in response.text
    if phase in {"rollback", "restore", "wait"}:
        assert "before retry" in response.json()["detail"]["remediation"]
    assert removed == (["main"] if phase == "start" else
                       ["stop", "helper", "main"] if phase == "wait" else ["helper", "main"])


@pytest.mark.parametrize("volume", ["/private/path", "sk-proj-0123456789abcdef0123456789abcdef",
                                    "ghp_123456789012345678901234567890123456"])
def test_untrusted_metadata_is_not_echoed(tmp_path, monkeypatch, volume):
    def fail_create(self, *args, **kwargs):
        raise SandboxHardeningError("secret", volume_name=volume, reason="secret-category")

    monkeypatch.setattr(WorkspaceRegistry, "create", fail_create)
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/sessions?launch=false", json={"sandbox_backend": "local"})
    assert response.status_code == 409
    assert response.json()["detail"]["volume"] is None
    assert response.json()["detail"]["reason"] == "hardening_failed"
    assert "secret" not in response.text
    assert "/private/path" not in response.text
    assert volume not in response.text


def test_long_valid_volume_is_not_exposed(tmp_path, monkeypatch):
    volume = "agent-harness-" + "a" * 128

    def fail_create(self, *args, **kwargs):
        raise SandboxHardeningError("private", volume_name=volume, reason="migration_failed")

    monkeypatch.setattr(WorkspaceRegistry, "create", fail_create)
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/sessions?launch=false", json={"sandbox_backend": "local"})
    assert response.status_code == 409
    assert response.json()["detail"]["volume"] is None
    assert volume not in response.text


def test_hardening_error_subclass_keeps_structured_409(tmp_path, monkeypatch):
    """#1647 P2-1：子类也必须拿到 409 + 结构化诊断，不能 KeyError 成未分类 500。

    Starlette 的 `exception_handler` 按 **MRO** 派发（父类 handler 接得住子类），而
    `sandbox_hardening_http_error` 曾用 `type(exc)` 精确索引 ⇒ 子类进得去 handler、
    却在查表时 KeyError，被通用 500 兜底。注册面比查表面宽就是缺陷本身：用户拿到的
    正是本票要消灭的那条未分类 500。`memory_http_error`（#1440 P2-1）是同款先例。

    用例放在本文件而非 `test_domain_error_mapping.py`：那一份钉的是 `http_error` 用的
    `_DOMAIN_ERROR_STATUS` 表，本文件钉的是 sandbox 409 这条**独立**的响应契约
    （reason 白名单 / volume 恒 null / remediation 都在这里），子类是同一契约的变体。
    """

    class SpecialHardeningError(SandboxHardeningError):
        pass

    def fail_create(self, *args, **kwargs):
        raise SpecialHardeningError(
            "token=secret123 /workspace/private-file",
            volume_name="subclass-volume",
            reason="migration_rollback_failed",
        )

    monkeypatch.setattr(WorkspaceRegistry, "create", fail_create)
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/sessions?launch=false", json={"sandbox_backend": "local"}
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "migration_rollback_failed"
    assert detail["volume"] is None
    assert detail["remediation"]
    assert "secret123" not in response.text
    assert "/workspace/private-file" not in response.text


class _CollectingHandler(logging.Handler):
    """收集 `agent_harness.web` 的记录。

    不用 `caplog`：`create_app` 的 lifespan 会调 `setup_logging`，而它**清空 root
    handler**（`logging.py` 幂等约定），pytest 挂在 root 上的捕获 handler 也一并被清掉
    ⇒ caplog 恒空。挂在本 logger 自己的 handler 上不受影响。
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_hardening_failure_is_logged_server_side(tmp_path, monkeypatch):
    """#1647 P2-1：409 响应面不写原文，但服务端日志必须留痕。

    本票的 handler 替换掉的是通用 500 通道（`logging...exception(...)`，ERROR +
    traceback）。只删日志不补就是运维面回归：`str(exc)` 里恰好是回滚失败时最需要的
    那串 `MIGRATE_FATAL` / exit code，而 `model/failure.py` 对用户承诺「完整原始信息
    见后端日志」。这条用例同时锁住两件事：日志里有原文，响应体里没有。
    """

    def fail_create(self, *args, **kwargs):
        raise SandboxHardeningError(
            "MIGRATE_FATAL rollback failed",
            volume_name="agent-harness-log",
            reason="migration_rollback_failed",
        )

    monkeypatch.setattr(WorkspaceRegistry, "create", fail_create)
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    collector = _CollectingHandler()
    web_logger = logging.getLogger("agent_harness.web")
    web_logger.addHandler(collector)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/sessions?launch=false", json={"sandbox_backend": "local"}
            )
    finally:
        web_logger.removeHandler(collector)

    assert response.status_code == 409
    records = [r for r in collector.records if r.levelno >= logging.ERROR]
    assert records, "409 路径必须在 agent_harness.web 留 ERROR 级日志"
    # 断言按 .jsonl 里真正落盘的形态取：`formatter.format(r)` 对带 `exc_info` 的记录
    # **已经**包含 traceback（`Formatter.format` 内部调 `formatException`），也就是
    # `getMessage()` 看不到、但落到 agent.jsonl 里的那部分。它才是"原文有没有留下"
    # 的判据；单看 `getMessage()` 会漏掉 traceback 里那段。
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    logged = "\n".join(formatter.format(r) for r in records)
    assert "migration_rollback_failed" in logged
    # 原文在 traceback 里，落盘形态（`format()`）看得见。
    assert "MIGRATE_FATAL rollback failed" in logged
    # 与上一条分开：原文能被看见的前提是记录带了 exc_info。丢了它，上面的断言也会红，
    # 但红了以后读的人分不清是「换了 logger 名」还是「丢了 traceback」。
    assert all(r.exc_info for r in records), "ERROR 日志必须带 exc_info（否则原文不在落盘内容里）"
    # 定位到「哪个请求」，否则一行 reason 帮不上排查。
    assert "POST" in logged and "/api/sessions" in logged, logged
    # 日志有原文 ≠ 响应有原文。
    assert "MIGRATE_FATAL" not in response.text
    assert "agent-harness-log" not in response.text


def _enclosing_function(module: ast.Module, node: ast.AST) -> ast.AST | None:
    """`node` 最内层的所属函数（动态站点的名字解析范围就到这里）。"""
    parents = {
        child: parent for parent in ast.walk(module) for child in ast.iter_child_nodes(parent)
    }
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current
        current = parents.get(current)
    return None


def test_hardening_reason_whitelist_matches_producer_call_sites():
    """#1647 P3-3 / #1678 R2-F1：reason 白名单只有一处定义，且与**产生侧调用点**双向相等。

    白名单此前抄在 `docker.py`（产生侧）与 `web/domain_errors.py`（消费侧）两处。
    抄两份的失效形态是静默的：`docker.py` 新增一个 reason，响应端不认识它，就降级成
    `hardening_failed`——没有任何门禁会红，客户端只是少了一档分类。

    扫描面取**本模块的产生点**，不是名字形状：早先的版本用 `(hardening|migration)_[a-z_]+`
    正则界定扫描面，于是「新增一个不以此开头的 reason」既不进扫描面、也不引发差集
    非空（加法变异 `95: "oom_killed"` 存活 15 passed）。reason 的取值域是
    `SandboxHardeningError.reason`，与名字怎么拼无关，所以这里改从 AST 取
    `raise SandboxHardeningError(...)` 的 `reason=` 实参，加上 `__init__` 的 kwonly
    默认值（不传 reason 的 raise 站点取的就是它）。

    扫描面有**三条明示边界**，越界的产生点不会被这条钉子看见：
    ① 只扫 `sandbox/docker.py` 一个文件——别的模块新增 `raise` 站点不在此列（本类目前
    只在该文件被抛出；扩到全仓会连测试文件里那些故意非法的 reason 一起要求登记）；
    ② 被调用者只认裸名 `SandboxHardeningError` 与**模块级别名**（`_Err = SandboxHardeningError`）；
    子类名与属性写法（`docker.SandboxHardeningError`）不在此列；
    ③ 只认**显式** `reason=` 关键字：`**{...}` 展开无法闭合扫描面，所以它不是"漏扫"而是
    直接红（见下方 `assert not any(k.arg is None ...)`）。

    动态站点（`reason=reason` 变量传参）**解析而不是豁免**：它的可能值由所属函数里给
    这个变量赋的值决定（现状是那张 exit-code 字典），所以扫的是赋值右值里的字符串
    常量。解不出任何字符串就直接红——"扫不到就算了"正是旧版失效的形态。这里做的是
    等式闭合，不是控制流分析：如果哪天有人把 reason 改成从别处算出来的，这条断言会
    先红，逼着重写契约，而不是让新值静悄悄地从扫描面漏过去。
    """
    module = ast.parse(Path(sandbox_docker.__file__).read_text(encoding="utf-8"))

    # 不传 reason 的 raise 站点取 `__init__` 的默认值。
    default_kwonly = next(
        kw_default
        for cls in module.body
        if isinstance(cls, ast.ClassDef) and cls.name == "SandboxHardeningError"
        for func in cls.body
        if isinstance(func, ast.FunctionDef) and func.name == "__init__"
        for arg, kw_default in zip(func.args.kwonlyargs, func.args.kw_defaults)
        if arg.arg == "reason"
    )
    assert isinstance(default_kwonly, ast.Constant), default_kwonly
    default = default_kwonly.value

    # 产生点也可能经模块级别名抛出（`_Err = SandboxHardeningError` 后 `raise _Err(...)`）：
    # 认得出的别名一律进扫描面。只认模块级的直接赋值（右值是 `ast.Name`），不做流敏感
    # 分析——链式别名按文件顺序收敛。
    producers = {"SandboxHardeningError"}
    for stmt in module.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        if isinstance(value, ast.Name) and value.id in producers:
            producers |= {t.id for t in targets if isinstance(t, ast.Name)}

    literals: set[str] = set()
    dynamic: list[int] = []
    for node in ast.walk(module):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in producers
        ):
            continue
        # `**{...}` 展开闭合不了扫描面：`reason` 可能就藏在被展开的字典里，而 `k.arg`
        # 为 `None` 会让下面取不到实参、被当成"没传 reason"而落到默认值上（那是**漏报**，
        # 与"解不出就红"相反）。所以这里直接红，要求写成显式关键字。
        assert not any(k.arg is None for k in node.keywords), (
            f"{node.lineno}: `**` 展开的实参闭合不了扫描面（reason 可能藏在里面），"
            "请写成显式 `reason=` 关键字"
        )
        passed = next((k.value for k in node.keywords if k.arg == "reason"), None)
        if passed is None:
            literals.add(default)
            continue
        if isinstance(passed, ast.Constant) and isinstance(passed.value, str):
            literals.add(passed.value)
            continue
        # 动态站点：`reason=<变量>`。它的可能值由**同一函数作用域里**给该名字赋的值
        # 决定（现状是那张 exit-code 字典）；解析不出任何字符串就红，不当作"扫不到"。
        dynamic.append(node.lineno)
        scope = _enclosing_function(module, node)
        assert scope is not None and isinstance(passed, ast.Name), (
            f"{node.lineno}: `reason=` 既不是字符串字面量也不是作用域内变量，扫描面无法闭合"
        )
        assignments = [
            stmt.value
            for stmt in ast.walk(scope)
            if isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == passed.id for t in stmt.targets)
        ]
        names = {
            sub.value
            for value in assignments
            for sub in ast.walk(value)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
        }
        assert names, (
            f"{node.lineno}: 动态站点的 `reason={passed.id}` 在作用域内没有字符串赋值，"
            "无法闭合扫描面"
        )
        literals.update(names)

    # 常量自身那一笔要**排除**在扫描面之外：把它算进去，扫描就变成「常量等于常量」
    # 的恒真命题（变异「往常量里多塞一个没人产生的值」会存活，实测正是如此）。
    declaration = next(
        node
        for node in module.body
        if isinstance(node, ast.AnnAssign | ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_HARDENING_REASONS"
            for t in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        )
    )
    declared = {
        node.value
        for node in ast.walk(declaration.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert declared == set(_HARDENING_REASONS), (
        "_HARDENING_REASONS 的字面量与导入的常量不一致（模块内有第二份定义？）"
    )
    assert dynamic, (
        "预期 `_migrate_volume_ownership` 里有一处 reason 由 exit-code 字典推导"
        "（`reason=reason`）——扫描面为零说明这处推导消失了，请复核 expectation"
    )
    assert literals == set(_HARDENING_REASONS), (
        "产生侧的 reason 取值与 _HARDENING_REASONS 不一致："
        f"只在产生侧 {sorted(literals - set(_HARDENING_REASONS))} / "
        f"只在常量 {sorted(set(_HARDENING_REASONS) - literals)}"
        f"（动态站点行号：{dynamic}）"
    )
