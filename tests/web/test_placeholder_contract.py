"""M-11 占位文案契约闸门：前端常量 vs 后端权威源（#938）。

`IMAGE_OMITTED_PLACEHOLDER` 是后端真的会放进模型上下文的那句文案
（`attachments/projection.py::IMAGE_OMITTED_PLACEHOLDER`，逐字移植自 Pi）；前端
`web/src/lib/attachments.ts` 的同名常量必须与它逐字相等（界面标注与模型所见同源）。
两侧各自都有测试钉着自己那份 ⇒ "一边改写、另一边没跟"会让全门禁绿。本用例是
那个方向的唯一闸门：**直接读前端源文件**，用正则提取字面量与后端常量逐字比对，
单边改必红（失败信息写明两处路径）。

对账做法 ADAPT 自本仓 `tests/web/test_attachment_limits_contract.py`
（#824 / MM-04 留下的跨端契约先例）。
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_harness.attachments.projection import IMAGE_OMITTED_PLACEHOLDER

#: 前端常量的**声明点**（跨端对账要读的源文件）。
_WEB_ATTACHMENTS_TS = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "attachments.ts"

#: `export const IMAGE_OMITTED_PLACEHOLDER = '...';` 的字面量抓取式。
_PLACEHOLDER_RE = re.compile(r"export const IMAGE_OMITTED_PLACEHOLDER = '([^']*)';")


def test_web_placeholder_matches_backend():
    """前端 `IMAGE_OMITTED_PLACEHOLDER` == 后端 `projection.IMAGE_OMITTED_PLACEHOLDER`（逐字）。"""
    source = _WEB_ATTACHMENTS_TS.read_text(encoding="utf-8")
    match = _PLACEHOLDER_RE.search(source)
    assert match is not None, (
        f"未能在 {_WEB_ATTACHMENTS_TS} 找到 `export const IMAGE_OMITTED_PLACEHOLDER = '...';`"
        "（改名 / 挪走 / 换引号形式？）——本用例是前端文案与后端权威源同值的唯一闸门，"
        "找不到就必须红，不能跳过"
    )
    web_placeholder = match.group(1)
    assert web_placeholder == IMAGE_OMITTED_PLACEHOLDER, (
        f"前端占位文案（{_WEB_ATTACHMENTS_TS}）与后端权威源"
        f"（src/agent_harness/attachments/projection.py::IMAGE_OMITTED_PLACEHOLDER）不一致："
        f"前端={web_placeholder!r} 后端={IMAGE_OMITTED_PLACEHOLDER!r}。"
        "这条串是后端放进模型上下文的原句，改一侧必须逐字同步另一侧"
    )
