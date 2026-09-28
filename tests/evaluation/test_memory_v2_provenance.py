from __future__ import annotations

from types import SimpleNamespace

import pytest

from evaluation import memory_v2_provenance
from scripts import gate0


def test_capture_code_identity_rejects_hidden_index_flags(monkeypatch):
    monkeypatch.setattr(
        memory_v2_provenance,
        "worktree_divergence",
        lambda: {"tracked": [], "hidden": ["h file.py"], "risky": []},
    )
    monkeypatch.setattr(
        memory_v2_provenance, "_git", lambda *_args: "a" * 40,
    )

    with pytest.raises(RuntimeError, match="verified committed worktree"):
        memory_v2_provenance.capture_code_identity()


def test_capture_code_identity_rejects_head_change(monkeypatch):
    identities = iter(("a" * 40, "b" * 40, "c" * 40, "d" * 40))
    monkeypatch.setattr(
        memory_v2_provenance, "_git", lambda *_args: next(identities),
    )
    monkeypatch.setattr(
        memory_v2_provenance,
        "worktree_divergence",
        lambda: {"tracked": [], "hidden": [], "risky": []},
    )

    with pytest.raises(RuntimeError, match="identity changed"):
        memory_v2_provenance.capture_code_identity()


@pytest.mark.parametrize("fail_at", (1, 2, 3))
def test_worktree_divergence_fails_when_a_git_probe_fails(monkeypatch, fail_at):
    calls = 0

    def fake_git(*_args):
        nonlocal calls
        calls += 1
        return SimpleNamespace(returncode=int(calls == fail_at), stdout="")

    monkeypatch.setattr(gate0, "git", fake_git)

    with pytest.raises(RuntimeError, match="cannot verify worktree divergence"):
        gate0.worktree_divergence()
