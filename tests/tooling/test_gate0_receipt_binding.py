from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE0_PATH = REPO / "scripts" / "gate0.py"
LEDGER = "docs/review_ledger.tsv"
LEDGER_SHARD = "docs/review_ledger.d/001-ticket.tsv"


def _load_gate0():
    spec = importlib.util.spec_from_file_location("_gate0_i1555", GATE0_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate0 = _load_gate0()


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00",
    })
    return env


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False, env=_git_env(),
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout


def _git_blob(repo: Path, content: bytes) -> str:
    proc = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, capture_output=True,
        input=content, check=False, env=_git_env(),
    )
    assert proc.returncode == 0, f"git hash-object failed: {proc.stderr!r}"
    return proc.stdout.decode("ascii").strip()


def _commit(repo: Path, message: str, files: dict[str, bytes]) -> str:
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _ledger_fingerprint(entries: dict[str, bytes]) -> str:
    """Test-side encoding: sorted path + LF-normalized bytes with length framing."""
    digest = hashlib.sha256(b"intelligence-agent/review-ledger/v1\0")
    for path, content in sorted(entries.items()):
        path_bytes = path.encode("utf-8")
        normalized = content.replace(b"\r\n", b"\n")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(normalized).to_bytes(8, "big"))
        digest.update(normalized)
    return digest.hexdigest()


def _receipt(repo: Path, tested_sha: str, ledger: dict[str, bytes], base_sha: str,
             *, bound_ledger: dict[str, bytes] | None = None) -> bytes:
    tree = _git(repo, "rev-parse", f"{tested_sha}^{{tree}}").strip()
    lanes = [
        {"name": name, "status": "PASS", "rc": 0}
        for name in ("diff-check", "ruff", "oxlint", "tsc", "stylelint", "guards", "coverage")
    ]
    doc = {
        "schema": 1,
        "gate": "gate0",
        "sha": tested_sha,
        "tree": tree,
        "result": "PASS",
        "passed": 7,
        "total": 7,
        "lanes": lanes,
        "review_ledger": {
            "sha256": _ledger_fingerprint(ledger if bound_ledger is None else bound_ledger),
            "coverage": {"base": base_sha, "total": 1, "covered": 1},
        },
    }
    return (json.dumps(doc, indent=2) + "\n").encode()


def _receipt_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ledger_binding: bool = True,
    gate0_lanes: tuple[str, ...] | None = None,
    gate0_source_suffix: str = "",
) -> tuple[Path, str, str, dict[str, bytes]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "gate-test")
    _git(repo, "config", "user.email", "gate-test@example.invalid")
    ledger = {
        LEDGER: b"# legacy file\r\n",
        LEDGER_SHARD: b"date\tdesc\trange\n2020-01-01\toriginal evidence\tbase..HEAD\n",
    }
    if gate0_lanes is None:
        gate0_lanes = gate0.GATE0_LANE_IDS
    gate0_source = f"GATE0_LANE_IDS = {gate0_lanes!r}\n{gate0_source_suffix}"
    base_sha = _commit(repo, "base", {
        LEDGER: b"# legacy file\n",
        "scripts/gate0.py": gate0_source.encode(),
    })
    _git(repo, "branch", "origin/main", base_sha)
    tested_files = {
        "src/change.py": b"value = 1\n",
        **ledger,
    }
    if ledger_binding:
        tested_files["scripts/gate0.py"] = (
            gate0_source + f"# {gate0._LEDGER_BINDING_MARKER}\n"
        ).encode()
    tested_sha = _commit(repo, "code under review", tested_files)
    receipt = json.loads(_receipt(repo, tested_sha, ledger, base_sha))
    if not ledger_binding:
        receipt.pop("review_ledger")
    _commit(repo, "record gate receipt", {
        f"docs/gate/{tested_sha}.json": (json.dumps(receipt, indent=2) + "\n").encode(),
    })
    monkeypatch.setattr(gate0, "REPO_ROOT", str(repo))
    return repo, base_sha, tested_sha, ledger


@pytest.mark.parametrize("column", ["desc", "range"])
def test_receipt_rejects_ledger_fingerprint_from_post_tested_tree_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: str,
) -> None:
    """A receipt must bind the complete ledger in its tested SHA, not a later ledger."""
    repo, base_sha, tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    receipt_path = f"docs/gate/{tested_sha}.json"

    # E2/E3 shape: a later ledger edit changes desc or range, then an attacker/bug writes
    # that later content's digest into the old tested tree's receipt.
    shard = ledger[LEDGER_SHARD].decode()
    date, desc, revision = shard.splitlines()[1].split("\t")
    changed_row = {
        "desc": f"{date}\trewritten evidence\t{revision}\n",
        "range": f"{date}\t{desc}\t{base_sha}..{base_sha}\n",
    }[column]
    changed_ledger = {
        LEDGER: ledger[LEDGER],
        LEDGER_SHARD: f"date\tdesc\trange\n{changed_row}".encode(),
    }
    _commit(repo, f"post-test ledger mutation ({column})", changed_ledger)
    forged = _receipt(repo, tested_sha, ledger, base_sha, bound_ledger=changed_ledger)
    _commit(repo, "record mismatched receipt", {receipt_path: forged})

    receipts = gate0.head_tree_receipts(repo)
    assert not receipts[tested_sha]["valid"]
    assert "ledger" in receipts[tested_sha]["reason"].lower()
    ok, detail, _warn, _status = gate0.receipt_present_check(repo_root=repo, bootstrap_sha=base_sha)
    assert not ok, detail


@pytest.mark.parametrize("column", ["desc", "range"])
def test_receipt_present_rejects_natural_post_receipt_ledger_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: str,
) -> None:
    """A later docs commit must not silently change the ledger attested by a receipt."""
    repo, base_sha, tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    receipt_path = f"docs/gate/{tested_sha}.json"
    original_receipt_blob = _git(repo, "rev-parse", f"HEAD:{receipt_path}").strip()

    shard = ledger[LEDGER_SHARD].decode()
    date, desc, revision = shard.splitlines()[1].split("\t")
    changed_row = {
        "desc": f"{date}\trewritten evidence\t{revision}\n",
        "range": f"{date}\t{desc}\t{base_sha}..{base_sha}\n",
    }[column]
    _commit(repo, f"post-receipt ledger mutation ({column})", {
        LEDGER_SHARD: f"date\tdesc\trange\n{changed_row}".encode(),
    })

    # The receipt itself is unchanged and remains valid for its measured tree.
    assert _git(repo, "rev-parse", f"HEAD:{receipt_path}").strip() == original_receipt_blob
    receipts = gate0.head_tree_receipts(repo)
    assert receipts[tested_sha]["valid"], receipts[tested_sha]["reason"]

    ok, detail, _warn, _status = gate0.receipt_present_check(repo_root=repo, bootstrap_sha=base_sha)
    assert not ok, detail
    assert "ledger" in detail.lower()


def test_receipt_validation_reads_ledger_from_tested_git_tree_not_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _base_sha, tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)

    # A dirty working-tree mutation cannot rewrite what the receipt attested to.
    (repo / LEDGER_SHARD).write_bytes(
        ledger[LEDGER_SHARD].replace(b"original evidence", b"working tree mutation")
    )
    receipts = gate0.head_tree_receipts(repo)
    assert receipts[tested_sha]["valid"], receipts[tested_sha]["reason"]
    ok, detail, _warn, _status = gate0.receipt_present_check(
        repo_root=repo, bootstrap_sha=_git(repo, "rev-parse", "origin/main").strip(),
    )
    assert ok, detail


def test_ledger_fingerprint_covers_shards_and_normalizes_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _base_sha, tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    original = gate0.review_ledger_sha256(repo, tested_sha)
    assert original == _ledger_fingerprint(ledger)

    normalized_sha = _commit(repo, "normalize shard line endings", {
        LEDGER_SHARD: ledger[LEDGER_SHARD].replace(b"\n", b"\r\n"),
    })
    assert gate0.review_ledger_sha256(repo, normalized_sha) == original

    extra_shard_sha = _commit(repo, "append ledger shard", {
        "docs/review_ledger.d/002-ticket.tsv": b"2020-01-02\tsecond row\tbase..HEAD\n",
    })
    assert gate0.review_ledger_sha256(repo, extra_shard_sha) != original

    nested_shard_sha = _commit(repo, "append nested ledger shard", {
        "docs/review_ledger.d/archive/003-ticket.tsv": b"2020-01-03\tthird row\tbase..HEAD\n",
    })
    tree_request = f"{nested_shard_sha}^{{tree}}".encode()
    tree_oid, tree_kind, tree_content = gate0._cat_file_batch(repo, [tree_request])[tree_request]
    assert tree_kind == b"tree"
    assert gate0._review_ledger_sha256_many(
        repo, {tree_oid.decode(): tree_content},
    )[tree_oid.decode()] == gate0.review_ledger_sha256(repo, nested_shard_sha)


def test_ledger_fingerprint_rejects_symlinked_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checker follows shard symlinks, so the receipt fingerprint must reject them."""
    repo, base_sha, _tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    target = repo / "docs" / "ledger-target.tsv"
    target.write_bytes(ledger[LEDGER_SHARD])
    _git(repo, "add", "docs/ledger-target.tsv")
    link_blob = _git_blob(repo, b"../ledger-target.tsv")
    _git(repo, "update-index", "--add", "--cacheinfo",
         f"120000,{link_blob},{LEDGER_SHARD}")
    _git(repo, "commit", "-q", "-m", "replace review ledger shard with symlink")

    with pytest.raises(ValueError, match="regular file"):
        gate0.review_ledger_sha256(repo, "HEAD")

    tree_request = b"HEAD^{tree}"
    tree_oid, tree_kind, tree_content = gate0._cat_file_batch(repo, [tree_request])[tree_request]
    assert tree_kind == b"tree"
    with pytest.raises(ValueError, match="regular files or directories"):
        gate0._review_ledger_sha256_many(repo, {tree_oid.decode(): tree_content})

    ok, detail, _warn, _status = gate0.receipt_present_check(
        repo_root=repo, bootstrap_sha=base_sha,
    )
    assert not ok, detail
    assert "review ledger" in detail.lower()


def test_legacy_receipt_without_ledger_binding_remains_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base_sha, tested_sha, _ledger = _receipt_repo(
        tmp_path, monkeypatch, ledger_binding=False,
    )
    receipt_path = f"docs/gate/{tested_sha}.json"

    receipts = gate0.head_tree_receipts(repo)
    assert receipts[tested_sha]["valid"], receipts[tested_sha]["reason"]
    assert "review_ledger" not in json.loads(_git(repo, "show", f"HEAD:{receipt_path}"))
    ok, detail, _warn, _status = gate0.receipt_present_check(repo_root=repo, bootstrap_sha=base_sha)
    assert ok, detail


def test_post_landing_legacy_receipt_edit_invalidates_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base_sha, tested_sha, _ledger = _receipt_repo(
        tmp_path, monkeypatch, ledger_binding=False,
    )
    receipt_path = f"docs/gate/{tested_sha}.json"
    receipt = json.loads(_git(repo, "show", f"HEAD:{receipt_path}"))
    receipt["description"] = "rewritten legacy attestation"
    _commit(repo, "rewrite legacy receipt description", {
        receipt_path: (json.dumps(receipt, indent=2) + "\n").encode(),
    })

    assert gate0.head_tree_receipts(repo)[tested_sha]["valid"]
    ok, detail, _warn, _status = gate0.receipt_present_check(
        repo_root=repo, bootstrap_sha=base_sha,
    )
    assert not ok, detail
    assert "receipt changed after first landing" in detail


def test_post_landing_coverage_summary_edit_invalidates_bound_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base_sha, tested_sha, _ledger = _receipt_repo(tmp_path, monkeypatch)
    receipt_path = f"docs/gate/{tested_sha}.json"
    receipt = json.loads(_git(repo, "show", f"HEAD:{receipt_path}"))
    receipt["review_ledger"]["coverage"]["covered"] = 0
    _commit(repo, "rewrite attested coverage summary", {
        receipt_path: (json.dumps(receipt, indent=2) + "\n").encode(),
    })

    # Shape and ledger contents still look valid; the original attestation blob changed.
    assert gate0.head_tree_receipts(repo)[tested_sha]["valid"]
    ok, detail, _warn, _status = gate0.receipt_present_check(
        repo_root=repo, bootstrap_sha=base_sha,
    )
    assert not ok, detail
    assert "receipt changed after first landing" in detail


def test_historical_legacy_lane_manifest_is_frozen() -> None:
    assert gate0._GATE0_LEGACY_LANE_IDS == (
        "diff-check", "ruff", "oxlint", "tsc", "guards", "coverage",
    )


def test_historical_receipt_validation_batches_git_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base_sha, _tested_sha, ledger = _receipt_repo(
        tmp_path, monkeypatch, ledger_binding=False,
    )
    for index in range(5):
        tested_sha = _commit(repo, f"historical code {index}", {
            f"src/history-{index}.py": f"value = {index}\n".encode(),
        })
        old_receipt = json.loads(_receipt(repo, tested_sha, ledger, base_sha))
        old_receipt.pop("review_ledger")
        _commit(repo, f"historical receipt {index}", {
            f"docs/gate/{tested_sha}.json": (json.dumps(old_receipt) + "\n").encode(),
        })

    batch_calls = []
    original_git_at_bytes = gate0._git_at_bytes
    original_git_at = gate0._git_at

    def count_batches(repo_root, *args, **kwargs):
        if args[:2] == ("cat-file", "--batch"):
            batch_calls.append(args)
        return original_git_at_bytes(repo_root, *args, **kwargs)

    def reject_per_receipt_git_calls(repo_root, *args):
        if args and args[0] in ("show", "rev-parse"):
            pytest.fail(f"receipt validation spawned a per-object Git process: {args!r}")
        return original_git_at(repo_root, *args)

    monkeypatch.setattr(gate0, "_git_at_bytes", count_batches)
    monkeypatch.setattr(gate0, "_git_at", reject_per_receipt_git_calls)
    receipts = gate0.head_tree_receipts(repo)

    assert len(receipts) == 6
    assert all(receipt["valid"] for receipt in receipts.values())
    assert 1 <= len(batch_calls) <= 5


def test_legacy_receipt_still_detects_post_receipt_ledger_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base_sha, tested_sha, _ledger = _receipt_repo(
        tmp_path, monkeypatch, ledger_binding=False,
    )
    receipt_path = f"docs/gate/{tested_sha}.json"
    assert "review_ledger" not in json.loads(_git(repo, "show", f"HEAD:{receipt_path}"))
    assert gate0.head_tree_receipts(repo)[tested_sha]["valid"]

    _commit(repo, "mutate ledger after legacy receipt", {
        LEDGER_SHARD: b"date\tdesc\trange\n2020-01-01\tchanged evidence\tbase..HEAD\n",
    })
    ok, detail, _warn, _status = gate0.receipt_present_check(repo_root=repo, bootstrap_sha=base_sha)
    assert not ok
    assert "stale ledger fingerprint" in detail


def test_post_rollout_receipt_cannot_omit_ledger_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base_sha, tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    receipt_path = f"docs/gate/{tested_sha}.json"
    old_shape = json.loads(_receipt(repo, tested_sha, ledger, base_sha))
    old_shape.pop("review_ledger")
    _commit(repo, "drop required ledger binding", {
        receipt_path: (json.dumps(old_shape, indent=2) + "\n").encode(),
    })

    receipts = gate0.head_tree_receipts(repo)
    assert not receipts[tested_sha]["valid"]
    assert "review_ledger" in receipts[tested_sha]["reason"]


@pytest.mark.parametrize("source_available", [False, True], ids=["missing-object", "resolved-object"])
def test_unverifiable_historical_receipt_is_not_valid_gate0_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    source_available: bool,
) -> None:
    repo, base_sha, tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    receipt_sha = tested_sha if source_available else "0" * 40
    archived = json.loads(_receipt(repo, tested_sha, ledger, base_sha))
    archived["sha"] = receipt_sha
    archived["source_object_verification"] = {
        "status": "UNVERIFIABLE_HISTORICAL",
        "checked_at": "2026-10-27",
        "reason_code": "source_absent_in_checked_local_and_github_repositories",
    }
    _commit(repo, "retain unverifiable historical receipt", {
        f"docs/gate/{receipt_sha}.json": (json.dumps(archived, indent=2) + "\n").encode(),
    })

    result = gate0.head_tree_receipts(repo)[receipt_sha]
    assert not result["valid"]
    assert "unverifiable" in result["reason"]


@pytest.mark.parametrize(
    "mutation", ["missing", "failed", "duplicate", "empty", "unknown"]
)
def test_receipt_requires_a_nonempty_all_pass_lane_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    repo, base_sha, tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    receipt_path = f"docs/gate/{tested_sha}.json"
    receipt_doc = json.loads(_receipt(repo, tested_sha, ledger, base_sha))

    if mutation == "missing":
        receipt_doc["lanes"].pop()
    elif mutation == "failed":
        receipt_doc["lanes"][0]["status"] = "FAIL"
        receipt_doc["lanes"][0]["rc"] = 1
    elif mutation == "duplicate":
        receipt_doc["lanes"][1]["name"] = receipt_doc["lanes"][0]["name"]
    elif mutation == "unknown":
        receipt_doc["lanes"][0]["name"] = "invented-gate-lane"
    else:
        receipt_doc["lanes"] = []
        receipt_doc["passed"] = 0
        receipt_doc["total"] = 0

    _commit(repo, f"write malformed lane manifest ({mutation})", {
        receipt_path: (json.dumps(receipt_doc, indent=2) + "\n").encode(),
    })

    receipt = gate0.head_tree_receipts(repo)[tested_sha]
    assert not receipt["valid"]
    assert ("result/passed/total" in receipt["reason"] if mutation == "empty"
            else "车道" in receipt["reason"])


def test_receipt_accepts_legacy_gate_manifest_without_stylelint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_lanes = tuple(name for name in gate0.GATE0_LANE_IDS if name != "stylelint")
    repo, _base_sha, tested_sha, ledger = _receipt_repo(
        tmp_path, monkeypatch, gate0_lanes=legacy_lanes,
    )
    receipt_path = f"docs/gate/{tested_sha}.json"
    receipt_doc = json.loads(_receipt(repo, tested_sha, ledger, _base_sha))
    receipt_doc["lanes"] = [lane for lane in receipt_doc["lanes"] if lane["name"] != "stylelint"]
    receipt_doc["passed"] = receipt_doc["total"] = len(receipt_doc["lanes"])

    _commit(repo, "write legacy six-lane receipt", {
        receipt_path: (json.dumps(receipt_doc, indent=2) + "\n").encode(),
    })

    receipt = gate0.head_tree_receipts(repo)[tested_sha]
    assert receipt["valid"], receipt["reason"]


def test_receipt_rejects_seven_lane_manifest_for_legacy_gate_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_lanes = tuple(name for name in gate0.GATE0_LANE_IDS if name != "stylelint")
    repo, _base_sha, tested_sha, _ledger = _receipt_repo(
        tmp_path, monkeypatch, gate0_lanes=legacy_lanes,
    )

    receipt = gate0.head_tree_receipts(repo)[tested_sha]
    assert not receipt["valid"]
    assert "车道清单与被测 Gate-0 定义不符" in receipt["reason"]


def test_receipt_rejects_omitting_stylelint_from_tested_gate_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _base_sha, tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    receipt_path = f"docs/gate/{tested_sha}.json"
    receipt_doc = json.loads(_receipt(repo, tested_sha, ledger, _base_sha))
    receipt_doc["lanes"] = [lane for lane in receipt_doc["lanes"] if lane["name"] != "stylelint"]
    receipt_doc["passed"] = receipt_doc["total"] = len(receipt_doc["lanes"])

    _commit(repo, "omit required stylelint lane from receipt", {
        receipt_path: (json.dumps(receipt_doc, indent=2) + "\n").encode(),
    })

    receipt = gate0.head_tree_receipts(repo)[tested_sha]
    assert not receipt["valid"]
    assert "被测 Gate-0 定义不符" in receipt["reason"]


@pytest.mark.parametrize(
    "source_suffix",
    [
        "if True:\n    GATE0_LANE_IDS = ('other-lane',)\n",
        "def GATE0_LANE_IDS():\n    pass\n",
        "class GATE0_LANE_IDS:\n    pass\n",
        "import collections as GATE0_LANE_IDS\n",
        "from collections import namedtuple as GATE0_LANE_IDS\n",
        "try:\n    raise RuntimeError()\nexcept RuntimeError as GATE0_LANE_IDS:\n    pass\n",
        "match None:\n    case GATE0_LANE_IDS:\n        pass\n",
        "match []:\n    case [*GATE0_LANE_IDS]:\n        pass\n",
        "match {}:\n    case {**GATE0_LANE_IDS}:\n        pass\n",
    ],
    ids=["conditional-assignment", "function-binding", "class-binding",
         "import-alias", "from-import-alias", "exception-binding", "match-as",
         "match-star", "match-mapping-rest"],
)
def test_receipt_rejects_legacy_manifest_with_ambiguous_gate_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_suffix: str,
) -> None:
    legacy_lanes = tuple(name for name in gate0.GATE0_LANE_IDS if name != "stylelint")
    repo, _base_sha, tested_sha, ledger = _receipt_repo(
        tmp_path,
        monkeypatch,
        gate0_lanes=legacy_lanes,
        gate0_source_suffix=source_suffix,
    )
    receipt_path = f"docs/gate/{tested_sha}.json"
    receipt_doc = json.loads(_receipt(repo, tested_sha, ledger, _base_sha))
    receipt_doc["lanes"] = [lane for lane in receipt_doc["lanes"] if lane["name"] != "stylelint"]
    receipt_doc["passed"] = receipt_doc["total"] = len(receipt_doc["lanes"])

    _commit(repo, "record receipt with ambiguous Gate-0 source", {
        receipt_path: (json.dumps(receipt_doc, indent=2) + "\n").encode(),
    })

    receipt = gate0.head_tree_receipts(repo)[tested_sha]
    assert not receipt["valid"]
    assert "被测 Gate-0 定义不符" in receipt["reason"]


@pytest.mark.parametrize("column", ["desc", "range"])
def test_full_gate_run_can_refresh_stale_ledger_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: str,
) -> None:
    repo, base_sha, _tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(gate0, "_RECEIPT_BOOTSTRAP_SHA", base_sha)
    shard = ledger[LEDGER_SHARD].decode()
    date, desc, revision = shard.splitlines()[1].split("\t")
    changed_row = {
        "desc": f"{date}\trewritten evidence\t{revision}\n",
        "range": f"{date}\t{desc}\t{base_sha}..{base_sha}\n",
    }[column]
    current_sha = _commit(repo, f"change ledger before refresh ({column})", {
        LEDGER_SHARD: f"date\tdesc\trange\n{changed_row}".encode(),
    })

    stale_ok, stale_detail, _warn, _status = gate0.receipt_present_check(
        repo_root=repo, bootstrap_sha=base_sha,
    )
    assert not stale_ok
    assert "stale ledger fingerprint" in stale_detail

    results = []
    coverage_output = (
        f"覆盖区间: {base_sha[:12]}..HEAD\n"
        "提交总数 1 / 已审查 1 / 待判定 0\n"
    )
    for name in ("diff-check", "ruff", "oxlint", "tsc", "stylelint", "guards", "coverage"):
        lane = gate0.Lane(name, name, ["python", "-c", "pass"], str(repo))
        results.append((lane, 0, 0.1, coverage_output if name == "coverage" else ""))
    gate0._apply_receipt_check(results, will_record_receipt=True)
    assert all(rc == 0 for _lane, rc, _seconds, _output in results)

    tree = _git(repo, "rev-parse", f"{current_sha}^{{tree}}").strip()
    receipt_path = gate0.write_reading(
        head=current_sha, tree=tree, argv=[], wall=0.7, results=results,
        affected=None, changed=[], worktree={"untracked": []},
    )
    receipt_doc = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    assert receipt_doc["result"] == "PASS"
    _commit(repo, "record refreshed ledger receipt", {
        f"docs/gate/{current_sha}.json": Path(receipt_path).read_bytes(),
    })

    refreshed_ok, refreshed_detail, _warn, _status = gate0.receipt_present_check(
        repo_root=repo, bootstrap_sha=base_sha,
    )
    assert refreshed_ok, refreshed_detail


@pytest.mark.parametrize(
    ("selected", "will_record_receipt", "failed_lane"),
    [
        (("guards",), True, None),
        (gate0.GATE0_LANE_IDS, False, None),
        (gate0.GATE0_LANE_IDS, True, "ruff"),
    ],
    ids=["only-guards", "no-record", "failed-lane"],
)
def test_stale_ledger_refresh_requires_full_successful_recording_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected: tuple[str, ...],
    will_record_receipt: bool,
    failed_lane: str | None,
) -> None:
    repo, base_sha, _tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(gate0, "_RECEIPT_BOOTSTRAP_SHA", base_sha)
    _commit(repo, "change ledger before attempted refresh", {
        LEDGER_SHARD: ledger[LEDGER_SHARD].replace(b"original evidence", b"updated evidence"),
    })

    coverage_output = (
        f"覆盖区间: {base_sha[:12]}..HEAD\n"
        "提交总数 1 / 已审查 1 / 待判定 0\n"
    )
    results = [
        (lane, int(lane.name == failed_lane), 0.1,
         coverage_output if lane.name == "coverage" else "")
        for lane in gate0.build_lanes("")
        if lane.name in selected
    ]
    gate0._apply_receipt_check(results, will_record_receipt=will_record_receipt)

    guard = next(result for result in results if result[0].name == "guards")
    assert guard[1] == 1
    assert "stale ledger fingerprint" in guard[3]


def test_stale_receipts_for_multiple_code_trees_cannot_refresh_from_one_head_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base_sha, _first_code_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(gate0, "_RECEIPT_BOOTSTRAP_SHA", base_sha)
    second_code_sha = _commit(repo, "second code change", {"src/second.py": b"value = 2\n"})
    _commit(repo, "record second gate receipt", {
        f"docs/gate/{second_code_sha}.json": _receipt(repo, second_code_sha, ledger, base_sha),
    })
    _commit(repo, "change ledger after both receipts", {
        LEDGER_SHARD: ledger[LEDGER_SHARD].replace(b"original evidence", b"updated evidence"),
    })

    ok, detail, _warn, _status = gate0.receipt_present_check(
        repo_root=repo, bootstrap_sha=base_sha,
    )

    assert not ok
    assert "stale ledger fingerprint" in detail


@pytest.mark.parametrize(
    ("selected", "will_record_receipt", "failed_lane"),
    [
        (gate0.GATE0_LANE_IDS, True, None),
        (("guards",), True, None),
        (gate0.GATE0_LANE_IDS, False, None),
        (gate0.GATE0_LANE_IDS, True, "ruff"),
        (gate0.GATE0_LANE_IDS, True, "coverage"),
    ],
    ids=["full-success", "only-guards", "no-record", "failed-lane", "coverage-pending"],
)
def test_first_receipt_bootstrap_requires_full_successful_recording_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected: tuple[str, ...],
    will_record_receipt: bool,
    failed_lane: str | None,
) -> None:
    repo, _base_sha, tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(gate0, "_RECEIPT_BOOTSTRAP_SHA", tested_sha)
    _git(repo, "branch", "-f", "origin/main", tested_sha)
    _commit(repo, "change ledger before first current receipt", {
        LEDGER_SHARD: ledger[LEDGER_SHARD].replace(b"original evidence", b"updated evidence"),
    })
    current_sha = _commit(repo, "new code without a receipt", {
        "src/new-code.py": b"value = 2\n",
    })

    missing_ok, missing_detail, _warn, _status = gate0.receipt_present_check(
        repo_root=repo, bootstrap_sha=tested_sha,
    )
    assert not missing_ok
    assert "missing receipt" in missing_detail
    assert "stale ledger fingerprint" not in missing_detail

    coverage_output = (
        f"覆盖区间: {tested_sha[:12]}..HEAD\n"
        "提交总数 2 / 已审查 2 / 待判定 0\n"
    )
    results = [
        (lane, int(lane.name == failed_lane), 0.1,
         coverage_output if lane.name == "coverage" else "")
        for lane in gate0.build_lanes("")
        if lane.name in selected
    ]
    gate0._apply_receipt_check(results, will_record_receipt=will_record_receipt)
    guard = next(result for result in results if result[0].name == "guards")

    can_bootstrap = will_record_receipt and selected == gate0.GATE0_LANE_IDS and failed_lane is None
    coverage_pending = (
        will_record_receipt and selected == gate0.GATE0_LANE_IDS and failed_lane == "coverage"
    )
    assert (guard[1] == 0) is (can_bootstrap or coverage_pending)
    if not can_bootstrap:
        if coverage_pending:
            assert next(result for result in results if result[0].name == "coverage")[1] == 1
            assert sum(rc != 0 for _lane, rc, _seconds, _output in results) == 1
            return
        assert "missing receipt" in guard[3]
        return

    tree = _git(repo, "rev-parse", f"{current_sha}^{{tree}}").strip()
    receipt_path = gate0.write_reading(
        head=current_sha, tree=tree, argv=[], wall=0.7, results=results,
        affected=None, changed=[], worktree={"untracked": []},
    )
    receipt_doc = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    assert receipt_doc["result"] == "PASS"
    assert receipt_doc["passed"] == receipt_doc["total"] == len(gate0.GATE0_LANE_IDS)
    _commit(repo, "record first full gate receipt", {
        f"docs/gate/{current_sha}.json": Path(receipt_path).read_bytes(),
    })
    receipt = gate0.head_tree_receipts(repo)[current_sha]
    assert receipt["valid"], receipt["reason"]


def test_full_gate_cannot_attest_missing_receipts_across_code_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base_sha, _first_code_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(gate0, "_RECEIPT_BOOTSTRAP_SHA", base_sha)
    _commit(repo, "change ledger after existing receipt", {
        LEDGER_SHARD: ledger[LEDGER_SHARD].replace(b"original evidence", b"updated evidence"),
    })
    _commit(repo, "first code change without a receipt", {"src/first.py": b"value = 2\n"})
    _commit(repo, "second code change without a receipt", {"src/second.py": b"value = 3\n"})

    results = [
        (lane, 0, 0.1, "")
        for lane in gate0.build_lanes("")
    ]
    gate0._apply_receipt_check(results, will_record_receipt=True)

    guard = next(result for result in results if result[0].name == "guards")
    assert guard[1] == 1
    assert "missing receipt" in guard[3]


def test_written_receipt_records_ledger_fingerprint_and_coverage_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base_sha, tested_sha, ledger = _receipt_repo(tmp_path, monkeypatch)
    coverage = gate0.Lane("coverage", "coverage", ["python", "scripts/check_review_coverage.py"], str(repo))
    output = (
        f"覆盖区间: {base_sha[:12]}..HEAD\n"
        "提交总数 1 / 已审查 1 / 待判定 0\n"
    )
    result_path = gate0.write_reading(
        head=tested_sha,
        tree=_git(repo, "rev-parse", f"{tested_sha}^{{tree}}").strip(),
        argv=[],
        wall=0.1,
        results=[(coverage, 0, 0.1, output)],
        affected=None,
        changed=[],
        worktree={"untracked": []},
    )
    doc = json.loads(Path(result_path).read_text(encoding="utf-8"))
    assert doc["review_ledger"] == {
        "sha256": _ledger_fingerprint(ledger),
        "coverage": {"base": base_sha, "total": 1, "covered": 1},
    }
