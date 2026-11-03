"""Fetch and retain Git objects needed to verify historical gate receipts."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


def git(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], input=input_text, capture_output=True, encoding="utf-8", errors="replace", check=True
    )


def main() -> int:
    receipts = Path("docs/gate")
    shas = set()
    for path in receipts.glob("*.json"):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        sha = receipt["sha"]
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or path.stem != sha:
            raise ValueError(f"Invalid receipt SHA: {path}")
        # The existing receipt test independently checks the exact allowed exception.
        if receipt.get("source_object_verification") is None:
            shas.add(sha)

    if not shas:
        raise ValueError("No verifiable gate receipts found")
    reachable = set(git("rev-list", "--all").stdout.splitlines())
    dangling = sorted(shas - reachable)
    if not dangling:
        return 0

    probes = git(
        "cat-file", "--batch-check=%(objectname) %(objecttype)",
        input_text="".join(f"{sha}^{{commit}}\n" for sha in dangling),
    ).stdout.splitlines()
    missing = [sha for sha, result in zip(dangling, probes, strict=True) if result.endswith(" missing")]
    if missing:
        try:
            git("fetch", "--no-tags", "origin", *(f"{sha}:refs/gate-evidence/{sha}" for sha in missing))
        except subprocess.CalledProcessError as exc:
            print(f"Cannot fetch gate receipt commits: {', '.join(missing)}\n{exc.stderr}", file=sys.stderr)
            return 1

    for sha in sorted(set(dangling) - set(missing)):
        git("update-ref", f"refs/gate-evidence/{sha}", sha, "0" * 40)
    print(f"Retained {len(dangling)} gate receipt commits under refs/gate-evidence/")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"Gate receipt prerequisite failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
