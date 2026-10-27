"""#1487：永久允许规则迁出工作区 + 控制目录对 agent 执行进程不可写——TDD 红阶段。

裁决 24-A（card-24）：迁出工作区 + 控制目录对 agent **真正不可写**；验收口径 =
执行进程确实写不到，而非文件搬家；无隔离 local 的同权残余如实标注，不声称全
模式通过。

对标（PORT DESIGN，票面 2026-10-23 已核）：Claude Code protected paths
（https://code.claude.com/docs/en/sandboxing#protected-paths）——保护对象是
**实际执行进程**而非目录名；per-project 用户级存储形态同 ``~/.claude/projects``。

新契约（本文件红阶段引用尚不存在的符号 / 参数，当前应为 AttributeError /
TypeError 红；实现后按本文件行为契约转绿）：

- ``default_control_root()``：默认 ``Path.home()/.agent-harness/approve-policy``；
  env ``AGENT_HARNESS_APPROVE_POLICY_ROOT`` 覆盖（测试隔离 / 部署钉根的唯一缝）；
- ``project_key(project_root)``：resolved 根路径的 sha256 hex（跨平台文件名安全）；
- ``ApprovePolicyStore(project_root, *, control_root=None)``：``.path`` 落控制目录
  （**不在** project_root 之下）；``.legacy_path`` = 旧工作区内路径；``.legacy_present``；
  ``.import_legacy() -> LegacyImportResult(imported, skipped)``：**显式**严格导入
  （load 永不自动信任旧位——旧位在 agent 可达空间，静默导入 = 信任 agent 自建文件）。
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path

import pytest

from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tooling.approval import (
    ApprovalIdentity,
    ApprovalRequest,
    ApprovalResponse,
    PermissionDecision,
)
from agent_harness.tooling.approve_policy import (
    ApprovePolicyRule,
    ApprovePolicyStore,
    LegacyImportError,
    LegacyImportResult,
    PolicyGranularity,
    default_control_root,
    project_key,
)
from agent_harness.tooling.contract import PermissionPolicy, ToolPermission
from agent_harness.tools import BashTool, EditTool, WriteTool

_ENV_OVERRIDE = "AGENT_HARNESS_APPROVE_POLICY_ROOT"
_LEGACY_REL = Path(".agent-harness") / "approve-policy.json"


# ── helpers ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _control_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把默认控制根钉到 tmp（env 覆盖缝）：本文件绝不触碰真实 home。"""
    control = tmp_path / "control-home"
    monkeypatch.setenv(_ENV_OVERRIDE, str(control))
    return control


def _identity(
    *,
    tool_name: str = "bash",
    canonical: str = "npm run build",
    args_hash: str = "h1",
    permission: ToolPermission = ToolPermission.WORKSPACE_WRITE,
) -> ApprovalIdentity:
    return ApprovalIdentity(
        tool_name=tool_name,
        kind="COMMAND",
        canonical=canonical,
        args_hash=args_hash,
        permission=permission,
        policy_at_approval=PermissionPolicy.WORKSPACE_WRITE,
        contract_version="v1",
    )


def _rule(
    identity: ApprovalIdentity, granularity: PolicyGranularity, *, rule_id: str = "r1",
    created_at: str = "2026-10-05T00:00:00+00:00",
) -> ApprovePolicyRule:
    key = identity.key() if granularity is PolicyGranularity.EXACT else identity.canonical
    return ApprovePolicyRule(
        id=rule_id,
        tool=identity.tool_name,
        key=key,
        granularity=granularity,
        permission_at_approval=identity.permission,
        created_at=created_at,
    )


def _seed_legacy(project_root: Path, entries: list[dict]) -> Path:
    """在旧工作区路径手写规则文件（迁移源；模拟升级前的既有文件）。"""
    target = project_root / _LEGACY_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"rules": entries}, ensure_ascii=False), encoding="utf-8")
    return target


def _legacy_entry(rule: ApprovePolicyRule) -> dict:
    return rule.to_dict()


class _PolicyApproval:
    """恒返回 APPROVE_POLICY 的回调（既有文件同款）。"""

    def __init__(self, granularity: PolicyGranularity = PolicyGranularity.EXACT) -> None:
        self.calls = 0
        self._granularity = granularity

    async def __call__(self, _req: ApprovalRequest) -> ApprovalResponse:
        self.calls += 1
        return ApprovalResponse(
            approved=True,
            decision=PermissionDecision.APPROVE_POLICY,
            policy_granularity=self._granularity,
        )


def _registry(sandbox: LocalSubprocessSandbox) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(BashTool(sandbox))
    return registry


# ── 存储位置：迁出工作区 ────────────────────────────────────────────────────


def test_store_path_lives_outside_project_root(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    store = ApprovePolicyStore(project)
    assert store.path.parent == default_control_root()
    assert project not in store.path.parents
    assert store.path.name == f"{project_key(project)}.json"


def test_default_control_root_env_override_and_home_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_ENV_OVERRIDE, raising=False)
    assert default_control_root() == Path.home() / ".agent-harness" / "approve-policy"
    pinned = tmp_path / "pinned"
    monkeypatch.setenv(_ENV_OVERRIDE, str(pinned))
    assert default_control_root() == pinned
    # P4-1：空串（部署变量展开失败产物）不当作覆盖——回落默认并留痕，不静默丢钉根。
    monkeypatch.setenv(_ENV_OVERRIDE, "")
    assert default_control_root() == Path.home() / ".agent-harness" / "approve-policy"
    # B 轴 P3-1：纯空白 / 相对路径同样拒绝——相对根随 cwd 解析会把控制文件放回
    # agent 领地（cwd 即项目根），与构造参数空值同口径（忽略 + 回落默认）。
    monkeypatch.setenv(_ENV_OVERRIDE, "   ")
    assert default_control_root() == Path.home() / ".agent-harness" / "approve-policy"
    monkeypatch.setenv(_ENV_OVERRIDE, "relative-control-dir")
    assert default_control_root() == Path.home() / ".agent-harness" / "approve-policy"
    # `~` 前缀展开（env 值不经过 shell）。
    monkeypatch.setenv(_ENV_OVERRIDE, "~/.z1487-pinned-control")
    assert default_control_root() == Path.home() / ".z1487-pinned-control"


def test_explicit_control_root_param_wins(tmp_path: Path) -> None:
    project, explicit = tmp_path / "proj", tmp_path / "explicit"
    project.mkdir()
    store = ApprovePolicyStore(project, control_root=explicit)
    assert store.path.parent == explicit


@pytest.mark.parametrize("empty", ["", Path(), "."])
def test_empty_control_root_param_rejected(tmp_path: Path, empty) -> None:
    """P3-2：空 control_root 的 Path 语义是「当前目录」= 把控制文件放回 agent 领地。"""
    project = tmp_path / "proj"
    project.mkdir()
    with pytest.raises(ValueError, match="agent 领地"):
        ApprovePolicyStore(project, control_root=empty)


def test_three_production_sites_share_one_control_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """executor / web / cli 三处构造点必须落同一份控制文件（读写同一真相）。"""
    from agent_harness.cli import _approve_policy_store
    from agent_harness.web.app import approve_policy_store

    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    expected = ApprovePolicyStore(project).path

    sandbox = LocalSubprocessSandbox(workspace_root=project)
    executor = ToolExecutor(
        _registry(sandbox), policy=PermissionPolicy.READ_ONLY,
        approval_callback=_PolicyApproval(), project_root=project,
    )
    assert executor._approve_policy_store.path == expected
    assert _approve_policy_store(project).path == expected
    assert approve_policy_store().path == expected


def test_save_and_load_only_touches_control_location(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    store = ApprovePolicyStore(project)
    store.add_rule(_rule(_identity(), PolicyGranularity.EXACT))
    assert store.path.exists()
    assert len(store.load()) == 1
    assert not (project / ".agent-harness").exists()


# ── 迁移：显式严格导入，load 永不自动信任旧位 ──────────────────────────────


def test_load_never_auto_imports_legacy_and_reports_presence(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _seed_legacy(project, [_legacy_entry(_rule(_identity(), PolicyGranularity.EXACT))])
    store = ApprovePolicyStore(project)
    assert store.load() == []
    assert not store.path.exists()
    assert store.legacy_present is True


def test_import_legacy_strict_validation_matrix(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    good_exact = _rule(_identity(), PolicyGranularity.EXACT, rule_id="good-exact")
    good_cmd = _rule(
        _identity(canonical="npm run build"), PolicyGranularity.COMMAND, rule_id="good-cmd",
    )
    no_created = _legacy_entry(good_exact) | {"id": "no-created", "created_at": ""}
    bad_date = _legacy_entry(good_exact) | {"id": "bad-date", "created_at": "not-a-date"}
    newline_cmd = _legacy_entry(
        _rule(_identity(canonical="echo a\necho b"), PolicyGranularity.COMMAND,
              rule_id="newline-cmd"),
    )
    bad_granularity = _legacy_entry(good_exact) | {"id": "bad-gran", "granularity": "prefix"}
    _seed_legacy(project, [
        _legacy_entry(good_exact), _legacy_entry(good_cmd),
        no_created, bad_date, newline_cmd, bad_granularity,
    ])
    store = ApprovePolicyStore(project)
    result = store.import_legacy()
    assert isinstance(result, LegacyImportResult)
    assert [r.id for r in result.imported] == ["good-exact", "good-cmd"]
    assert result.skipped == 4
    assert [r.id for r in store.load()] == ["good-exact", "good-cmd"]


def test_import_legacy_preserves_match_semantics_and_created_at(
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    identity = _identity(canonical="npm run build")
    rule = _rule(identity, PolicyGranularity.EXACT, rule_id="keep-me",
                 created_at="2025-12-31T23:59:59+00:00")
    _seed_legacy(project, [_legacy_entry(rule)])
    store = ApprovePolicyStore(project)
    store.import_legacy()

    persisted = store.load()[0]
    assert persisted.created_at == "2025-12-31T23:59:59+00:00"
    assert store.find_match(identity) is not None
    # 精确匹配仍在：args hash 不同 → exact 不命中。
    assert store.find_match(_identity(canonical="npm run build", args_hash="h2")) is None
    # 防提权重验仍在：permission 抬高 → 不命中（同 test_approve_policy.py 既有形态）。
    raised = _identity(canonical="npm run build", permission=ToolPermission.DANGER)
    assert store.find_match(raised) is None


def test_import_legacy_dedupes_by_id_and_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    store = ApprovePolicyStore(project)
    existing = _rule(_identity(), PolicyGranularity.EXACT, rule_id="dup")
    store.add_rule(existing)
    legacy_other = _rule(_identity(canonical="other cmd"), PolicyGranularity.EXACT,
                         rule_id="fresh")
    _seed_legacy(project, [
        _legacy_entry(existing) | {"key": "forged-different-key"},
        _legacy_entry(legacy_other),
    ])
    first = store.import_legacy()
    assert [r.id for r in first.imported] == ["fresh"]
    assert first.skipped == 1
    second = store.import_legacy()
    assert second.imported == []
    # 二次导入：两条旧条目（dup / fresh）此时都是已知 id → 都计入 skipped。
    assert second.skipped == 2
    assert [r.id for r in store.load()] == ["dup", "fresh"]
    # 控制-side 同 id 规则不被旧位改写。
    assert store.load()[0].key == existing.key


def test_legacy_file_grants_nothing_once_control_exists(tmp_path: Path) -> None:
    """agent 自建文件不被导入为授权：控制文件在场后，旧位内容零效力。"""
    project = tmp_path / "proj"
    project.mkdir()
    store = ApprovePolicyStore(project)
    store.add_rule(_rule(_identity(canonical="legit"), PolicyGranularity.EXACT))
    planted_identity = _identity(canonical="rm -rf /tmp/x")
    _seed_legacy(project, [_legacy_entry(
        _rule(planted_identity, PolicyGranularity.EXACT, rule_id="planted"),
    )])
    assert store.load() == [
        _rule(_identity(canonical="legit"), PolicyGranularity.EXACT),
    ]
    assert store.find_match(planted_identity) is None


def test_import_legacy_without_file_is_noop(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    store = ApprovePolicyStore(project)
    result = store.import_legacy()
    assert result.imported == []
    assert result.skipped == 0
    assert store.legacy_present is False


def test_import_legacy_corrupt_whole_file_raises(tmp_path: Path) -> None:
    """A 轴审查 P3-1：旧文件在场但整体读不动 ⇒ LegacyImportError，不当「没有规则」。

    迁移输出会建议删旧文件——「读不动」必须如实上抛，坏文件里可能有可恢复规则。
    """
    project = tmp_path / "proj"
    project.mkdir()
    target = project / _LEGACY_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ not json", encoding="utf-8")
    store = ApprovePolicyStore(project)
    with pytest.raises(LegacyImportError, match="合法 JSON"):
        store.import_legacy()
    assert not store.path.exists(), "迁移失败不得半途写出控制文件"


def test_import_legacy_invalid_utf8_raises(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    target = project / _LEGACY_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xfe{\"rules\": []}")
    store = ApprovePolicyStore(project)
    with pytest.raises(LegacyImportError, match="读取失败"):
        store.import_legacy()


def test_import_legacy_wrong_top_level_shape_raises(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    target = project / _LEGACY_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('["r1"]', encoding="utf-8")
    store = ApprovePolicyStore(project)
    with pytest.raises(LegacyImportError, match="形态"):
        store.import_legacy()


# ── agent 执行进程不可达（围栏矩阵）────────────────────────────────────────


@pytest.fixture
def fenced(tmp_path: Path) -> tuple[LocalSubprocessSandbox, ApprovePolicyStore, Path]:
    """workspace = tmp/proj（agent 领地）；控制文件在 env 钉的 tmp/control-home。"""
    project = tmp_path / "proj"
    project.mkdir()
    store = ApprovePolicyStore(project)
    store.add_rule(_rule(_identity(), PolicyGranularity.EXACT))
    before = store.path.read_text(encoding="utf-8")
    sandbox = LocalSubprocessSandbox(workspace_root=project)
    return sandbox, store, before


@pytest.mark.asyncio
async def test_write_tool_cannot_reach_control_file(fenced) -> None:
    sandbox, store, before = fenced
    tool = WriteTool(sandbox)
    result = await tool.execute(
        tool.args_schema(path=str(store.path), content='{"rules": []}'),
    )
    assert result.ok is False
    assert result.error_code is not None and "PERMISSION" in str(result.error_code)
    assert store.path.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_write_tool_cannot_reach_via_workspace_symlink(fenced) -> None:
    """链接绕到：workspace 内符号链接指向控制文件 → resolve 围栏仍拒。

    无建链特权的主机（Windows 非 Developer Mode，WinError 1314）skip——
    围栏语义在可建链环境（POSIX / 开发者模式）执行验证（#1510 同族惯例）。
    """
    sandbox, store, before = fenced
    link = sandbox.workspace_root / "jump-to-policy.json"
    try:
        link.symlink_to(store.path)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable on this host: {exc}")
    if not link.is_symlink():  # no-op 环境静默未建链（test_workspace_index 同款核验）
        pytest.skip("symlink creation silently no-op on this host")
    tool = WriteTool(sandbox)
    result = await tool.execute(
        tool.args_schema(path=str(link), content='{"rules": []}'),
    )
    assert result.ok is False
    assert result.error_code is not None and "PERMISSION" in str(result.error_code)
    assert store.path.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_edit_tool_cannot_reach_control_file(fenced) -> None:
    """B 轴 P4-4：edit 与 write 走同一 Sandbox 写时围栏，票面 AC 点名两者。"""
    sandbox, store, before = fenced
    tool = EditTool(sandbox)
    result = await tool.execute(
        tool.args_schema(path=str(store.path), old_string="rules", new_string="evil"),
    )
    assert result.ok is False
    assert result.error_code is not None and "PERMISSION" in str(result.error_code)
    assert store.path.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_executor_approval_installs_into_control_dir_not_workspace(
    tmp_path: Path,
) -> None:
    """症状反转：APPROVE_POLICY 安装落在控制目录；工作区不再出现 .agent-harness。"""
    project = tmp_path / "proj"
    project.mkdir()
    sandbox = LocalSubprocessSandbox(workspace_root=project)
    executor = ToolExecutor(
        _registry(sandbox), policy=PermissionPolicy.READ_ONLY,
        approval_callback=_PolicyApproval(), project_root=project,
    )
    result = await executor.execute(
        {"id": "c1", "name": "bash", "args": {"command": "echo hi"}},
    )
    assert result.result.ok is True
    store = ApprovePolicyStore(project)
    assert store.path.exists()
    assert len(store.load()) == 1
    assert not (project / ".agent-harness").exists()


def test_docker_agent_cannot_reach_control_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docker 引擎：容器只挂 workspace 具名卷（docker.py volumes 构造），宿主
    home 不进容器——控制文件字节在容器全局不可寻（canary 全盘 grep 为空）。
    无 daemon 主机 skip（该引擎的可写性证据 = 挂载面构造 + 有 daemon 环境跑本用例）。"""
    from uuid import uuid4

    from agent_harness.sandbox.capabilities import probe_docker_capabilities

    caps = probe_docker_capabilities()
    if not caps.available:
        pytest.skip(f"docker daemon unavailable: {caps.reason}")

    # 控制文件放宿主默认控制根（不 env 钉根——要验证的是真实默认位），
    # 写入 canary 后在容器内全盘找：找不到 = 宿主控制文件不可达。
    monkeypatch.delenv(_ENV_OVERRIDE, raising=False)
    project = tmp_path / "proj"
    project.mkdir()
    store = ApprovePolicyStore(project)
    canary = f"z1487-canary-{uuid4().hex}"
    store.add_rule(_rule(_identity(canonical=canary), PolicyGranularity.EXACT))
    assert store.path.parent == Path.home() / ".agent-harness" / "approve-policy"

    from agent_harness.sandbox.docker import DockerSandbox

    suffix = uuid4().hex
    sandbox = DockerSandbox(
        container_name=f"agent-harness-z1487-{suffix}",
        volume_name=f"agent-harness-z1487-{suffix}",
    )
    try:
        sandbox.ensure_started()
        result = sandbox.exec(
            f"grep -rl {canary} / --exclude-dir=proc --exclude-dir=sys 2>/dev/null"
            " | head -1",
        )
        assert result.stdout.strip() == ""
        # 容器内也看不到工作区旧位配置目录（迁移后 Runtime 不再创建它）。
        assert sandbox.exec("test -e /workspace/.agent-harness").exit_code != 0
    finally:
        sandbox.delete()
        # 本用例真实写入本机 home 控制根（canary），清理干净不留测试残渣——
        # add_rule 还会留 <hash>.json.lock 旁路锁文件（A 轴审查 P4-4），一并删。
        store.path.unlink(missing_ok=True)
        with suppress(OSError):
            (Path(str(store.path) + ".lock")).unlink(missing_ok=True)
        with suppress(OSError):
            store.path.parent.rmdir()
