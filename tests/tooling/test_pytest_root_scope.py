"""仓库根 `pytest` 的收集面守卫（issue #856）。

## 它守什么

`docs/agents/verification.md` §2 车道 ② 的命令是**无参数**的仓库根调用：

    PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q -p no:randomly

没有 `testpaths` 时，pytest 的收集面 = **整个 rootdir**（实测 `config.args == [<仓库根>]`）。
而仓库根下有两棵 **gitignored** 的 electron-builder 产物树，各自打包了一份完整 Python 3.13 运行时：

    desktop/dist-installer/     （含 win-unpacked/resources/python，942 MB / 5402 个 .py）
    desktop/installer/staging/  （5402 个 .py）

收集走进它们会在**收集期** import pywin32 的
`win32comext/taskscheduler/test/test_addtask.py`；该模块级的 COM 调用把解释器打成**访问违例**
（`rc=3221225477`，Git Bash 下 `rc=139`）⇒ **一个用例都没跑**，而输出里没有任何失败签名，
最容易被读成「偶发环境抖动」或干脆当成跑过了。CI 的检出里没有这两棵树 ⇒ CI 恒绿，
只有**建过安装包**的机器会踩到。

## 它怎么判

用 pytest **自己**的配置解析（`Config.parse([])`，不做任何收集、不 import 任何用例）读出这次调用
**实际会收集什么**：

- 无参数调用 ⇒ 收集面必须落在 `tests/` 内（`testpaths`）；
- 显式 `pytest .` ⇒ 收集面仍是 rootdir，所以那两棵产物树必须落在 `--ignore` 里。

两条合起来 = 「从仓库根出发的调用都走不进产物树」。断言读的是**解析结果**而不是 pyproject 的文本：
键名写错、被别的配置顶掉、被 CLI 参数绕过，都会红。

## 它**不**守什么

不证明产物树里那 5402 个 `.py` 全都可安全 import —— 只证明**收集面到不了**它们。产物树本身是
gitignored 的构建产物，不在版本控制内，CI 上也不存在。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: 仓库根下两棵 gitignored 的打包产物树（各含一份 Python 运行时）。收集面一旦到得了它们，
#: 就是本文件 docstring 里那个「零用例访问违例」。
PACKAGING_TREES = ("desktop/dist-installer", "desktop/installer/staging")

#: 探针：只做 pytest 的**配置解析**，不做收集。打印一行带标记的 JSON 供本测试读回。
_PROBE_SOURCE = """
import json
import sys

from _pytest.config import get_config

argv = json.loads(sys.argv[1])
config = get_config(args=list(argv))
config.parse(list(argv))
print("<<<scope-probe>>>" + json.dumps({
    "argv": list(argv),
    "args": [str(a) for a in config.args],
    "ignore": [str(p) for p in (config.getoption("ignore") or [])],
    "rootdir": str(config.rootpath),
    "invocation_dir": str(config.invocation_params.dir),
}))
"""

_MARKER = "<<<scope-probe>>>"


def _effective_scope(tmp_path: Path, argv: list[str]) -> dict:
    """在仓库根解析一次 pytest 配置，返回这次调用**实际会收集什么**。"""
    probe = tmp_path / "scope_probe.py"
    probe.write_text(_PROBE_SOURCE, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(probe), json.dumps(argv)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
        timeout=180,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(_MARKER)]
    assert lines, (
        f"配置解析探针没有输出（argv={argv}, rc={proc.returncode}）"
        f"\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    scope = json.loads(lines[-1][len(_MARKER):])
    assert scope["rootdir"] == str(REPO), (
        f"探针的 rootdir 不是仓库根：{scope['rootdir']}（argv={argv}）"
    )
    assert scope["invocation_dir"] == str(REPO), (
        f"探针的 invocation dir 不是仓库根：{scope['invocation_dir']}"
    )
    return scope


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def test_bare_repo_root_pytest_collects_only_tests(tmp_path):
    """车道 ② 的原样命令（无参数）收集面必须只有 `tests/`，不能是整个 rootdir。"""
    scope = _effective_scope(tmp_path, [])
    roots = [Path(a) for a in scope["args"]]
    assert roots, "配置解析给出空收集面（pytest 会退回 invocation dir = 仓库根）"
    outside = [str(r) for r in roots if not _is_within(r, REPO / "tests")]
    assert not outside, (
        f"仓库根无参数 pytest 的收集面必须落在 tests/ 内，实测 {scope['args']}；"
        f"越界项 {outside} 会让收集走进 gitignored 打包产物树，"
        "在收集期把解释器打成访问违例、零用例执行（#856）"
    )


def test_packaging_trees_are_ignored_when_walking_from_root(tmp_path):
    """显式 `pytest .` 仍从 rootdir 走 ⇒ 那两棵产物树必须在 `--ignore` 里。"""
    scope = _effective_scope(tmp_path, ["."])
    # pytest 用 `absolutepath`（= cwd 相对）解析 `--ignore`，而 cwd 就是仓库根。
    ignored = {(REPO / p).resolve() for p in scope["ignore"]}
    missing = [t for t in PACKAGING_TREES if (REPO / t).resolve() not in ignored]
    assert not missing, (
        f"从仓库根走 rootdir 时这些产物树没有被忽略：{missing}；"
        f"实测 --ignore = {scope['ignore']}。"
        "它们的 .py 在收集期就会把解释器打成访问违例（#856）"
    )
