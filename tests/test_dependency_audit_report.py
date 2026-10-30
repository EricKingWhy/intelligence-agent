import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_dependency_audit.py"


def test_report_warns_for_a_scanner_finding(tmp_path: Path) -> None:
    report = tmp_path / "audit.json"
    report.write_text(
        json.dumps({"dependencies": [{"name": "example", "vulns": [{"id": "CVE-2020-0001"}]}]}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "pip-audit", "1", str(report)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "::warning::" in result.stdout


@pytest.mark.parametrize(
    ("scanner", "status", "contents"),
    [
        ("pip-audit", "1", '{"dependencies": []}'),
        ("pip-audit", "2", '{"dependencies": []}'),
        ("pip-audit", "0", "not json"),
        ("pnpm", "1", '{"error": {"code": "ENETWORK"}, "advisories": {}}'),
    ],
)
def test_report_rejects_failed_or_invalid_scans(
    tmp_path: Path, scanner: str, status: str, contents: str
) -> None:
    report = tmp_path / "audit.json"
    report.write_text(contents, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), scanner, status, str(report)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "::error::" in result.stdout
