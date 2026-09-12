"""领域异常 → HTTP 映射的单一 source of truth（ARCH-5，#144）。

此前这段映射在 11 个 handler（app.py 10 + lineage.py 1）里各自重述，共 37 个
except 臂。本测试把「审计结论」钉成契约：状态码只允许在
`web/domain_errors.py::_DOMAIN_ERROR_STATUS` 一处改动。
"""

from __future__ import annotations

import pytest

from agent_harness.memory.errors import MemoryNotFound
from agent_harness.session.errors import SessionNotFound, SessionServiceError
from agent_harness.web.domain_errors import (
    _DOMAIN_ERROR_STATUS,
    _MEMORY_ERROR_STATUS,
    _WORKSPACE_ERROR_STATUS,
    http_error,
    memory_http_error,
    workspace_http_error,
)
from agent_harness.workspace import (
    WorkspaceError,
)


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
        # WS-4 / #154：会话↔项目的移动在当前状态下不成立（无 cwd 锚 / cwd 不属于该项目 /
        # 重排目标不在该项目账本里）——状态冲突而非入参非法，与 422 分开。
        "WorkspaceMoveInvalid": 409,
        # BUG-011：seq 冲突（并发写者抢先落盘 / 日志已损坏）——冲突不是「不存在」，
        # 必须与 SessionNotFound 的 404 区分开（旧行为把它翻成 404 掩蔽了日志损坏）。
        "SeqConflict": 409,
    }


def test_http_error_reads_status_from_the_single_map():
    """detail 原样透传（各端点契约的 detail 文案不变），status 来自映射表。"""
    http = http_error(SessionNotFound("session 'x' not found"))
    assert http.status_code == 404
    assert http.detail == "session 'x' not found"


# ── WS-4 / #154：第二张表（workspace 包 / OS 词汇）──


def test_workspace_map_covers_every_workspace_exception():
    """`workspace` 包的每个领域异常都必须显式登记（`workspace_http_error` 直接索引）。"""
    missing = _all_subclasses(WorkspaceError) - set(_WORKSPACE_ERROR_STATUS)
    assert not missing, (
        "以下 workspace 领域异常未登记 HTTP 状态码："
        f"{sorted(c.__name__ for c in missing)}"
    )


def test_workspace_map_is_the_audited_contract():
    """WS-4 的项目端点审计结论——状态码不得漂移。

    关键区分：**未知目标**（无该项目 / 路径不存在）= 404；**路径存在但不是目录** =
    422（入参非法）；**账本序请求与账本现状冲突**（会话/锚点不在该项目账本里）= 409；
    **OS 拒绝该路径**（非法字符 EINVAL / 超长 ENAMETOOLONG，裸 `OSError`）= 422；
    **无权访问** = 403。
    `WorkspaceRegistryCorrupt` **刻意不登记**：索引损坏是服务端完整性故障，500 才诚实。
    """
    assert {c.__name__: s for c, s in _WORKSPACE_ERROR_STATUS.items()} == {
        "UnknownWorkspace": 404,
        "FileNotFoundError": 404,
        "UnknownLedgerEntry": 409,
        "NotADirectoryError": 422,
        "OSError": 422,
        "PermissionError": 403,
    }


def test_workspace_map_covers_the_os_errors_create_can_raise():
    """`create()` 会原样透传的 OS 错误必须都有状态码——否则客户端可控路径换来 500。

    上一例只能枚举 `WorkspaceError` 子类，**发现不了"代码会抛的 OS 异常没登记"**，
    所以这条显式钉住：`os.stat` 的 EINVAL/ENAMETOOLONG 是裸 `OSError`、权限问题是
    `PermissionError`、目标不存在是 `FileNotFoundError`、不是目录是 `NotADirectoryError`。
    """
    for exc in (
        OSError(22, "Invalid argument"),
        PermissionError(13, "denied"),
        FileNotFoundError(2, "missing"),
        NotADirectoryError(20, "not a dir"),
    ):
        http = workspace_http_error(exc)
        assert 400 <= http.status_code < 500, f"{type(exc).__name__} → {http.status_code}"


def test_workspace_http_error_preserves_detail():
    http = workspace_http_error(NotADirectoryError(20, "不是目录", "D:/x"))
    assert http.status_code == 422
    assert http.detail  # str(OSError) 原样透传，文案由异常自己带


# ── MEM-4 / #159：第三张表（memory 领域 / 归属词汇）──


def test_memory_map_is_the_audited_contract():
    """记忆端点的错误语义——状态码不得漂移。

    关键区分：**未知记忆 id** = 404（用户对着一个具体 id 点删除，"这条不在了"要报出来，
    不是静默成功）；**存在但属于别人** = 403（领域层的归属校验如实上报，不伪装成 404）。
    `ValueError`（"要了 SESSION scope 却没有可信绑定"）**刻意不登记**：本票的用户 API 只暴露
    USER scope，真冒出它说明是我们自己的上下文处理写错了，500 才诚实。
    """
    assert {c.__name__: s for c, s in _MEMORY_ERROR_STATUS.items()} == {
        "MemoryNotFound": 404,
        "PermissionError": 403,
    }


def test_memory_http_error_preserves_detail_and_rejects_unregistered_types():
    error = MemoryNotFound("mem-1")
    http = memory_http_error(error)
    assert http.status_code == 404
    assert "mem-1" in http.detail

    assert memory_http_error(PermissionError("不属于当前用户")).status_code == 403

    # 未登记类型是编码错误：直接索引 → KeyError（由覆盖测试先红挡住），不静默 500。
    with pytest.raises(KeyError):
        memory_http_error(ValueError("session binding missing"))
