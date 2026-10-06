"""W-10（#354）：目录租约键规范化——绕过互斥的路径形态全部收敛/拒绝。

票面验收："junction/UNC/盘符大小写/尾分隔符/父子目录不能绕过互斥"、
"不能只比较原字符串"、"不能安全解析 fail-closed"。本文件钉**键层**语义
（互斥判定在 test_workspace_lease.py）；Linux 上用等价逻辑覆盖：

- POSIX 形态走 `os.path.realpath(strict=True)`：符号链接（junction 的
  等价物）在此由 OS 解析；链接环 / 不存在 → `LeasePathError`。
- Windows 形态走词法分支（POSIX 宿主的等价层）：盘符/全路径 casefold、
  分隔符方向、`.`/`..`、尾分隔符、UNC 前缀写法。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_harness.workspace.lease_paths import (
    LeasePathError,
    normalize_dir_key,
    paths_conflict,
)
from tests.symlink_capability import make_dir_link, needs_dir_link

# —— POSIX 形态：realpath 解析 + fail-closed ——


def test_trailing_separator_and_dot_segments_converge(tmp_path: Path) -> None:
    base = tmp_path / "proj"
    base.mkdir()
    key = normalize_dir_key(str(base))
    assert key == normalize_dir_key(str(base) + "/")
    assert key == normalize_dir_key(str(base) + "/./")
    assert key == normalize_dir_key(str(tmp_path / "proj"))
    assert key == normalize_dir_key(str(tmp_path / "other" / ".." / "proj"))


@needs_dir_link
def test_symlink_resolves_to_same_key_as_target(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    # Windows 无特权宿主退回 junction（mklink /J）——两者都是 reparse point，
    # realpath 都解析到 real，断言语义不变（#731，先例 tests/web/test_host_dirs_api.py）。
    if not make_dir_link(link, real):
        pytest.skip("宿主无法创建目录链接（探测与使用间能力变化）")
    assert normalize_dir_key(str(link)) == normalize_dir_key(str(real))


def test_missing_path_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(LeasePathError):
        normalize_dir_key(str(tmp_path / "does-not-exist"))


@pytest.mark.skipif(os.name != "nt", reason="Windows 形态 nt 分支专属（POSIX 宿主走词法等价层）")
@pytest.mark.parametrize("exc", [OSError(2, "no such file"), ValueError("bad reparse point")])
def test_windows_realpath_failure_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """nt 臂 realpath 失败必须收敛为 LeasePathError（API 422），不得逃逸成 500。

    POSIX 臂的 realpath 本就包在 try/except OSError 里；nt 臂 #726 起也调
    realpath，ntpath 在异态（winerror≠0 的 reparse 解析）可抛 ValueError，
    须与 POSIX 臂同款收敛。
    """
    real = tmp_path / "real"
    real.mkdir()

    def _boom(_p: str) -> str:
        raise exc

    monkeypatch.setattr(os.path, "realpath", _boom)
    with pytest.raises(LeasePathError):
        normalize_dir_key(str(real))


@needs_dir_link
def test_symlink_loop_fail_closed(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    # 悬空端构造（目标允许暂不存在，_dir_link_created 用 lstat 核验）；
    # 真实 Windows 宿主实测：junction 环 realpath 不抛但 stat 抛
    # OSError [winerror 1921] ⇒ 存在性校验 fail-closed，语义同 POSIX 链接环。
    if not make_dir_link(a, b) or not make_dir_link(b, a):
        pytest.skip("宿主无法创建目录链接（探测与使用间能力变化）")
    with pytest.raises(LeasePathError):
        normalize_dir_key(str(a))


def test_relative_path_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(LeasePathError):
        normalize_dir_key("relative/dir")


def test_empty_and_nul_rejected() -> None:
    with pytest.raises(LeasePathError):
        normalize_dir_key("   ")
    with pytest.raises(LeasePathError):
        normalize_dir_key("a\x00b")


# —— Windows 形态（POSIX 宿主上的等价词法层）——


@pytest.mark.skipif(os.name == "nt", reason="Windows 宿主走 realpath 分支，词法断言不变")
class TestWindowsFormLexical:
    def test_drive_case_and_separator_direction_converge(self) -> None:
        key = normalize_dir_key("D:\\code\\proj")
        assert key == normalize_dir_key("d:/code/proj")
        assert key == normalize_dir_key("D:/code/proj")

    def test_trailing_separator_converge(self) -> None:
        assert normalize_dir_key("D:\\code\\proj") == normalize_dir_key("D:\\code\\proj\\")

    def test_dot_and_dotdot_segments_converge(self) -> None:
        assert normalize_dir_key("D:\\code\\proj") == normalize_dir_key("D:\\code\\.\\proj")
        assert normalize_dir_key("D:\\code\\proj") == normalize_dir_key(
            "d:\\code\\other\\..\\proj"
        )

    def test_casefold_whole_path_ntfs_insensitive(self) -> None:
        assert normalize_dir_key("D:\\Code\\Proj") == normalize_dir_key("d:\\code\\proj")

    def test_unc_prefix_forms_converge(self) -> None:
        key = normalize_dir_key("\\\\server\\share\\dir")
        assert key == normalize_dir_key("//server/share/dir")
        assert key == normalize_dir_key("\\\\SERVER\\Share\\DIR")

    def test_unc_share_root_is_its_own_key(self) -> None:
        assert normalize_dir_key("\\\\server\\share") == normalize_dir_key("//server/share")

    def test_drive_relative_rejected(self) -> None:
        with pytest.raises(LeasePathError):
            normalize_dir_key("D:relative")
        with pytest.raises(LeasePathError):
            normalize_dir_key("relative\\path")


# —— 相交判定（父子目录）——


class TestPathsConflict:
    def test_equal_keys_conflict(self) -> None:
        assert paths_conflict("/a/b", "/a/b")

    def test_parent_child_conflict_both_directions(self) -> None:
        assert paths_conflict("/a/b", "/a/b/c")
        assert paths_conflict("/a/b/c", "/a/b")

    def test_root_ancestor_conflict(self) -> None:
        assert paths_conflict("/a", "/a/deeply/nested/dir")

    def test_prefix_not_component_is_disjoint(self) -> None:
        # "/a/bc" 与 "/a/b" 只是字符串前缀，不是路径前缀——必须判独立。
        assert not paths_conflict("/a/b", "/a/bc")

    def test_siblings_disjoint(self) -> None:
        assert not paths_conflict("/a/b", "/a/c")

    # 真实 Windows 宿主上合成路径被 #726 存在性校验 fail-closed 拒绝；
    # Windows 形态词法等价只能由 POSIX 宿主等价层钉住（同 TestWindowsFormLexical）。
    @pytest.mark.skipif(os.name == "nt", reason="Windows 宿主路径需真实存在（#726 fail-closed），合成键词法等价在 POSIX 宿主钉住")
    def test_windows_form_parent_child(self) -> None:
        assert paths_conflict(
            normalize_dir_key("D:\\code"), normalize_dir_key("d:/code/proj")
        )

    @pytest.mark.skipif(os.name == "nt", reason="Windows 宿主路径需真实存在（#726 fail-closed），合成键词法等价在 POSIX 宿主钉住")
    def test_windows_prefix_not_component_is_disjoint(self) -> None:
        assert not paths_conflict(
            normalize_dir_key("D:\\code\\b"), normalize_dir_key("D:\\code\\bc")
        )
