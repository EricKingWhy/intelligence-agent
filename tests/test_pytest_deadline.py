from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_item_deadline_reports_nodeid_and_exits_nonzero() -> None:
    with tempfile.TemporaryDirectory(prefix="_pytest_deadline_", dir=PROJECT_ROOT / "tests") as temp:
        probe_dir = Path(temp)
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
        nodeid = f"{probe.relative_to(PROJECT_ROOT).as_posix()}::test_stalled"
        env = os.environ.copy()
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTEST_PLUGINS", None)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", nodeid.split("::", 1)[0]],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"[test-timeout] {nodeid}" in result.stderr
    assert "in test_stalled" in result.stderr
