"""Validate scanner outcomes and surface findings without suppressing scanner failures."""

import json
import sys
from pathlib import Path


def main() -> int:
    scanner, status, path = sys.argv[1:]
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        if scanner == "pip-audit":
            findings = sum(len(dependency["vulns"]) for dependency in report["dependencies"])
        elif scanner == "pnpm":
            if report.get("error"):
                raise ValueError("registry audit returned an error")
            findings = len(report["advisories"])
        else:
            raise ValueError("unknown scanner")
        if int(status) not in (0, 1) or (int(status) == 1) != (findings > 0):
            raise ValueError("scanner exit status and report disagree")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"::error::{scanner} scan failed or produced an invalid report: {exc}")
        return 1
    if findings:
        print(f"::warning::{scanner} ({Path(path).stem}) reported {findings} advisory records; inspect the audit artifact")
    else:
        print(f"{scanner}: no reported advisories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
