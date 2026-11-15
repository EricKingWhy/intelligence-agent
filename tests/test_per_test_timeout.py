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

    不写进仓库 `tests/`：探针含 `sleep(3600)`，硬杀/取消时残留会被整套收集
    （`test_pytest_deadline.py` 同样复制到仓外临时目录）。
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

        @pytest.mark.test_timeout(30, reason="slow probe")
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


def test_hanging_fixture_teardown_reports_its_nodeid_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    """teardown 期挂死同样归因：看门狗在 yield 前武装、finally 里才取消，yield 包含 teardown。"""
    result = _run_probe(
        tmp_path,
        """
        import time
        import pytest

        @pytest.fixture
        def hanging_teardown_fixture():
            yield
            time.sleep(3600)

        def test_uses_hanging_teardown_fixture(hanging_teardown_fixture):
            assert True
        """,
        "-o", "test_timeout=1",
        timeout=20,
    )

    assert result.returncode != 0, "teardown 期挂死必须让退出码非零"
    assert "test_uses_hanging_teardown_fixture" in result.stdout + result.stderr, (
        "teardown 期挂死的归因必须点名用例 nodeid：\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "test-timeout" in result.stdout + result.stderr


def test_marker_both_forms_reports_usage_error(tmp_path: Path) -> None:
    """位置参数与 seconds= 关键字并存是二义性：报 UsageError，不静默取其一。"""
    result = _run_probe(
        tmp_path,
        """
        import pytest

        @pytest.mark.test_timeout(30, seconds=60, reason="ambiguous probe")
        def test_dual_form_marker():
            assert True
        """,
        "-o", "test_timeout=300",
        timeout=20,
    )
    assert result.returncode != 0, "双形式 marker 必须让退出码非零"
    assert "不要同时给位置参数" in result.stdout + result.stderr, (
        "双形式 marker 应报 UsageError（“不要同时给位置参数”），而不是静默取其一：\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


def test_marker_non_numeric_value_reports_usage_error(tmp_path: Path) -> None:
    """非数字 marker 值报 UsageError（而非裸 ValueError），且退出码非零。"""
    result = _run_probe(
        tmp_path,
        """
        import pytest

        @pytest.mark.test_timeout(seconds="abc", reason="invalid-value probe")
        def test_bad_marker_value():
            assert True
        """,
        "-o", "test_timeout=300",
        timeout=20,
    )
    assert result.returncode != 0, "非数字 marker 值必须让退出码非零"
    assert "必须是数字" in result.stdout + result.stderr, (
        "非数字 marker 值应报 UsageError（“必须是数字”），而不是裸 ValueError：\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


def test_marker_keyword_form_is_accepted(tmp_path: Path) -> None:
    """marker 的关键字式（注册签名 `test_timeout(seconds)`）同样生效，不误导报错。"""
    result = _run_probe(
        tmp_path,
        """
        import time
        import pytest

        @pytest.mark.test_timeout(seconds=30, reason="slow keyword probe")
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


def test_marker_zero_is_rejected(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        """
        import time
        import pytest

        @pytest.mark.test_timeout(0, reason="zero-value probe")
        def test_unbounded_on_purpose():
            assert True
        """,
        "-o", "test_timeout=0.2",
        timeout=20,
    )
    assert result.returncode != 0, "marker 0 必须不能关闭单测看门狗"
    assert "正有限数" in result.stdout + result.stderr


def test_marker_requires_a_reason(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        """
        import pytest

        @pytest.mark.test_timeout(30)
        def test_missing_reason():
            assert True
        """,
        "-o", "test_timeout=300",
        timeout=20,
    )
    assert result.returncode != 0, "显式覆盖必须带上它的证据理由"
    assert "非空的 reason=" in result.stdout + result.stderr


def test_ini_zero_or_inf_cannot_disable_the_watchdog(tmp_path: Path) -> None:
    """ini 级逃生口同样被拒：`-o test_timeout=0` / `inf` 不得静默关闭看门狗。

    这是旧语义（`0 = 不设上限`）的反转；不锁住的话实现里 `... or 0.0` 之类
    的回退会悄悄复活逃生口。
    """
    body = """
        def test_ok():
            assert True
    """
    for value in ("0", "inf"):
        result = _run_probe(tmp_path, body, "-o", f"test_timeout={value}", timeout=20)
        assert result.returncode != 0, f"ini test_timeout={value} 不得被接受"
        assert "正有限数" in result.stdout + result.stderr, (
            f"ini test_timeout={value} 应报 UsageError：\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )


def test_empty_ini_value_is_reported_as_non_numeric(tmp_path: Path) -> None:
    """空 ini 值必须报「必须是数字」，**不得**被静默当成 `0.0` 再当成「无上限」。

    钉的是 `getini(...)` 后的 `or 0.0` 回退：它把「没配」和「配了 0」混成同一个值，
    正是旧逃生口的形状。回归锁 = 报错文案必须点出**值本身**有问题。
    """
    result = _run_probe(
        tmp_path,
        """
        def test_ok():
            assert True
        """,
        "-o", "test_timeout=",
        timeout=20,
    )
    assert result.returncode != 0, "空 ini 值不得被接受"
    assert "必须是数字" in result.stdout + result.stderr, (
        "空 ini 值应报「秒数必须是数字」（不得静默按 0.0 处理）：\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


def test_bad_marker_is_rejected_even_when_deselected(tmp_path: Path) -> None:
    """全量预校验对 `-m` 反选的条目**不豁免**（fail-closed）。

    有意为之：marker 写错就是代码缺陷，不该因为本地用 `-m not …` 跑子集就漏过去。
    删掉 `pytest_collection_modifyitems` 时这条必须变红——反选条目不会进
    `pytest_runtest_protocol`，只有收集期预校验能拦到它。断言看**报错文案**而不是
    只看 rc：全被反选时 pytest 自己也会给 rc=5。
    """
    result = _run_probe(
        tmp_path,
        """
        import pytest

        @pytest.mark.integration
        @pytest.mark.test_timeout(30)
        def test_deselected_but_invalid():
            assert True

        def test_valid():
            assert True
        """,
        "-m", "not integration",
        timeout=20,
    )
    assert "reason=" in result.stdout + result.stderr, (
        "反选不得豁免坏 marker；预期收集期 UsageError：\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


def test_default_timeout_is_configured_and_positive(pytestconfig: pytest.Config) -> None:
    """默认值必须真的配在 pyproject 里（否则机制只在 `-o` 下才存在）。"""
    value = float(pytestconfig.getini("test_timeout"))
    assert value > 0, "默认单测超时未配置：整套仍只能靠 CI job 上限收场"
    # 只防"手滑写成 <120s"这一族：实测最长单条在 Gate-1 是 91.12s、本机慢盘 426.93s，
    # 120 对后者并不构成余量（600s 例外由 test_workspace_index 的 marker 单独承担）。
    assert value >= 120, f"默认 {value}s 低于 120s 手滑下限"
