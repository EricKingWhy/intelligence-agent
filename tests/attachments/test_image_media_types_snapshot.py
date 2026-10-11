"""M-10 快照守卫：`validation.py` 两个图片类型表 vs 跨端快照文件（#938）。

`image_media_types.snapshot.txt` 是 canonical text：TUI / web 两侧的守卫测试都读
**这份文件**对账（TUI: `tui/test/image-media-types-snapshot.test.ts`；
web: `web/src/lib/attachments.image-types.test.ts`）。本用例钉住"快照文件 ==
从 `validation.py` 权威源渲染出的文本"——改了 `SUPPORTED_IMAGE_MEDIA_TYPES` /
`EXTENSION_MEDIA_TYPES` 而不重生成快照，这里就红（fail-closed），TUI / web
侧的旧快照才不会继续被当成真。

来源标注：跨语言守卫做法 ADAPT 自本仓 `tests/web/test_attachment_limits_contract.py`
（#824 / MM-04 留下的"python 读对端源文件逐字对账"先例）；single-source-of-truth
模式来源 DSH（DeepSeek Harness，MIT，`5badb150`）
`packages/attachment/attachment/src/types.ts:8` 的 `ImageMediaType` 联合类型。

**权威源坐标（#988 F-2 订正）**：两张表定义在 `attachments/validation.py`（中性模块）；
`attachments/types.py` 此前原样再导出它们，故旧注释把权威源指到 `types.py` 是**失实指针**
——收窄再导出面后，全仓只剩这一条权威坐标。
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent_harness.attachments.validation import (
    EXTENSION_MEDIA_TYPES,
    SUPPORTED_IMAGE_MEDIA_TYPES,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "src" / "agent_harness" / "attachments" / "image_media_types.snapshot.txt"

_HEADER = (
    "# #938 M-10：图片类型集的跨端快照（canonical text）。",
    "# 权威源：src/agent_harness/attachments/validation.py::SUPPORTED_IMAGE_MEDIA_TYPES / EXTENSION_MEDIA_TYPES。",
    "# 改权威源后重生成：python tests/attachments/test_image_media_types_snapshot.py --write",
)


def render_snapshot() -> str:
    """从 `validation.py` 的两个表渲染快照文本（media_types / extensions 均按字母序）。"""
    lines = [
        *_HEADER,
        "[media_types]",
        *sorted(SUPPORTED_IMAGE_MEDIA_TYPES),
        "[extension_media_types]",
        *[f"{extension}={EXTENSION_MEDIA_TYPES[extension]}" for extension in sorted(EXTENSION_MEDIA_TYPES)],
    ]
    return "\n".join(lines) + "\n"


def test_snapshot_matches_validation_py():
    """快照文件与权威源渲染文本逐字相等（LF、无 BOM、无行尾空格）。"""
    expected = render_snapshot()
    actual = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{SNAPSHOT_PATH} 与 src/agent_harness/attachments/validation.py 的图片类型表不一致："
        "改了权威源必须重生成快照（TUI / web 守卫读的是这份文件），命令："
        "python tests/attachments/test_image_media_types_snapshot.py --write"
    )


if __name__ == "__main__":
    if "--write" in sys.argv:
        SNAPSHOT_PATH.write_text(render_snapshot(), encoding="utf-8", newline="\n")
        print(f"wrote {SNAPSHOT_PATH}")
    else:
        test_snapshot_matches_validation_py()
        print("ok: snapshot matches validation.py")
