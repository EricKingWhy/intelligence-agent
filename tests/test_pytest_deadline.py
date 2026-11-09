from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def test_item_deadline_reports_nodeid_and_exits_nonzero() -> None:
    """挂死探针必须在**仓外**临时目录跑（与 `test_per_test_timeout.py::_run_probe` 同款）。

    探针含 `sleep(30)`，写进仓库 `tests/` 时一旦硬杀（CI `cancel-in-progress`、OOM、
    VM 重置）就会留下可被整套收集的残留 ⇒ 下一次全量/覆盖率作业被它整进程打死。
    复制 `conftest.py` 过去，机制照样随目录生效。
    """
    with tempfile.TemporaryDirectory(prefix="_pytest_deadline_") as temp:
        probe_dir = Path(temp)
        shutil.copy(CONFTEST, probe_dir / "conftest.py")
        probe = probe_dir / "test_deadline_probe.py"
        probe.write_text(
            "import time\n"
            "import pytest\n"
            "\n"
            "@pytest.mark.test_timeout(5.0, reason='deadline hook self-test')\n"
            "def test_stalled():\n"
            "    time.sleep(30)\n",
            encoding="utf-8",
        )
        nodeid = "test_deadline_probe.py::test_stalled"
        env = dict(os.environ)
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTEST_PLUGINS", None)
        # 工作树优先：venv 的 intelligence_agent.pth 指向主仓 src，不盖住会读到别的树。
        env["PYTHONPATH"] = os.pathsep.join(
            [str(PROJECT_ROOT / "src"), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", nodeid],
            cwd=probe_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"[test-timeout] {nodeid}" in result.stderr
    assert "in test_stalled" in result.stderr
