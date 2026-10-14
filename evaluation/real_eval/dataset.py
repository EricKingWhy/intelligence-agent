"""real_eval 数据集：分层 Train/Test 的题目 schema 与加载（issue #1005）。

每行 JSONL 一条题（`datasets/train.jsonl` / `datasets/test.jsonl`）：
    {"name", "task", "case_type", "assertion", "expected", "judge_criteria",
     "split", "tags", "source"}

`case_type` 复用 `evaluation.runner` 的语义（tool_selection / recovery /
kill_resume）+ 本票新增 open_qa（J 断言）。`assertion.kind` 只用 D / J 两值：
D = 现有 deterministic 断言（代码判断），J = Judge 语义判定。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_REAL_EVAL_DIR = Path(__file__).resolve().parent
DATASETS_DIR = _REAL_EVAL_DIR / "datasets"
MANIFEST_PATH = _REAL_EVAL_DIR / "manifest.json"

#: 断言类别（D = deterministic 代码断言；J = Judge 语义判定）。
ASSERTION_KINDS = frozenset({"D", "J"})
#: 题面类型（前三个复用 evaluation.runner；open_qa 为本票新增）。
CASE_TYPES = frozenset({"tool_selection", "recovery", "kill_resume", "open_qa"})
#: 数据划分。
SPLITS = frozenset({"train", "test"})


class RealEvalCase(BaseModel):
    """一道评测题（数据集一行）。"""

    name: str
    task: str
    case_type: str
    #: {"kind": "D"|"J"}——J 类可另带判分提示。
    assertion: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    judge_criteria: str = ""
    split: str = "train"
    tags: list[str] = Field(default_factory=list)
    source: str = ""

    @property
    def assertion_kind(self) -> str:
        return str(self.assertion.get("kind", "D"))

    def validate_row(self) -> None:
        """fail-closed：数据集是输入，坏行不能静默跳过。"""
        if self.assertion_kind not in ASSERTION_KINDS:
            raise ValueError(
                f"case {self.name!r} assertion.kind 非法: {self.assertion_kind!r}"
            )
        if self.case_type not in CASE_TYPES:
            raise ValueError(f"case {self.name!r} case_type 非法: {self.case_type!r}")
        if self.split not in SPLITS:
            raise ValueError(f"case {self.name!r} split 非法: {self.split!r}")


def load_cases_from_file(path: str | Path) -> list[RealEvalCase]:
    """读单个 JSONL（空行忽略），逐行校验。"""
    cases: list[RealEvalCase] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        case = RealEvalCase.model_validate(json.loads(line))
        case.validate_row()
        cases.append(case)
    return cases


def load_cases(dataset_dir: str | Path | None = None) -> list[RealEvalCase]:
    """读 train.jsonl + test.jsonl（顺序：train 在前），逐行校验、查重名。"""
    root = Path(dataset_dir) if dataset_dir is not None else DATASETS_DIR
    cases: list[RealEvalCase] = []
    for split in ("train", "test"):
        path = root / f"{split}.jsonl"
        if not path.exists():
            continue
        cases.extend(load_cases_from_file(path))
    seen: set[str] = set()
    for case in cases:
        if case.name in seen:
            raise ValueError(f"数据集重名: {case.name!r}")
        seen.add(case.name)
    return cases


def load_manifest(path: str | Path | None = None) -> dict[str, Any]:
    """读 manifest（每题来源 / 替换/排除理由 / 偏差说明）。"""
    target = Path(path) if path is not None else MANIFEST_PATH
    return json.loads(target.read_text(encoding="utf-8"))
