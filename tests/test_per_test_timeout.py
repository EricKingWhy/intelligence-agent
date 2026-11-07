"""#1514（并入 #1515）：单测级**可归因**超时。

判据（并入章节的三条验收框，逐条对应见用例 docstring）：

- 超时必须点名**具体 nodeid** 并让退出码非零，而不是等 CI job 的 30 分钟上限；
- 默认值有实测依据（见 `pyproject.toml` 注释与 `docs/` 里的读数），慢/平台
  用例按 marker 例外，不为过门削弱持久化断言；
- 超时后**明确终止原因**（本机制 = 硬退出整个进程，等价 pytest-timeout 的
  `thread` 方法；因此"后续用例继续跑"这条**不承诺**，见下）。

机制选型（为什么不装 pytest-timeout）：票面禁止自动新增依赖，且先查已装能力——
`pytest-timeout` **未安装**；pytest 9.1.1 自带的 `faulthandler_timeout` 只能
dump 全线程栈、**不带 nodeid**，且默认不退出进程。故用一个约 20 行的 hook 本体
（`tests/conftest.py::pytest_runtest_protocol`，另加少量辅助函数如
`_effective_timeout` / `_abort`）：stdlib 看门狗线程 + `faulthandler`
+ `os._exit(1)`，与其他插件的 `pytest_runtest_protocol` 包装顺序无耦合。

`os._exit` 的代价照实说：`thread` 方法会终止整个进程、可能不跑 teardown——这正是
`pytest-timeout` 官方文档写明并在本票引用的语义。所以第三条验收框走的是它的
"或明确终止原因"分支：进程在超时点直接退出，nodeid 与全线程栈落盘，退出码 1。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def _run_probe(
    tmp_path: Path, body: str, *args: str, timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    """在临时目录里跑一个探针文件（conftest 复制过去，机制随目录生效）。

    不写进仓库 `tests/`：探针含 `sleep(3600)`，残留会被整套收集。
    """
    shutil.copy(CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "test_probe.py").write_text(
        textwrap.dedent(body), encoding="utf-8",
    )
    env = dict(os.environ)
    # 工作树优先：venv 的 intelligence_agent.pth 指向主仓 src，不盖住会读到别的树。
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "test_probe.py", "-p", "no:cacheprovider",
         *args],
        check=False, cwd=tmp_path, capture_output=True, text=True, timeout=timeout, env=env,
    )


def test_never_returning_test_reports_its_nodeid_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    """验收框 ①：故意永不返回的用例 ⇒ 报出**该 nodeid**，不等 job 的 30 分钟上限。

    红态（机制缺失）：探针永久挂住 ⇒ `subprocess.run(timeout=20)` 抛
    `TimeoutExpired`，即"整套只能靠 job 总超时收场"这个原始症状。
    """
    result = _run_probe(
        tmp_path,
        """
        import time

        def test_never_returns():
            time.sleep(3600)

        def test_after_the_hang():
            assert True
        """,
        "-o", "test_timeout=1",
        timeout=20,
    )

    assert result.returncode != 0, "超时必须让退出码非零"
    assert "test_never_returns" in result.stdout + result.stderr, (
        "超时归因必须点名 nodeid，而不是只给一条无主的栈：\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    # 明确终止原因：thread 方法的语义是硬退出，后续用例**不会**再跑。
    assert "test_after_the_hang" not in result.stdout + result.stderr, (
        "硬退出后不应再执行后续用例（若实现改成软中断，这条要随语义一起改）"
    )
    assert "test-timeout" in result.stdout + result.stderr


def test_hanging_fixture_setup_reports_its_nodeid_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    """setup 期挂死同样归因：看门狗包住 setup+call+teardown，不只包 call 期。"""
    result = _run_probe(
        tmp_path,
        """
        import time
        import pytest

        @pytest.fixture
        def hanging_fixture():
            time.sleep(3600)
            yield

        def test_uses_hanging_fixture(hanging_fixture):
            assert True
        """,
        "-o", "test_timeout=1",
        timeout=20,
    )

    assert result.returncode != 0, "setup 期挂死必须让退出码非零"
    assert "test_uses_hanging_fixture" in result.stdout + result.stderr, (
        "setup 期挂死的归因必须点名用例 nodeid：\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "test-timeout" in result.stdout + result.stderr


def test_marker_can_relax_the_timeout_for_a_genuinely_slow_case(
    tmp_path: Path,
) -> None:
    """验收框 ②：慢用例按 marker 例外，不被默认值误杀。"""
    result = _run_probe(
        tmp_path,
        """
        import time
        import pytest

        @pytest.mark.test_timeout(30)
        def test_slow_but_legitimate():
            time.sleep(0.5)
            assert True
        """,
        "-o", "test_timeout=0.2",
        timeout=20,
    )
    assert result.returncode == 0, (
        f"marker 例外未生效：{result.stdout}\n{result.stderr}"
    )


def test_marker_keyword_form_is_accepted(tmp_path: Path) -> None:
    """marker 的关键字式（注册签名 `test_timeout(seconds)`）同样生效，不误导报错。"""
    result = _run_probe(
        tmp_path,
        """
        import time
        import pytest

        @pytest.mark.test_timeout(seconds=30)
        def test_slow_but_legitimate():
            time.sleep(0.5)
            assert True
        """,
        "-o", "test_timeout=0.2",
        timeout=20,
    )
    assert result.returncode == 0, (
        f"关键字式 marker 未被接受：{result.stdout}\n{result.stderr}"
    )


def test_marker_zero_disables_the_watchdog(tmp_path: Path) -> None:
    """例外也可以是"关掉"：需要跑满任意长的用例显式声明。"""
    result = _run_probe(
        tmp_path,
        """
        import time
        import pytest

        @pytest.mark.test_timeout(0)
        def test_unbounded_on_purpose():
            time.sleep(0.5)
            assert True
        """,
        "-o", "test_timeout=0.2",
        timeout=20,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_default_timeout_is_configured_and_positive(pytestconfig: pytest.Config) -> None:
    """默认值必须真的配在 pyproject 里（否则机制只在 `-o` 下才存在）。"""
    value = float(pytestconfig.getini("test_timeout"))
    assert value > 0, "默认单测超时未配置：整套仍只能靠 CI job 上限收场"
    # 实测依据：全量套件最长正常用例远小于它（读数见 report）。给个下限防手滑。
    assert value >= 60, f"默认 {value}s 太紧，会把合法的慢用例打成假红"
