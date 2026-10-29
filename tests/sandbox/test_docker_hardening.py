"""#1486：工作区 Docker 基础降权档（非 root / cap_drop=ALL / no-new-privileges）。

红阶段（TDD）：基线 `_ensure_started` 的 `containers.run(...)` 不带 `user` /
`cap_drop` / `security_opt`，`python:3-slim`（及无 `USER` 的自定义镜像）按
root 运行；复用路径按名字找回容器后直接起用，从不检验其是否降权。旧卷由
root 进程写入后，非 root 容器读不到也写不了，基线没有属主迁移。

本文件钉住票面三个可测验收：
- AC1：新建与复用（含历史未硬化容器）的实际 `docker inspect` 读数——
  `Config.User` 非 root、`HostConfig.CapDrop` 含 `ALL`、`HostConfig.SecurityOpt`
  含 `no-new-privileges:true`；不是只查传参。
- AC2：旧卷属主迁移——历史文件可读写、编译 / 装依赖 / 重启可用；迁移失败
  必须响亮报错、回滚到迁移前读数（原数据完好），恢复路径（重试）可用。
- AC3：local 仍可选且 CLI 明示无 OS 隔离；容器默认不限网 / 不强限资源；
  自定义镜像（声明非 root / root）与历史容器都不静默跳过硬化。

需要真实 Docker Engine（python:3-slim + bash/tar/find）；Engine 不可用时
用例**真实失败**（不静默跳过），对齐 #1048/#1510 先例。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from agent_harness.sandbox.capabilities import probe_docker_capabilities


def _require_docker_engine() -> None:
    caps = probe_docker_capabilities()
    if not caps.available:
        pytest.fail(
            f"#1486 基础降权档需要真实 Docker Engine 验证，当前不可用：{caps.reason}"
        )


@pytest.fixture(scope="module")
def docker_client():
    _require_docker_engine()
    import docker

    client = docker.from_env(use_context=False)
    try:
        yield client
    finally:
        client.close()


def _sandbox(name_prefix: str):
    from agent_harness.sandbox.docker import DockerSandbox

    suffix = uuid4().hex
    return DockerSandbox(
        container_name=f"agent-harness-{name_prefix}-{suffix}",
        volume_name=f"agent-harness-{name_prefix}-{suffix}",
    )


def _seed_volume(client, volume_name: str, script: str) -> None:
    container = client.containers.run(
        "python:3-slim",
        ["/bin/sh", "-lc", script],
        volumes={volume_name: {"bind": "/workspace", "mode": "rw"}},
        detach=True,
    )
    try:
        assert container.wait()["StatusCode"] == 0
    finally:
        container.remove(force=True)


def _manifest(client, volume_name: str) -> str:
    """卷内逐条 `类型:属主:模式:路径`（uid/gid 用 getent 前的数字表示）。"""
    result = client.containers.run(
        "python:3-slim",
        ["sh", "-c", "find /workspace -printf '%y %U:%G %m %p\\n' | LC_ALL=C sort"],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        remove=True,
    )
    return result.decode()


def _inspect(client, container_name: str) -> dict:
    container = client.containers.get(container_name)
    container.reload()
    return container.attrs


# ---------------------------------------------------------------------------
# AC1：新建 / 复用容器的真实 inspect 读数
# ---------------------------------------------------------------------------


def test_new_container_inspect_shows_non_root_cap_drop_all_and_no_new_privileges(
    docker_client,
) -> None:
    sandbox = _sandbox("h1486-new")
    try:
        sandbox.ensure_started()
        attrs = _inspect(docker_client, sandbox._container_name)
        assert attrs["Config"]["User"] == "1000:1000"
        assert attrs["HostConfig"]["CapDrop"] == ["ALL"]
        assert any(
            "no-new-privileges" in opt
            for opt in attrs["HostConfig"]["SecurityOpt"] or []
        )
        assert attrs["HostConfig"]["Privileged"] is False
        # 真实进程身份，而不只是 inspect 字段。
        assert sandbox.exec("id -u").stdout.strip() == "1000"
        # 非 root 下工作区仍可写（挂载适配）。
        sandbox.write_text("probe.txt", "ok")
        assert sandbox.read_text("probe.txt") == "ok"
    finally:
        sandbox.delete()


def test_reused_hardened_container_keeps_hardening_and_identity(docker_client) -> None:
    """进程重启后的复用路径（按确定性名字找回）不放松已有硬化。"""
    sandbox = _sandbox("h1486-reuse")
    try:
        sandbox.ensure_started()
        first_id = sandbox._container.id
        sandbox.stop()  # 只停容器，保留 volume 与名字（resume 语义）

        from agent_harness.sandbox.docker import DockerSandbox

        revived = DockerSandbox(
            container_name=sandbox._container_name,
            volume_name=sandbox._volume_name,
        )
        revived.ensure_started()
        try:
            assert revived._container.id == first_id, "复用不得重建合规容器"
            attrs = _inspect(docker_client, revived._container_name)
            assert attrs["Config"]["User"] == "1000:1000"
            assert attrs["HostConfig"]["CapDrop"] == ["ALL"]
            assert any(
                "no-new-privileges" in opt
                for opt in attrs["HostConfig"]["SecurityOpt"] or []
            )
            assert revived.exec("id -u").stdout.strip() == "1000"
        finally:
            revived.stop()
    finally:
        sandbox.stop()
        sandbox.delete()


def test_historical_unhardened_container_is_explicitly_recreated(docker_client) -> None:
    """历史容器（旧代码建的 root 容器）不得静默复用：保留卷、重建为降权容器。"""
    sandbox = _sandbox("h1486-legacy")
    legacy = docker_client.containers.run(
        "python:3-slim",
        ["sleep", "infinity"],
        name=sandbox._container_name,
        detach=True,
        volumes={sandbox._volume_name: {"bind": "/workspace", "mode": "rw"}},
    )
    legacy.reload()
    legacy_attrs = legacy.attrs
    assert legacy_attrs["Config"]["User"] in ("", "root")
    assert not legacy_attrs["HostConfig"].get("CapDrop")
    legacy_id = legacy.id
    _seed_volume(
        docker_client,
        sandbox._volume_name,
        "printf 'historic' > /workspace/historic.txt",
    )
    try:
        sandbox.ensure_started()
        attrs = _inspect(docker_client, sandbox._container_name)
        assert sandbox._container.id != legacy_id, "历史未硬化容器必须被显式重建"
        assert attrs["Config"]["User"] == "1000:1000"
        assert attrs["HostConfig"]["CapDrop"] == ["ALL"]
        assert any(
            "no-new-privileges" in opt
            for opt in attrs["HostConfig"]["SecurityOpt"] or []
        )
        # 重建不得丢卷内数据，且非 root 可读写历史文件。
        assert sandbox.read_text("historic.txt") == "historic"
        sandbox.write_text("historic.txt", "migrated")
        assert sandbox.read_text("historic.txt") == "migrated"
    finally:
        sandbox.delete()


# ---------------------------------------------------------------------------
# AC2：旧卷属主迁移
# ---------------------------------------------------------------------------


def test_legacy_volume_ownership_migrated_then_compile_install_and_restart_work(
    docker_client,
) -> None:
    sandbox = _sandbox("h1486-migrate")
    _seed_volume(
        docker_client,
        sandbox._volume_name,
        "mkdir -p /workspace/pkg /workspace/empty && "
        "printf 'print(\"ok\")\\n' > /workspace/pkg/mod.py && "
        "printf 'LEGACY' > /workspace/old.txt && "
        "printf 'SETUID' > /workspace/suid.txt && chmod 4755 /workspace/suid.txt && "
        "printf 'SP' > '/workspace/with space.txt' && "
        "ln -s old.txt /workspace/link",
    )
    try:
        sandbox.ensure_started()
        # 历史文件可读写。
        assert sandbox.read_text("old.txt") == "LEGACY"
        sandbox.write_text("old.txt", "MIGRATED")
        assert sandbox.read_text("with space.txt") == "SP"
        # 编译 / 装依赖（venv 落在卷内）。
        result = sandbox.exec(
            "python -m compileall -q /workspace/pkg && "
            "python -m venv /workspace/.venv && "
            "/workspace/.venv/bin/pip install -q --disable-pip-version-check six && "
            "printf INSTALL_OK",
            timeout=180,
        )
        assert result.exit_code == 0, result.stderr
        assert "INSTALL_OK" in result.stdout
        # chown 清掉的 setuid 位被恢复（迁移只改属主，不改权限）。
        mode = sandbox.exec("stat -c %a /workspace/suid.txt").stdout.strip()
        assert mode == "4755"
        # 重启（停止 + 重新按名字找回）后仍可用，属主不被反复改写。
        sandbox.stop()
        sandbox.ensure_started()
        assert sandbox.read_text("old.txt") == "MIGRATED"
        attrs = _inspect(docker_client, sandbox._container_name)
        assert attrs["Config"]["User"] == "1000:1000"
    finally:
        sandbox.delete()


@pytest.mark.parametrize(
    ("seed", "path", "expected_mode"),
    [
        ("printf 'SG' > /workspace/sg.txt && chmod 2755 /workspace/sg.txt",
         "sg.txt", "2755"),
        ("mkdir -p /workspace/shared && chmod 2775 /workspace/shared",
         "shared", "2775"),
    ],
    ids=["setgid-file", "setgid-dir"],
)
def test_setgid_mode_survives_ownership_migration(
    docker_client, seed: str, path: str, expected_mode: str
) -> None:
    """setgid 位（文件 2755 / 目录 2775）迁移后必须保留，工作区必须能起来。

    `chown -R` 会清掉 setgid；把位加回去要 `CAP_FSETID`。缺它时 helper 的
    chmod 静默丢位 → 复核 `MIGRATE_ERROR content or mode changed` → 每次
    重试都同样失败，卷里只要有一个 setgid 条目工作区就永久不可用。
    """
    sandbox = _sandbox("h1486-setgid")
    _seed_volume(docker_client, sandbox._volume_name, seed)
    try:
        sandbox.ensure_started()
        mode = sandbox.exec(f"stat -c %a /workspace/{path}").stdout.strip()
        assert mode == expected_mode, f"{path} 的 setgid 位被迁移丢掉：{mode}"
        assert _manifest(docker_client, sandbox._volume_name).count(
            f"1000:1000 {expected_mode} /workspace/{path}"
        ) == 1
        # 重试路径不再粘住：停止后按名字找回，迁移是幂等的。
        sandbox.stop()
        sandbox.ensure_started()
        assert sandbox.exec(f"stat -c %a /workspace/{path}").stdout.strip() == (
            expected_mode
        )
    finally:
        sandbox.delete()


def test_ownership_migration_failure_rolls_back_and_retry_recovers(
    docker_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """迁移失败：响亮报错、卷回滚到迁移前读数、重试（恢复路径）可成功。"""
    import agent_harness.sandbox.docker as docker_module

    sandbox = _sandbox("h1486-migfail")
    _seed_volume(
        docker_client,
        sandbox._volume_name,
        "mkdir -p /workspace/d && printf 'KEEP' > /workspace/d/keep.txt && "
        "printf 'X' > /workspace/suid.txt && chmod 4755 /workspace/suid.txt",
    )
    before = _manifest(docker_client, sandbox._volume_name)
    assert before.startswith("d 0:0 755 /workspace")
    assert "f 0:0 4755 /workspace/suid.txt" in before

    # 同一份迁移脚本，在 chown 之后注入失败：验证的是生产脚本的回滚分支，
    # 不是另写一段替身逻辑。
    real_script = docker_module._OWNER_MIGRATION_SCRIPT
    anchor = (
        'chown -R "$U:$G" /workspace || '
        "abort_with_rollback 'MIGRATE_ERROR chown failed' 97"
    )
    assert anchor in real_script
    injected = real_script.replace(
        anchor,
        anchor + "\nabort_with_rollback 'MIGRATE_TEST_INJECTED after chown' 94",
    )
    assert injected != real_script
    monkeypatch.setattr(docker_module, "_OWNER_MIGRATION_SCRIPT", injected)

    with pytest.raises(Exception) as excinfo:
        sandbox.ensure_started()
    assert "MIGRATE" in str(excinfo.value) or "迁移" in str(excinfo.value)
    # 不误报成功：容器没有进入可用状态。
    assert sandbox._container is None
    # 回滚后原数据逐条与迁移前一致（含属主、模式、内容）。
    assert _manifest(docker_client, sandbox._volume_name) == before

    # 恢复路径：换回生产脚本后重试成功，数据仍在。
    monkeypatch.setattr(docker_module, "_OWNER_MIGRATION_SCRIPT", real_script)
    sandbox.ensure_started()
    try:
        assert sandbox.read_text("d/keep.txt") == "KEEP"
        assert _manifest(docker_client, sandbox._volume_name).startswith(
            "d 1000:1000 755 /workspace"
        )
    finally:
        sandbox.delete()


# ---------------------------------------------------------------------------
# AC3：自定义镜像不静默跳过；默认不限网 / 不强限资源
# ---------------------------------------------------------------------------


def _commit_custom_image(docker_client, tag: str, user_change: str | None) -> str:
    base = docker_client.containers.run(
        "python:3-slim", ["sleep", "infinity"], detach=True
    )
    try:
        changes = [f"USER {user_change}"] if user_change else []
        docker_client.api.commit(
            base.id, repository=tag.rsplit(":", 1)[0], tag=tag.rsplit(":", 1)[1],
            changes=changes,
        )
    finally:
        base.remove(force=True)
    return tag


@pytest.mark.parametrize(
    ("declared_user", "expected_uid", "expected_user_field"),
    [
        ("1001", "1001", "1001:1001"),
        ("nobody", "65534", "65534:65534"),
    ],
)
def test_custom_image_non_root_user_is_respected(
    docker_client, declared_user: str, expected_uid: str, expected_user_field: str
) -> None:
    image = _commit_custom_image(
        docker_client, f"agent-harness-test-1486:{uuid4().hex}", declared_user
    )
    from agent_harness.sandbox.docker import DockerSandbox

    suffix = uuid4().hex
    sandbox = DockerSandbox(
        image=image,
        container_name=f"agent-harness-h1486-img-{suffix}",
        volume_name=f"agent-harness-h1486-img-{suffix}",
    )
    try:
        sandbox.ensure_started()
        attrs = _inspect(docker_client, sandbox._container_name)
        assert attrs["Config"]["User"] == expected_user_field
        assert attrs["HostConfig"]["CapDrop"] == ["ALL"]
        assert sandbox.exec("id -u").stdout.strip() == expected_uid
        sandbox.write_text("img.txt", "ok")
        assert sandbox.read_text("img.txt") == "ok"
    finally:
        sandbox.delete()
        docker_client.images.remove(image, force=True)


def test_custom_image_root_user_is_de_privileged(docker_client) -> None:
    image = _commit_custom_image(
        docker_client, f"agent-harness-test-1486:{uuid4().hex}", "root"
    )
    from agent_harness.sandbox.docker import DockerSandbox

    suffix = uuid4().hex
    sandbox = DockerSandbox(
        image=image,
        container_name=f"agent-harness-h1486-rootimg-{suffix}",
        volume_name=f"agent-harness-h1486-rootimg-{suffix}",
    )
    try:
        sandbox.ensure_started()
        attrs = _inspect(docker_client, sandbox._container_name)
        assert attrs["Config"]["User"] == "1000:1000"
        assert sandbox.exec("id -u").stdout.strip() == "1000"
    finally:
        sandbox.delete()
        docker_client.images.remove(image, force=True)


def test_default_profile_keeps_build_network_and_no_resource_limits(
    docker_client,
) -> None:
    """AC3：默认不额外限网、不强限资源（只做降权档，不做隔离计算档）。"""
    sandbox = _sandbox("h1486-defaults")
    try:
        sandbox.ensure_started()
        attrs = _inspect(docker_client, sandbox._container_name)
        host = attrs["HostConfig"]
        assert host["NetworkMode"] != "none"
        assert host["Memory"] in (0, None)
        assert host["NanoCpus"] in (0, None)
        assert host["PidsLimit"] in (None, 0)
        assert host["ReadonlyRootfs"] is False
        assert attrs["Mounts"][0]["RW"] is True
    finally:
        sandbox.delete()
