"""Dependabot 纯版本号升级的机械归属 —— 覆盖闸门的第五条归属判据（2026-10-07）。

守的是：作者名可伪造，所以放行的证据必须是**逐行内容形状**。反控比正控更重要：
任何一行不是版本号形状、任何一个文件不在清单内、作者不是 Dependabot、文件表为空，都必须 False。
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PATH = os.environ.get("WBI_GATE_UNDER_TEST") or os.path.join(_ROOT, "scripts", "check_review_coverage.py")
_spec = importlib.util.spec_from_file_location("check_review_coverage_dependabot", _PATH)
crc = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(crc)

BOT = "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>"


def _diff(minus: str, plus: str) -> str:
    return f"--- a/x\n+++ b/x\n@@ -1 +1 @@\n-{minus}\n+{plus}\n"


PYPROJECT = _diff('    "fastapi>=0.141.1",', '    "fastapi>=0.142.2",')
PACKAGE = _diff('    "typescript": "~6.0.3",', '    "typescript": "~7.0.2",')
WORKFLOW = _diff(
    "        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0",
    "        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
)


@pytest.mark.parametrize(
    ("files", "diffs"),
    [
        (["pyproject.toml"], {"pyproject.toml": PYPROJECT}),
        (["pyproject.toml", "uv.lock"], {"pyproject.toml": PYPROJECT}),
        (["uv.lock"], {}),
        (["web/package.json", "web/pnpm-lock.yaml"], {"web/package.json": PACKAGE}),
        ([".github/workflows/gate0.yml"], {".github/workflows/gate0.yml": WORKFLOW}),
    ],
)
def test_pure_version_bump_is_attributed(files, diffs):
    assert crc.is_dependabot_version_bump(BOT, files, diffs)


def test_forged_or_other_author_is_rejected():
    assert not crc.is_dependabot_version_bump("someone <a@b.c>", ["pyproject.toml"], {"pyproject.toml": PYPROJECT})
    assert not crc.is_dependabot_version_bump(
        "dependabot[bot] <evil@example.com>", ["pyproject.toml"], {"pyproject.toml": PYPROJECT})


def test_empty_file_list_is_fail_closed():
    assert not crc.is_dependabot_version_bump(BOT, [], {})


def test_file_outside_allowlist_is_rejected():
    files = ["pyproject.toml", "scripts/gate0.py"]
    assert not crc.is_dependabot_version_bump(BOT, files, {"pyproject.toml": PYPROJECT, "scripts/gate0.py": PYPROJECT})


def test_manifest_with_no_readable_lines_is_fail_closed():
    assert not crc.is_dependabot_version_bump(BOT, ["pyproject.toml"], {"pyproject.toml": ""})


@pytest.mark.parametrize(
    ("path", "line"),
    [
        # pyproject 里能静默关掉车道的配置（gate0.yml 头部「不保证」段点名过）
        ("pyproject.toml", 'addopts = "--collect-only"'),
        ("pyproject.toml", "select = []"),
        ("pyproject.toml", "[tool.ruff]"),
        # package.json 的 scripts 段：值不是版本号形状
        ("web/package.json", '    "typecheck": "true",'),
        ("web/package.json", '    "lint": "echo ok",'),
        # workflow：run 行、tag 引用（非 40 位 SHA）、换 action 之外的任何改动
        (".github/workflows/gate0.yml", "        run: echo skip"),
        (".github/workflows/gate0.yml", "        uses: actions/checkout@v7"),
        (".github/workflows/gate0.yml", "    name: gate0-renamed"),
    ],
)
def test_any_non_version_line_is_rejected(path, line):
    good = {"pyproject.toml": PYPROJECT, "web/package.json": PACKAGE}.get(path, WORKFLOW)
    sneaky = good + f"+{line}\n"
    assert not crc.is_dependabot_version_bump(BOT, [path], {path: sneaky})


def test_changed_lines_skips_file_headers():
    assert crc.changed_lines("--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n context\n") == ["a", "b"]


def test_changed_lines_keeps_content_starting_with_dashes_or_pluses():
    # P1（2026-10-07）：内容行 `--- x` / `+++ x` 在 diff 里长成 `---- x` / `++++ x`，
    # 必须保留并参与形状核对，不能当文件头吞掉 —— 否则伪造 dependabot 作者可在
    # workflow 里夹带 `+++ uses: evil@<40hex>` 而闸门照样放行（实证：旧实现返回 True）。
    evil_sha = "a" * 40
    evil_diff = (
        "--- a/.github/workflows/gate0.yml\n"
        "+++ b/.github/workflows/gate0.yml\n"
        "@@ -1 +1 @@\n"
        "+        uses: actions/checkout@" + evil_sha + " # v7.0.1\n"
        "++++ uses: evil/action@" + evil_sha + "\n"
        "---- sneaky: removed-line\n"
    )
    assert crc.changed_lines(evil_diff) == [
        "        uses: actions/checkout@" + evil_sha + " # v7.0.1",
        "+++ uses: evil/action@" + evil_sha,
        "--- sneaky: removed-line",
    ]
    assert not crc.is_dependabot_version_bump(
        BOT, [".github/workflows/gate0.yml"], {".github/workflows/gate0.yml": evil_diff})
