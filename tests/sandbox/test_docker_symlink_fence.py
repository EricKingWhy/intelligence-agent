"""#1510：Docker 文件工具围栏按容器内 symlink 实际目标判界。

红阶段（TDD）：`DockerSandbox.resolve_within_workspace` 只做词法规范化，
不解析容器内符号链接；而 read_text 的 `get_archive`、write_text 的
`cat >`、copy_in 的 `put_archive(父目录)` 都按容器语义**跟随**链接——
`/workspace/link -> /etc/x` 一类链接会让"workspace 路径"静默穿透到容器
文件系统的任意位置（宿主侧基类 base.py 用 `Path.resolve()` 跟随链接做
同一围栏，Docker 后端未对齐）。

本文件钉住：
- 越界目标（末级链接 / 中间目录链接 / 相对绝对链接 / 悬空出界链接）的
  读、写、copy_in 一律 PermissionError，且**实际目标字节不变**；
- 合法内部链接（相对/绝对/中间目录）照常读写穿（宿主语义：解析后操作）；
- 新文件、缺失父目录、路径规范化（`..`/`./`/绝对相对）行为明确；
- `path_kind` 的"不跟随末级链接"探测（#1167）与 `list_files` 的
  `find -type f`（不枚举链接）不回归；
- 链接环行为明确（`realpath -m` 对环静默返回词法路径；拒绝在执行侧——
  read 的 daemon APIError 映射为裸 `OSError`，web 侧落既有 422 分类）；
- `path_kind` 的**中间目录**链接同样过容器围栏（不泄出界目标类型）。

AC③ 的工具层配对（PermissionError → `PERMISSION_DENIED` ToolResult）
由 `tests/tools/test_coding_tools.py` 既有用例（fake 沙箱断言映射）与
本文件的沙箱层 PermissionError 断言**合并覆盖**（两轴审查一致判定）。

需要真实 Docker Engine（python:3-slim + coreutils realpath）。Engine
不可用时用例**真实失败**（不静默跳过）——票面 AC 明确要求实际容器用例
而非只 mock SDK（对齐 #1048 AC4 的先例）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_harness.sandbox.capabilities import probe_docker_capabilities


def _require_docker_engine() -> None:
    caps = probe_docker_capabilities()
    if not caps.available:
        pytest.fail(
            f"#1510 围栏语义需要真实 Docker Engine 验证，当前不可用：{caps.reason}"
        )


@pytest.fixture
def fenced_sandbox() -> Iterator[object]:
    from uuid import uuid4

    from agent_harness.sandbox.docker import DockerSandbox

    _require_docker_engine()
    suffix = uuid4().hex
    sandbox = DockerSandbox(
        container_name=f"agent-harness-fence-test-{suffix}",
        volume_name=f"agent-harness-fence-test-{suffix}",
    )
    try:
        sandbox.ensure_started()
        yield sandbox
    finally:
        sandbox.delete()


# ---------------------------------------------------------------------------
# AC①：越界读写被拒，且实际目标字节不变
# ---------------------------------------------------------------------------


def test_read_final_symlink_escape_rejected_and_target_unchanged(
    fenced_sandbox: object,
) -> None:
    sandbox = fenced_sandbox
    sandbox.exec(
        "printf 'SENTINEL' > /etc/harness-fence-canary && "
        "ln -sfn /etc/harness-fence-canary /workspace/leak-link"
    )

    with pytest.raises(PermissionError, match="越出 workspace"):
        sandbox.read_text("leak-link")

    # 实际目标字节不变（读路径只读，仍按 AC 显式钉住）。
    result = sandbox.exec("cat /etc/harness-fence-canary")
    assert result.exit_code == 0
    assert result.stdout == "SENTINEL"


def test_write_final_symlink_escape_rejected_and_target_bytes_unchanged(
    fenced_sandbox: object,
) -> None:
    sandbox = fenced_sandbox
    sandbox.exec(
        "printf 'ORIGINAL' > /etc/harness-fence-canary-w && "
        "ln -sfn /etc/harness-fence-canary-w /workspace/pwn-link"
    )

    with pytest.raises(PermissionError, match="越出 workspace"):
        sandbox.write_text("pwn-link", "EVIL")

    result = sandbox.exec("cat /etc/harness-fence-canary-w")
    assert result.exit_code == 0
    assert result.stdout == "ORIGINAL"


def test_intermediate_dir_symlink_escape_rejected_for_read_write_copy(
    fenced_sandbox: object,
    tmp_path: Path,
) -> None:
    sandbox = fenced_sandbox
    sandbox.exec(
        "mkdir -p /etc/harness-fence-dir && "
        "printf 'SECRET' > /etc/harness-fence-dir/secret.txt && "
        "ln -sfn /etc/harness-fence-dir /workspace/dir-link"
    )

    with pytest.raises(PermissionError, match="越出 workspace"):
        sandbox.read_text("dir-link/secret.txt")

    with pytest.raises(PermissionError, match="越出 workspace"):
        sandbox.write_text("dir-link/new.txt", "evil")

    host_file = tmp_path / "copied.txt"
    host_file.write_text("evil-copy", encoding="utf-8")
    with pytest.raises(PermissionError, match="越出 workspace"):
        sandbox.copy_in(host_file, "dir-link/copied.txt")

    leaked = sandbox.exec(
        "cat /etc/harness-fence-dir/secret.txt; "
        "test ! -e /etc/harness-fence-dir/new.txt && printf NO_NEW; "
        "test ! -e /etc/harness-fence-dir/copied.txt && printf NO_COPY"
    )
    assert leaked.exit_code == 0
    assert "SECRET" in leaked.stdout
    assert "NO_NEW" in leaked.stdout
    assert "NO_COPY" in leaked.stdout


def test_lexical_escape_still_rejected(fenced_sandbox: object) -> None:
    sandbox = fenced_sandbox
    with pytest.raises(PermissionError):
        sandbox.read_text("/workspace/../../etc/passwd")
    with pytest.raises(PermissionError):
        sandbox.write_text("/workspace/../../etc/pwned", "evil")


def test_dangling_outward_link_is_permission_error(fenced_sandbox: object) -> None:
    """悬空出界链接：按宿主语义（Path.resolve(strict=False)+判界）拒绝。

    现行为是 get_archive 跟随后 NotFound→FileNotFoundError——修复后应先在
    围栏处按解析目标判界（与 base.py 一致）。
    """
    sandbox = fenced_sandbox
    sandbox.exec("ln -sfn /etc/harness-fence-nothing /workspace/dang-link")

    with pytest.raises(PermissionError, match="越出 workspace"):
        sandbox.read_text("dang-link")
    with pytest.raises(PermissionError, match="越出 workspace"):
        sandbox.write_text("dang-link", "evil")


# ---------------------------------------------------------------------------
# AC②：合法内部链接 / 新文件 / 规范化行为明确
# ---------------------------------------------------------------------------


def test_internal_relative_and_absolute_links_read_write(
    fenced_sandbox: object,
) -> None:
    sandbox = fenced_sandbox
    sandbox.exec(
        "printf 'hello-internal' > /workspace/real.txt && "
        "ln -sfn real.txt /workspace/rel-link && "
        "ln -sfn /workspace/real.txt /workspace/abs-link"
    )

    assert sandbox.read_text("rel-link") == "hello-internal"
    assert sandbox.read_text("abs-link") == "hello-internal"

    # 写穿内部链接：内容落在**目标**上，链接本身保留（宿主 os.replace 到
    # 解析后路径的等价语义）。
    sandbox.write_text("abs-link", "rewritten")
    result = sandbox.exec("cat /workspace/real.txt && test -L /workspace/abs-link")
    assert result.exit_code == 0
    assert result.stdout == "rewritten"


def test_internal_intermediate_dir_link_write(
    fenced_sandbox: object,
) -> None:
    sandbox = fenced_sandbox
    sandbox.exec("mkdir -p /workspace/sub && ln -sfn /workspace/sub /workspace/sub-link")

    sandbox.write_text("sub-link/nested.txt", "nested")
    result = sandbox.exec("cat /workspace/sub/nested.txt")
    assert result.exit_code == 0
    assert result.stdout == "nested"


def test_new_file_with_missing_parents_and_relative_forms(
    fenced_sandbox: object,
) -> None:
    sandbox = fenced_sandbox
    sandbox.write_text("a/b/new.txt", "deep")
    assert sandbox.read_text("a/b/new.txt") == "deep"
    # `a/b/..` 词法回退到 `a`，再进 `b` ⇒ 仍是 a/b/new.txt。
    assert sandbox.read_text("./a/b/../b/new.txt") == "deep"

    sandbox.exec("printf 'norm' > /workspace/norm.txt")
    assert sandbox.read_text("/workspace/./norm.txt") == "norm"

    with pytest.raises(FileNotFoundError):
        sandbox.read_text("plain-missing.txt")


# ---------------------------------------------------------------------------
# 不回归：path_kind（#1167）与 list_files
# ---------------------------------------------------------------------------


def test_path_kind_symlink_detection_not_regressed(fenced_sandbox: object) -> None:
    sandbox = fenced_sandbox
    sandbox.exec(
        "printf 'x' > /workspace/pk-real.txt && "
        "ln -sfn pk-real.txt /workspace/pk-link"
    )

    assert sandbox.path_kind("pk-link") == "symlink"
    assert sandbox.path_kind("pk-real.txt") == "file"
    assert sandbox.path_kind("pk-missing") is None
    # 词法越界仍拒绝探测。
    assert sandbox.path_kind("../../etc/passwd") is None
    # workspace 根本身。
    assert sandbox.path_kind(".") == "directory"


def test_path_kind_intermediate_dir_link_fenced(fenced_sandbox: object) -> None:
    """Standards 轴 S1：中间目录链接不再泄出界目标的类型/存在性。

    修复前 `path_kind("dir-link/x")` 的 `test` 会跟随中间链接，报出
    workspace 外目标的类型；修复后父目录过容器围栏，出界即拒绝探测
    （与宿主后端对越界路径恒 None 对齐）。内部中间目录链接照常工作。
    """
    sandbox = fenced_sandbox
    sandbox.exec(
        "mkdir -p /etc/harness-fence-kind && "
        "printf 'x' > /etc/harness-fence-kind/secret.txt && "
        "ln -sfn /etc/harness-fence-kind /workspace/pk-dl"
    )
    assert sandbox.path_kind("pk-dl/secret.txt") is None

    sandbox.exec(
        "mkdir -p /workspace/pk-sub && "
        "printf 'x' > /workspace/pk-sub/f.txt && "
        "ln -sfn /workspace/pk-sub /workspace/pk-sl"
    )
    assert sandbox.path_kind("pk-sl/f.txt") == "file"
    # 末级链接探测不回归（#1167）：中间目录合法时，末级仍是链接本体。
    sandbox.exec("ln -sfn f.txt /workspace/pk-sl/tail-link")
    assert sandbox.path_kind("pk-sl/tail-link") == "symlink"


def test_symlink_loop_behavior_is_defined(fenced_sandbox: object) -> None:
    """链接环行为明确（Correctness 轴 F2 复核后的实测语义）。

    实测事实：`realpath -m` 对不可解析组件（环）静默返回词法路径
    （exit 0），环不会被围栏拦截；拒绝发生在执行侧——
    - read：daemon `get_archive` 以 `evalSymlinksInScope: too many links`
      500 拒绝（APIError），read_text 将其映射为裸 `OSError`（web 侧落
      既有 422"路径不合法"档，不产生未分类 500）；
    - write：`cat >` 在 shell 侧 ELOOP 失败 → RuntimeError"无法写入"。
    环在界内，不是越界（PermissionError）；fail-closed、无出界。
    """
    sandbox = fenced_sandbox
    sandbox.exec(
        "ln -sfn loop-b /workspace/loop-a && ln -sfn loop-a /workspace/loop-b"
    )

    with pytest.raises(OSError) as excinfo:
        sandbox.read_text("loop-a")
    assert not isinstance(excinfo.value, PermissionError)
    assert not isinstance(excinfo.value, FileNotFoundError)

    with pytest.raises(RuntimeError, match="无法写入"):
        sandbox.write_text("loop-a", "x")


def test_list_files_does_not_enumerate_symlinks(fenced_sandbox: object) -> None:
    sandbox = fenced_sandbox
    sandbox.exec(
        "printf 'x' > /workspace/ls-real.txt && "
        "ln -sfn ls-real.txt /workspace/ls-link"
    )

    listed = sandbox.list_files("ls-*")
    assert listed == ["ls-real.txt"]
