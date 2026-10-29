"""The receipt prerequisite must fetch objects without weakening tree validation."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_gate_receipts.py"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, encoding="utf-8", errors="replace")
    return result.stdout.strip()


def test_fetches_unreferenced_receipt_commit_and_keeps_archived_exception(tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    source = tmp_path / "source"
    target = tmp_path / "target"
    remote.mkdir()
    source.mkdir()
    target.mkdir()
    git(remote, "init", "--bare")
    git(source, "init")
    git(source, "config", "user.name", "Tester")
    git(source, "config", "user.email", "test@example.com")
    (source / "payload").write_text("base", encoding="utf-8")
    git(source, "add", "payload")
    git(source, "commit", "-m", "base")
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "origin", "HEAD:refs/heads/main")
    (source / "payload").write_text("receipt source", encoding="utf-8")
    git(source, "add", "payload")
    git(source, "commit", "-m", "receipt")
    sha = git(source, "rev-parse", "HEAD")
    tree = git(source, "rev-parse", "HEAD^{tree}")
    git(source, "push", "origin", "HEAD:refs/evidence/source")

    git(target, "init")
    git(target, "remote", "add", "origin", str(remote))
    git(target, "fetch", "origin", "refs/heads/main:refs/remotes/origin/main")
    receipts = target / "docs" / "gate"
    receipts.mkdir(parents=True)
    (receipts / f"{sha}.json").write_text(json.dumps({"sha": sha, "tree": tree}), encoding="utf-8")
    archived = "f" * 40
    (receipts / f"{archived}.json").write_text(
        json.dumps({"sha": archived, "tree": "0" * 40, "source_object_verification": {"status": "UNVERIFIABLE_HISTORICAL"}}),
        encoding="utf-8",
    )
    assert subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=target, check=False).returncode != 0
    run = subprocess.run([sys.executable, str(SCRIPT)], cwd=target, encoding="utf-8", errors="replace", capture_output=True, check=False)
    assert run.returncode == 0, run.stderr
    assert git(target, "rev-parse", f"{sha}^{{tree}}") == tree
    assert git(target, "rev-parse", f"refs/gate-evidence/{sha}") == sha
    git(target, "update-ref", "-d", f"refs/gate-evidence/{sha}")
    run = subprocess.run([sys.executable, str(SCRIPT)], cwd=target, encoding="utf-8", errors="replace", capture_output=True, check=False)
    assert run.returncode == 0, run.stderr
    assert git(target, "rev-parse", f"refs/gate-evidence/{sha}") == sha


def test_unfetchable_receipt_stays_failure(tmp_path: Path) -> None:
    git(tmp_path, "init")
    receipts = tmp_path / "docs" / "gate"
    receipts.mkdir(parents=True)
    sha = "a" * 40
    (receipts / f"{sha}.json").write_text(json.dumps({"sha": sha, "tree": "b" * 40}), encoding="utf-8")
    run = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, encoding="utf-8", errors="replace", capture_output=True, check=False)
    assert run.returncode != 0
    assert sha in run.stderr
