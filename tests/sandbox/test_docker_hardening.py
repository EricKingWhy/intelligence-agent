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

from contextlib import suppress
from uuid import uuid4

import docker
import pytest

from agent_harness.sandbox.capabilities import probe_docker_capabilities
from agent_harness.sandbox.docker import SandboxHardeningError


def _require_docker_engine() -> None:
    caps = probe_docker_capabilities()
    if not caps.available:
        pytest.fail(
            f"#1486 基础降权档需要真实 Docker Engine 验证，当前不可用：{caps.reason}"
        )


@pytest.fixture(scope="module")
def docker_client():
    _require_docker_engine()
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


def _sha_manifest(client, volume_name: str) -> str:
    """卷内容逐条 sha256（只算常规文件；类型/模式/属主由 _manifest 管）。"""
    result = client.containers.run(
        "python:3-slim",
        ["sh", "-c",
         "cd /workspace && find . -type f -exec sha256sum {} + | LC_ALL=C sort"],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        remove=True,
    )
    return result.decode()


def _backup_volume_name(workspace_volume: str) -> str:
    return f"{workspace_volume}-migration-backup"


def _write_backup_files(client, workspace_volume: str, script: str) -> None:
    """在备份卷里摆资产（模拟上次被硬杀留下的快照残件）。"""
    backup = _backup_volume_name(workspace_volume)
    try:
        client.volumes.get(backup)
    except docker.errors.NotFound:
        client.volumes.create(backup)
    container = client.containers.run(
        "python:3-slim",
        ["/bin/sh", "-lc", script],
        volumes={backup: {"bind": "/backup", "mode": "rw"}},
        detach=True,
    )
    try:
        assert container.wait()["StatusCode"] == 0
    finally:
        container.remove(force=True)


def _backup_has_restore_artifacts(client, workspace_volume: str) -> list[str]:
    backup = _backup_volume_name(workspace_volume)
    result = client.containers.run(
        "python:3-slim",
        ["sh", "-c", "ls -1 /backup 2>/dev/null | LC_ALL=C sort"],
        volumes={backup: {"bind": "/backup", "mode": "ro"}},
        remove=True,
    )
    return [line for line in result.decode().split() if line]


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


# ---------------------------------------------------------------------------
# AC2：迁移中途被硬杀 → 恢复路径不得改坏活数据
# ---------------------------------------------------------------------------


def _seed_bulk_volume(client, volume_name: str) -> None:
    """一个够大的历史卷：让「快照中途」这个窗口稳稳可命中。

    快照是整卷 `tar -cpf`，卷越大窗口越宽；~120MB 足够让轮询线程在写盘期间
    稳定命中（实测 8MB 太快，杀到之前 tar 已经写完）。
    """
    _seed_volume(
        client,
        volume_name,
        "mkdir -p /workspace/bulk && "
        "for i in $(seq 1 120); do "
        "dd if=/dev/urandom of=/workspace/bulk/f$i.bin bs=512k count=2 status=none; "
        "done && "
        "printf 'MANIFEST' > /workspace/anchor.txt",
    )


def _kill_helper_mid_snapshot(client, volume_name: str, deadline: float = 60.0) -> bool:
    """在 helper 写 `pre.tar` 期间 `kill -9` 它。返回是否真的杀到了。"""
    import time

    backup = _backup_volume_name(volume_name)
    started = time.monotonic()
    while time.monotonic() - started < deadline:
        try:
            backup_volume_containers = [
                container
                for container in client.containers.list(all=True)
                if any(
                    mount.get("Name") == backup
                    for mount in container.attrs.get("Mounts") or []
                )
            ]
        except Exception:  # noqa: BLE001 — daemon 抖动时继续轮询
            backup_volume_containers = []
        for container in backup_volume_containers:
            try:
                container.reload()
                if container.status != "running":
                    continue
                size = container.exec_run(
                    ["sh", "-c",
                     "stat -c %s /backup/pre.tar.tmp 2>/dev/null || echo 0"]
                ).output.decode().strip()
            except docker.errors.NotFound:  # 容器被 get 之后又消失/被删
                continue
            if size.isdigit() and int(size) > 0:
                with suppress(Exception):
                    container.kill()
                return True
        time.sleep(0.05)
    return False


def test_kill_during_snapshot_does_not_corrupt_workspace_on_retry(
    docker_client,
) -> None:
    """P1-C：快照写到一半 helper 被硬杀，下一次启动的「恢复」不得改坏活文件。

    症状（修复前）：`pre.tar` 边写边落，被杀后留下截断归档；下次启动步骤 0
    直接 `tar -xpf` 解它，tar 在 EOF 前已覆盖/截断部分活文件才报错——恢复
    动作本身成了破坏源，且重试永久粘住（每次 exit 89）。
    """
    import threading

    sandbox = _sandbox("h1486-killsnap")
    _seed_bulk_volume(docker_client, sandbox._volume_name)
    before = _sha_manifest(docker_client, sandbox._volume_name)
    before_meta = _manifest(docker_client, sandbox._volume_name)
    assert len(before.strip().splitlines()) == 121

    killer_result: dict[str, bool] = {}
    killer = threading.Thread(
        target=lambda: killer_result.update(
            killed=_kill_helper_mid_snapshot(docker_client, sandbox._volume_name)
        ),
        daemon=True,
    )
    killer.start()
    try:
        with pytest.raises(SandboxHardeningError):
            sandbox.ensure_started()  # 被杀死的 helper：exit 137
    finally:
        killer.join(timeout=70)

    assert killer_result.get("killed") is True, "没能在快照中途杀掉 helper"
    # 快照阶段只读：这一刻活数据必须完好。
    assert _sha_manifest(docker_client, sandbox._volume_name) == before

    # 重试：要么干净地恢复+迁移成功，要么响亮失败但**绝不改坏数据**。
    retry_error: Exception | None = None
    try:
        sandbox.ensure_started()
    except Exception as error:  # noqa: BLE001 — 允许响亮失败
        retry_error = error
    after_sha = _sha_manifest(docker_client, sandbox._volume_name)
    after_meta = _manifest(docker_client, sandbox._volume_name)
    assert after_sha == before, "重试（恢复）改坏了活文件内容"
    if retry_error is None:
        # 迁移成功：属主迁到 1000，类型/模式/路径不变。
        assert after_meta.replace("1000:1000", "0:0") == before_meta
        assert sandbox.read_text("anchor.txt") == "MANIFEST"
    # 不论哪条路径，重试都不得粘住：再启一次仍然只是「成功或同样响亮失败」。
    if retry_error is not None:
        with pytest.raises(SandboxHardeningError):
            sandbox.ensure_started()
        assert _sha_manifest(docker_client, sandbox._volume_name) == before
    sandbox.delete()


@pytest.mark.parametrize("truncation", ["mid-file", "member-boundary"])
def test_truncated_backup_is_never_extracted_into_live_tree(
    docker_client, truncation: str,
) -> None:
    """P1-C：备份卷里留着截断的 `pre.tar` 时，恢复不得动活数据。

    备份刻意做成**与活树同路径**（快照本来就是同一个卷的副本），这正是原
    verdict 的复现形态：解包会按路径覆盖活文件。

    `mid-file`：截在某个成员的数据区中间——旧实现直接 `tar -xpf` 到
    /workspace，tar 在 EOF 前已经把前面几个成员**写进活树**，之后才报错；
    恢复动作本身成了破坏源。
    `member-boundary`：截在 512 字节成员边界上——tar 认为「归档到此结束」，
    退出码 0、只解出前几个成员，同样已经覆盖了活数据。
    """
    sandbox = _sandbox("h1486-trunc")
    # 活树：4 个同尺寸文件，内容各不相同，方便逐条看见「被覆盖了哪几个」。
    _seed_volume(
        docker_client,
        sandbox._volume_name,
        "mkdir -p /workspace/d && "
        "for i in 1 2 3 4; do "
        "head -c 8192 /dev/zero | tr '\\0' L > /workspace/d/f$i.bin; done",
    )
    live_sha = _sha_manifest(docker_client, sandbox._volume_name)
    assert len(live_sha.strip().splitlines()) == 4

    # 备份：同样的路径，内容全换成 B（一旦被解进活树，sha 立刻变）。
    # 成员布局：./d(512) + 4 × (512 头 + 8192 数据) = 512 + 4×8704 = 35328 字节。
    body = (
        "mkdir -p /backup/src/d && "
        "for i in 1 2 3 4; do "
        "head -c 8192 /dev/zero | tr '\\0' B > /backup/src/d/f$i.bin; done && "
        "cd /backup/src && tar -cpf /backup/pre.tar --numeric-owner . && "
        "rm -rf /backup/src && "
    )
    if truncation == "mid-file":
        # 成员布局：./ 512 | ./d/ 512 | f4 头 512 + 数据 8192 | f3 头 512 + 数据 …
        # 12000 落在 f3 的数据区中间 —— tar 自己会报错，但报错前已经写进活树。
        script = body + "truncate -s 12000 /backup/pre.tar"
    else:
        # 9728 = 512 + 512 + 512 + 8192：正好在 f4 成员结束处。tar 认为「归档到此
        # 结束」（rc=0）并原样解出 ./、./d/、f4 —— 只靠退出码完全防不住。
        script = body + "truncate -s 9728 /backup/pre.tar"
    _write_backup_files(docker_client, sandbox._volume_name, script)
    backup_bytes = docker_client.containers.run(
        "python:3-slim",
        ["sh", "-c", "stat -c %s /backup/pre.tar"],
        volumes={
            _backup_volume_name(sandbox._volume_name): {
                "bind": "/backup", "mode": "ro",
            }
        },
        remove=True,
    ).decode().strip()
    assert backup_bytes in ("12000", "9728"), backup_bytes

    try:
        with pytest.raises(Exception) as excinfo:
            sandbox.ensure_started()
        message = str(excinfo.value)
        assert "89" in message, message
        # 核心断言：备份一个字节都没进活树（sha 逐条不变）。
        assert _sha_manifest(docker_client, sandbox._volume_name) == live_sha
        # 备份留卷内供人工恢复；不留半成品容器。
        assert "pre.tar" in _backup_has_restore_artifacts(
            docker_client, sandbox._volume_name
        )
        with pytest.raises(docker.errors.NotFound):
            docker_client.containers.get(sandbox._container_name)
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


def _commit_image_with_named_user(
    docker_client, tag: str, *, create_user: bool
) -> str:
    """提交一个 `USER app:app` 镜像；create_user 决定镜像里到底有没有该用户。"""
    base = docker_client.containers.run(
        "python:3-slim", ["sleep", "infinity"], detach=True
    )
    try:
        if create_user:
            result = base.exec_run(
                ["sh", "-c", "groupadd -g 1500 app && useradd -u 1500 -g 1500 -m app"]
            )
            assert result.exit_code == 0, result.output
        docker_client.api.commit(
            base.id, repository=tag.rsplit(":", 1)[0], tag=tag.rsplit(":", 1)[1],
            changes=["USER app:app"],
        )
    finally:
        base.remove(force=True)
    return tag


def test_custom_image_named_user_and_group_is_resolved_and_workspace_usable(
    docker_client,
) -> None:
    """`USER app:app`（镜像内确有该用户）：名字必须解析成数字，迁移真的发生。

    复核 P1（Spec F3 = Standards P1-2）：含 `:` 的声明被原样下传 → 迁移脚本
    `find -uid app` 报 non-numeric、输出为空 → 误报 `MIGRATE_OK records=0
    (already owned)`，卷仍 0:0，容器用户写不了工作区。
    """
    image = _commit_image_with_named_user(
        docker_client, f"agent-harness-test-1486:{uuid4().hex}", create_user=True
    )
    from agent_harness.sandbox.docker import DockerSandbox

    suffix = uuid4().hex
    sandbox = DockerSandbox(
        image=image,
        container_name=f"agent-harness-h1486-named-{suffix}",
        volume_name=f"agent-harness-h1486-named-{suffix}",
    )
    try:
        sandbox.ensure_started()
        attrs = _inspect(docker_client, sandbox._container_name)
        assert attrs["Config"]["User"] == "1500:1500"
        assert sandbox.exec("id -u").stdout.strip() == "1500"
        manifest = _manifest(docker_client, sandbox._volume_name)
        assert "1500:1500 755 /workspace" in manifest
        assert "0:0 755 /workspace" not in manifest
        # 容器用户自己可写；harness 写的文件也归它。
        assert sandbox.exec("printf X > /workspace/via_exec.txt").exit_code == 0
        sandbox.write_text("via_api.txt", "ok")
        assert sandbox.read_text("via_api.txt") == "ok"
        assert "1500:1500 666 /workspace/via_api.txt" in _manifest(
            docker_client, sandbox._volume_name
        )
    finally:
        sandbox.delete()
        docker_client.images.remove(image, force=True)


def test_custom_image_unknown_named_user_fails_loudly_without_leftovers(
    docker_client,
) -> None:
    """`USER app:app` 但镜像里没有该用户：响亮 SandboxHardeningError、无半成品。"""
    image = _commit_image_with_named_user(
        docker_client, f"agent-harness-test-1486:{uuid4().hex}", create_user=False
    )
    from agent_harness.sandbox.docker import DockerSandbox, SandboxHardeningError

    suffix = uuid4().hex
    sandbox = DockerSandbox(
        image=image,
        container_name=f"agent-harness-h1486-ghost-{suffix}",
        volume_name=f"agent-harness-h1486-ghost-{suffix}",
    )
    try:
        with pytest.raises(SandboxHardeningError) as excinfo:
            sandbox.ensure_started()
        assert "app:app" in str(excinfo.value)
        with pytest.raises(docker.errors.NotFound):
            docker_client.containers.get(sandbox._container_name)
        sandbox.delete()  # 不得 409：卷上没有残留容器挂着
        with pytest.raises(docker.errors.NotFound):
            docker_client.volumes.get(sandbox._volume_name)
    finally:
        docker_client.images.remove(image, force=True)


def test_container_start_failure_is_wrapped_and_cleaned_up(
    docker_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create 成功但 start 失败：`SandboxHardeningError` + 半成品容器被清掉。

    `containers.create` 不查 passwd，镜像声明了容器里不存在的用户时错误要到
    `start()` 才出现；那一步必须和迁移在同一个清理边界里，不能漏出裸 APIError
    500 + 停止态容器 + 挂着卷（后者会让 `delete()` 报 409）。

    名字解析已在 `_resolve_container_user` 阶段 fail-closed（见上一条用例），
    这里用真实 create + 真实卷、只把 `start` 换成抛错，验的是**清理边界**。
    """
    sandbox = _sandbox("h1486-startfail")
    # `client.containers` 每次访问都是新集合对象，只能打在类上。
    containers_cls = type(sandbox._client.containers)
    real_create = containers_cls.create

    def create_with_failing_start(self, *args, **kwargs):
        container = real_create(self, *args, **kwargs)
        # 只影响主容器；helper 也走 `containers.run → create`，别一起打坏。
        if kwargs.get("name") == sandbox._container_name:
            def fail_start(**_kwargs):
                raise docker.errors.APIError("simulated start failure")

            container.start = fail_start  # type: ignore[method-assign]
        return container

    monkeypatch.setattr(containers_cls, "create", create_with_failing_start)
    try:
        with pytest.raises(SandboxHardeningError) as excinfo:
            sandbox.ensure_started()
        assert "启动容器" in str(excinfo.value)
        with pytest.raises(docker.errors.NotFound):
            docker_client.containers.get(sandbox._container_name)
        sandbox.delete()
        with pytest.raises(docker.errors.NotFound):
            docker_client.volumes.get(sandbox._volume_name)
    finally:
        sandbox.delete()


@pytest.mark.parametrize(
    ("label", "run_kwargs"),
    [
        ("privileged", {"privileged": True}),
        ("nnp-false", {"security_opt": ["no-new-privileges:false"]}),
        ("root-uid", {"user": "0:1000"}),
        ("cap-add", {"cap_add": ["SYS_ADMIN"]}),
        ("host-network", {"network_mode": "host"}),
    ],
    ids=["privileged", "nnp-false", "root-uid", "cap-add", "host-network"],
)
def test_loosely_hardened_container_is_not_reused(
    docker_client, label: str, run_kwargs: dict,
) -> None:
    """P2-A：`_is_hardened` 是复用路径唯一安全门，必须按结构化白名单判。

    修复前（实测）：这五类容器全部被判「已硬化」并静默复用——`privileged`
    容器内 `Seccomp: 0`、`nnp:false` 是 `NoNewPrivs: 0`、`0:1000` 的
    `id -u` 就是 0。字段看着沾边、实际完全没隔离。
    """
    from agent_harness.sandbox.docker import DockerSandbox

    sandbox = _sandbox("h1486-loose")
    # 先按最接近合规的形态建（其余字段都给足），只让被测的那一项不合规。
    kwargs = {
        "user": "1000:1000",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        **run_kwargs,
    }
    legacy = docker_client.containers.run(
        "python:3-slim",
        ["sleep", "infinity"],
        name=sandbox._container_name,
        detach=True,
        volumes={sandbox._volume_name: {"bind": "/workspace", "mode": "rw"}},
        **kwargs,
    )
    legacy.reload()
    legacy_id = legacy.id
    assert DockerSandbox._is_hardened(legacy.attrs) is False, (
        f"{label} 容器被判成已硬化，会被静默复用"
    )
    try:
        sandbox.ensure_started()
        assert sandbox._container.id != legacy_id, f"{label} 历史容器必须重建"
        attrs = _inspect(docker_client, sandbox._container_name)
        assert attrs["HostConfig"]["Privileged"] is False
        assert attrs["HostConfig"]["CapAdd"] in (None, [])
        assert attrs["HostConfig"]["SecurityOpt"] == ["no-new-privileges:true"]
        assert attrs["Config"]["User"] == "1000:1000"
        assert sandbox.exec("id -u").stdout.strip() == "1000"
    finally:
        sandbox.delete()


def test_is_hardened_rejects_named_user_that_cannot_be_resolved() -> None:
    """P2-A：名字用户名解析不出数字时**拒绝复用**（不猜）。"""
    from agent_harness.sandbox.docker import DockerSandbox

    attrs = {
        "Config": {"User": "nobody:nogroup"},
        "HostConfig": {
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges:true"],
            "Privileged": False,
            "NetworkMode": "bridge",
        },
    }
    assert DockerSandbox._is_hardened(attrs) is False


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
