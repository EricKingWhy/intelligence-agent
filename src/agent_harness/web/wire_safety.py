"""wire 层编码安全边界（#548 / #562）：入站 Unicode 与 422 错误响应。

## 背景：一个隐式假设，两处恒假

「``str`` 一定能 utf-8 编码」是本仓多处 serialize 前紧接 utf-8 出口的**隐式假设**，
对 lone surrogate（U+D800–U+DFFF 单代理）恒假。2026-10-03 在当前 main（基线
``22cd4d64``）重建复现，得到两处后果——**与票面原始根因描述不同，以实测为准**：

1. **422 错误处理器自身的错误**（#562 FUZZ-01）。FastAPI 默认的
   ``request_validation_exception_handler`` 把 ``jsonable_encoder(exc.errors())``
   原样交给 ``JSONResponse``，而 ``exc.errors()`` 带着攻击者原文（``input``）。
   ``JSONResponse.render`` 的 ``json.dumps`` 编不出 surrogate，``allow_nan=False``
   又拒 ``inf``——于是「正确地拒绝一个坏请求」退化成 500。实测被拒字段通杀：
   ``/api/sessions``、``/api/projects``、``budget.local.*`` 等全部同形。
   同一路径上的深嵌套 ``input`` 还会让 ``jsonable_encoder`` 递归爆栈
   （``fastapi/encoders.py``）——三个症状一个根：**错误响应回显了原文**。
2. **接收路径**（#548）。`POST /api/sessions` 与 `POST /api/sessions/{id}/messages`
   的 lone surrogate 其实在 **pydantic 边界就被拒**（``type='string_unicode'``），
   根本没走到 SSE / store；票面所记的 SSE/store 500 在当前 main 上**不可复现**。
   真正的「接受路径」是 **``/api/ws`` 的 ``send_message`` 帧**：它不做 pydantic
   校验，``json.loads`` 收下 ``\\ud800`` 后直达
   ``runtime.py`` → ``session.append(USER_MESSAGE, …)`` → ``store.append_event``
   的 ``fh.write`` 抛 ``UnicodeEncodeError``，被 WS 读循环的 ``except Exception``
   吞进日志。现象比 500 更坏：客户端收到 ``{"type":"launched","run_started":true}``，
   事件流里却没有 ``user/message``——**输入静默丢失**，会话停在「已 resume 无
   user message」的中间态。

## 产品语义（用户 2026-10-03 裁决）

**入口拒绝 + 无副作用**。不做 U+FFFD 替换（替换会丢用户数据），不做全仓
``safe_dumps`` 重构；``ensure_ascii=False`` 对合法中文/emoji 本身正确，不是消灭目标。

## 本模块只做两件事

- `lone_surrogate_path`：在 JSON 解码后的结构里定位第一个 lone surrogate，**只回
  位置不回值**（错误响应不得回显原始秘密）。WS 入口用它做帧级拒绝。
- `install_wire_safety`：把 422 的 ``detail`` 投影成 ``{type, loc, msg}``。丢掉
  ``input`` / ``ctx`` / ``url`` 从根上掐断「回显原文」这条路——OpenAPI 的
  ``HTTPValidationError`` 只把这三个键列为 required，故**契约无需改动**。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

_SURROGATE_LO = 0xD800
_SURROGATE_HI = 0xDFFF

#: detail 为空时的兜底（恶意构造下 ``errors()`` 也可能给不出条目）。
_FALLBACK_DETAIL: list[dict[str, Any]] = [
    {"type": "validation_error", "loc": [], "msg": "invalid request"}
]


def _is_lone_surrogate(ch: str) -> bool:
    return _SURROGATE_LO <= ord(ch) <= _SURROGATE_HI


def _has_lone_surrogate(text: str) -> bool:
    return any(_is_lone_surrogate(ch) for ch in text)


def lone_surrogate_path(value: Any, path: str = "$") -> str | None:
    """在 JSON 解码后的结构里找**第一个** lone surrogate，返回其位置，找不到返回 None。

    只回位置不回**值**：用户正文一个字都不带出去——这是「错误响应不得回显原始秘密」
    的直接体现。**键**是字段名，与 FastAPI/pydantic 的 ``loc`` 同一条口径（``loc``
    本来就回显字段名），故保留原名；但它同样按「帧是否含畸形 Unicode」判，命中时
    返回值里给**转义**形态——否则「安全」的返回值会把同一个不可编码字符原样带到
    错误帧里（独立审查 P3-S3；顺带修掉旧实现只扫值、**整个漏掉坏键**的洞）。

    **刻意写成迭代而不是递归**：WS 帧是唯一绕过 pydantic 的入口，深嵌套帧能让任何
    递归扫描器自己 ``RecursionError``——那会把「守卫」变成新的失败面（实测 depth≈1000
    即爆，而本机 ``json.loads`` 能吃到约 3000）。显式栈让本函数对任意深度都是全函数，
    与它「不得再抛」的职责一致。
    """
    stack: list[tuple[Any, str]] = [(value, path)]
    while stack:
        current, where = stack.pop()
        if isinstance(current, str):
            if _has_lone_surrogate(current):
                return where
        elif isinstance(current, dict):
            items = list(current.items())
            # 键先扫一遍（保文档序的「第一个」），再逆序入栈 ⇒ 弹出即文档顺序。
            for key, _ in items:
                if _has_lone_surrogate(str(key)):
                    return f"{where}.{_safe_text(str(key))}"
            for key, item in reversed(items):
                stack.append((item, f"{where}.{_safe_text(str(key))}"))
        elif isinstance(current, (list, tuple)):
            for index in range(len(current) - 1, -1, -1):
                stack.append((current[index], f"{where}[{index}]"))
    return None


def _safe_text(text: str) -> str:
    """把 lone surrogate 写成 ``\\uXXXX`` **转义文本**，其余字符逐字保留。

    只动本就无法编码的字符：不把中文转义掉，也不做 U+FFFD 替换（替换会丢用户数据，
    而错误文案里的位点信息必须与原请求对得上）。
    """
    if not any(_is_lone_surrogate(ch) for ch in text):
        return text
    return "".join(
        f"\\u{ord(ch):04x}" if _is_lone_surrogate(ch) else ch for ch in text
    )


def _safe_loc_part(part: Any) -> Any:
    """``loc`` 的整数下标必须保号（schema 是 ``string | integer``）。

    统一 ``str()`` 会把 ``["body", 3]`` 变成 ``["body", "3"]``——形状漂移，
    照抄默认实现即可无谓得罪按整数消费 ``loc`` 的调用方。
    """
    if isinstance(part, bool) or not isinstance(part, (str, int)):
        return _safe_text(str(part))
    return _safe_text(part) if isinstance(part, str) else part


def _project_error_item(item: Any) -> dict[str, Any] | None:
    """单条校验错误 → 安全投影；单条损坏返回 None（不牵连整份 detail）。"""
    try:
        loc = list(item.get("loc", ()))
    except Exception:  # noqa: BLE001 — 畸形条目：跳过而不是让处理器抛
        return None
    return {
        "type": _safe_text(str(item.get("type", "validation_error"))),
        "loc": [_safe_loc_part(part) for part in loc],
        "msg": _safe_text(str(item.get("msg", "invalid request"))),
    }


def _validation_error_detail(exc: RequestValidationError) -> list[dict[str, Any]]:
    """``exc.errors()`` → 只含 ``{type, loc, msg}`` 的可编码投影。

    ``errors()`` 自身也可能在畸形输入下抛（如深嵌套导致的 ``RecursionError``），
    这里兜住：错误处理器**不得**再抛，否则又是一个裸 500。
    """
    try:
        errors = list(exc.errors())
    except Exception:  # noqa: BLE001 — 兜底：不把「错误处理器失败」升级成 500
        return list(_FALLBACK_DETAIL)
    detail = [
        projected
        for item in errors
        if (projected := _project_error_item(item)) is not None
    ]
    return detail or list(_FALLBACK_DETAIL)


async def _request_validation_error_response(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 校验错误的**唯一**出口（#548 / #562）。

    形状与 FastAPI 默认一致（``detail`` 是 ``{loc, msg, type}`` 数组），只是不再
    回显 ``input`` / ``ctx`` / ``url``：surrogate、``inf``、深嵌套递归三个症状同根，
    一起消失。
    """
    try:
        return JSONResponse(status_code=422, content={"detail": _validation_error_detail(exc)})
    except Exception:  # noqa: BLE001 — 最后一道：错误处理器绝不能自己 500
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})


def install_wire_safety(app: FastAPI) -> None:
    """把 422 校验错误出口换成上面的安全实现（在 ``create_app`` 里一行调用）。"""
    app.add_exception_handler(RequestValidationError, _request_validation_error_response)
