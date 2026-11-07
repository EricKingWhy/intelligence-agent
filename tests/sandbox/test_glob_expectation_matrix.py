"""#1530 AC1：三 backend 的 glob 现行期望矩阵（先记录现行语义，再谈收敛）。

本文件只**记录**当前树（`2680bb88`）的行为，不主张它们是对的。平台差异本身是期望的
一部分，**不被统一抹掉**：

1. **大小写**：三处 matcher 都用 `fnmatch.fnmatch`，而它经 `os.path.normcase` ⇒
   大小写折叠**随宿主**而变（POSIX 敏感 / Windows 不敏感）。注意 Docker backend 的
   匹配跑在**宿主** Python 里（`find` 只负责列文件），所以容器内的 POSIX 路径在
   Windows 宿主上也会被折成小写 —— 这是现行行为的真实形状，不是本文档的笔误。
2. **basename 提取**：`local` 用宿主 `Path(...).name`、`docker` 用 `PurePosixPath`、
   `windows_psec` 用 `ntpath.basename`。三 backend 的 `list_files` 实际产出的
   `rel_path` 都是 POSIX 风格（`local` 用 `rel.as_posix()`、`docker` 取 `find -printf '%P'`、
   `psec` 用 `f"{prefix}/{name}"`）⇒ 对**真实输入**三者一致；差异只在
   「`rel_path` 含反斜杠」这条潜伏形态上（`backslash-pattern` / `drive-letter-backslash`
   与文件末尾的机制锁用例）。
3. `**` 的收敛方式（`pattern.replace("**/", "").replace("**", "*")`）三处逐字相同。

矩阵共 19 个不同用例（每行 `dimension` 标签互不相同），× 3 backend = 57 例，+ 2 条机制锁 + 1 条 AC2 接线用例 = 60 例。

矩阵的用途：后续「等价纯匹配策略复用」以此为判定基准；无法合并的分支必须有
本文件里的具体反例，不能一句「无法合并」带过。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from agent_harness.sandbox import docker as docker_backend
from agent_harness.sandbox import local as local_backend
from agent_harness.sandbox import windows_psec as psec_backend
from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

IS_WINDOWS = os.name == "nt"

MATCHERS = {
    "local": local_backend._glob_match,
    "docker": docker_backend._glob_match_posix,
    "psec": psec_backend._glob_match,
}

# (rel_path, pattern, 维度, POSIX 宿主期望, Windows 宿主期望)
# 覆盖票面点名的维度：反斜杠 / 盘符 / 相对路径 / basename / ** / 大小写。
MATRIX = [
    ("pkg/inner.py", "*", "star", True, True),
    ("pkg/inner.py", "", "empty-pattern-equals-star", True, True),
    ("pkg/inner.py", "*.py", "extension", True, True),
    ("pkg/inner.py", "*.txt", "extension-miss", False, False),
    ("pkg/inner.py", "pkg/*.py", "relative-dir-prefix", True, True),
    ("pkg/deep/inner.py", "pkg/*.py", "dir-star-crosses-slash", True, True),
    ("pkg/inner.py", "inner.py", "bare-basename", True, True),
    ("pkg/deep/inner.py", "inner.py", "bare-basename-deep", True, True),
    ("pkg/deep/nest/a.py", "**/*.py", "double-star-deep", True, True),
    ("root.py", "**/*.py", "double-star-top-level", True, True),
    ("pkg/notes.txt", "**/*.py", "double-star-miss", False, False),
    ("pkg/deep/up.PY", "*.py", "case-fold", False, True),
    ("pkg/deep/up.PY", "*.PY", "case-exact", True, True),
    ("pkg/inner.py", r"pkg\*.py", "backslash-pattern", False, True),
    ("C:/outside/a.py", "*.py", "drive-letter-path", True, True),
    (r"C:\outside\a.py", "*.py", "drive-letter-backslash", True, True),
    ("../evil.py", "*.py", "traversal-shaped-path", True, True),
    (".hidden.py", "*.py", "dotfile", True, True),
    ("pkg/.cache/x.py", "**/*.py", "hidden-dir", True, True),
]


@pytest.mark.parametrize(
    ("rel_path", "pattern", "dimension", "expect_posix", "expect_nt"),
    MATRIX,
    ids=[case[2] for case in MATRIX],
)
@pytest.mark.parametrize("backend", sorted(MATCHERS))
def test_current_glob_expectation_matrix(
    backend: str,
    rel_path: str,
    pattern: str,
    dimension: str,
    expect_posix: bool,
    expect_nt: bool,
) -> None:
    """三 backend 的纯匹配策略在矩阵每一维上的现行判定。

    期望值按**宿主**给（大小写折叠来自 `os.path.normcase`，反斜杠 pattern 来自
    `ntpath.normcase` 把 `\\` 折成 `/`），不是按 backend 给 —— 这正是「平台差异不被
    统一抹掉」的落点。
    """
    expected = expect_nt if IS_WINDOWS else expect_posix
    assert MATCHERS[backend](rel_path, pattern) is expected, (
        f"backend={backend} rel_path={rel_path!r} pattern={pattern!r} ({dimension})"
    )


def test_backslash_rel_path_exposes_basename_derivation_difference() -> None:
    """基提取差异的**具体反例**（三 backend 唯一可见的分歧面）。

    `list_files` 不产出反斜杠 `rel_path`，所以这是潜伏形态；但一旦有人把 Windows
    宿主路径直接喂进 matcher，三处就不再同语义：`ntpath.basename` 会切反斜杠，
    宿主 `Path(...).name` 在 POSIX 上不会，`PurePosixPath` 永远不切。

    期望按宿主写：Windows 上三者都切（`Path` 绑定 ntpath）、POSIX 上只有 psec 切。
    """
    rel_path = r"pkg\inner.py"
    pattern = "inner.py"  # 只可能通过 basename 命中
    expected = {"local": IS_WINDOWS, "docker": False, "psec": True}
    for backend, want in expected.items():
        assert MATCHERS[backend](rel_path, pattern) is want, backend


def test_match_semantics_is_host_case_folding_not_backend_specific() -> None:
    """三 backend 共用宿主 `os.path.normcase` ⇒ 大小写语义由**宿主**决定。

    这条把「Docker 匹配跑在宿主 Python 里」这个事实钉住：容器内 POSIX 路径在
    Windows 宿主上同样被折叠。收敛时若把 flavor 显式化（posix/nt），必须在这里
    留下新的期望。
    """
    rel_path = "pkg/UP.PY"
    pattern = "*.py"
    for backend, matcher in MATCHERS.items():
        assert matcher(rel_path, pattern) is IS_WINDOWS, backend


def test_three_backends_route_list_files_through_the_shared_matcher(
    monkeypatch, tmp_path
) -> None:
    """AC2：共享匹配器必须**经三 backend 的 `list_files`** 实际调用，而非只测孤立 helper。

    做法：把每个 backend 模块命名空间里的 `glob_match` 换成间谍（各 backend 用
    `from ... import glob_match` 绑定到自己的模块全局），再走**真实**的 `list_files`。

    docker / psec 的 `list_files` 不需要容器 / Windows 主机：前者只依赖 `self.exec()`
    拿 `find` 输出，后者只依赖 `self._root_handle()` / `self._collect_files()`。故用
    `object.__new__` 注入这两个边界依赖（仓内先例：`tests/sandbox/test_exec_hardening.py`），
    `list_files` 本体是真实调用。这样「`list_files` 绕过 matcher」的接线回归才抓得住。
    """
    import agent_harness.sandbox.paths as paths_module

    calls: list[tuple[str, str]] = []
    real = paths_module.glob_match

    def spy(rel_path: str, pattern: str, *, basename) -> bool:
        calls.append((rel_path, pattern))
        return real(rel_path, pattern, basename=basename)

    for module in (local_backend, docker_backend, psec_backend):
        monkeypatch.setattr(module, "glob_match", spy, raising=True)

    from agent_harness.sandbox import DockerSandbox, LocalSubprocessSandbox

    # --- local：真 workspace 上的真 list_files ---
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "workspace")
    sandbox.write_text("pkg/inner.py", "x")
    sandbox.write_text("pkg/deep/up.PY", "x")

    # sorted() 之后的大小写折叠结果：Windows 上两个都命中，且按字典序 deep 在前。
    expected = ["pkg/deep/up.PY", "pkg/inner.py"] if IS_WINDOWS else ["pkg/inner.py"]
    assert sorted(sandbox.list_files("**/*.py")) == expected

    assert {rel for rel, _ in calls} == {"pkg/deep/up.PY", "pkg/inner.py"}, calls
    assert {pattern for _, pattern in calls} == {"**/*.py"}, calls
    calls.clear()

    # --- docker：stub exec 提供 find 输出，list_files 本体真跑 ---
    docker = object.__new__(DockerSandbox)
    docker.exec = lambda *args, **kwargs: SimpleNamespace(
        exit_code=0, stdout="pkg/inner.py\npkg/deep/up.PY\nroot.py\n"
    )
    expected_docker = (
        ["pkg/deep/up.PY", "pkg/inner.py", "root.py"]
        if IS_WINDOWS
        else ["pkg/inner.py", "root.py"]
    )
    assert docker.list_files("**/*.py") == expected_docker
    assert {rel for rel, _ in calls} == {"pkg/inner.py", "pkg/deep/up.PY", "root.py"}, calls
    assert {pattern for _, pattern in calls} == {"**/*.py"}, calls
    calls.clear()

    # --- psec：stub _root_handle / _collect_files，list_files 本体真跑 ---
    psec = object.__new__(WindowsPsecSandbox)
    psec._root_handle = lambda: 0
    psec._collect_files = lambda handle, prefix, results: results.extend(
        ["pkg/inner.py", "pkg/deep/up.PY", "pkg/notes.txt"]
    )
    expected_psec = (
        ["pkg/deep/up.PY", "pkg/inner.py"]
        if IS_WINDOWS
        else ["pkg/inner.py"]
    )
    assert psec.list_files("**/*.py") == expected_psec
    assert {rel for rel, _ in calls} == {"pkg/inner.py", "pkg/deep/up.PY", "pkg/notes.txt"}, calls
    assert {pattern for _, pattern in calls} == {"**/*.py"}, calls

    # 越界闸仍在匹配**之前**：matcher 没被调用就抛了。
    calls.clear()
    with pytest.raises(PermissionError):
        psec.list_files("../x.py")
    assert calls == []

    # matcher 就是共享实现 + 各自 basename 口径（反斜杠基线差异）。
    assert docker_backend._glob_match_posix("pkg/inner.py", "**/*.py") is True
    assert psec_backend._glob_match("pkg/inner.py", "**/*.py") is True
    assert docker_backend._glob_match_posix(r"pkg\inner.py", "inner.py") is False
    assert psec_backend._glob_match(r"pkg\inner.py", "inner.py") is True
