"""real_eval 数据集：分层 Train/Test 的题目 schema 与加载（issue #1005）。

每行 JSONL 一条题（`datasets/train.jsonl` / `datasets/test.jsonl`）：
    {"name", "task", "case_type", "assertion", "expected", "judge_criteria",
     "split", "tags", "source"}

`case_type` 复用 `evaluation.runner` 的语义（tool_selection / recovery /
kill_resume）+ 本票新增 open_qa（J 断言）。`assertion.kind` 只用 D / J 两值：
D = 现有 deterministic 断言（代码判断），J = Judge 语义判定。
"""

from __future__ import annotations

import hashlib
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
#: `case_type ⟺ assertion.kind` 的**唯一**映射（open_qa 走 Judge，其余走确定性断言）。
#: 运行分流（runner 按 case_type）与统计分类（stats 按 assertion_kind）都读这个口径；
#: 二者不一致的行在加载期即 fail-closed，不静默误路由。
KIND_BY_CASE_TYPE: dict[str, str] = {
    "tool_selection": "D",
    "recovery": "D",
    "kill_resume": "D",
    "open_qa": "J",
}


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
        """断言类别；`assertion.kind` 缺失即 raise（不静默回落 D）。

        fail-closed：漏写 kind 的题必须由数据加载期暴露，不能被当成确定性断言跑。
        """
        kind = self.assertion.get("kind")
        if kind is None:
            raise ValueError(f"case {self.name!r} 缺 assertion.kind（不得缺省）")
        return str(kind)

    def validate_row(self) -> None:
        """fail-closed：数据集是输入，坏行不能静默跳过。"""
        kind = self.assertion.get("kind")
        if kind is None:
            raise ValueError(f"case {self.name!r} 缺 assertion.kind（不得缺省）")
        if self.assertion_kind not in ASSERTION_KINDS:
            raise ValueError(
                f"case {self.name!r} assertion.kind 非法: {self.assertion_kind!r}"
            )
        if self.case_type not in CASE_TYPES:
            raise ValueError(f"case {self.name!r} case_type 非法: {self.case_type!r}")
        if self.split not in SPLITS:
            raise ValueError(f"case {self.name!r} split 非法: {self.split!r}")
        expected_kind = KIND_BY_CASE_TYPE[self.case_type]
        if self.assertion_kind != expected_kind:
            raise ValueError(
                f"case {self.name!r} case_type={self.case_type!r} 与 "
                f"assertion.kind={self.assertion_kind!r} 不匹配（应为 {expected_kind!r}）"
            )


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


def dataset_digest(dataset_dir: str | Path | None = None) -> str:
    """train.jsonl + test.jsonl 的**合并内容哈希**（前 12 位）。

    内容即版本：数据集任何一行的改动都会改变它。`config_hash` 纳入本值，
    使「数据集换了但配置没换」也判为不可比（第 2 问要的 dataset 版本）。
    不存在的 split 文件跳过（空目录 → 空内容的哈希）。
    """
    root = Path(dataset_dir) if dataset_dir is not None else DATASETS_DIR
    digest = hashlib.sha256()
    for split in ("train", "test"):
        path = root / f"{split}.jsonl"
        if not path.exists():
            continue
        digest.update(split.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]
