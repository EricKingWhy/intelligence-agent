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


def run_script(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=repo, encoding="utf-8", errors="replace", capture_output=True, check=False
    )


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
    run = run_script(target)
    assert run.returncode == 0, run.stderr
    assert git(target, "rev-parse", f"{sha}^{{tree}}") == tree
    assert git(target, "rev-parse", f"refs/gate-evidence/{sha}") == sha
    # Turn "unreferenced" into "reachable only through a ref a bundle clone drops": this line
    # stands for *every* non-branch/tag namespace (remote-tracking, notes, stash, …) — a
    # bundle clone imports none of them, so the script must treat the receipt as dangling and
    # rebuild `refs/gate-evidence/<sha>` from the local object.
    git(target, "update-ref", "-d", f"refs/gate-evidence/{sha}")
    git(target, "update-ref", "refs/remotes/audit/receipt", sha)
    run = run_script(target)
    assert run.returncode == 0, run.stderr
    assert git(target, "rev-parse", f"refs/gate-evidence/{sha}") == sha
    # Re-running is a no-op: a receipt reachable through `refs/gate-evidence/*` is already
    # preserved — the other half of the predicate (`--glob=refs/gate-evidence/*`). Without
    # it the existing ref would be rebuilt and `update-ref` would fail on it.
    run = run_script(target)
    assert run.returncode == 0, run.stderr
    bundle = tmp_path / "evidence.bundle"
    clone = tmp_path / "bundle-clone"
    git(target, "bundle", "create", str(bundle), "--all")
    git(tmp_path, "clone", "--no-checkout", str(bundle), str(clone))
    git(clone, "bundle", "verify", str(bundle))
    git(clone, "fetch", "origin", "refs/gate-evidence/*:refs/gate-evidence/*")
    git(clone, "gc", "--prune=now")
    assert git(clone, "rev-parse", f"refs/gate-evidence/{sha}^{{tree}}") == tree


def test_branch_reachable_receipt_gets_no_new_evidence_ref(tmp_path: Path) -> None:
    """A receipt already reachable from a branch survives without a synthetic evidence ref.

    `--branches --tags` is what tells the script "this one is already safe". Dropping either
    half turns every receipt behind a plain branch into a dangling one and rebuilds
    `refs/gate-evidence/*` for it — the 300-ref drift a real repository hit.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "payload").write_text("base", encoding="utf-8")
    git(repo, "add", "payload")
    git(repo, "commit", "-m", "base")
    (repo / "payload").write_text("receipt source", encoding="utf-8")
    git(repo, "add", "payload")
    git(repo, "commit", "-m", "receipt")
    sha = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    # Park the receipt commit on a local branch, then move the checked-out branch past it.
    git(repo, "branch", "evidence-holder", sha)
    git(repo, "reset", "--hard", "HEAD~1")
    receipts = repo / "docs" / "gate"
    receipts.mkdir(parents=True)
    (receipts / f"{sha}.json").write_text(json.dumps({"sha": sha, "tree": tree}), encoding="utf-8")

    run = run_script(repo)
    assert run.returncode == 0, run.stderr
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/gate-evidence/{sha}"], cwd=repo, check=False
    ).returncode != 0


def test_unfetchable_receipt_stays_failure(tmp_path: Path) -> None:
    git(tmp_path, "init")
    receipts = tmp_path / "docs" / "gate"
    receipts.mkdir(parents=True)
    sha = "a" * 40
    (receipts / f"{sha}.json").write_text(json.dumps({"sha": sha, "tree": "b" * 40}), encoding="utf-8")
    run = run_script(tmp_path)
    assert run.returncode != 0
    assert sha in run.stderr
