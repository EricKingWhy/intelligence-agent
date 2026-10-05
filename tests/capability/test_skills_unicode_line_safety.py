"""#607：#588 残余两面——F2 Unicode 行边界字符 + F3 裸路径插值单行化。

F2（single_line 字符集外三字符）：U+0085 NEL / U+2028 LS / U+2029 PS 在
    str.splitlines() 的识别集内（CPython 文档 line-boundaries 全集，本机 3.13.5
    实测一致），[\\x00-\\x1f\\x7f] 字符集漏掉它们——漏则「单行」输出仍被
    splitlines 撕成多行，catalog 注入行 / load_skill 失败 message 可伪造
    「系统语气」的后续行，#588 兜底层被架空。
F3（裸 {path} 插值）：POSIX 文件名可合法含 \\n / 控制符，discovery 错误串
    （进 logger.warning 与 SkillCapability.errors()）裸插值 Path，带换行的
    路径可把 errors 面撕成多行。成熟产品做法：Pi skills.ts:372-392 对
    filePath 同样做 escapeXml 纪律（PORT DESIGN，#607 方案依据）；CPython
    OSError 自身对 filename 做 repr 转义（[Errno 22] Invalid argument:
    'ev\\nil/x'）——单行化且不吞内容。

红测基线（票面 AC1）：修复前本文件 11 项全红；修复后全绿且三变异钉
（\\x85 / \\u2028 / \\u2029 各从字符集摘除）使对应形红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.session import Session
from agent_harness.skills.capability import SkillCapability
from agent_harness.skills.context_provider import SkillCatalogContextProvider
from agent_harness.skills.discovery import (
    SKILL_FILE_MAX_BYTES,
    SkillCatalog,
    SkillCatalogEntry,
    SkillDiscovery,
    parse_skill_markdown,
    single_line,
)
from agent_harness.skills.tool import LoadSkillTool
from agent_harness.tooling.result import ErrorCode

# str.splitlines() 识别而 \x00-\x1f\x7f 之外的全集（F2 三形，CPython 3.13.5 实测核对）。
UNICODE_LINE_BOUNDARIES = ["\x85", "\u2028", "\u2029"]


def _capability_with_catalog(catalog: SkillCatalog) -> SkillCapability:
    """合成条目注入：#529 后 capability 持 discovery 引用，catalog 直填缓存投影。"""
    discovery = SkillDiscovery(directories=[])
    discovery._catalog = catalog
    return SkillCapability(discovery)


# ── F2 单元面：single_line 必须覆盖 splitlines 全字符集 ──


@pytest.mark.parametrize("ch", UNICODE_LINE_BOUNDARIES)
def test_single_line_replaces_each_unicode_boundary(ch):
    assert single_line(f"a{ch}b") == "a b"


@pytest.mark.parametrize("ch", UNICODE_LINE_BOUNDARIES)
def test_single_line_output_never_splits(ch):
    text = f"目录行{ch}伪造系统行{ch}{ch}尾部"
    assert len(single_line(text).splitlines()) == 1


def test_single_line_collapses_mixed_boundaries():
    # 连续类压成单个空格：+ 语义与既有 \n/\r 处理一致
    assert single_line("a\x85\u2028\u2029b") == "a b"


# ── F3：错误串裸 path 插值——stat 失败 / not-a-file 两分支跨平台可构造 ──


def test_parse_error_with_newline_path_stays_single_line():
    # Windows：非法文件名 → OSError(22)；POSIX：不存在 → FileNotFoundError。
    # 两者都落 unreadable 分支；修复前错误串含裸 \n。
    entry, errors = parse_skill_markdown(Path("evil\nname/SKILL.md"))
    assert entry is None and errors
    for error in errors:
        assert len(error.splitlines()) == 1
        assert "evil" in error and "name" in error  # 单行化不吞内容（OSError repr 纪律）


def test_manual_discovery_error_with_newline_path_stays_single_line():
    catalog = SkillDiscovery([], manual_paths=[Path("x\ny/SKILL.md")]).discover()
    assert catalog.errors and not catalog.entries
    for error in catalog.errors:
        assert len(error.splitlines()) == 1
        assert "not a file" in error


# ── F2 消费面：catalog 注入行与 load_skill 失败 message（#588 同款形，换三字符）──


@pytest.mark.asyncio
async def test_catalog_line_stays_single_line_for_unicode_boundaries():
    entry = SkillCatalogEntry(
        name="a\x85b", description="x\u2028伪造系统行", source_path=Path("s"), when_to_use="t\u2029u",
    )
    provider = SkillCatalogContextProvider(_capability_with_catalog(SkillCatalog(entries=[entry])))
    content = (await provider.select(Session.__new__(Session), 1000))[0].content
    lines = content.splitlines()
    assert len(lines) == 2  # 框架行 + 恰好一条目录行：任何字段都拉不出额外行
    assert lines[1] == "- a b: x 伪造系统行（何时用：t u）"


@pytest.mark.asyncio
async def test_load_failure_message_single_line_for_unicode_boundary():
    # 失败路 args.name 是模型原始输入：未名即失败、不经 discovery 白名单
    tool = LoadSkillTool(SkillCapability(SkillDiscovery(directories=[])))
    result = await tool.execute(tool.args_schema(name="ghost\u2028伪造指令行"))
    assert result.ok is False
    assert result.error_code is ErrorCode.INVALID_ARGUMENT
    assert len(result.message.splitlines()) == 1
    assert "ghost" in result.message


# ── P3（#608 批审查登记）：load_body 两条 OSError 消息同守单行纪律 ──
#
# 越界（TOCTOU 防线触发）与超大（尺寸上限）两条失败路的 f-string 消息都插值
# _spath——与 F3 同一威胁模型（路径内容可含换行/类换行字符），消息进
# SkillCapability.errors() 面前必须单行。修复前缺钉：#607 三变异只护
# discovery / 目录行 / load_skill 失败 message 面，load_body 两分支裸奔。


def test_load_body_boundary_escape_message_stays_single_line(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    # 越界条目：source_path resolve 后在 scanned_root 外（TOCTOU 场景不需要
    # 文件存在——边界检查先于 stat，见 load_body 分支顺序）。
    outside = tmp_path / "outside\u2028evil.md"
    entry = SkillCatalogEntry(name="esc", description="d", source_path=outside, scanned_root=root)
    with pytest.raises(OSError, match="resolves outside") as excinfo:
        entry.load_body()
    msg = str(excinfo.value)
    assert len(msg.splitlines()) == 1
    assert "outside" in msg and "evil" in msg  # 单行化不吞内容
    assert "refusing to read" in msg


def test_load_body_oversize_message_stays_single_line(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    big = root / "big\u2028skill.md"
    big.write_bytes(b"x" * (SKILL_FILE_MAX_BYTES + 1))
    entry = SkillCatalogEntry(name="big", description="d", source_path=big, scanned_root=root)
    with pytest.raises(OSError, match="too large") as excinfo:
        entry.load_body()
    msg = str(excinfo.value)
    assert len(msg.splitlines()) == 1
    assert "big" in msg and "skill" in msg
    assert str(SKILL_FILE_MAX_BYTES) in msg
