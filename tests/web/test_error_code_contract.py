"""跨端机读码的**机械闸门**（#227）。

**为什么必须有这条**（#225 的实测教训）：机读码在两侧各有一份字面量（后端常量
`web/artifacts.py::ARTIFACT_STORAGE_UNAVAILABLE`，前端 `web/src/lib/api.ts` 的
`ARTIFACT_STORAGE_UNAVAILABLE`）。两侧**各自**都有测试钉着自己那份字面量，于是：

- 只改一侧 ⇒ 该侧红（这一半已经有人守）；
- **两侧一起改、但改得不一致**（例：后端改成 `..._v2`、前端忘了跟）⇒ 后端测试绿
  （它比的是自己那份常量）、前端测试绿（它比的是自己那份字面量）、e2e 也绿
  （mock 是手写的，不读后端）——**全门禁绿而线上界面把这个 503 判成"通用失败态"**，
  用户看到一句与真因无关的失败。

本用例是那个方向的唯一闸门：**直接读前端源文件**，把它的字面量与后端常量对起来。
单边改名必红，失败信息指出该改哪两侧。同款先例：`tests/web/test_web_phase5_staged_endpoints.py`
的图标名对账（#217）、`tests/test_event_vocabulary_generated.py` 的词汇表对账。
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_harness.web import artifacts as artifacts_module

#: 前端那份字面量的**声明点**（跨端对账要读的源文件）。
_WEB_API_TS = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "api.ts"

#: 抓 `const ARTIFACT_STORAGE_UNAVAILABLE = '…'` 的赋值（值必须是单引号字符串字面量）。
_FRONTEND_CONST_RE = re.compile(
    r"const ARTIFACT_STORAGE_UNAVAILABLE = '([^']+)'"
)


def test_frontend_literal_matches_backend_constant():
    """前后端同值由**这一条**守住，不是靠两边各自自证。"""
    source = _WEB_API_TS.read_text(encoding="utf-8")
    match = _FRONTEND_CONST_RE.search(source)
    assert match is not None, (
        f"未能在 {_WEB_API_TS} 找到 `const ARTIFACT_STORAGE_UNAVAILABLE = '…'` 字面量"
        "（改名 / 改成模板串 / 挪走？）——本用例是跨端同值的唯一闸门，找不到就必须红，"
        "不能跳过"
    )
    assert match.group(1) == artifacts_module.ARTIFACT_STORAGE_UNAVAILABLE, (
        "前后端的 artifact 存储不可用码不同值："
        f"前端 {match.group(1)!r} vs 后端 {artifacts_module.ARTIFACT_STORAGE_UNAVAILABLE!r}——"
        "单边改名会让全门禁仍绿而界面把该 503 判成通用失败态（#225 的坑）"
    )
