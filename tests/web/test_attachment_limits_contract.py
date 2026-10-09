"""前端镜像上限 vs 服务端权威默认值的**跨端闸门**（#825 / MM-04 独立审查 P3-8）。

**为什么必须有这一条**：`web/src/lib/attachments.ts::IMAGE_LIMITS` 是 `config.py` 那组
`attachment_max_*` 默认值的**手写镜像**（#824 / MM-03 没有下发这些值的端点，见该文件
模块头「上限常数的口径」）。两侧各自都有测试钉着自己那份值：

- `web/src/lib/attachments.test.ts` 把镜像与**同文件**的字面量比 ⇒ 服务端改默认值时
  它永远绿；
- 后端测试钉后端自己的默认值。

于是「两侧一起改、但改得不一致」（例：服务端 20 MiB → 50 MiB、前端忘了跟）会让全门禁
绿，而前端预检与服务端权威判据分叉（表现为「预检放行、服务端 413」——用户直到发送失败
才知道）。本用例是那个方向的唯一闸门：**直接读前端源文件**，把它的字面量与 `Settings`
的默认值逐项对起来。单边改必红，失败信息指出该改哪两侧。同款先例：
`tests/web/test_error_code_contract.py`（跨端机读码）、
`tests/web/test_web_phase5_staged_endpoints.py`（图标名对账）。
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_harness.attachments.types import parse_allowed_media_types
from agent_harness.config import Settings

#: 前端那份镜像的**声明点**（跨端对账要读的源文件）。
_WEB_LIMITS_TS = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "attachments.ts"

#: 三个整数字面量字段的抓取式（值与逗号/换行之间可能出现空格）。
_INT_FIELD_RE = {
    name: re.compile(rf"{name}:\s*([^,\n]+)")
    for name in ("maxImageBytes", "maxImagesPerMessage", "maxMessageImageBytes")
}

#: `mediaTypes: ['image/png', …]` 的数组体 + 数组里的单引号字符串。
_MEDIA_TYPES_RE = re.compile(r"mediaTypes:\s*\[([^\]]*)\]")
_TS_STRING_RE = re.compile(r"'([^']*)'")


def _ts_int(expr: str, field: str) -> int:
    """把 TS 的整数字面量表达式（`20 * 1024 * 1024`）算成 int。

    只接受纯乘加字面量：出现别的字符就**响亮失败**——本用例是闸门，读不懂前端那份值
    等于没核对，不能静默跳过（那正是它要防的漂移形态）。
    """
    total = 0
    for term in expr.split("+"):
        product = 1
        for factor in term.split("*"):
            value = factor.strip()
            assert value.isdigit(), (
                f"{_WEB_LIMITS_TS} 的 `{field}` 值 {expr!r} 不是纯乘加整数字面量"
                "（本用例只能机械地对账字面量；形式变了就必须同步改这条闸门，不能跳过）"
            )
            product *= int(value)
        total += product
    return total


def _client_int(source: str, field: str) -> int:
    match = _INT_FIELD_RE[field].search(source)
    assert match is not None, (
        f"未能在 {_WEB_LIMITS_TS} 找到 `{field}:`（改名 / 挪走 / 换成变量？）——"
        "本用例是前端镜像与服务端默认值同值的唯一闸门，找不到就必须红，不能跳过"
    )
    return _ts_int(match.group(1), field)


def test_client_image_limits_match_backend_defaults():
    """前端 `IMAGE_LIMITS` 的每一项 == `Settings` 的默认值（20 MiB / 20 / 200 MiB / 四种）。"""
    source = _WEB_LIMITS_TS.read_text(encoding="utf-8")
    # `_env_file=None` + conftest 的 env 密封（`settings_env_sealed`）⇒ 读到的就是
    # 代码里的默认值，而不是某台机器 `.env` / 环境变量里的部署值。
    settings = Settings(_env_file=None)

    for field, expected, backend_name in (
        ("maxImageBytes", settings.attachment_max_image_bytes, "attachment_max_image_bytes"),
        (
            "maxImagesPerMessage",
            settings.attachment_max_images_per_message,
            "attachment_max_images_per_message",
        ),
        (
            "maxMessageImageBytes",
            settings.attachment_max_message_image_bytes,
            "attachment_max_message_image_bytes",
        ),
    ):
        actual = _client_int(source, field)
        assert actual == expected, (
            f"前端镜像 {field}={actual} 与服务端默认值 {backend_name}={expected} 不一致："
            f"改一处必须连带改另一处（{_WEB_LIMITS_TS} ↔ src/agent_harness/config.py），"
            "否则预检放行的请求会被服务端权威拒绝（413/422）"
        )


def test_client_media_types_match_backend_defaults():
    """前端允许的 media type 列表 == 服务端默认 `attachment_allowed_media_types` 的解析结果。"""
    source = _WEB_LIMITS_TS.read_text(encoding="utf-8")
    match = _MEDIA_TYPES_RE.search(source)
    assert match is not None, (
        f"未能在 {_WEB_LIMITS_TS} 找到 `mediaTypes: [...]`——本用例是跨端类型的唯一闸门"
    )
    client_types = _TS_STRING_RE.findall(match.group(1))
    backend_types = list(
        parse_allowed_media_types(Settings(_env_file=None).attachment_allowed_media_types)
    )
    assert client_types == backend_types, (
        f"前端 media types {client_types} 与服务端默认 {backend_types} 不一致："
        f"改一处必须连带改另一处（{_WEB_LIMITS_TS} ↔ src/agent_harness/config.py 的 "
        "attachment_allowed_media_types）"
    )
