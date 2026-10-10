/** Composer — task input at the bottom of the conversation column.
 *
 * Submits on Cmd/Ctrl+Enter; Enter = queue mode, Cmd/Ctrl+Enter = steer (ADR-0030
 * §5.1 D10：streaming 不再禁用输入——「本页有活流」≠「服务端有在途 run」，
 * 两者混用正是 issue #196 的根因）。等待审批时仍禁用（与审批 UI 竞争）。
 * presetTask: 外部注入的示例任务（空状态 chip 点击），注入后仍可自由编辑。
 */

import { memo, useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { ArrowUp, Brain, Check, ImagePlus, Pencil, Play, Shield, Square, TriangleAlert, User, X, Zap } from 'lucide-react';
import type { PresetTask, UndeliveredInput } from '../types';
import { modKey } from '../lib/platform';
import { toolScopeNote } from '../lib/agentProfileScope';
import { catalogIcon } from '../lib/catalogIcons';
import type { CatalogEntry, ModelCatalogEntry } from '../lib/api';
import type { ActiveFallbackModelProjection } from '../lib/modelReasoningEffortProjection';
import { budgetText, filesFromClipboard, getImageLimits, loadImageLimits, type ImageIntakeLimits } from '../lib/attachments';
import { routeHostFiles } from '../lib/hostFiles';
import { installDocumentDropEvents } from '../lib/dropEvents';
import { effectiveModelEntry } from '../lib/modelSelection';
import { useDraftAttachments } from '../hooks/useDraftAttachments';
import { ComposerAttachments } from './ComposerAttachments';
import { ModelPicker } from './ModelPicker';
import { BudgetPicker } from './BudgetPicker';
import { OptionPicker, toCatalogOptions } from './OptionPicker';
import { ReasoningEffortSlider } from './ReasoningEffortSlider';

/** #283：升到这一档要在 pill 浮层里给一次显式确认（ADR-0041 D1——后端**不加**强制标志位，
 *  确认是 UX 层的责任）。字面量与后端 `PermissionPolicy.DANGER_FULL_ACCESS` 同值：它同时
 *  「目录里的一个 id」与「危险判定」，所以不在前端另立第二张表（那样两处一漂移，
 *  「确认」就会挂在错的档位上——比不确认更糟）。 */
const DANGER_PERMISSION_MODE = 'danger-full-access';

/** 非视觉模型禁用附图的原因前缀（#937 / M-24 收口：title 与可见提示两处同源，改一处即两处）。 */
const NON_VISION_MODEL_REASON = '当前模型不支持视觉（supports_vision=false）';

interface Props {
  streaming: boolean;
  /** UI-01（D4-⑤）：存在待决审批时锁住 composer——运行被阻塞，新任务
   *  与审批互斥，不允许两条修复路径同时开放（评审 Riley 红旗）。 */
  approvalPending?: boolean;
  /** 有待回答的约束澄清时锁住新消息，避免新 run 覆盖待恢复问题。 */
  constraintInputPending?: boolean;
  onSubmit: (task: string, rememberAsProceduralRule: boolean, attachmentIds?: string[]) => void;
  /** steer 提交（ADR-0030 §5.1：同一份输入立即投递——Ctrl/Cmd+Enter）。
   *  缺席 = 回退到 onSubmit（queue），既有调用零改动。 */
  onSteer?: (task: string, rememberAsProceduralRule: boolean, attachmentIds?: string[]) => void;
  /** #825（MM-04）：**当前会话 id**——附图上传端点是 per-session 的
   *  （`POST /api/sessions/{id}/attachments`），所以没有会话就没有附图入口
   *  （`null` = 新建态 → 入口禁用并说明原因，而不是让用户传到一个不存在的会话下）。
   *  会话切换时草稿清空并中止在途上传（见 `hooks/useDraftAttachments.ts`）。 */
  sessionId?: string | null;
  onCancel: () => void;
  presetTask?: PresetTask | null;
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
   *  `POST /messages {content, queue_id}`（后端取消旧项 + 按 mode 重新投递）。 */
  onEditItem?: (item: UndeliveredInput, newContent: string) => void;
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
  onSubmit,
  onSteer,
  sessionId = null,
  onCancel,
  presetTask,
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
  /** 注入示例任务 / 提交后要把焦点交回的输入框（A-06）。 */
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  // ADR-0030 §5.2 就地编辑态：正在编辑的排队项 id + 草稿内容。只存 id 不存整条
  // item——条目会随事件流增删，存 id 让渲染始终对账当前事实（不变量 #22）。
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState('');

  // ── #825（MM-04）附图草稿 ──
  // 状态机在 hook 里（上传/缩略图/失败重试/换会话清理），这里只做**入口门禁**与
  // 把「已就绪」的引用交给提交通道。（纯文本发送不依赖任何草稿状态：发送按钮的
  // 可用性只看文本——AC4「上传失败不得阻塞纯文本发送」的落点。）
  const attachments = useDraftAttachments(sessionId);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  /** 整页拖拽计数：document 级 dragenter/dragleave 会成对触发，用计数判断"还在页面内"。 */
  const dragDepth = useRef(0);
  const [dragActive, setDragActive] = useState(false);

  // ── #937 / M-08：附图上限从服务端下发 ──
  // 初始值取模块当前值（可能已被 App 层拉到）；挂载后拉一次并落进本地 state——
  // 预检文案（budgetText）与文件选择器 accept 都消费这份。失败时 loadImageLimits
  // 返回离线 fallback（IMAGE_LIMITS），不抛、不打扰用户。
  const [limits, setLimits] = useState<ImageIntakeLimits>(() => getImageLimits());
  useEffect(() => {
    let alive = true;
    void loadImageLimits().then((next) => {
      if (alive) setLimits(next);
    });
    return () => {
      alive = false;
    };
  }, []);

  // ADR-0030 §5.1 D10：`locked` 拆开——approvalPending 仍禁用（等待审批时输入
  // 无意义且与审批 UI 竞争）；streaming **不再**禁用。issue #196 的根因正是
  // 「streaming 表示本页有活流」被当成「服务端有在途 run」：跨客户端场景下
  // 输入框可用、消息却走 queue 分支静默丢失。现在流式期间发消息 = 显式选择
  // queue（Enter）/ steer（Ctrl/Cmd+Enter），两条通道都有后端消费。
  const locked = approvalPending || constraintInputPending;
  // 纯 streaming 有自己的 affordances（停止键/Esc 提示）。
  const showLock = locked && !streaming;
  const lockHint = constraintInputPending
    ? '请先回答当前约束澄清，再发送新消息'
    : '等待审批决策后再继续';
  const lockedPlaceholder = constraintInputPending
    ? '等待约束澄清回答…'
    : '等待审批决策…';

  // ── #283 权限 pill：会话内可改 + 升档确认 ──
  // 禁用只剩两个**操作性**原因（都不是「档位不可变」）：等审批（后端闸门会 409，
  // ADR-0041 D5）与本轮进行中（档位下一轮才生效，本轮改了 UI 无从交代结果）。
  // `#236` 的「会话内一律只读」那一条已随本票删除。
  const permissionLocked = locked || streaming;
  const permissionDisabledHint = locked
    ? '等待审批决策后再改档'
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

  const submit = (mode: 'queue' | 'steer') => {
    const trimmed = value.trim();
    if (!trimmed || locked) return;
    // #825（MM-04）：只带**已就绪**的引用（在途/失败的草稿不参与本次发送——AC4：
    // 上传失败不阻塞纯文本发送）。文本仍必填：`POST /messages` 的 `content` 是必填项，
    // 空文本 + 纯图片不是本票支持的路由。
    const attachmentIds = attachments.readyIds;
    // steer 通道的**失败兜底不在这一层**（#219）：没有可打断的在途 run 时后端回 409
    // （session/service.py::SteerTargetNotFound），由交付层改投 queue 把消息送到
    // （见 useSession.sendFollowUp 的 steer 回退）。这里不能拿 `streaming` 当
    // 「服务端有在途 run」用——那正是 issue #196 的病灶（`streaming` 只表示
    // **本页有活流**，跨客户端时判反）。
    if (mode === 'steer' && onSteer) {
      onSteer(trimmed, rememberAsProceduralRule, attachmentIds);
    } else {
      onSubmit(trimmed, rememberAsProceduralRule, attachmentIds);
    }
    setValue('');
    setRememberAsProceduralRule(false);
    // 已进请求的那些草稿清掉。注意**清空发生在提交瞬间、失败不回填**（与文本同一
    // 语义：`setValue('')` 也不会因为 POST 失败而把文本还回来）——`attachmentIds`
    // 只含**已就绪**的引用，在途/失败的草稿没进请求，因此仍留在栏里，等用户重试
    // 或下一轮再带。
    attachments.clearSent(attachmentIds);
  };

  // §5.1 发送键语义（与上游一致）：Enter = queue（默认）；Ctrl/Cmd+Enter = steer。
  // IME composition 守卫（审查 P1）：中文输入法按 Enter 确认拼音时
  // `isComposing` 为真——此时不提交，否则半截拼音会被当成任务发出。
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit('steer');
      return;
    }
    // Enter（无修饰键）= queue。Shift+Enter 保留换行（真实用户习惯）。
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit('queue');
    }
  };

  const currentModel = effectiveModelEntry(selectedModel, models);
  // ── #825（MM-04）附图入口门禁（AC1/AC7）──
  // 三条互斥原因，**都不许静默**：会话缺失、模型非视觉、等待审批/澄清。原因同时是
  // 拖放遮罩上的文案与按钮 title，用户不必对着禁止光标猜。
  const visionUnsupported = currentModel?.supportsVision === false;
  const attachBlockedReason = locked
    ? lockHint
    : sessionId === null
      ? '图片需要先有会话：附图上传挂在会话上，请先新建或选中一个会话'
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
   *  [sessionId, commit, setIntakeError, startUpload]，同一会话内标识不变）：
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
                    onChange={(e) => setEditDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') {
                        e.preventDefault();
                        setEditingId(null);
                        return;
                      }
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        const trimmed = editDraft.trim();
                        if (!trimmed) return;
                        onEditItem?.(item, trimmed);
                        setEditingId(null);
                      }
                    }}
                  />
                  <span className="queue-item-actions">
                    <button
                      className="queue-item-btn"
                      onClick={() => {
                        const trimmed = editDraft.trim();
                        if (!trimmed) return;
                        onEditItem?.(item, trimmed);
                        setEditingId(null);
                      }}
                      aria-label="保存排队消息"
                      title="保存这一条"
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
                  disabled={!onEditItem}
                  aria-label="编辑排队消息"
                  title="编辑这条排队消息"
                >
                  <Pencil size={13} />
                </button>
                <button
                  className="queue-item-btn"
                  onClick={() => onSteerItem?.(item)}
                  disabled={!onSteerItem || constraintInputPending}
                  aria-label="立即发送"
                  title={constraintInputPending
                    ? '请先回答当前约束澄清'
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
              disabled={constraintInputPending}
              aria-label="立即发送全部"
              title={constraintInputPending
                ? '请先回答当前约束澄清'
                : '立刻投递待发送输入（POST /queue/flush）'}
            >
              <Play size={13} />
              立即发送全部
            </button>
          )}
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
        <textarea
          ref={inputRef}
          id="composer-input"
          name="task"
          className="composer"
          placeholder={showLock ? lockedPlaceholder : `描述一个任务…（${modKey()}+Enter 发送）`}
          value={value}
          onChange={(e) => setValue(e.target.value)}
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
        {hasControls && (
          <div className="composer-controls">
            <ModelPicker
              models={models}
              selectedModel={selectedModel}
              onModelChange={onModelChange ?? (() => {})}
              disabled={locked}
            />
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
            <OptionPicker
              ariaLabel="权限模式"
              title="工具调用如何批准？"
              options={toCatalogOptions(permissionModes, catalogIcon)}
              value={selectedPermissionMode}
              onChange={handlePermissionSelect}
              icon={Shield}
              placeholder="权限"
              // 禁用只剩操作性原因（审批待决 / 本轮进行中）——不再是「档位不可变」。
              disabled={permissionLocked}
              disabledHint={permissionDisabledHint}
              // 会话内没有「默认（未选）」这个目标：端点只接受三个具体档位，`null` 发过去
              // 必然 422 ⇒ 留着就是一个点下去必错的死胡同。
              showDefault={!permissionInSession}
              footer={renderPermissionFooter}
            />
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
                disabled={locked || !value.trim()}
                aria-label="发送"
                title={`发送（Enter 排队 · ${modKey()}+Enter 立即）`}
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
              disabled={locked || !value.trim()}
              aria-label="发送"
              title={`发送（Enter 排队 · ${modKey()}+Enter 立即）`}
            >
              <ArrowUp size={16} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
});
