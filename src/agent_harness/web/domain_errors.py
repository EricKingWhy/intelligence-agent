"""领域异常 → HTTP 的翻译层（ARCH-5，#144）。

`SessionService` 正确地不知道 HTTP（不变量：领域层不依赖传输层）。但 web 层此前把
「领域异常 → status + detail」这段映射在 **11 个 handler** 里各重述一遍（app.py 10 +
lineage.py 1，共 **37 个 except 臂**）——同一个异常在不同 handler 给出不同状态码不会被
任何检查发现，新增一个领域异常要改每一处。本模块是这段翻译的**唯一 home**。

## 审计结论（逐 handler，ARCH-5 施工前核对，状态码与改动前逐字一致）

| 端点 | 翻译的领域异常 |
| --- | --- |
| `GET /api/sessions/{id}/events` | InvalidSessionId, SessionNotFound |
| `POST /api/sessions` | WorkspaceNameInvalid, InvalidDecision |
| `GET /api/sessions/{id}/stream` | InvalidSessionId, SessionNotFound |
| `POST /api/sessions/{id}/resume` | InvalidSessionId, SessionNotFound, ActiveRunConflict, RecoveryConflict, SeqConflict |
| `POST /api/sessions/{id}/cancel` | InvalidSessionId, SessionNotFound |
| `POST /api/sessions/{id}/approve` | InvalidSessionId, SessionNotFound, ApprovalQueueMissing, ApprovalRequestMissing, InvalidDecision, ApprovalAlreadyResolved |
| `POST /api/sessions/{id}/recover` | InvalidSessionId, SessionNotFound, RecoveryConflict, SeqConflict |
| `POST /api/sessions/{id}/model` | InvalidSessionId, SessionNotFound, UnknownModel, SeqConflict |
| `POST /api/sessions/{id}/messages` | InvalidSessionId, SessionNotFound, ActiveRunConflict, RecoveryConflict, QueueItemNotFound, SteerTargetNotFound, SeqConflict |
| `POST /api/sessions/{id}/queue/{qid}/cancel` | InvalidSessionId, SessionNotFound, QueueItemNotFound, SeqConflict |
| `POST /api/sessions/{id}/forks`（lineage.py） | InvalidSessionId, SessionNotFound, ActiveRunConflict, InvalidForkBoundary |

审计发现：**每个异常在所有 handler 里状态码一致**（这正是可单源化的前提）。
两个特例写进契约、不得「顺手统一」：

- `approve` 的 **404 有三个不可区分来源**（SessionNotFound / ApprovalQueueMissing /
  ApprovalRequestMissing 都是 404）——OBS-015 的结论，客户端无法从状态码区分，属有意为之；
- `ApprovalAlreadyResolved` 是 **409（幂等已决）而非 404**——与上面三个 404 分开，
  因此「审批已决」是可区分的。

**BUG-011 追加**：`SeqConflict` → **409**（seq 冲突：并发写者抢先落盘，或日志已损坏）。
旧行为是 `service.resume_and_launch` 把 `ValueError` 一刀切翻成 `SessionNotFound`（404），
使真机会话 `dd983104` 的日志损坏被显示成 `续聊失败：Send failed: 404`。上表中 5 个
「会构造/追加 Session 聚合」的端点各自声明了它；上方的 37 个 except 臂是 ARCH-5 当时的
历史审计记录，不在本次补记范围内。

## 设计取舍（为什么不再往前一步）

- **不用 FastAPI 全局 `exception_handler`**：那会把整张表应用到每个端点，使一个本来
  只会 500 的意外异常突然变成 404/422。issue 边界明确禁止「引入该端点本来不产生的
  状态码」——每个端点仍用**自己的 except 元组**声明「本端点翻译哪些」，子集语义不变。
- **不用装饰器**：app.py 与 lineage.py 都启用了 `from __future__ import annotations`，
  注解是字符串；包装函数定义在别的模块会改变 `__globals__`，FastAPI 的
  `get_type_hints` 解析 `ForkRequest` / `ResumeRequest` 这类本模块名会 NameError。
  而定义在本模块又无法同时服务两个模块（app → lineage 有导入顺序约束）。收益不抵风险。
"""

from __future__ import annotations

from fastapi import HTTPException

from agent_harness.session.errors import (
    ActiveRunConflict,
    ApprovalAlreadyResolved,
    ApprovalQueueMissing,
    ApprovalRequestMissing,
    InvalidDecision,
    InvalidForkBoundary,
    InvalidSessionId,
    QueueItemNotFound,
    RecoveryConflict,
    SeqConflict,
    SessionNotFound,
    SessionServiceError,
    SteerTargetNotFound,
    UnknownModel,
    WorkspaceNameInvalid,
)

#: 领域异常 → HTTP status 的**唯一**映射源（ARCH-5）。新增领域异常只改这里；
#: 需要用它的端点再在自己的 except 元组里声明。状态码口径见模块 docstring 的审计表。
#: 覆盖全部 `SessionServiceError` 子类——由
#: `tests/web/test_domain_error_mapping.py` 双向钉住（漏登记先红、值漂移先红）。
_DOMAIN_ERROR_STATUS: dict[type[SessionServiceError], int] = {
    # 422：入参/引用非法（客户端 bug，不是冲突）
    InvalidSessionId: 422,
    WorkspaceNameInvalid: 422,
    InvalidDecision: 422,
    UnknownModel: 422,
    InvalidForkBoundary: 422,
    # 404：目标不存在（approve 的三个来源有意不可区分，见模块 docstring）
    SessionNotFound: 404,
    ApprovalQueueMissing: 404,
    ApprovalRequestMissing: 404,
    QueueItemNotFound: 404,
    # 409：状态冲突（含幂等已决、需人工裁决的崩溃遗留、seq 冲突）
    ActiveRunConflict: 409,
    RecoveryConflict: 409,
    ApprovalAlreadyResolved: 409,
    SteerTargetNotFound: 409,
    # BUG-011：seq 冲突是「资源当前状态与请求冲突」，**不是**「资源不存在」——
    # 旧行为把它翻成 404（`send_message` 的 `Send failed: 404`），掩盖了日志损坏。
    SeqConflict: 409,
}


def http_error(exc: SessionServiceError) -> HTTPException:
    """领域异常 → `HTTPException`；状态码取自 `_DOMAIN_ERROR_STATUS` 单一映射源。

    `detail` 仍是 `str(exc)`——各端点的错误文案契约不变。调用方保持
    `raise http_error(e) from e` 以保留异常链。

    直接索引（不 `.get` 回退）：未登记类型是**编码错误**，且已被
    `test_status_map_covers_every_domain_exception` 挡住；此处再兜一层只会掩盖它。
    """
    return HTTPException(
        status_code=_DOMAIN_ERROR_STATUS[type(exc)], detail=str(exc)
    )
