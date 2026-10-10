"""SessionService 领域异常层级（从 service.py 抽出，候选 2 纯结构重构）。

调用方（Web handler / CLI）负责把领域异常翻译为 HTTP status / CLI 错误消息。
所有异常都继承 ``SessionServiceError``，便于调用方统一 catch。

抛出点不限于 ``SessionService``：``session/session.py``（聚合加载校验）与
``session/store.py``（落盘守卫）也抛 ``SeqConflict``——它描述的是「会话事件流的
seq 纪律」，属会话领域、与传输层无关；基类名沿用历史命名，不再另立层级。

本模块无行为变化——异常类逐字搬移，``service.py`` 重新导出以保持所有既有
导入路径（``from agent_harness.session.service import SessionNotFound``）不变。
"""

from __future__ import annotations

from typing import Literal


class SessionServiceError(Exception):
    """SessionService 所有领域异常的基类。"""


class SessionNotFound(SessionServiceError):
    """session_id 不存在（store 中无事件）。"""


class InvalidSessionId(SessionServiceError):
    """session_id 格式不合法（安全校验失败）。"""


class ActiveRunConflict(SessionServiceError):
    """session 已有在途 run，不允许并发。"""


class CompactionInProgress(ActiveRunConflict):
    """该会话已有一个手动压缩在途（per-session in-flight 防重，F6 #635）。

    与「在途 run」共用一个 HTTP 状态（409），但**类型可区分**：CLI 按类型映射
    "压缩已在进行中" 文案，不再靠错误字符串子串匹配。继承 `ActiveRunConflict`
    使 Web 的既有 `except ActiveRunConflict` 与领域错误表零改动即可覆盖子类
    （同 `WorkspacePathInvalid(WorkspaceNameInvalid)` 先例）。
    """


class CompactionConcurrentWrite(ActiveRunConflict):
    """手动压缩落盘窗口内检测到并发改动（write guard 复验失败，F6 #635）。

    两种触发面：重拿 `session_lock` 后仍 `is_busy`（有 run 在收尾写日志）、或事件数
    与快照（含本次自身失败记录）不符。同为 409，但类型化后 CLI 可给出可操作文案。
    继承 `ActiveRunConflict` 的理由同 `CompactionInProgress`。

    `reason` 区分两种成因，供调用方给**诚实且可操作**的文案（G1 #635）：

    - ``"run_busy"``：重拿锁后仍 `is_busy`——run 在收尾窗口（finalizer 仍在写日志）。
      真实动作是"等 run 结束"，不是"重试"；且此前的失败记录可能已落盘，故也不是
      "零改动"。
    - ``"event_drift"``：事件数与快照不符——压缩期间被并发写者改动，应重试。
    """

    def __init__(
        self, message: str, *, reason: Literal["run_busy", "event_drift"],
    ) -> None:
        super().__init__(message)
        self.reason = reason


class SessionHasChildren(SessionServiceError):
    """会话是别的会话的 fork 父，不能删（ADR-0029 D4）。

    刻意**不级联**：静默删掉用户没选中的子会话不可接受；也刻意**不静默 orphan**：
    子会话会带着一个悬空来源链接（lineage 显示 "(parent missing)"）。所以拒绝，
    并把子会话数量写进 detail，由用户先处理子会话。"""


class ApprovalQueueMissing(SessionServiceError):
    """session 没有交互式审批队列（permission_mode 非交互，或 run 已结束）。"""


class ApprovalRequestMissing(SessionServiceError):
    """approval_id 在 session 事件中找不到对应的 approval-requested。"""


class ApprovalAlreadyResolved(SessionServiceError):
    """approval_id 已被决策（防重复）。"""


class PendingApprovalConflict(SessionServiceError):
    """会话有**未裁决**的审批，当前动作按此状态不允许（F18-A #282；ADR-0041 §4.1）。

    判据与 ``delete_session`` 的「④ 挂起审批」相同（会话级待审批队列非空）。**刻意不
    复用 ``ActiveRunConflict``**：那条是「有在途 run 就不许并发操作」；本条只针对「有
    **待裁决会议**」——在途但无待审批时改档是允许的（下一轮生效，不打断本轮）。
    """


class InvalidDecision(SessionServiceError):
    """决策值不合法或不在 allowed_decisions 内。"""


class RecoveryConflict(SessionServiceError):
    """恢复需要人工裁决（UNKNOWN 工具状态）。

    ``pending_decisions``（#547）是可选的机器可读载荷：HTTP 层把它附进 409
    响应体（``detail`` 升级为 ``{"message", "pending_decisions"}``），UI 据此
    渲染裁决表单。只在 recover 的裁决预检分支携带；其余构造点（resume/messages
    闸门、协调器 ``RecoveryError`` 转译）保持纯文本 ``detail`` 不变。
    """

    def __init__(
        self,
        message: str,
        *,
        pending_decisions: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.pending_decisions = pending_decisions


class EventLogCorruptError(SessionServiceError):
    """events.jsonl 存在无法安全跳过的损坏，恢复入口拒绝继续（#565）。

    覆盖三类（audit 增强块的判别）：

    - **完整坏行**：换行结尾的坏 JSON / 非事件字典 / 非法 seq / 坏字段 / 无效
      UTF-8——含坏尾行；换行说明写入已完成，内容坏不是"还没写完"；
    - **seq 断层**：持久化 seq 连续是写入侧不变量（全部经 ``Session.append``
      max+1），文件里的断层只可能来自坏行跳过或整行丢失；
    - **seq 重复**：同 SeqConflict 的读时冲突面，恢复入口先拦下。

    与 `SeqConflict`（写时冲突可重试）不同，本错误**不可重试**：重读同一文件
    无用，需要人工按定位记录（行号 / 字节偏移 / sha256，由 store 的 WARNING
    日志与报告携带，不含行内容）核对原字节后修复文件。409 而非 500：不是
    服务端 bug，是 durable 资源当前状态与"继续恢复"这个请求冲突。
    """


class SeqConflict(SessionServiceError):
    """事件 seq 与已落盘日志冲突（重复 / 回退），或日志本身已不满足单调性。

    两种触发面（消息里区分，状态码同为 409——都是「资源当前状态与请求冲突」，
    而不是「资源不存在」）：

    - **写时冲突**：并发写者各自基于同一份快照取号，后一个的 seq 已被占用。
      调用方可重新读取快照后重试（``service.change_model`` 即如此）。
    - **读时冲突**：日志里已存在重复 / 回退 seq（历史损坏，如真机会话
      ``dd983104`` 的两条 seq=5）——不可自愈，需人工处置。

    BUG-011 前这两种情况要么被静默写坏（重复 seq 落盘），要么被
    ``service.resume_and_launch`` 的 ``except ValueError`` 一刀切翻译成
    ``SessionNotFound``（HTTP 404，把数据完整性问题谎报成「会话不存在」）。
    """


class WorkspaceNameInvalid(SessionServiceError):
    """workspace 名字不合法（路径逃逸风险）。"""


class WorkspacePathInvalid(WorkspaceNameInvalid):
    """`cwd` 路径不合法（ADR-0027 / #169 AC1）：非绝对路径 / 不存在 / 不是目录。

    继承 `WorkspaceNameInvalid`：HTTP 层同一 422 语义（detail 文案区分），handler 的
    `except WorkspaceNameInvalid` 天然覆盖。**不能**只靠父类——领域错误表是精确类型
    索引，子类必须自己登记（`web/domain_errors.py`）。
    """


class WorkspaceNotFound(SessionServiceError):
    """workspace_id 不存在（项目未注册 / 装配里没有 workspace 索引）。

    WS-3 / #153：按项目列会话时，未注册的项目**不能**伪装成"空列表"——那是在谎报
    "这个项目没有会话"（不变量 #21 同族：缺席不造假）。由
    `SessionService.list_sessions` 把 workspace 层的 `UnknownWorkspace` 翻成本异常
    （同一套"下层异常翻译成本层词汇"的既有做法，见 `ForkBoundaryError` →
    `InvalidForkBoundary`）。

    「会话 cwd 没了/不是目录」形态已拆出子型 `SessionCwdUnavailable`（见下）——
    本类型只承载「未注册」语义。
    """


class SessionCwdUnavailable(WorkspaceNotFound):
    """会话的 durable cwd 不存在或不是目录（#266 守卫；#615① / #624-1 同形）。

    拆分动因：父类一个类型曾同时承载「workspace_id 未注册」（`projects.py` /
    `list_sessions`）与「cwd 没了」（`resume_and_launch` 守卫）两种语义——本文件
    父类 docstring 与 `web/domain_errors.py` 的 #266 注释（"那条是'目录没了'"）
    对同一类型的描述互相矛盾。沿用仓内 `WorkspacePathInvalid(WorkspaceNameInvalid)`
    先例：继承父类 ⇒ HTTP 层同一 404 语义（detail 文案区分）、handler 的
    `except WorkspaceNotFound` 天然覆盖（app.py 各元组与 `on_run_terminal` 零改动）。
    **不能**只靠父类——领域错误表是精确类型索引，子类必须自己登记
    （`web/domain_errors.py`）。成熟产品同型：stdlib `NotADirectoryError`→`OSError`、
    httpx `ConnectError`→…→`HTTPError`、sqlite3 `IntegrityError`→`DatabaseError`
    ——父类 catch 覆盖 + 子类携精确语义。
    """


class WorkspaceBindingConflict(SessionServiceError):
    """会话的 durable cwd 与 WorkspaceRegistry 登记的目录不一致（#266）。

    两个事实源（`session/started.cwd` 与沙箱映射 / 进程内 cache）对同一会话给出不同
    目录时**类型化失败**：不自动覆盖映射、不静默选任一侧——ADR-0027 之后
    `workspace_root` 可能就是用户的真实仓库，选错一侧等于让工具在用户没选过的目录里
    执行。续聊入口在**任何** Sandbox 实例化之前对账，所以失败时不会 mkdir、不会起 run。

    fork 子会话不在对账范围内（它的 cwd 锚记的是项目归属、映射是 copy-on-fork 的副本
    目录，按设计就不同，ADR-0017 决策 5）——那是续聊入口的判定，不是本异常的形状。
    """


class WorkspaceMoveInvalid(SessionServiceError):
    """请求的会话↔项目移动在当前状态下不成立（WS-4 / #154，AC7 的对应物）。

    三种来源，都是**状态冲突**而不是"参数写错"，所以是 409 而非 422：

    1. 会话 header 没有 cwd 锚（历史遗留）——无法判定它属于哪个目录，写进账本会留下
       "账本有 id 但会话无 cwd"的中间态；
    2. 会话的 cwd 指向的项目 ≠ 请求里的项目——项目归属由**目录**决定（ADR-0025 D1），
       不能凭调用方指定；
    3. 重排的会话或锚点不在该项目的可见成员里——账本序只在项目内定义。

    与 `WorkspaceNameInvalid`（名字形态非法 → 422）刻意分开：那条是"请求本身不合法"，
    这条是"请求合法但当前状态不允许"。
    """


class QueueItemNotFound(SessionServiceError):
    """排队消息不存在 / 已消费 / 已取消。"""


class SteerTargetNotFound(SessionServiceError):
    """steer 目标 run 不存在（无在途 run）。"""


class ProtectedFactReferenceInvalid(SessionServiceError):
    """A user-supplied protected-fact link is stale or does not match its target."""


class SupersedeTargetInvalid(SessionServiceError):
    """supersede 目标不合法（ADR-0030 §4.4 第 1 步）。

    五种情形共用一个 409：目标不存在 / 不是 user/message / 是注入消息
    （``injected_by``，编辑它等于篡改 runtime 文案）/ 已被取代过 / 不是最新一条
    用户消息。刻意都报 409 而不是把"目标不对"谎报成 404 会话不存在：
    会话是存在的，是这次编辑按当前状态不允许（D8：只允许最新一条，防误删长历史）。
    """


class UnknownModel(SessionServiceError):
    """模型切换目标不在 catalog 中（provider + model_id 未命中）。"""


class AttachmentReferenceInvalid(SessionServiceError):
    """发送消息时引用的附件不合法（#823 / MM-02）。

    三种情形共用一个 422（入参非法，客户端可纠正）：id 形态不是 `sha256:<64hex>`、
    本会话字节存储里不存在（含别的会话、从未上传）、已上传但字节读不回/解码不出尺寸。
    读端点对"未被事件引用"另有 404 口径（PRD D5）；这里是**发送**侧——请求里的
    引用不成立，属入参错误，不是"资源不存在"。
    """


class AttachmentNotReferenced(SessionServiceError):
    """读回附件时该 id 未被本会话事件引用（#934 M-05 / M-06）。

    读端点（`GET .../attachments/{id}/content`）的唯一授权异常：只有被本会话某条
    `user/message` 事件真实引用的 `attachment_id` 才允许读回（PRD D5 / DSH
    `ATTACHMENT_NOT_REFERENCED` 语义）。调用方（web/）把它译为 404，且与"从未
    上传 / 属于别的会话"**不可区分**（不泄露存在性）。

    判定逻辑住在 `session/derive.py::assert_attachment_referenced`（与谓词
    `referenced_attachment_ids` 同一模块）；本异常只是领域→传输的翻译载体。
    """


class TooManyAttachments(SessionServiceError):
    """单条消息引用的图片**数量**超过部署上限（#824 / MM-03）。

    HTTP 422（入参非法，客户端可纠正）：请求里挂了超过
    `attachment_max_images_per_message` 张图。判定在**发送端点**（数量是"一条消息"
    的属性，上传端点看不到），且在任何落盘之前——被拒请求零事件、零存储残留。
    """


class AttachmentMessageTooLarge(SessionServiceError):
    """单条消息引用的图片**总字节**超过部署上限（#824 / MM-03）。

    HTTP 413（`Content Too Large`，与既有 body / 单张图超限同码）：请求里图片
    字节之和超过 `attachment_max_message_image_bytes`。同样是发送端点在任何落盘
    之前的判定；单张图的字节上限（upload 端点的 413）与之独立。
    """


class ModelDoesNotSupportImages(SessionServiceError):
    """所选模型不支持视觉，但不能发送带图片的消息（#824 / MM-03，AC4）。

    HTTP 422（客户端可纠正——改选视觉模型或去掉附图）。这是**服务端权威门禁**
    （PRD D6 "不信任客户端"）：即便入口 UI 被绕过，服务端也拒绝把图发给看不懂的
    模型。与投影层降级（`derive_messages(supports_vision=False)` 的占位符）构成
    双保险——门禁拦在发送前，降级兜住"发送后 fallback 到非视觉模型"的场景。
    """


class InvalidForkBoundary(SessionServiceError):
    """fork 锚点非法（不是用户消息 seq / 前缀含未终态 run）。"""


class SnapshotTokenMismatch(SessionServiceError):
    """清理预览快照已过期（抄 Kubernetes resourceVersion 乐观并发：预览后引用集
    变化 → 409 重算）。

    预览返回的 ``snapshot_token`` 是那一刻可达集 / 未决 Operation / 子会话 / 事件
    文件的确定指纹；执行时重算不符即拒，绝不在"用户看到的那一版"之外动手——否则
    预览之后新增的引用会被静默删掉。调用方应重新预览取新 token。
    """
