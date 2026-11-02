"""The move-only note must survive workflow YAML parsing (#1622)."""

from pathlib import Path

import yaml


def test_move_only_note_preserves_the_complete_github_expression() -> None:
    workflow_path = Path(__file__).resolve().parents[2] / ".github/workflows/coverage-ratchet.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    expression = next(
        step["env"]["COVERAGE_RATCHET_MOVE_ONLY_NOTE"]
        for step in workflow["jobs"]["ratchet"]["steps"]
        if "COVERAGE_RATCHET_MOVE_ONLY_NOTE" in step.get("env", {})
    )

    assert expression.startswith(
        "${{ contains(github.event.pull_request.labels.*.name, 'coverage-ratchet:move-only') && "
    )
    assert "format('PR #{0} " in expression
    assert expression.endswith(", github.event.pull_request.number) || '' }}")
