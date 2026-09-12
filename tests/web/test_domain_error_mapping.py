"""领域异常 → HTTP 映射的单一 source of truth（ARCH-5，#144）。

此前这段映射在 11 个 handler（app.py 10 + lineage.py 1）里各自重述，共 37 个
except 臂。本测试把「审计结论」钉成契约：状态码只允许在
`web/domain_errors.py::_DOMAIN_ERROR_STATUS` 一处改动。
"""

from __future__ import annotations

from agent_harness.session.errors import SessionNotFound, SessionServiceError
from agent_harness.web.domain_errors import _DOMAIN_ERROR_STATUS, http_error


def _all_subclasses(cls: type) -> set[type]:
    out: set[type] = set()
    for sub in cls.__subclasses__():
        out.add(sub)
        out |= _all_subclasses(sub)
    return out


def test_status_map_covers_every_domain_exception():
    """新增领域异常必须显式登记状态码。

    这是本设计的**正确性前提**：`http_error` 用直接索引查表（不猜、不回退），
    所以漏登记的类型一旦被端点捕获就会 KeyError。宁可在这里先红。
    """
    missing = _all_subclasses(SessionServiceError) - set(_DOMAIN_ERROR_STATUS)
    assert not missing, (
        "以下领域异常未登记 HTTP 状态码："
        f"{sorted(c.__name__ for c in missing)}"
    )


def test_status_map_is_the_audited_contract():
    """ARCH-5 逐 handler 审计结论（11 handler / 37 except 臂）——状态码不得漂移。

    审计要点：同一个领域异常在所有 handler 里状态码**一致**（这是「可单源化」的
    前提）；`approve` 的三个 404（SessionNotFound / ApprovalQueueMissing /
    ApprovalRequestMissing）**有意不可区分**（OBS-015）；`ApprovalAlreadyResolved`
    是 409 幂等已决而非 404。
    """
    assert {c.__name__: s for c, s in _DOMAIN_ERROR_STATUS.items()} == {
        "InvalidSessionId": 422,
        "SessionNotFound": 404,
        "WorkspaceNameInvalid": 422,
        "InvalidDecision": 422,
        "UnknownModel": 422,
        "InvalidForkBoundary": 422,
        "ActiveRunConflict": 409,
        "RecoveryConflict": 409,
        "ApprovalAlreadyResolved": 409,
        "SteerTargetNotFound": 409,
        "ApprovalQueueMissing": 404,
        "ApprovalRequestMissing": 404,
        "QueueItemNotFound": 404,
        # WS-3 / #153：按项目列会话时未注册的 workspace_id——与「项目存在但没有会话」
        # 必须可区分，所以是 404 而不是「空列表」（不变量 #21 同族：缺席不造假）。
        "WorkspaceNotFound": 404,
        # BUG-011：seq 冲突（并发写者抢先落盘 / 日志已损坏）——冲突不是「不存在」，
        # 必须与 SessionNotFound 的 404 区分开（旧行为把它翻成 404 掩蔽了日志损坏）。
        "SeqConflict": 409,
    }


def test_http_error_reads_status_from_the_single_map():
    """detail 原样透传（各端点契约的 detail 文案不变），status 来自映射表。"""
    http = http_error(SessionNotFound("session 'x' not found"))
    assert http.status_code == 404
    assert http.detail == "session 'x' not found"
