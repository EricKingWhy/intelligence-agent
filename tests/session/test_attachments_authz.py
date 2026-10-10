"""#934（M-05 / M-06）：附件读回授权唯一入口 `assert_attachment_referenced` 的契约测试。

守卫的"存在证明"：本文件的否定用例（`test_unreferenced_id_raises`、
`test_non_user_message_reference_does_not_authorize`）直接钉住入口函数的行为——
把入口里的 `raise` 删掉/注释掉，这两个用例必须变红（消融实验见
`~/workspace/system/dispatch/934-ablation-red.log`（删 `raise` → 变红）与
`~/workspace/system/dispatch/934-ablation-green.log`（恢复 → 变绿））。这就是 M-05 要的机械守卫：
不变量"每份被服务的字节都经过授权"的存在性由测试证明，而非由注释保证。
"""

from __future__ import annotations

import pytest

from agent_harness.session import MODEL_COMPLETED, USER_MESSAGE, SessionEvent
from agent_harness.session.derive import assert_attachment_referenced
from agent_harness.session.errors import AttachmentNotReferenced
from agent_harness.web.domain_errors import http_error

_AID = "sha256:" + "a" * 64
_OTHER_AID = "sha256:" + "b" * 64
_REF = {
    "kind": "image",
    "attachment_id": _AID,
    "media_type": "image/png",
    "bytes": 1024,
    "width": 640,
    "height": 480,
}


def _event(seq: int, type_: str, data: dict) -> SessionEvent:
    return SessionEvent(seq=seq, type=type_, session_id="s1", data=data)


def test_referenced_id_passes() -> None:
    """被本会话某条 user/message 真实引用 → 不抛（绿路径）。"""
    events = [_event(0, USER_MESSAGE, {"content": "看图", "attachments": [_REF]})]
    # 不抛即通过：入口返回 None 无信息量，`assert ... is None` 是噪音。
    assert_attachment_referenced(events, _AID)


def test_unreferenced_id_raises() -> None:
    """**M-05 变异守卫**：事件流里没有该 id → 必须抛。

    把 `assert_attachment_referenced` 里的 `raise` 删掉，这个用例变红——
    这就是守卫的"存在证明"（删检查→红→恢复→绿，证据见消融日志）。
    """
    events = [_event(0, USER_MESSAGE, {"content": "纯文本"})]
    with pytest.raises(AttachmentNotReferenced):
        assert_attachment_referenced(events, _AID)


def test_non_user_message_reference_does_not_authorize() -> None:
    """引用只出现在非 user/message 事件里 → 仍抛（谓词语义钉死）。

    授权判据是"被某条 **user/message** 真实引用"（PRD D5）；别的事件类型
    即使带了同形引用数组也不能放行——否则改谓语事件类型会静默放宽授权。
    """
    events = [_event(0, MODEL_COMPLETED, {"attachments": [_REF]})]
    with pytest.raises(AttachmentNotReferenced):
        assert_attachment_referenced(events, _AID)


def test_other_unreferenced_id_raises_when_one_is_referenced() -> None:
    """引用了 A 但请求 B → B 仍抛（按 id 精确判定，不按"会话引用过附件"粗放行）。"""
    events = [_event(0, USER_MESSAGE, {"content": "看图", "attachments": [_REF]})]
    with pytest.raises(AttachmentNotReferenced):
        assert_attachment_referenced(events, _OTHER_AID)


def test_error_translates_to_http_404() -> None:
    """行为级：web 层把该领域异常翻译为 HTTP 404（不可区分口径，不泄露存在性）。

    覆盖纪律（`tests/web/test_domain_error_mapping.py`）要求每个领域异常都
    在 `web/domain_errors.py` 显式登记；这里钉住的是翻译**行为**（拿到 404），
    而非 `issubclass` 的层级形状——层级形状是登记纪律的副产品。
    """
    assert http_error(AttachmentNotReferenced("x")).status_code == 404
