"""#1353 AC10：记忆域的 Milvus 引用清零（票面原文）。

以下路径 grep `milvus|Milvus|pymilvus` 必须零命中：

- `src/agent_harness/memory/`
- `src/agent_harness/capability/wiring.py`
- `src/agent_harness/capability/factories.py`
- `pyproject.toml`
- `tests/memory/`

`knowledge/` 除外（RAG 侧的 Milvus 是独立评估，另案）。

# 为什么检查器自己要有一个自检

"零命中"有两种来源：真的清零，或扫描器根本没扫到东西（路径写错、扩展名过滤掉全部
文件、正则写坏）。后者的读数和前者一模一样。所以第二个用例先证明扫描器**能**抓到
命中，第一个用例的"零命中"才有意义。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 票面点名的五条路径（相对仓库根）。
TARGETS = (
    Path("src/agent_harness/memory"),
    Path("src/agent_harness/capability/wiring.py"),
    Path("src/agent_harness/capability/factories.py"),
    Path("pyproject.toml"),
    Path("tests/memory"),
)

#: 票面豁免：RAG 侧另案。
EXEMPT_PARTS = frozenset({"knowledge"})

#: 只扫文本类文件；二进制（sqlite / pyc / 图片）读不出"引用"，混进来只会制造噪声。
TEXT_SUFFIXES = frozenset({
    ".py", ".pyi", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".jsonl",
    ".md", ".txt", ".rst", ".sh", ".ps1",
})

PATTERN = re.compile(r"milvus", re.IGNORECASE)


def _iter_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in TEXT_SUFFIXES
        and "__pycache__" not in path.parts
        and not EXEMPT_PARTS.intersection(path.parts)
    )


def _display(root: Path, path: Path) -> str:
    """命中位置：仓库内用仓库相对路径，仓库外的临时夹具用其根相对路径。"""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        base = root if root.is_dir() else root.parent
        return str(path.relative_to(base))


def _scan(root: Path) -> list[str]:
    hits: list[str] = []
    for path in _iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if PATTERN.search(line):
                hits.append(f"{_display(root, path)}:{number}: {line.strip()}")
    return hits


def test_memory_domain_has_no_milvus_reference() -> None:
    """五条路径零命中；缺路径按 fail-closed 处理（不把"没扫到"当"没引用"）。"""
    missing = [str(target) for target in TARGETS if not (REPO_ROOT / target).exists()]
    assert missing == [], (
        f"AC10：它们不在预期位置，零命中不能成立（可能被改名或移动）：{missing}"
    )

    files = [path for target in TARGETS for path in _iter_files(REPO_ROOT / target)]
    assert files, "AC10：扫描集为空 —— 零命中会是假绿"

    hits = [hit for target in TARGETS for hit in _scan(REPO_ROOT / target)]
    assert hits == [], (
        "AC10：记忆域仍有 Milvus 引用（票面要求清零）：\n" + "\n".join(hits)
    )


def test_scanner_detects_a_planted_reference(tmp_path: Path) -> None:
    """扫描器自检：种一条 `Milvus` 必须被扫到，否则上一条的"零命中"不构成证据。"""
    planted = tmp_path / "planted.py"
    planted.write_text("# pymilvus is still imported here\n", encoding="utf-8")

    hits = _scan(tmp_path)

    assert len(hits) == 1, f"扫描器没有按预期抓到植入的引用：{hits}"
    assert "planted.py:1" in hits[0]


def test_knowledge_tree_is_out_of_scope(tmp_path: Path) -> None:
    """豁免面：`knowledge/` 下的引用不计入（票面明确另案）。"""
    exempt = tmp_path / "knowledge" / "index.py"
    exempt.parent.mkdir(parents=True)
    exempt.write_text("from pymilvus import connections\n", encoding="utf-8")

    assert _scan(tmp_path) == []


@pytest.mark.parametrize("spelling", ["milvus", "MILVUS", "Milvus", "pymilvus"])
def test_all_spellings_match(spelling: str, tmp_path: Path) -> None:
    """大小写与 `pymilvus` 都是同一个判据：票面写的就是 `milvus|Milvus|pymilvus`。"""
    target = tmp_path / "sample.py"
    target.write_text(f"x = {spelling!r}\n", encoding="utf-8")

    assert len(_scan(tmp_path)) == 1
