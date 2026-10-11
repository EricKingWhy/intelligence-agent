"""跨端**引用形状**契约闸门：`ImageRef` ↔ `web/src/lib/attachmentRefs.ts`（#939 M-07）。

**为什么必须有这一条**：事件里 `user/message.data["attachments"]` 一条引用的形状在
两侧各写一遍——后端 pydantic 模型 `attachments.projection.ImageRef`（持久化契约
`attachments.types.ImageAttachmentRef` 是它的基类），前端 TS 接口
`attachmentRefs.ts::ImageAttachmentRef`。两侧各有自己的测试钉着自己那份，于是
「后端加了字段、前端漏跟」会让**全门禁绿**：后端把新字段写进事件、前端解析器丢弃它，
表现是"某个字段在 UI 上永远是缺省值"——正是 `ImageRef.name` 这类缺省字段的失效形态。

这是 **web 那份镜像**的唯一闸门：**直接读前端源文件**，把它的字段集与后端模型逐项
对起来。（TUI 的 `tui/src/adapter.ts::TurnImageRef` 是同一持久化形状的第二份手写
副本、目前仍无守卫——已登记残余，别把本文件读成"该方向已封口"。）
单边改必红。同款先例：`tests/web/test_attachment_limits_contract.py`（跨端上限）、
`tests/web/test_error_code_contract.py`（跨端机读码）。

判定口径（刻意窄）：只对**字段名集合**与**判别字面量**做机械对账。类型映射
（`str`↔`string`、`int`↔`number`）与可选性走同一张表，不做跨语言类型推演——
后者做不到机械可靠，硬做只会变成一套需要自己维护的翻译器。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args, get_type_hints

from agent_harness.attachments.projection import KIND_IMAGE, ImageRef

#: 前端那份声明的**唯一**声明点（跨端对账要读的源文件）。
_WEB_REFS_TS = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "attachmentRefs.ts"

#: 接口块的开头；块体一直取到首个顶格 `}`（接口内无嵌套对象字面量，见文件本身）。
_INTERFACE_OPEN_RE = re.compile(r"^export interface ImageAttachmentRef \{$", re.MULTILINE)
#: 块注释（JSDoc）——字段抓取前先剥掉，否则注释里的 `名: 值` 会被当成字段。
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
#: 一行字段声明：`  name?: string | null;` → (名字, 是否可选, 类型)。
_FIELD_RE = re.compile(r"^\s*(\w+)(\??):\s*(.+);$", re.MULTILINE)
#: 注释里的模块坐标（`= 后端 \`a.b.C\``）——曾经指错模块（#939 M-07）。
_BACKEND_COORD_RE = re.compile(r"=\s*后端\s*`([\w.]+)`")


def _ts_interface_body() -> str:
    source = _WEB_REFS_TS.read_text(encoding="utf-8")
    match = _INTERFACE_OPEN_RE.search(source)
    assert match is not None, (
        f"未能在 {_WEB_REFS_TS} 找到顶格的 `export interface ImageAttachmentRef {{`"
        "——本用例是跨端引用形状的唯一闸门，接口改名 / 挪走 / 缩进变化都必须红，"
        "不能跳过（跳过等于闸门悄悄失效）"
    )
    rest = source[match.end() :]
    end = re.search(r"^\}$", rest, re.MULTILINE)
    assert end is not None, f"{_WEB_REFS_TS} 的 `ImageAttachmentRef` 接口块没有闭合的顶格 `}}`"
    return rest[: end.start()]


def _ts_fields() -> dict[str, str]:
    """前端接口的字段表：名字 → 类型表达式（剥掉注释后逐行抓）。"""
    body = _BLOCK_COMMENT_RE.sub("", _ts_interface_body())
    return {name: type_expr.strip() for name, _opt, type_expr in _FIELD_RE.findall(body)}


def test_frontend_ref_fields_match_backend_model() -> None:
    """前端接口的字段名集合 == 后端 `ImageRef` 的模型字段（不多不少）。"""
    ts_fields = set(_ts_fields())
    backend_fields = set(ImageRef.model_fields)
    assert ts_fields == backend_fields, (
        f"{_WEB_REFS_TS} 的字段 {sorted(ts_fields)} 与后端 "
        f"{ImageRef.__module__}.ImageRef 的字段 {sorted(backend_fields)} 不一致：\n"
        f"  只在前端：{sorted(ts_fields - backend_fields)}\n"
        f"  只在后端：{sorted(backend_fields - ts_fields)}\n"
        "两侧一起改（前端漏跟 ⇒ 该字段在 UI 上永远缺省，全门禁仍绿）"
    )


def test_frontend_kind_literal_matches_backend_discriminant() -> None:
    """前端 `kind` 的**字面量**类型 == 后端判别值常量（判别字段不能两边各写一个串）。"""
    kind_type = _ts_fields().get("kind")
    assert kind_type is not None, f"{_WEB_REFS_TS} 的接口里没有 `kind` 判别字段"
    literals = re.findall(r"'([^']*)'", kind_type)
    assert literals == [KIND_IMAGE], (
        f"前端 `kind: {kind_type}` 的字面量 {literals} 与后端 "
        f"`attachments.projection.KIND_IMAGE = {KIND_IMAGE!r}` 不一致"
    )


def test_nullable_backend_fields_are_optional_on_the_frontend() -> None:
    """后端可空（`X | None`）的字段，前端必须带 `?`。

    只对**可空**字段判据，不对"有缺省值"判：`kind` 两边都始终有值（后端有缺省但
    `image_reference` 恒写它），把它算成"可选"是拿模型实现细节冒充跨端契约。可空字段
    才是真岔路——后端 `name: str | None`，前端若写成必填 `name: string`，解析器就得
    伪造一个值才过类型检查，那正是"缺省 vs 伪值"的分叉。
    """
    body = _BLOCK_COMMENT_RE.sub("", _ts_interface_body())
    ts_optional = {name for name, opt, _t in _FIELD_RE.findall(body) if opt == "?"}
    backend_nullable = {
        name
        for name, annotation in get_type_hints(ImageRef).items()
        if type(None) in get_args(annotation)
    }
    assert ts_optional == backend_nullable, (
        f"可空性漂移（{_WEB_REFS_TS} ↔ {ImageRef.__module__}.ImageRef）："
        f"前端可选 {sorted(ts_optional)}，后端可空 {sorted(backend_nullable)}"
    )


def test_frontend_comment_points_at_the_defining_module() -> None:
    """注释里的后端坐标必须指向**真正定义这个类**的模块（#939 M-07 修的那处错）。"""
    source = _WEB_REFS_TS.read_text(encoding="utf-8")
    match = _BACKEND_COORD_RE.search(source)
    assert match is not None, (
        f"未能在 {_WEB_REFS_TS} 找到 `= 后端 \\`模块.类\\`` 形式的坐标注释——"
        "本用例靠它把注释与实现绑在一起，注释形式变了就必须同步改这条闸门"
    )
    declared = match.group(1)
    actual = f"{ImageRef.__module__.removeprefix('agent_harness.')}.ImageRef"
    assert declared == actual, (
        f"{_WEB_REFS_TS} 的注释说引用形状是 `{declared}`，但 `ImageRef` 实际定义在 "
        f"`{actual}`（注释指向基类所在的 `attachments.types` 会误导读者去找一个"
        "不含 `kind` 的类）"
    )
