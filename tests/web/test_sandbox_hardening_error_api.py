"""Web diagnostics for sandbox hardening failures."""

import ast
import logging
import re
import traceback
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
    # 按落盘形态取文本：`.exception(...)` 把原文放在 traceback 里，
    # `getMessage()` 看不到它，`format()` 才是 agent.jsonl 里真正写下去的东西。
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    logged = "\n".join(formatter.format(r) for r in records)
    logged += "\n".join(
        "".join(traceback.format_exception(*r.exc_info)) if r.exc_info else ""
        for r in records
    )
    assert "migration_rollback_failed" in logged
    assert "MIGRATE_FATAL rollback failed" in logged  # 原文进日志（r.getMessage 已含 exc）
    # 定位到「哪个请求」，否则一行 reason 帮不上排查。
    assert "POST" in logged and "/api/sessions" in logged, logged
    # 日志有原文 ≠ 响应有原文。
    assert "MIGRATE_FATAL" not in response.text
    assert "agent-harness-log" not in response.text


def test_hardening_reason_whitelist_matches_producer_literals():
    """#1647 P3-3：reason 白名单只有一处定义，且与产生侧的字符串字面量**双向相等**。

    白名单此前抄在 `docker.py`（产生侧）与 `web/domain_errors.py`（消费侧）两处。
    抄两份的失效形态是静默的：`docker.py` 新增一个 reason，响应端不认识它，就降级成
    `hardening_failed`——没有任何门禁会红，客户端只是少了一档分类。

    这里的钉子走 AST 而不是 `grep`：扫 `docker.py` 里所有形如 `(hardening|migration)_*`
    的字符串字面量（raise 点、exit-code 映射、`__init__` 默认值全在内），断言它与
    `_HARDENING_REASONS` 逐值相等。消费侧已 import 同一常量，所以一处改不动两处。
    """
    shape = re.compile(r"(?:hardening|migration)_[a-z_]+")
    module = ast.parse(Path(sandbox_docker.__file__).read_text(encoding="utf-8"))

    # 常量自身那一笔要**排除**在扫描面之外：把它算进去，扫描就变成
    # 「常量等于常量」的恒真命题（变异「往常量里多塞一个没人产生的值」会存活，
    # 实测正是如此）。扫描面 = 产生侧的所有**其它**字面量。
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
    produced = {
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and shape.fullmatch(node.value)
        and node not in set(ast.walk(declaration))
    }
    assert declared == set(_HARDENING_REASONS), (
        "_HARDENING_REASONS 的字面量与导入的常量不一致（模块内有第二份定义？）"
    )
    literals = produced
    assert literals == set(_HARDENING_REASONS), (
        "docker.py 的 reason 字面量与 _HARDENING_REASONS 不一致："
        f"只在产生侧 {sorted(literals - set(_HARDENING_REASONS))} / "
        f"只在常量 {sorted(set(_HARDENING_REASONS) - literals)}"
    )
