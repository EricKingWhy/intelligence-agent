/** Composer — task input at the bottom of the conversation column.
 *
 * Submits on Cmd/Ctrl+Enter; Enter = queue mode, Cmd/Ctrl+Enter = steer (ADR-0030
 * §5.1 D10：streaming 不再禁用输入——「本页有活流」≠「服务端有在途 run」，
 * 两者混用正是 issue #196 的根因）。等待审批时仍禁用（与审批 UI 竞争）。
 * presetTask: 外部注入的示例任务（空状态 chip 点击），注入后仍可自由编辑。
 */

import { memo, useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { ArrowUp, Brain, Check, ImagePlus, Pencil, Play, Shield, Square, TriangleAlert, User, X, Zap } from 'lucide-react';
import type { FailedDraft, PresetTask, UndeliveredInput } from '../types';
import { modKey } from '../lib/platform';
import { toolScopeNote } from '../lib/agentProfileScope';
import { catalogIcon } from '../lib/catalogIcons';
import { getSessionEvents, type CatalogEntry, type ConstraintScope, type ModelCatalogEntry } from '../lib/api';
import type { ActiveFallbackModelProjection } from '../lib/modelReasoningEffortProjection';
import { budgetText, filesFromClipboard, getImageLimits, loadImageLimits, type ImageIntakeLimits } from '../lib/attachments';
import { onTokenChange } from '../lib/auth';
import { routeHostFiles } from '../lib/hostFiles';
import { installDocumentDropEvents } from '../lib/dropEvents';
import { effectiveModelEntry } from '../lib/modelSelection';
import type { Ownership } from '../lib/ownership';
import { SCOPE_LABELS } from '../lib/constraints';
import { useDraftAttachments } from '../hooks/useDraftAttachments';
import { useCompactSession } from '../hooks/useCompactSession';
import { ComposerAttachments } from './ComposerAttachments';
import { ModelPicker } from './ModelPicker';
import { BudgetPicker } from './BudgetPicker';
import { OptionPicker, toCatalogOptions } from './OptionPicker';
import { ReasoningEffortSlider } from './ReasoningEffortSlider';
import {
  SLASH_COMMANDS,
  filterSlashCommands,
  slashSubmitMatch,
  slashTriggerMatch,
  type SlashCommand,
} from '../lib/slashCommands';

/** #283：升到这一档要在 pill 浮层里给一次显式确认（ADR-0041 D1——后端**不加**强制标志位，
 *  确认是 UX 层的责任）。字面量与后端 `PermissionPolicy.DANGER_FULL_ACCESS` 同值：它同时
 *  「目录里的一个 id」与「危险判定」，所以不在前端另立第二张表（那样两处一漂移，
 *  「确认」就会挂在错的档位上——比不确认更糟）。 */
const DANGER_PERMISSION_MODE = 'danger-full-access';

/** 非视觉模型禁用附图的原因前缀（#937 / M-24 收口：title 与可见提示两处同源，改一处即两处）。 */
const NON_VISION_MODEL_REASON = '当前模型不支持视觉（supports_vision=false）';

/** #1191（Phase 2）：登记作用域三选一 + 逐字 hint 文案（票面 §四.2）。
 *  顺序即渲染顺序；`session` 是默认项（票面 §四.1「默认选中"本会话"」）。 */
const SCOPE_OPTIONS: readonly {
  id: ConstraintScope;
  label: string;
  hint: string;
}[] = [
  {
    id: 'session',
    label: SCOPE_LABELS.session,
    hint: '只在当前会话生效，随会话结束自然失效。适合一次性的要求。',
  },
  {
    id: 'project',
    label: SCOPE_LABELS.project,
    hint: '写入项目级存储，本仓库开的所有会话都生效，别的项目不受影响。',
  },
  {
    id: 'global',
    label: SCOPE_LABELS.global,
    hint: '写入用户级存储，所有项目的新会话都生效。适合长期习惯。',
  },
];

/** #1027 发送门禁的阻断文案（Gmail 式阻断，PORT DESIGN）：界面提示与发送按钮
 *  title 两处同源（#937 / M-24 同款手法），改一处即两处。 */
const SEND_BLOCKED_REASON = '附件上传中，请等待完成';

/** #1283 P1：composer 被锁的**真实原因**——文案全部由它映射，不再各写一份三元链。
 *  优先级与 #1223 的既有行为逐字一致：约束澄清 > ask_user > 审批。 */
type LockReason = 'constraint' | 'ask_user' | 'approval';

interface LockCopy {
  hint: string;
  placeholder: string;
  permissionHint: string;
}

const LOCK_COPY: Record<LockReason, LockCopy> = {
  // #1283 复审 P4：约束场景的**后端 409 原句**按 #1223 冻结为「请先提交答案」
  // （service._pending_user_input_conflict_message），与这里的「回答」动词差是
  // 有意的口径差，不是漂移——改这句前先核对 #1223 的冻结结论。
  constraint: {
    hint: '请先回答当前约束澄清，再发送新消息',
    placeholder: '等待约束澄清回答…',
    permissionHint: '等待约束澄清回答后再改档',
  },
  ask_user: {
    hint: '请先回答当前提问，再发送新消息',
    placeholder: '等待回答…',
    permissionHint: '等待回答当前提问后再改档',
  },
  approval: {
    hint: '等待审批决策后再继续',
    placeholder: '等待审批决策…',
    permissionHint: '等待审批决策后再改档',
  },
};

/** 队列条（编辑/立即/flush）的 title。只有**待答用户输入**会锁队列条，审批锁
 *  没有队列语义。用**窄键必填表**（#1283 复审 P4）：将来新增纳入锁定的 kind 却
 *  漏填文案时 `tsc` 直接报错，而不是靠运行期 `?? ''` 静默出空 title。 */
type QueueLockReason = Extract<LockReason, 'constraint' | 'ask_user'>;

const QUEUE_HINT: Record<QueueLockReason, string> = {
  constraint: '请先回答当前约束澄清',
  ask_user: '请先回答当前提问',
};

/** 发送 / 保存的统一受理契约（#1029）：显式 `false` = **未受理**（409 / 网络失败 /
 *  `422`…）⇒ 保留用户输入；`true` / `undefined`（旧签名）⇒ 按已受理收尾。
 *  `onSubmit` / `onSteer` / `onEditItem` 共用这一份，避免同一契约写三遍。
 *  export：消费方（App 的 handleEditItem 等）引用同一别名，契约变更时 tsc 全仓追平。 */
export type SubmitResult = void | boolean | Promise<void | boolean>;

interface Props {
  streaming: boolean;
  /** UI-01（D4-⑤）：存在待决审批时锁住 composer——运行被阻塞，新任务
   *  与审批互斥，不允许两条修复路径同时开放（评审 Riley 红旗）。 */
  approvalPending?: boolean;
  /** 有待回答的约束澄清时锁住新消息，避免新 run 覆盖待恢复问题。 */
  constraintInputPending?: boolean;
  /** #1223：待回答的 `ask_user` 通用提问时同样锁住新消息——与约束澄清**同一条**锁定
   *  语义（把用户引向对话流里的待回答卡片，而不是让他发新消息撞后端 409）。
   *  文案必须**中性**：ask_user 是通用提问、不是约束澄清，见下方 `lockReason`。 */
  askUserInputPending?: boolean;
  /** 返回值 = 这次发送**是否已被受理**（#1029，契约见 `SubmitResult`）：
   *  - `false` ⇒ 未受理（待裁决 409 / 网络失败 / 422…），文本与已进请求的草稿
   *    **原样留在输入框**，等用户处理完再发（`11 §6.1`：「409 冲突后 MUST 刷新权威
   *    状态并保留用户未发送的输入」）；
   *  - `true` / `undefined`（旧签名）⇒ 按旧行为清空——既有调用零回归。
   *  发送口**抛错**同样按未受理处理（保留输入，见 submit 的 `.then(_, onRejected)`）
   *  ——它绝不能被读成"已受理"。
   *  Promise 形态由 App 的 `handleSubmit` 返回（回执在途期间同样不清空）。 */
  onSubmit: (
    task: string,
    rememberAsProceduralRule: boolean,
    attachmentIds?: string[],
    scope?: ConstraintScope,
  ) => SubmitResult;
  /** steer 提交（ADR-0030 §5.1：同一份输入立即投递——Ctrl/Cmd+Enter）。
   *  缺席 = 回退到 onSubmit（queue），既有调用零改动。 */
  onSteer?: (
    task: string,
    rememberAsProceduralRule: boolean,
    attachmentIds?: string[],
    scope?: ConstraintScope,
  ) => SubmitResult;
  /** #1191（Phase 2）：当前会话是否落在 git 仓库内（项目级登记可选）。
   *  `null`/缺省 = 未知（新建态、或尚未取得项目信息）⇒ 不置灰，交由后端 422 兜底；
   *  `false` = 已确知非 git 目录 ⇒ 「本项目」置灰不可选（票面 §七.4）。 */
  projectAvailable?: boolean | null;
  /** #825（MM-04）；#1015 修订：**当前会话 id**——附图上传端点是 per-session 的
   *  （`POST /api/sessions/{id}/attachments`）。`null` = 新建态：入口**不再禁用**，
   *  首次投喂经 `onEnsureSession` 懒建会话后再上传。会话切换时草稿清空并中止在途
   *  上传（见 `hooks/useDraftAttachments.ts`）。 */
  sessionId?: string | null;
  /** #1015：新建态懒建会话的**唯一**入口（App 侧单例：in-flight 去重保证并发投喂
   *  只建出一个会话）。resolve 会话 id；resolve `null` = 懒建失败，hook 经 AC3
   *  通道给可见错误。Composer 只问"现在能不能附"，不问"会话从哪来"。 */
  onEnsureSession: () => Promise<string | null>;
  /** #1028 AC2：常驻归属 chip——当前会话/新建态的项目归属与 cwd。数据由 App 组装
   *  （项目名来自后端 `SessionSummary.workspace`，cwd 来自 `session/started` 投影）；
   *  本组件只渲染文案。`null`/缺省 = 不渲染 chip：**空白新建态**（无选中会话、无项目
   *  语境，含首屏）App 传 `null`——此刻并无"会话"，显「未分组」语义悬空，故不编字面值。 */
  ownership?: Ownership | null;
  onCancel: () => void;
  presetTask?: PresetTask | null;
  /** #1148：**分离投递**（窗外落定）迟到的失败原文——把没送出去的草稿还给用户。
   *
   *  窗外投递的结局来得比判定窗口晚（响应头要等 run 结束才下发），提交时无从等待，
   *  于是先按已受理清空；真失败时由 hook 经这条 props 把原文送回来，这里按
   *  `restoreFailedDrafts` 的同一纪律回填（只在输入框为空时，绝不覆盖更新的草稿）。
   *  对象引用变化即触发一次注入（裸 string 会被 `Object.is` 挡掉第二次，见 `FailedDraft`）。 */
  failedDraft?: FailedDraft | null;
  /** T10 #103 模型目录（GET /api/models）：空 = 端点缺席/解析失败 → 选择器
   *  降级隐藏（不伪造列表）。条目即目录真相，零硬编码模型名。 */
  models?: ModelCatalogEntry[];
  /** 当前选中（null = 默认链，提交不带 model 字段）。 */
  selectedModel?: string | null;
  onModelChange?: (name: string | null) => void;
  // ── Phase 2b Composer control row（Ticket F1）──
  /** GET /api/permission-modes 清单。空 → 隐藏控件。 */
  permissionModes?: CatalogEntry[];
  selectedPermissionMode?: string | null;
  /** 改档提交口。**失败必须向上抛**：本组件把原因就地回显在权限浮层里，不另开错误通道。
   *  缺席 = 只读展示（与其余控件同款降级）。 */
  onPermissionModeChange?: (id: string | null) => Promise<void> | void;
  /** #283：这个 pill 当前作用于**已存在的会话**（`selectedId !== null`）。
   *
   *  会话内改档走 #282 的 `POST /api/sessions/{id}/permission`（**下一轮 run 生效**）——
   *  #236 的「权限档创建时确定、会话内不可修改」已被 F18-A 推翻，那句悬停提示随本票
   *  删除（它是「假话引导」：改档可行之后它就不成立了）。
   *  新会话（`false`）仍是创建期选择：随 create 请求发出，本地意图即真相。 */
  permissionInSession?: boolean;
  /** GET /api/agent-profiles 清单。空 → 隐藏控件。 */
  agentProfiles?: CatalogEntry[];
  selectedAgentProfile?: string | null;
  onAgentProfileChange?: (id: string | null) => void;
  /** GET /api/reasoning-efforts 清单。空 → 隐藏控件。 */
  reasoningEfforts?: CatalogEntry[];
  selectedReasoningEffort?: string | null;
  reasoningEffortProjection?: ActiveFallbackModelProjection | null;
  onReasoningEffortChange?: (id: string | null) => void;
  // ── #426/#536：新建会话的预算入口（turns / total_tokens / deadline_at，单入口
  //    popover——BudgetPicker）。各维草稿是输入原样字符串，判形/换算在映射层
  //    `toCreateBudget` 一处。**仅新建会话态显示**（`permissionInSession=false`）：
  //    预算属于启动 run 的请求（#422），续聊 /messages 不带 budget（#308）。
  budgetRunTurns?: string;
  onBudgetRunTurnsChange?: (value: string) => void;
  /** `budget.run.max_total_tokens` 的草稿。 */
  budgetRunTokens?: string;
  onBudgetRunTokensChange?: (value: string) => void;
  /** `budget.run.deadline_at` 的草稿——#536 起双形态：时长 token（`"2h"` 等）
   *  或 `datetime-local` 原始值（高级路径）；换算在 `amend.resolveDeadlineDraft`。 */
  budgetRunDeadline?: string;
  onBudgetRunDeadlineChange?: (value: string) => void;
  // ── ADR-0030 §5.2 队列条（#195）──
  /** 未投递输入（事件流逐事件折叠，事件流是唯一事实）。空 → 队列条不渲染。 */
  undelivered?: UndeliveredInput[];
  /** 「立即」= 升级为 steer（POST /messages mode:steer，先取消原排队项）。 */
  onSteerItem?: (item: UndeliveredInput) => void;
  /** 「取消」= POST /sessions/{sid}/queue/{queue_id}/cancel。 */
  onCancelItem?: (item: UndeliveredInput) => void;
  /** 「编辑」= 就地编辑该排队项（ADR-0030 §5.2）：提交后走
   *  `POST /messages {content, queue_id}`（后端取消旧项 + 按 mode 重新投递）。
   *  返回值契约同 `onSubmit`（#1029 / `SubmitResult`）：显式 `false` = 未受理
   *  ⇒ 保留编辑态与文本。 */
  onEditItem?: (item: UndeliveredInput, newContent: string) => SubmitResult;
  /** ADR-0030 §4.6「立即发送」：立刻投递队首待发送输入（POST /queue/flush）。
   *  缺席 = 不渲染按钮（重启后手动投递是 ADR 明确的前端职责）。 */
  onFlush?: () => void;
}
// memo：流式期间 props 稳定（streaming 布尔不变、回调由 App useCallback 固定），
// 输入框不随对话区每个 delta 重渲染。
export const Composer = memo(function Composer({
  streaming,
  approvalPending = false,
  constraintInputPending = false,
  askUserInputPending = false,
  onSubmit,
  onSteer,
  projectAvailable = null,
  sessionId = null,
  onEnsureSession,
  ownership = null,
  onCancel,
  presetTask,
  failedDraft = null,
  models = [],
  selectedModel = null,
  onModelChange,
  permissionModes = [],
  selectedPermissionMode = null,
  onPermissionModeChange,
  permissionInSession = false,
  agentProfiles = [],
  selectedAgentProfile = null,
  onAgentProfileChange,
  reasoningEfforts = [],
  selectedReasoningEffort = null,
  reasoningEffortProjection = null,
  onReasoningEffortChange,
  budgetRunTurns,
  onBudgetRunTurnsChange,
  budgetRunTokens,
  onBudgetRunTokensChange,
  budgetRunDeadline,
  onBudgetRunDeadlineChange,
  undelivered = [],
  onSteerItem,
  onCancelItem,
  onEditItem,
  onFlush,
}: Props) {
  const [value, setValue] = useState('');
  const [rememberAsProceduralRule, setRememberAsProceduralRule] = useState(false);
  /** #1191：登记作用域（勾选「作为可复用规则」后可选；默认本会话）。 */
  const [scope, setScope] = useState<ConstraintScope>('session');
  /** 注入示例任务 / 提交后要把焦点交回的输入框（A-06）。 */
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  // #1195：`/permission` `/model` 拉起现有 picker——两组件的 open 都是内部 state
  //（无受控接口，本体在 Scope Lock 外），程序化 click 各自的 trigger 按钮即可。
  const permissionPickerRef = useRef<HTMLDivElement | null>(null);
  const modelPickerRef = useRef<HTMLDivElement | null>(null);
  /** #1029：发送回执在途标记。清空挪到回执之后，输入框在在途期间仍有文本 ⇒
   *  「空文本」不再天然挡住重复提交，改由本 ref 显式挡（同步读，见 submit）。
   *
   *  在途期间按 Enter 被这里挡下时**不再是静默的**：`sending` 让发送键可见地进入
   *  忙态（#1139，原先只是注释里的已知限制）。窗口 = 一次 ack
   *  （`raceEarlyResponse` 1200ms 窗内 + WS attach），不是整场 run。 */
  const sendingRef = useRef(false);
  /** #1139：忙态的**可见**那一半。`sendingRef` 是同步闸（同一 tick 的第二次按键读不到
   *  刚 set 的 state），这个 state 只负责让用户看见「上一条还在飞」——此前在途按键
   *  被静默吞掉，用户读到的是"按键没反应"。 */
  const [sending, setSending] = useState(false);
  // ADR-0030 §5.2 就地编辑态：正在编辑的排队项 id + 草稿内容。只存 id 不存整条
  // item——条目会随事件流增删，存 id 让渲染始终对账当前事实（不变量 #22）。
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState('');

  /** #1283 P2-2：保存排队项。返回值契约同 `onSubmit`（#1029）——显式 `false`（含
   *  409 / 网络失败）⇒ **保留**编辑态与用户改好的文本；其余（`true` / `undefined`）
   *  ⇒ 关闭编辑。此前无脑 `setEditingId(null)` 会让 409 后改好的文本从界面消失。
   *
   *  #1283 复审 P3：锁可能在编辑态**打开之后**才到（待答请求从另一路事件流推来），
   *  此时保存按钮已禁用、但 Enter 仍会走到这里——首行守卫把按钮/键盘两条路径一起
   *  挡住，不发必然 409 的请求。
   *
   *  #1283 复审残余（登记不修）：`sendFollowUp` 的**分离投递**在 >1200ms 窗外先回
   *  `true`（见 `useSession.ts` 的函数头注释），迟到的 409 由 `reportDetachedFailure`
   *  把文本还回**主输入框**——不丢文本，但此时编辑态已关闭、旧队列项仍在。该窗口属
   *  #1029 既有契约，不是本票新增；本票只保证窗内路径不再无脑清空。 */
  const commitEdit = async (item: UndeliveredInput) => {
    if (pendingUserInputLocked) return;
    const trimmed = editDraft.trim();
    if (!trimmed) return;
    try {
      const accepted = await onEditItem?.(item, trimmed);
      if (accepted !== false) setEditingId(null);
    } catch (err) {
      // 抛错（非 409 路径）同样保留编辑态：宁可让用户再试，也不静默丢输入；
      // 但编程错误不能表现成「点保存没反应」，留一条可观察痕迹（复审 P4）。
      console.error('[Composer] 保存排队消息失败，已保留草稿', err);
    }
  };

  // ── #825（MM-04）附图草稿 ──
  // 状态机在 hook 里（上传/缩略图/失败重试/换会话清理），这里只做**入口门禁**与
  // 把「已就绪」的引用交给提交通道。（#1027 修订：发送按钮的可用性除文本外，还看
  // 是否有**在途上传**——AC4「上传失败不得阻塞纯文本发送」保留，failed 仍不阻塞，
  // 但发送时被显式剔除并给一次性可见提示，绝不静默丢弃。）
  const attachments = useDraftAttachments(sessionId, onEnsureSession);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  /** 整页拖拽计数：document 级 dragenter/dragleave 会成对触发，用计数判断"还在页面内"。 */
  const dragDepth = useRef(0);
  const [dragActive, setDragActive] = useState(false);
  /** #1027 兜底提示：发送时被剔除的未就绪附件数（一次性可见提示，可关闭）。 */
  const [droppedNotice, setDroppedNotice] = useState<string | null>(null);
  // #1195 内联 `/` 命令菜单：触发态由输入值派生（不另记布尔，避免与文本分叉）；
  // 高亮 index 与错误面是真正的 UI 状态。info 面是**中性**信息（查看结果 / 执行
  // 回执——不是错误，不染红；审查 B-S-4/A-P3-2）。
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashError, setSlashError] = useState<string | null>(null);
  const [slashInfo, setSlashInfo] = useState<string | null>(null);
  // Esc 关闭后 value 不变 ⇒ 派生匹配仍成立，菜单会立刻重开——需要显式 dismissed。
  // value 一变（继续打字 / 清空）即解除，重新进入触发态。
  const [slashDismissed, setSlashDismissed] = useState(false);
  // A-P3-3：outside-click 关闭——document mousedown 落在菜单与输入框之外即视为关闭。
  const slashMenuRef = useRef<HTMLDivElement | null>(null);
  // S-7（P4）：textarea ↔ 菜单的 ARIA 关联（listbox 所有权 + 展开态 + 高亮项）。
  const slashMenuId = useId();
  // A-P4：错误/信息面自动消退——上一条命令的结论不该常驻到下一次会话结束。
  useEffect(() => {
    if (slashError === null) return;
    const t = setTimeout(() => setSlashError(null), 8000);
    return () => clearTimeout(t);
  }, [slashError]);
  useEffect(() => {
    if (slashInfo === null) return;
    const t = setTimeout(() => setSlashInfo(null), 8000);
    return () => clearTimeout(t);
  }, [slashInfo]);

  // ── #1027 发送门禁（Gmail 式阻断，PORT DESIGN）──
  // 门禁锚的是 `useDraftAttachments` 的真实 status 状态机（uploading/ready/failed），
  // **不许裸加时长**：有在途上传时发送按钮禁用 + Ctrl+Enter 拦截，界面常驻阻断提示。
  const uploadingCount = attachments.items.filter((item) => item.status === 'uploading').length;
  // #1098：pre-intake 窗口（`ensureSession` 在途 / 字节探测未完成、草稿未入列）
  // 同样阻断——只钉「已入列 uploading」，粘贴后立刻 Enter 会把图文静默拆散
  // （图留在草稿栏跟下一条消息走）。AC4 不变：intake 失败（懒建失败/格式拒收）
  // 走 hook 的 `finally` 清 pending，纯文本照发。
  const intakePendingCount = attachments.intakePendingCount;
  const sendBlocked = uploadingCount > 0 || intakePendingCount > 0;
  /** 阻断原因文案（#1027 体系 + #1098 pre-intake）：界面常驻，键盘用户被拦截时
   *  读到的就是它；`title` 与提示同源，不对着按钮猜。 */
  const sendBlockedReason =
    uploadingCount > 0
      ? `${SEND_BLOCKED_REASON}${uploadingCount > 1 ? `（${uploadingCount} 张）` : ''}`
      : `${SEND_BLOCKED_REASON}（${intakePendingCount} 张准备中）`;

  // #1027：换会话即清兜底提示——草稿随会话清空（hook 的换会话 effect），提示是
  // 上次发送的结论，不跟去新会话（否则在新会话里误报"附件未随消息发出"）。
  useEffect(() => {
    setDroppedNotice(null);
  }, [sessionId]);

  // ── #937 / M-08：附图上限从服务端下发 ──
  // 初始值取模块当前值（可能已被 App 层拉到）；挂载后拉一次落进本地 state，并
  // 订阅 token 变更补拉（P4-1：否则 token 换部署后显示与执行漂移）。预检文案
  // （budgetText）与文件选择器 accept 都消费这份。失败时 loadImageLimits 返回
  // 调用前旧值，不抛、不打扰用户。App 层与这里同时触发时 in-flight 去重保证
  // 只打一次 GET。
  const [limits, setLimits] = useState<ImageIntakeLimits>(() => getImageLimits());
  useEffect(() => {
    let alive = true;
    const refresh = (): void => {
      void loadImageLimits().then((next) => {
        if (alive) setLimits(next);
      });
    };
    refresh();
    const unsubscribe = onTokenChange(refresh);
    return () => {
      alive = false;
      unsubscribe();
    };
  }, []);

  // ADR-0030 §5.1 D10：`locked` 拆开——approvalPending 仍禁用（等待审批时输入
  // 无意义且与审批 UI 竞争）；streaming **不再**禁用。issue #196 的根因正是
  // 「streaming 表示本页有活流」被当成「服务端有在途 run」：跨客户端场景下
  // 输入框可用、消息却走 queue 分支静默丢失。现在流式期间发消息 = 显式选择
  // queue（Enter）/ steer（Ctrl/Cmd+Enter），两条通道都有后端消费。
  // #1223：`ask_user` 通用提问与约束澄清**共用同一条锁定语义**（都是"有待回答的
  // 用户输入"），差别只在文案——ask_user 不是约束澄清，故走中性措辞。
  // #1283：文案按**实际 lock 原因**一次算清（constraint > ask_user > approval，与
  // 既有优先级逐字一致）；`lockHint` / `lockedPlaceholder` / 权限 pill 的禁用原因 /
  // 队列条 title 全部从它派生，避免四份三元链各写一遍优先级、改文案时漂移。
  const lockReason: LockReason | null = constraintInputPending
    ? 'constraint'
    : askUserInputPending
      ? 'ask_user'
      : approvalPending
        ? 'approval'
        : null;
  const locked = lockReason !== null;
  // 纯 streaming 有自己的 affordances（停止键/Esc 提示）。
  const showLock = locked && !streaming;
  const lockCopy = lockReason === null ? null : LOCK_COPY[lockReason];
  const lockHint = lockCopy?.hint ?? '';
  const lockedPlaceholder = lockCopy?.placeholder ?? '';
  /** #1223：排队条的 steer/flush 也是"开新消息"路径，与输入框同一把锁管到底——
   *  否则输入框锁了、队列条还能点，用户照样撞后端 409。标题按具体待答项中性化。
   *  #1283 P2-2：队列项的「编辑→保存」也走 `POST /messages {queue_id}`，同一把锁。 */
  const pendingUserInputReason: QueueLockReason | null =
    lockReason === 'constraint' || lockReason === 'ask_user' ? lockReason : null;
  const pendingUserInputLocked = pendingUserInputReason !== null;
  const pendingUserInputHint = pendingUserInputReason === null
    ? ''
    : QUEUE_HINT[pendingUserInputReason];

  // ── #283 权限 pill：会话内可改 + 升档确认 ──
  // 禁用原因由 `lockReason` 一次算清（待回答用户输入 / 待审批，后端闸门都会 409，
  // ADR-0041 D5），再加上本轮进行中（档位下一轮才生效，本轮改了 UI 无从交代结果）。
  // `#236` 的「会话内一律只读」那一条已随本票删除。
  const permissionLocked = locked || streaming;
  // #1283 P1：禁用原因按真实 lock 原因分流——ask_user 待答时绝不能报「等待审批」。
  const permissionDisabledHint = lockCopy
    ? lockCopy.permissionHint
    : streaming
      ? '本轮进行中，等这一轮结束再改档'
      : undefined;

  // 三态互斥：待确认（`pendingDanger`）> 提交中（`permissionBusy`）> 失败原因。
  const [pendingDanger, setPendingDanger] = useState<string | null>(null);
  const [permissionBusy, setPermissionBusy] = useState(false);
  const [permissionError, setPermissionError] = useState<string | null>(null);
  const dangerDescId = useId();

  /** 提交改档。**浮层保持打开直到回执**——提前关掉浮层等于「点了没反应」，那正是本票要
   *  消灭的形态；失败就把后端 detail 原样留在原地（409 是「先裁决那条审批」这类可执行
   *  的话），成功才关。 */
  const commitPermission = (next: string | null, close: () => void) => {
    setPermissionError(null);
    setPermissionBusy(true);
    void Promise.resolve(onPermissionModeChange?.(next))
      .then(() => {
        setPendingDanger(null);
        setPermissionBusy(false);
        close();
      })
      .catch((e: unknown) => {
        setPermissionBusy(false);
        setPermissionError((e as Error).message);
      });
  };

  /** 权限选中回调。返回 `false` = 否决本次关闭（契约见 `OptionPicker` 的 `onChange`）。 */
  const handlePermissionSelect = (next: string | null, close: () => void): boolean => {
    setPermissionError(null);
    // 新会话：创建期选择，没有后端往返、也无所谓「确认」——立即关（既有行为零改动）。
    if (!permissionInSession) {
      onPermissionModeChange?.(next);
      return true;
    }
    // 会话内：**升**到完全访问要一次显式确认（取消即不发请求）；降档直接提交。
    // 「重复选当前档」不必再问一遍——它不改变任何东西。
    if (next === DANGER_PERMISSION_MODE && next !== selectedPermissionMode) {
      setPendingDanger(next);
      return false;
    }
    commitPermission(next, close);
    return false;
  };

  /** 权限浮层底部：确认面 / 失败原因 / 提交态 / 会话内生效时机披露，四者互斥。
   *  用渲染函数形态是因为「确认」之后必须由 picker 关掉自己（`close` 只在那边存在）。 */
  const renderPermissionFooter = ({ close }: { close: () => void }) => {
    if (pendingDanger !== null) {
      return (
        <div
          className="picker-confirm"
          role="alertdialog"
          aria-label="确认升级到完全访问"
          aria-describedby={dangerDescId}
        >
          <TriangleAlert size={12} aria-hidden="true" />
          <span id={dangerDescId}>
            完全访问：此后所有工具调用都不再需要审批，含网络与系统副作用。确认升级？
          </span>
          <button
            type="button"
            className="project-btn project-btn-danger"
            onClick={() => commitPermission(pendingDanger, close)}
            disabled={permissionBusy}
          >
            {permissionBusy ? '提交中…' : '升级'}
          </button>
          <button
            type="button"
            className="project-btn"
            onClick={() => setPendingDanger(null)}
            disabled={permissionBusy}
          >
            取消
          </button>
        </div>
      );
    }
    if (permissionError !== null) {
      return (
        <div className="picker-confirm" role="alert">
          <TriangleAlert size={12} aria-hidden="true" />
          <span>改档失败：{permissionError}</span>
          <button type="button" className="project-btn" onClick={() => setPermissionError(null)}>
            知道了
          </button>
        </div>
      );
    }
    if (permissionBusy) return <span>提交中…</span>;
    // ADR-0041 D4：改档**下一轮 run 生效** ⇒ UI 不得承诺「立即生效」，得把时机说清。
    return permissionInSession ? (
      <span>改档从下一轮 run 起生效（本轮不受影响）</span>
    ) : undefined;
  };

  // 外部示例任务注入（引用变化即触发；每次点击 chip 生成新对象）
  useEffect(() => {
    if (!presetTask) return;
    setValue(presetTask.text);
    // 注入后把焦点交到输入框：焦点留在 chip 上时，键盘用户按 Enter 会**再点一次
    // chip**（读作"没反应"），与"注入即可编辑/发送"的意图相反（真机审计 A-06）。
    inputRef.current?.focus();
  }, [presetTask]);

  /** #1148：**分离投递**（窗外落定）迟到的失败——把没送出去的原文还回输入框。
   *
   *  只在输入框**为空**时回填：用户若已经打了新内容，那是更新的意图，绝不用旧文本
   *  覆盖它——成熟产品同款纪律（DSH `restoreFailedDrafts` 只在编辑器空时重建，
   *  `facade.ts:771,778`；Slack/Discord 的"发送失败不消费用户输入"）。
   *
   *  为什么不能像 presetTask 那样直接 `setValue(注入值)`：那条通道是"用户点了示例
   *  任务 chip"，覆盖输入是用户此刻的明确意图；这里是失败回填，注入时用户可能已经
   *  在同一会话里打了新草稿，覆盖它就是另一种数据丢失。故与提交回调里
   *  `accepted === false` 那一支走**同一条**语义（DSH `settleDetachedFailure`：分离
   *  投递的失败并入同一条失败呈现，而不是另开一条只显示不恢复的旁路）。
   *
   *  依赖对象引用即可：每次失败都是新的 `{ text }`（承重理由见 `FailedDraft`），同一段
   *  文本连续失败两次也各算一次注入。
   *  StrictMode 下 effect 双跑是安全的：回填是幂等的 setValue（第二跑时输入框已非空
   *  ⇒ 原样返回，不会出现两份文本）。
   *  **刻意不抢焦点**：这条回执可能迟到几十秒，届时用户多半在别处（另一个输入框、
   *  排队项的就地编辑）——抢过来会让他的下一次按键落进输入框。presetTask 那条通道
   *  抢焦点是因为它由用户点击触发，与这里不是同一种时机。 */
  useEffect(() => {
    if (!failedDraft) return;
    setValue((current) => (current.trim() === '' ? failedDraft.text : current));
  }, [failedDraft]);

  const submit = (mode: 'queue' | 'steer') => {
    /* 提交那一刻的**原始**文本（含首尾空白 / 多行缩进）：未受理时的回填用它，
       逐字还给用户；`trimmed` 只用于上线内容与受理侧的切除定位（#1205）。 */
    const raw = value;
    const trimmed = value.trim();
    if (!trimmed || locked) return;
    /* #1195 带参提交拦截（票面正则 `^\/(\w+)(?:\s+([\s\S]+))?$`，审查 A-P1-1）：
       挂在 submit() 入口——Enter / Ctrl+Enter / 发送按钮**所有发送路径的唯一
       汇合点**，不再只 hook 无修饰 Enter 分支（否则 Ctrl+Enter 与按钮都会把
       `/goal <文本>` 当聊天发给 agent，违反票面 §4.1「一律 preventDefault 本地
       执行」与 AC6）。匹配对 **trim 后**的整条输入做（审查 A-P2-1）：`/goal␣`
       尾随空格同样落进命令路径，args 为空 ⇒ 走 §4.2「无参数则查看」。
       受 `locked` 门控（审查 A-P2-3）：与菜单触发同把锁——锁定态命令不执行，
       也不会被当成聊天发出。 */
    const submitHit = slashSubmitMatch(trimmed, SLASH_COMMANDS.map((c) => c.name));
    if (submitHit) {
      if (sessionId == null) {
        /* 无会话不可执行（审查 A-P3-1）：给可见反馈且**不清输入**——静默 return
           加清空等于把用户的命令文本无声丢掉。 */
        setSlashError('当前没有会话，命令暂不可用');
        return;
      }
      const cmd = SLASH_COMMANDS.find((c) => c.name === submitHit.name)!;
      void runSlashCommand(cmd, submitHit.args);
      setValue('');
      setSlashIndex(0);
      setSlashDismissed(false);
      return;
    }
    /* #1029：回执在途期间不许再提交。清空挪到回执之后（见下），文本框不再在提交
       瞬间变空 ⇒ 连按 Enter / 点发送不再被 `!value.trim()` 自然挡住，**必须**显式
       闸一次，否则一次输入会发出去 N 次。ref 同步判（不是 state）：同一 tick 里的
       第二次按键读不到刚 set 的 state。 */
    if (sendingRef.current) return;
    // #1027 门禁：上传中时拦截发送（按钮 disabled 只挡点击，Ctrl+Enter 走这里）。
    // 阻断提示是界面常驻的 `.composer-send-blocked`——这里只需拦下请求；不拦就会
    // 复现 #1027（readyIds 为空时请求体无 attachments 键，图被静默丢弃）。
    if (sendBlocked) return;
    // #825（MM-04）：只带**已就绪**的引用（在途/失败的草稿不参与本次发送——AC4：
    // 上传失败不阻塞纯文本发送）。文本仍必填：`POST /messages` 的 `content` 是必填项，
    // 空文本 + 纯图片不是本票支持的路由。
    const attachmentIds = attachments.readyIds;
    // #1027 兜底：未就绪（失败/在途残留）的草稿没进请求——绝不静默丢弃，一次性可见提示。
    // 提示是**本次发送**的结论：干净发送（无剥离）即清掉，不让过期文案误报下一次。
    const unreadyCount = attachments.items.filter(
      (item) => item.status !== 'ready' || !item.receipt,
    ).length;
    setDroppedNotice(unreadyCount > 0 ? `${unreadyCount} 个附件未随消息发出` : null);
    // steer 通道的**失败兜底不在这一层**（#219）：没有可打断的在途 run 时后端回 409
    // （session/service.py::SteerTargetNotFound），由交付层改投 queue 把消息送到
    // （见 useSession.sendFollowUp 的 steer 回退）。这里不能拿 `streaming` 当
    // 「服务端有在途 run」用——那正是 issue #196 的病灶（`streaming` 只表示
    // **本页有活流**，跨客户端时判反）。
    /* 清空**只在确认受理之后**发生（#1029）。此前是「提交瞬间无条件清空、失败不
       还原」——任何发送失败（待裁决 409 / 网络断 / 429…）用户文本都无声消失，而
       它既没进队列也没落日志，事后完全不可恢复。
       判据是发送口的返回值（不是猜的）：**显式 `false`** = 未受理 ⇒ 文本与草稿原样
       留在输入框，用户处理完（如先做恢复裁决）再发；`true`/`undefined`（旧签名）
       = 受理 ⇒ 按旧行为清空。发送口**抛错**同样按未受理处理（保留），不冒充受理。
       未落定期间（Promise 未 settle）同样**不清**——清空只发生在 ack 之后。
       未受理时还有第二条（#1205）：**在途期间用户自己把框清空/删掉了**，原文不能就此
       消失 ⇒ `restoreSubmitted` 只在框为空时把本次提交的正文回填；用户已经打了新草稿
       就绝不覆盖（与下面 `failedDraft` 的回填是同一条纪律）。 */
    const accepted =
      mode === 'steer' && onSteer
        ? onSteer(trimmed, rememberAsProceduralRule, attachmentIds, scope)
        : onSubmit(trimmed, rememberAsProceduralRule, attachmentIds, scope);
    sendingRef.current = true;
    setSending(true);
    /** 受理落定后的清空。**只在受理侧调用**（`false` 与抛错都不调）。 */
    const accept = () => {
      /* 只清**本次提交的那段**（#1029 P3）：清空挪到 ack 之后 ⇒ 在途期间输入框是
         活的，用户接着打的字不能被这一刀连带抹掉。
         判据是**定位后切除**，不是前缀比对：提交的是 `trimmed`，而框里是**原始**
         文本——首尾带空白 / 多行（粘贴任务、先回车再打字）时 `startsWith` 必假，
         受理成功后输入框永不清空（独立审查探针实证的回归）。`trimStart` 对齐后
         `indexOf` 定位，切掉那一段、原样保留两侧。
         定位不到（在途期间用户把提交的那段删了/改了）⇒ 原样不动——宁可不清，也不
         清掉用户没提交的内容（`sendingRef` 已挡住重复提交，留着不会造成重复发送）。 */
      setValue((v) => {
        const lead = v.length - v.trimStart().length;
        const at = v.trimStart().indexOf(trimmed);
        if (at === -1) return v;
        const rest = v.slice(0, lead + at) + v.slice(lead + at + trimmed.length);
        // 剩下的只有空白（首尾空白 / 多行粘贴的常态）⇒ 就是「清空」，不留残渣。
        return rest.trim() === '' ? '' : rest;
      });
      setRememberAsProceduralRule(false);
      // #1191：受理后复位作用域选择，下一次登记从默认「本会话」起。
      setScope('session');
      // 已进请求的那些草稿清掉。`attachmentIds` 只含**已就绪**的引用，在途/失败的
      // 草稿没进请求，因此仍留在栏里，等用户重试或下一轮再带。
      attachments.clearSent(attachmentIds);
    };
    /** 未受理时的回填（#1205）。**只在输入框为空时**回填本次提交的**原文**（`raw`，
        不是 `trimmed`——首尾空白/多行缩进在"用户已清空"这条路上同样不可再生，而框没被
        清空那条路本来就是逐字原文，两条恢复路径的保真度必须一致）：
        正常路径下未受理时框里本来就有原文（`accept` 没跑），这条只在「在途期间用户
        自己清空/删掉」时才真正生效；用户已经打了新草稿就原样不动——与 `failedDraft`
        的回填、DSH `restoreFailedDrafts`"只在编辑器为空时重建"是同一条纪律。
        判据用 `trim()`：只剩空白（用户把内容删干净）同样算空，否则原文会被判成
        "用户还有草稿"而丢掉。 */
    const restoreSubmitted = () => {
      setValue((current) => (current.trim() === '' ? raw : current));
    };
    void Promise.resolve(accepted)
      .then(
        (ok) => {
          // `undefined`（旧签名）/ `true` = 受理；只有显式 `false` 才保留。
          if (ok !== false) accept();
          else restoreSubmitted();
        },
        /* 发送口自己抛错 = **未受理**（正常路径由 App 收口成 `false`，不重复报错）。
           显式写成分支而不是 `.catch(() => {})`：后者读起来像"吞掉"，且把"抛错"
           归进了受理侧（清空）——那会让异常路径留下 `sendingRef=false` + 文本不清，
           用户再按 Enter 就重发一条**已经成功**的消息。
           这里指的是 **Promise 拒绝**（异步抛错）；发送口**同步** throw 会在本链挂上
           之前逸出 `submit`，既不走这里也不置忙态——但那时框里仍是原文（从未起飞），
           文本不丢，故不为它加一条 try/catch（现有两个调用方都返回 Promise）。
           Promise 拒绝与 `false` 同属未受理 ⇒ 同样走回填（#1205）：用户若已清空，
           原文回到框里。 */
        () => restoreSubmitted(),
      )
      .finally(() => {
        sendingRef.current = false;
        setSending(false);
      });
  };

  // ── #1195 内联 `/` 命令菜单 ──
  // 触发：行首或空格后半角 `/`（票面正则）。派生态——value 一变即重算，无第二份真值。
  // 无会话不触发（票面 §4.2：composer 本来也发不出去）。
  const slashMatch =
    sessionId != null && !locked ? slashTriggerMatch(value) : null;
  const slashItems = slashMatch ? filterSlashCommands(SLASH_COMMANDS, slashMatch.query) : [];
  const slashOpen = slashMatch !== null && !slashDismissed;
  // 定位切除的统一起点（B-S-3）：菜单开着时 = 命令 token（含分隔空白）在 value 里的起点。
  const slashCutStart = slashOpen && slashMatch !== null ? slashMatch.start : null;
  // S-8（P4）：高亮 index 的唯一钳制点（原先 onKeyDown / 渲染各写一遍 Math.min）。
  const selectedSlashIndex = Math.min(slashIndex, Math.max(slashItems.length - 1, 0));

  // A-P1-3：/compact 的三态宿主（#1197 契约，与 ContextUsagePanel 共用同一份
  // useCompactSession 状态机）——压缩中 spinner+禁用、被拒/成功回执走 info 面、
  // 失败走错误面。渲染见下方 slash 反馈区。
  const compact = useCompactSession(sessionId);

  // A-P3-3：outside-click 关闭——mousedown 落在菜单与输入框之外即关闭（与 Esc
  // 同一条 dismissed 通道：value 不变 ⇒ 派生匹配仍成立，必须显式 dismissed 才关得住）。
  useEffect(() => {
    if (!slashOpen) return;
    const onDocMouseDown = (e: MouseEvent) => {
      const target = e.target instanceof Node ? e.target : null;
      if (target === null) return;
      if (slashMenuRef.current?.contains(target)) return;
      if (inputRef.current?.contains(target)) return;
      setSlashDismissed(true);
    };
    document.addEventListener('mousedown', onDocMouseDown);
    return () => document.removeEventListener('mousedown', onDocMouseDown);
  }, [slashOpen]);

  const runSlashCommand = useCallback(
    async (cmd: SlashCommand, args: string | undefined) => {
      if (!sessionId) return;
      setSlashError(null);
      setSlashInfo(null);
      try {
        await cmd.run(args, {
          sessionId,
          /* A-P2（复审）：pill 在审批待决/本轮进行中是 disabled——程序化 click 打在
             disabled 按钮上等于无效果且零反馈（浏览器不给 disabled 按钮派 click）。
             与 pill 的 disabledHint **同源**给一条可见反馈（中性 info 面），不静默。 */
          openPermissionPicker: () => {
            if (permissionDisabledHint !== undefined) {
              setSlashInfo(permissionDisabledHint);
              return;
            }
            permissionPickerRef.current
              ?.querySelector<HTMLButtonElement>('button[aria-label="权限模式"]')
              ?.click();
          },
          openModelPicker: () =>
            modelPickerRef.current
              ?.querySelector<HTMLButtonElement>('button[aria-label="模型选择"]')
              ?.click(),
          reportError: (message) => setSlashError(message),
          reportInfo: (message) => setSlashInfo(message),
          // A-P1-3：压缩走共享三态宿主；成功回执（含被拒=零写入）落中性 info 面，
          // 失败由 hook 的 compactError 面呈现（后端 detail 原样）。
          runCompact: () =>
            compact.run({
              onSuccess: (r) =>
                setSlashInfo(
                  r.compacted_turn_count > 0
                    ? `压缩完成：${r.tokens_before.toLocaleString()} → ${r.tokens_after.toLocaleString()} tok`
                    : '没有可压缩的早期内容',
                ),
            }),
          getSessionEvents,
        });
      } catch (e) {
        setSlashError(e instanceof Error && e.message ? e.message : '命令执行失败');
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- compact.run 按 sessionId 稳定（useCallback）；permissionDisabledHint 是门禁真值，必须跟进刷新闭包
    [sessionId, compact.run, permissionDisabledHint],
  );

  /** 命令受理（执行 + 输入切除）的唯一入口：菜单 Enter、菜单点选、submit 拦截
   *  三条路都走这里。`cutFrom` = 定位切除的起点（审查 B-S-3：只删命令 token，
   *  保留命令前的前缀文本，如 `hello /compact` 执行后剩 `hello`）；`null` =
   *  整条输入就是命令调用（submit 拦截路径），全清。 */
  const acceptSlashCommand = (
    cmd: SlashCommand,
    args: string | undefined,
    cutFrom: number | null,
  ) => {
    void runSlashCommand(cmd, args);
    setValue((v) => (cutFrom === null ? '' : v.slice(0, cutFrom)));
    setSlashIndex(0);
    setSlashDismissed(false);
  };
  // §5.1 发送键语义（与上游一致）：Enter = queue（默认）；Ctrl/Cmd+Enter = steer。
  // IME composition 守卫（审查 P1）：中文输入法按 Enter 确认拼音时
  // `isComposing` 为真——此时不提交，否则半截拼音会被当成任务发出。
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    // ── #1195 内联菜单键盘语义（在 steer/queue 之前拦截）──
    // Esc：只关菜单，不清输入（票面 §4.1）。
    if (slashOpen && e.key === 'Escape') {
      e.preventDefault();
      setSlashDismissed(true);
      return;
    }
    if (slashOpen && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      // 菜单开时 ↑↓ 归菜单（不移输入框光标——票面 §4.1）。
      e.preventDefault();
      const count = Math.max(slashItems.length, 1);
      setSlashIndex((i) => (e.key === 'ArrowDown' ? (i + 1) % count : (i - 1 + count) % count));
      return;
    }
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit('steer');
      return;
    }
    // Enter（无修饰键）= queue。Shift+Enter 保留换行（真实用户习惯）。
    // 带参提交拦截已收编进 submit() 入口（审查 A-P1-1：所有发送路径的唯一汇合点
    // ——Enter / Ctrl+Enter / 发送按钮都经它，不再只在无修饰 Enter 分支 hook）。
    if (e.key === 'Enter' && !e.shiftKey) {
      // 菜单开着且有可见命中 → Enter 执行高亮项（票面 §3.1；唯一命中时高亮必是唯一项）。
      // 定位切除只删命令 token（slashCutStart），不丢命令前的前缀（审查 B-S-3）。
      if (slashOpen && slashItems.length > 0) {
        e.preventDefault();
        acceptSlashCommand(slashItems[selectedSlashIndex], undefined, slashCutStart);
        return;
      }
      e.preventDefault();
      submit('queue');
    }
  };

  const currentModel = effectiveModelEntry(selectedModel, models);
  // ── #825（MM-04）附图入口门禁（AC1/AC7）；#1015 修订 ──
  // 两条互斥原因，**都不许静默**：模型非视觉、等待审批/澄清。原因同时是
  // 拖放遮罩上的文案与按钮 title，用户不必对着禁止光标猜。
  // 「会话缺失」不再是门禁：新建态首次投喂经 `onEnsureSession` 懒建会话（#1015），
  // 入口常开——原来那句"三条互斥原因"随本票收口（issue #1015 待裁决 #4 的自述失真）。
  const visionUnsupported = currentModel?.supportsVision === false;
  const attachBlockedReason = locked
    ? lockHint
    : visionUnsupported
      ? `${NON_VISION_MODEL_REASON}，已禁用附图`
      : '';
  const canAttach = attachBlockedReason === '';
  const pickFiles = () => {
    if (canAttach) fileInputRef.current?.click();
  };
  const addDraftFiles = attachments.addFiles;
  /** 把文本插到**光标处**（混合剪贴板的文本回填，见 `onPaste`；`#826` 的 `@path` 引用同一条）。
   *
   *  受控 textarea 的 `value` 由 React 在提交阶段写入，那一刻浏览器会把插入符推到
   *  末尾——所以目标位置先存进 ref，由渲染后的 effect 消费（否则光标落在粘贴内容之后，
   *  用户接着敲的字会跑到粘贴文本的后面）。
   *
   *  `draftTextRef` 是 `value` 的**同步镜像**：同一 tick 里的第二次插入（粘贴的文本 +
   *  同一次事件里分流出的引用）必须看得见第一次的结果——`value` 闭包要等下一次渲染才
   *  更新，照它算第二次会把第一次插进去的内容整段丢掉。 */
  const pendingCaret = useRef<number | null>(null);
  const draftTextRef = useRef(value);
  useEffect(() => {
    draftTextRef.current = value;
  });
  useEffect(() => {
    const caret = pendingCaret.current;
    if (caret === null) return;
    pendingCaret.current = null;
    inputRef.current?.setSelectionRange(caret, caret);
  });
  /** 恒等稳定的插入口：`addFiles` 依赖它，而 `addFiles` 是拖放 effect 的依赖——
   *  每次渲染换新函数会把 5 个 document 监听重装一遍（既有注释同一条理由）。 */
  const insertText = useCallback((text: string) => {
    const element = inputRef.current;
    const current = draftTextRef.current;
    const start = element?.selectionStart ?? current.length;
    const end = element?.selectionEnd ?? start;
    const next = `${current.slice(0, start)}${text}${current.slice(end)}`;
    draftTextRef.current = next;
    pendingCaret.current = start + text.length;
    setValue(next);
  }, []);
  /** 只把"是否接受"这一条门禁挂在本组件上；真正的入列/上传在 hook 里。
   *  依赖那个**稳定的** `addFiles`（useDraftAttachments 的依赖是
   *  [sessionId, commit, setIntakeError, startUpload, ensureSession]，App 的
   *  `ensureSession` 是 useCallback 单例，同一会话内标识不变）：
   *  依赖整个 `attachments` 对象会让下面的拖放 effect 每个渲染重装 5 个监听
   *  （hook 每次渲染返回新对象字面量）。
   *
   *  #826（MM-05）：三条通道（拖放 / 粘贴 / 选择器）都先过 `routeHostFiles`——桌面
   *  （preload 桥在场）把「有真实路径的非图片文件」转成 `@path` 引用插进输入框，不上传
   *  字节；图片、剪贴板字节、目录与无桥时的任何文件原样交给既有上传入口（Web 行为不变）。
   *
   *  `prefixText` 只服务粘贴通道：混合剪贴板要在**同一次插入**里带上 `text/plain`（分开
   *  插两次会互相看不见对方——光标位置是渲染后的 DOM 状态，同一 tick 里它还没变过）。 */
  const addFiles = useCallback(
    (files: readonly File[], directories?: ReadonlySet<File>, prefixText = '') => {
      if (!canAttach) return;
      const routing = routeHostFiles(files, { directories });
      const mentions = routing.references.length > 0 ? `${routing.references.join(' ')} ` : '';
      if (prefixText !== '' || mentions !== '') insertText(`${prefixText}${mentions}`);
      if (routing.uploads.length > 0) addDraftFiles(routing.uploads, directories);
    },
    [canAttach, addDraftFiles, insertText],
  );
  // 整页拖放（AC1）：document 级监听，拖到任意位置都算——只把"是否接受"交给这里，
  // 计数/命中判断在 `lib/dropEvents.ts`（上游 COPY，含空目录剔除）。
  useEffect(
    () => installDocumentDropEvents(canAttach, addFiles, dragDepth, setDragActive),
    [canAttach, addFiles],
  );
  const effortModel = reasoningEffortProjection ? reasoningEffortProjection.model : currentModel;
  const reasoningEffortCapability =
    effortModel?.isAvailable === false ? undefined : effortModel?.reasoningEffort;
  const supportedEffortIds = new Set(reasoningEffortCapability?.supported ?? []);
  const modelReasoningEfforts = reasoningEffortCapability
    ? reasoningEfforts.filter((effort) => supportedEffortIds.has(effort.id))
    : [];
  const projectedDefault = reasoningEffortCapability?.default ?? null;
  const effectiveReasoningEffort = reasoningEffortProjection
    ? modelReasoningEfforts.some((effort) => effort.id === projectedDefault)
      ? projectedDefault
      : null
    : modelReasoningEfforts.some((effort) => effort.id === selectedReasoningEffort)
      ? selectedReasoningEffort
      : null;
  const selectedEffortIndex = modelReasoningEfforts.findIndex(
    (effort) => effort.id === effectiveReasoningEffort,
  );
  const reasoningEffortTone =
    selectedEffortIndex < 0
      ? undefined
      : selectedEffortIndex === 0
        ? 'blue'
        : selectedEffortIndex === modelReasoningEfforts.length - 1
          ? 'deep'
          : 'violet';

  // 控件行是否渲染——至少有一个非空目录或预算入口（#426）时才显示 control row 容器
  const hasControls =
    models.length > 0 ||
    permissionModes.length > 0 ||
    agentProfiles.length > 0 ||
    modelReasoningEfforts.length > 0 ||
    (!permissionInSession && onBudgetRunTurnsChange !== undefined);

  return (
    <div className="composer-wrap">
      {/* §5.2 队列条：Composer 输入框**正上方**（不与 .composer-esc-hint 争左下角，
          那是 #194 的病灶）。空队列不渲染（不占位不闪烁）。 */}
      {undelivered.length > 0 && (
        <div className="queue-bar" role="list" aria-label="待发送消息">
          {undelivered.map((item) => (
            <div key={item.id} className="queue-item" role="listitem" data-queue-id={item.id}>
              {editingId === item.id && item.kind === 'queue' ? (
                /* §5.2 就地编辑：该行原地换成输入框 + 保存/取消，不弹模态、
                   不回填主输入框（回填 + 取消原项会让"取消"变成不可逆的丢条目）。 */
                <>
                  <input
                    className="queue-item-edit"
                    value={editDraft}
                    autoFocus
                    aria-label="编辑排队消息内容"
                    /* #1283 复审 P4：锁在编辑态打开后才到时，Enter 是静默 no-op；
                       输入框自身给出原因（按钮的 disabled title 对键盘用户不可见）。 */
                    title={pendingUserInputLocked ? pendingUserInputHint : undefined}
                    onChange={(e) => setEditDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') {
                        e.preventDefault();
                        setEditingId(null);
                        return;
                      }
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        void commitEdit(item);
                      }
                    }}
                  />
                  <span className="queue-item-actions">
                    <button
                      className="queue-item-btn"
                      onClick={() => { void commitEdit(item); }}
                      disabled={pendingUserInputLocked}
                      aria-label="保存排队消息"
                      title={pendingUserInputLocked ? pendingUserInputHint : '保存这一条'}
                    >
                      <Check size={13} />
                    </button>
                    <button
                      className="queue-item-btn"
                      onClick={() => setEditingId(null)}
                      aria-label="取消编辑"
                      title="取消编辑（不改动这条）"
                    >
                      <X size={13} />
                    </button>
                  </span>
                </>
              ) : (
                <>
              <span className={`queue-item-badge queue-item-${item.kind}`}>
                {item.kind === 'steer' ? '引导' : '排队'}
              </span>
              {/* #825（MM-04）：投递前草稿已从附图栏清掉、受控读回又被授权闸门挡成
                  404（引用还没写进事件流）⇒ 这一条在 UI 里本来**零痕迹**，用户会以为
                  图丢了。只给张数、不渲染缩略图（那必然是一片"加载失败"）。 */}
              {item.attachments !== undefined && item.attachments.length > 0 && (
                <span
                  className="queue-item-attachments"
                  title="这条待发送输入带着图片，投递时会连附件引用一起发出去"
                >
                  附图 {item.attachments.length} 张
                </span>
              )}
              <span className="queue-item-content" title={item.content}>
                {item.content.length > 40 ? `${item.content.slice(0, 40)}…` : item.content}
              </span>
              {/* 三个动作只给**排队项**（ADR-0030 §5.2 的原文就是"每个排队项显示…
                  编辑/立即/取消"）。steer 项没有对应后端通道：取消/编辑都以 queue_id
                  定位（`cancel_queue` 只认 kind=queue），渲染出来点了就是静默 404。
                  等 steer 撤回有后端语义时再补（本票不加半条通道）。 */}
              {item.kind === 'queue' && (
              <span className="queue-item-actions">
                <button
                  className="queue-item-btn"
                  onClick={() => {
                    setEditingId(item.id);
                    setEditDraft(item.content);
                  }}
                  disabled={!onEditItem || pendingUserInputLocked}
                  aria-label="编辑排队消息"
                  title={pendingUserInputLocked ? pendingUserInputHint : '编辑这条排队消息'}
                >
                  <Pencil size={13} />
                </button>
                <button
                  className="queue-item-btn"
                  onClick={() => onSteerItem?.(item)}
                  disabled={!onSteerItem || pendingUserInputLocked}
                  aria-label="立即发送"
                  title={pendingUserInputLocked
                    ? pendingUserInputHint
                    : '立即发送（注入当前运行）'}
                >
                  <Zap size={13} />
                </button>
                <button
                  className="queue-item-btn"
                  onClick={() => onCancelItem?.(item)}
                  disabled={!onCancelItem}
                  aria-label="取消排队消息"
                  title="取消这条排队消息"
                >
                  <X size={13} />
                </button>
              </span>
              )}
                </>
              )}
            </div>
          ))}
          {/* §4.6「立即发送全部」：重启后/空闲时手动投递（POST /queue/flush）。
              仅 onFlush 在场时渲染——flush 会开新 run，streaming 时不给
              （在途 run 的 flush 是 409，交给逐项「立即」更合适）。 */}
          {onFlush && !streaming && (
            <button
              className="queue-item-btn queue-flush-btn"
              onClick={onFlush}
              disabled={pendingUserInputLocked}
              aria-label="立即发送全部"
              title={pendingUserInputLocked
                ? pendingUserInputHint
                : '立刻投递待发送输入（POST /queue/flush）'}
            >
              <Play size={13} />
              立即发送全部
            </button>
          )}
        </div>
      )}
      {/* #1195：内联 `/` 菜单——输入框正上方（composer-wrap 的直接子节点、dock 之前；
          absolute 定位锚在 wrap 上，bottom:100% 相对 dock 视觉位）。非 Dialog 中央浮层
          （票面 §3.1）。样式收进 app.css 的 .slash-menu 族（审查 B-S-6：~90 行 inline
          style 偏离 §9.3）；**定位前提**是 .composer-wrap 有 position（A-P1-2，
          见 app.css 该规则旁的注释与 slash 测试的 CSS 回归断言）。 */}
      {slashOpen && (
        <div
          ref={slashMenuRef}
          id={slashMenuId}
          data-testid="slash-menu"
          role="listbox"
          aria-label="斜杠命令"
          className="slash-menu"
        >
          {slashItems.length === 0 ? (
            <div className="slash-menu-empty">无匹配命令</div>
          ) : (
            ['add', 'command'].map((group) => {
              const groupItems = slashItems.filter((c) => c.group === group);
              if (groupItems.length === 0) return null;
              return (
                <div key={group}>
                  <div className="slash-menu-group">{group === 'add' ? '添加' : '指令'}</div>
                  {groupItems.map((cmd) => {
                    const index = slashItems.indexOf(cmd);
                    const selected = index === selectedSlashIndex;
                    return (
                      <div
                        key={cmd.name}
                        id={`slash-opt-${cmd.name}`}
                        role="option"
                        aria-selected={selected}
                        data-slash-name={cmd.name}
                        className={`slash-menu-item${selected ? ' selected' : ''}`}
                        onMouseDown={(e) => {
                          // mousedown 而非 click：textarea 的 blur 会在 click 之前到，
                          // blur 不关菜单（关闭路径是 Esc/执行/outside-click），但 mousedown 先选中。
                          e.preventDefault();
                          setSlashIndex(index);
                        }}
                        onClick={() => acceptSlashCommand(cmd, undefined, slashCutStart)}
                      >
                        <span className="slash-menu-label">{cmd.labelCN}</span>
                        <span className="slash-menu-name">/{cmd.name}</span>
                        <span className="slash-menu-desc">{cmd.description}</span>
                      </div>
                    );
                  })}
                </div>
              );
            })
          )}
        </div>
      )}
      {/* 反馈区：错误（红）/ 信息（中性，B-S-4）/ 压缩中 spinner（A-P1-3）。
          两类错误**可同时在场**（修正复审 P3 的旧说法"实际互斥"：runSlashCommand
          入口只清 slash 面、不清 hook 的 compactError——先 /compact 失败、再跑
          /goal 撞错，两条都在），`??` 让 slashError 优先、compactError 稍后由
          hook 的 8s 消退自行退场；合并成同一错误面是为避免两块同貌红条。 */}
      {(slashError !== null || compact.compactError !== null) && (
        <div data-testid="slash-error" role="alert" className="slash-error">
          {slashError ?? compact.compactError}
        </div>
      )}
      {slashInfo !== null && (
        <div data-testid="slash-info" role="status" className="slash-status">
          {slashInfo}
        </div>
      )}
      {compact.compacting && (
        <div data-testid="slash-compacting" role="status" className="slash-status">
          <span className="ctx-usage-spinner" aria-hidden="true" />
          正在压缩上下文…
        </div>
      )}
      <div className="composer-dock surface-floating">
        <ComposerAttachments
          items={attachments.items}
          intakeError={attachments.intakeError}
          budget={budgetText(
            attachments.items.map((item) => ({ bytes: item.file.size })),
            limits,
          )}
          dragActive={dragActive}
          canAcceptDrop={canAttach}
          dropBlockedReason={attachBlockedReason}
          onPickFiles={pickFiles}
          onRemove={attachments.remove}
          onRetry={attachments.retry}
          onDismissIntakeError={attachments.dismissIntakeError}
        />
        {/* 文件选择器（AC1）：`hidden` 而不是 display:none——`.click()` 仍可编程触发，
            且不进 tab 顺序（入口是旁边那个带 aria-label 的按钮）。`value` 每次清空，
            否则连选同一张图不会触发 change。 */}
        <input
          ref={fileInputRef}
          type="file"
          accept={limits.mediaTypes.join(',')}
          multiple
          hidden
          onChange={(event) => {
            const files = Array.from(event.target.files ?? []);
            event.target.value = '';
            addFiles(files);
          }}
        />
        {/* UI-01：审批待决时给出锁定原因（置灰不是隐形）。 */}
        {showLock && <div className="composer-locked-hint">{lockHint}</div>}
        {/* #825 AC7：非视觉模型**可见**的禁用原因（不是只藏在 title 里）。 */}
        {visionUnsupported && (
          <div className="composer-attach-hint" role="status">
            {NON_VISION_MODEL_REASON}：已禁用附图；历史附图会被省略为文本占位
          </div>
        )}
        {/* #1027：上传中时的发送阻断提示（与 .composer-attach-hint 同一版式语言）。
            常驻到上传落定——键盘用户按 Ctrl+Enter 被拦截时，读到的就是这一条。 */}
        {sendBlocked && (
          <div className="composer-send-blocked" role="status">
            {sendBlockedReason}
          </div>
        )}
        {/* #1027 兜底：发送时被剔除的未就绪附件，一次性可见提示（可关闭，不常驻）。 */}
        {droppedNotice !== null && (
          <div className="composer-attach-dropped" role="alert">
            <span className="composer-attach-dropped-text">{droppedNotice}</span>
            <button
              type="button"
              className="composer-attach-error-btn"
              onClick={() => setDroppedNotice(null)}
              aria-label="关闭附件未发出提示"
            >
              <X size={12} />
            </button>
          </div>
        )}
        {/* #1028 AC2：常驻归属 chip——把「当前会话在哪个项目 / 哪个 cwd」显形。
            归属由后端按 cwd 精确相等判定（前端不比对，见 lib/ownership.ts）；未归属
            时本 chip 就是那条「你不在任何项目里」的警示。 */}
        {ownership !== null && (
          <div
            className={`composer-owner-chip composer-owner-chip-${ownership.kind}`}
            role="note"
            title={ownership.title}
          >
            {ownership.label}
          </div>
        )}
        <textarea
          ref={inputRef}
          id="composer-input"
          name="task"
          className="composer"
          placeholder={showLock ? lockedPlaceholder : `描述一个任务…（${modKey()}+Enter 发送）`}
          value={value}
          /* S-7（P4）：菜单 = 本 textarea 拥有的 listbox——ARIA 关联三件套
              （展开态 / 所有权 / 当前高亮项），屏幕阅读器读得到"列表开着、选中谁"。 */
          aria-expanded={slashOpen}
          aria-controls={slashOpen ? slashMenuId : undefined}
          aria-activedescendant={
            slashOpen && slashItems.length > 0
              ? `slash-opt-${slashItems[selectedSlashIndex].name}`
              : undefined
          }
          onChange={(e) => {
            // #1195：输入变化 = 可见命令列表身份变化 ⇒ 高亮收回第一项；Esc 关闭态解除。
            setSlashIndex(0);
            setSlashDismissed(false);
            setValue(e.target.value);
          }}
          onKeyDown={onKeyDown}
          onPaste={(event) => {
            // AC1 剪贴板粘贴：只截取 `kind === 'file'` 的图片。没有文件时**不**
            // preventDefault——纯文本粘贴必须原样进输入框（这是粘贴的默认语义，
            // 抢先接管会把普通复制粘贴弄坏）。
            const files = filesFromClipboard(event.clipboardData);
            if (files.length === 0) return;
            // 门禁命中时同样**不**接管：`addFiles` 会直接 return，而
            // preventDefault 已经执行 ⇒ 事件被吞、毫无反馈（三方通道里另两条在禁用
            // 态都有显式出口，粘贴这条不能例外地静默）。
            if (!canAttach) return;
            event.preventDefault();
            // 网页 / Word 的剪贴板是**混合**的：`items` 里有图，`text/plain` 里还有
            // 文字（标题、选区文本、图注）。`preventDefault` 之后原生粘贴不再发生
            // ⇒ 不回填就等于把文本吞掉（上游 `keymap.ts:161-187` 同款做法：有文件时
            // 照样读 `text/plain`，非空即接管并交给文本通道）。
            const pastedText = event.clipboardData.getData('text/plain');
            // 文本与引用走**同一次**插入（顺序：粘贴文本在前，引用紧随其后）。
            addFiles(files, undefined, pastedText);
          }}
          rows={2}
          disabled={locked}
          aria-label="Agent 任务"
        />
        <div className="composer-rule-row">
          <label className="composer-rule-signal" htmlFor="remember-as-procedural-rule">
            <input
              id="remember-as-procedural-rule"
              type="checkbox"
              checked={rememberAsProceduralRule}
              onChange={(event) => setRememberAsProceduralRule(event.target.checked)}
              disabled={locked}
            />
            <span title="仅在这条消息明确表达希望长期沿用的操作规则时勾选。">
              将这条消息作为可复用规则
            </span>
          </label>
          {/* #1191：勾选后出现作用域三选一（默认「本会话」，票面 §四.1）。 */}
          {rememberAsProceduralRule && (
            <div className="composer-scope-pills" role="group" aria-label="登记作用域">
              {SCOPE_OPTIONS.map((option) => {
                const projectDisabled = option.id === 'project' && projectAvailable === false;
                return (
                  <button
                    key={option.id}
                    type="button"
                    className={`composer-scope-pill${scope === option.id ? ' on' : ''}`}
                    aria-pressed={scope === option.id}
                    disabled={projectDisabled || locked}
                    title={
                      projectDisabled
                        ? '当前会话不在 git 仓库内，无法登记项目级约束'
                        : option.hint
                    }
                    onClick={() => setScope(option.id)}
                  >
                    {option.label}
                  </button>
                );
              })}
            </div>
          )}
        </div>
        {/* 选中项下方的 hint 文案——逐字取自票面 §四.2（句子单独成节点，便于逐字核对）。 */}
        {rememberAsProceduralRule && (
          <div className="composer-scope-hint">
            <b>{SCOPE_OPTIONS.find((option) => option.id === scope)?.label}</b>
            <span className="composer-scope-hint-text">
              {SCOPE_OPTIONS.find((option) => option.id === scope)?.hint}
            </span>
          </div>
        )}
        {hasControls && (
          <div className="composer-controls">
            <div ref={modelPickerRef}>
            <ModelPicker
              models={models}
              selectedModel={selectedModel}
              onModelChange={onModelChange ?? (() => {})}
              disabled={locked}
            />
            </div>
            {reasoningEffortProjection && (
              <span
                className="composer-fallback-effort-note"
                role="status"
                title={`本轮已切换到备用模型 ${reasoningEffortProjection.modelName}`}
              >
                {reasoningEffortCapability ? '本轮备用模型默认档' : '本轮备用模型 · Provider 默认'}
              </span>
            )}
            {/* #201：三个档位下拉合并为同一个 OptionPicker——同一份实现、同一份视觉、
                同一套 ARIA（此前三处手抄 + 十条不一致）。
                ⚠ `aria-label` **刻意维持原值的中英混用**（`权限模式` / `Agent Profile` /
                `Reasoning Effort`）：票面冻结结论 B 明确「会影响到 e2e 定位器就不统一中文」，
                并点名「不得内置默认值，否则合并本身就会顺手把英文名改掉，等于偷偷做了 B 项」。
                所以这里传的是各调用点原来的名字——顺手统一会连同 locator 一起改，
                正是那条冻结结论要防的事。要统一请先改票面结论。 */}
            {/* #283：会话内**可改**——#236 的「创建时定、会话内不可修改」已被 F18-A 推翻。
                真值仍只来自投影（`session/started` / `permission/changed` 折叠出的
                `session_permission_mode`，由 App 传入）；新会话 → 仍是创建期选择（随 create
                请求发出）。升到「完全访问」要一次显式确认，确认面在本浮层的 footer 里
                （见 `renderPermissionFooter`）。 */}
            <div ref={permissionPickerRef}>
            <OptionPicker
              ariaLabel="权限模式"
              title="工具调用如何批准？"
              options={toCatalogOptions(permissionModes, catalogIcon)}
              value={selectedPermissionMode}
              onChange={handlePermissionSelect}
              icon={Shield}
              placeholder="权限"
              // 禁用原因按真实 lockReason 分流（待回答用户输入 / 待审批 / 本轮进行中）
              // ——不再是「档位不可变」。
              disabled={permissionLocked}
              disabledHint={permissionDisabledHint}
              // 会话内没有「默认（未选）」这个目标：端点只接受三个具体档位，`null` 发过去
              // 必然 422 ⇒ 留着就是一个点下去必错的死胡同。
              showDefault={!permissionInSession}
              footer={renderPermissionFooter}
            />
            </div>
            <OptionPicker
              ariaLabel="Agent Profile"
              title="这次会话用哪个档位？"
              options={toCatalogOptions(agentProfiles, catalogIcon)}
              value={selectedAgentProfile}
              onChange={(id) => onAgentProfileChange?.(id)}
              icon={User}
              placeholder="Agent"
              disabled={locked}
              // #201 冻结 AC：档位收窄提示（用户裁定"要提示，但从简，不能突兀"）。
              // 文案组装在纯函数里（`lib/agentProfileScope.ts`，vitest 直测）；
              // 没被收窄 / 后端没给 tool_scope → 该函数返回 null → 不渲染 footer。
              // 按**当前生效档位**披露：未选时按后端默认档位（main），因为那才是
              // 这次会话真正会用的工具面。
              footer={
                (() => {
                  const note = toolScopeNote(agentProfiles, selectedAgentProfile);
                  if (!note) return undefined;
                  // `tabIndex=0` 让它可聚焦（键盘用户也能拿到提示）；`aria-label`
                  // **以可见文字开头**再接 tooltip 内容——直接用 tooltip 当可访问名
                  // 会违反 WCAG 2.5.3（Label in Name：可访问名须包含可见文字，
                  // 否则语音控制/switch 用户照可见文字说不中）。
                  return (
                    <span
                      tabIndex={0}
                      title={note.title}
                      aria-label={`${note.text}；${note.title}`}
                    >
                      {note.text}
                    </span>
                  );
                })()
              }
            />
            <OptionPicker
              ariaLabel="Reasoning Effort"
              title={reasoningEffortProjection ? '备用模型的推理默认档' : '推理深度选哪一档？'}
              options={toCatalogOptions(modelReasoningEfforts, catalogIcon)}
              value={effectiveReasoningEffort}
              onChange={(id) => onReasoningEffortChange?.(id)}
              icon={Brain}
              currentTone={reasoningEffortTone}
              footer={reasoningEffortProjection && reasoningEffortCapability
                ? `本轮显示备用模型 ${reasoningEffortProjection.modelName} 的默认档位；运行结束后恢复主模型选择。`
                : undefined}
              customContent={
                <ReasoningEffortSlider
                  options={modelReasoningEfforts}
                  value={effectiveReasoningEffort}
                  defaultValue={reasoningEffortCapability?.default ?? null}
                  disabled={locked || reasoningEffortProjection !== null}
                  onChange={(id) => onReasoningEffortChange?.(id)}
                />
              }
              placeholder="推理"
              disabled={locked}
            />
            {/* #536：预算三项收敛为单入口 popover（设计稿 §2.3，替换 #426 三平铺）。
                会话内不显示——budget.run 属于**启动 run** 的请求（#422），续聊不带
                budget（#308）。留空 = 后端默认（不发键）；到顶/到点自动暂停，恢复
                面板抬高后继续。判形/换算唯一执行点在 lib/amend.ts（映射层）。 */}
            {!permissionInSession &&
              (onBudgetRunTurnsChange !== undefined ||
                onBudgetRunTokensChange !== undefined ||
                onBudgetRunDeadlineChange !== undefined) && (
              <BudgetPicker
                turns={budgetRunTurns ?? ''}
                onTurnsChange={onBudgetRunTurnsChange}
                tokens={budgetRunTokens ?? ''}
                onTokensChange={onBudgetRunTokensChange}
                deadline={budgetRunDeadline ?? ''}
                onDeadlineChange={onBudgetRunDeadlineChange}
                disabled={locked}
              />
            )}
          </div>
        )}
        {/* #194：右侧动作簇——发送/停止按钮与 Esc 提示**同一处、同一行**。
            此前提示绝对定位在 dock 左下角，正好压在档位行（模型选择器）上：同一个
            动作的两个 affordance 被放在了相反的两侧。现在两者共用一个右锚点，按钮
            的像素位置与改动前完全一致（right/bottom 同一组值），提示作为它的左邻出现。 */}
        <div className="composer-actions">
          {/* #825（MM-04）AC1/AC7：附图入口（第三个通道：选择器；另两个是粘贴与拖放）。
              禁用时 title 给出原因，且**不是唯一出口**——非视觉模型的可见说明在上方
              `.composer-attach-hint` 里。 */}
          <button
            type="button"
            className="composer-attach-trigger"
            onClick={pickFiles}
            disabled={!canAttach}
            aria-label="添加图片"
            title={canAttach ? '添加图片（也可直接粘贴或拖入页面）' : attachBlockedReason}
          >
            <ImagePlus size={16} />
          </button>
          {streaming && (
            /* Esc 中断提示（Claude Code "esc to interrupt" 语言）：键位绑定在 App
               全局，这里只做可见性——所以整个簇 pointer-events: none，只有按钮可点。 */
            <span className="composer-esc-hint" aria-hidden="true">
              <kbd>Esc</kbd> 停止
            </span>
          )}
          {streaming ? (
            <>
              {/* §5.1 D10：停止按钮与发送按钮**并存**（streaming 不再禁用输入）。
                  发送键 = queue（下个 run 接力）；点击停止 = 既有取消行为不变。 */}
              <button
                className="composer-send"
                onClick={() => submit('queue')}
                disabled={locked || !value.trim() || sendBlocked || sending}
                aria-label="发送"
                aria-busy={sending}
                title={
                  sendBlocked
                    ? sendBlockedReason
                    : sending
                      ? '上一条消息还在发送中…'
                      : `发送（Enter 排队 · ${modKey()}+Enter 立即）`
                }
              >
                <ArrowUp size={16} />
              </button>
              <button className="composer-stop" onClick={onCancel} aria-label="停止" title="停止">
                <Square size={14} />
              </button>
            </>
          ) : (
            <button
              className="composer-send"
              onClick={() => submit('queue')}
              disabled={locked || !value.trim() || sendBlocked || sending}
              aria-label="发送"
              aria-busy={sending}
              title={
                sendBlocked
                  ? sendBlockedReason
                  : sending
                    ? '上一条消息还在发送中…'
                    : `发送（Enter 排队 · ${modKey()}+Enter 立即）`
              }
            >
              <ArrowUp size={16} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
});
