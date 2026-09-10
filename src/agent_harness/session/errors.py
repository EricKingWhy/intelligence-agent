"""SessionService 领域异常层级（从 service.py 抽出，候选 2 纯结构重构）。

调用方（Web handler / CLI）负责把领域异常翻译为 HTTP status / CLI 错误消息。
所有异常都继承 ``SessionServiceError``，便于调用方统一 catch。

本模块无行为变化——异常类逐字搬移，``service.py`` 重新导出以保持所有既有
导入路径（``from agent_harness.session.service import SessionNotFound``）不变。
"""

from __future__ import annotations


class SessionServiceError(Exception):
    """SessionService 所有领域异常的基类。"""


class SessionNotFound(SessionServiceError):
    """session_id 不存在（store 中无事件）。"""


class InvalidSessionId(SessionServiceError):
    """session_id 格式不合法（安全校验失败）。"""


class ActiveRunConflict(SessionServiceError):
    """session 已有在途 run，不允许并发。"""


class ApprovalQueueMissing(SessionServiceError):
    """session 没有交互式审批队列（permission_mode 非交互，或 run 已结束）。"""


class ApprovalRequestMissing(SessionServiceError):
    """approval_id 在 session 事件中找不到对应的 approval-requested。"""


class ApprovalAlreadyResolved(SessionServiceError):
    """approval_id 已被决策（防重复）。"""


class InvalidDecision(SessionServiceError):
    """决策值不合法或不在 allowed_decisions 内。"""


class RecoveryConflict(SessionServiceError):
    """恢复需要人工裁决（UNKNOWN 工具状态）。"""


class WorkspaceNameInvalid(SessionServiceError):
    """workspace 名字不合法（路径逃逸风险）。"""


class QueueItemNotFound(SessionServiceError):
    """排队消息不存在 / 已消费 / 已取消。"""


class SteerTargetNotFound(SessionServiceError):
    """steer 目标 run 不存在（无在途 run）。"""


class UnknownModel(SessionServiceError):
    """模型切换目标不在 catalog 中（provider + model_id 未命中）。"""


class InvalidForkBoundary(SessionServiceError):
    """fork 锚点非法（不是用户消息 seq / 前缀含未终态 run）。"""
