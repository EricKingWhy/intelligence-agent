/** Conversation — center column rendering the projected ConversationState.
 *
 * Each Turn renders as (Trace Ladder, signature #2 — Brief §24 Phase 3):
 *   - user message (right-aligned bubble)
 *   - execution chain in TRUE event order: model segments ↔ ToolCards,
 *     projected by deriveChain (shared projection.ts layer — no second truth)
 *
 * Turn collapse: completed turns with model text collapse to a derived
 * summary line (N tools · M 轮 · 真实耗时) — replaces the old fabricated
 * "~N tok" estimate (zero-fake-metrics rule).
 */

import { memo, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { AlertTriangle } from 'lucide-react';
import { describeFailure, type FailureCopy } from '../hooks/useSession';
import type { ChainNode } from '../lib/projection';
import { deriveChain, emptyChildTurnIndex, latestEditableTurn } from '../lib/projection';
import type { TraceDensity } from '../lib/density';
import type { Disclosure } from '../lib/disclosure';
import { nextLevel, toolEventKey } from '../lib/disclosure';
import { OwletMark } from './OwletMark';
import {
  FOLLOW_BOTTOM, followOnJump, followOnScroll, nearBottom, nestedChainAbsorbs,
  wheelDeltaPixels, type FollowState,
} from '../lib/followLatest';
import { KIND_ICON, KIND_LABEL, modelKind, type RuntimeEventKind } from '../lib/eventKind';
import { GROUP_LABEL, groupChain, type ToolGroup, type ToolGroupStatus } from '../lib/toolGroups';
import { deriveTurnNav, turnAnchor, type TurnNavItem } from '../lib/turnNav';
import { useTickingNow } from '../hooks/useTickingNow';
import { Collapse } from './Collapse';
import type {
  ConversationState,
  AskUserAnswer,
  PendingAskUserRequest,
  PendingPlanApprovalRequest,
  PlanApprovalAnswer,
  ToolCall,
  Turn,
} from '../types';
import { formatDateTime, formatDuration, formatShortDuration, truncateForDisplay } from '../lib/format';
import { renderMarkdown } from '../lib/markdown';
import { caret } from './Caret';
import { kindGlyph, statusGlyph } from './Glyph';
import { ToolCard } from './ToolCard';
import { DelegationNode } from './DelegationNode';
import { ReasoningBlockView, type ReasoningDisclosureApi } from './ReasoningBlock';
import { PlanList } from './PlanList';
import { CopyButton } from './CopyButton';
import { ApprovalCard } from './ApprovalCard';
import { ApprovalEchoCard } from './ApprovalEchoCard';
import { ApprovalModal } from './ApprovalModal';
import { collectApprovalEchoes, notePendingApprovals } from '../lib/approvalEcho';
import { MemoryActivity, RecallNotice } from './MemoryActivity';
import { MessageImages } from './MessageImages';
import { AskUserCard } from './AskUserCard';
import { PlanApprovalCard } from './PlanApprovalCard';
import { ConversationSkeleton } from './ConversationSkeleton';

interface Props {
  conversation: ConversationState | null;
  loadingHistory: boolean;
  /** Trace Density 四档（Brief 冻结决策）——控制执行链节点粒度。 */
  density: TraceDensity;
  /** L0-L2 展开状态（manual override ?? density 默认）。缺省 = 无手动层。 */
  disclosure?: Disclosure;
  /** T2（#95）reasoning 开合状态（S6/S7 自动规则 + user_interacted override）。 */
  reasoningDisclosure?: ReasoningDisclosureApi;
  /** Inspector → 主区反向联动（PRD §9.2）：定位目标 key + 变更序号（nonce 保证
   *  重复跳同一目标也触发 effect）。 */
  jumpRequest?: { key: string; nonce: number } | null;
  /** 空状态示例任务回调——点击 chip 时由 App 注入 Composer。 */
  onPresetTask?: (text: string) => void;
  /** hover Inspect → 钻取到事件级 Inspector（PRD §9.1 联动）。 */
  onFocusTool?: (tool: ToolCall) => void;
  /** 打开子会话（Phase 13 委派节点入口，复用会话栏同一选择管线）。 */
  onOpenSession?: (sessionId: string) => void;
  /** Inspector 钻取子会话（v2 PRD §10.5 委派配对）——右栏原位展开 child。 */
  onInspectChild?: (child: { childSessionId: string; target: string }) => void;
  /** T7 #137：从指定用户消息 seq 分叉新会话。 */
  onFork?: (fromSeq: number) => void;
  /** ADR-0030（#195）§5.3：编辑最新一条用户消息（supersede 语义）。
   *  保存即发 POST /messages {supersedes_seq}；编辑态在 TurnView 原地。 */
  onEditTurn?: (fromSeq: number, newContent: string, attachmentIds: readonly string[]) => void;
  /** APR-01：审批卡提交时后端回 404（队列已 GC）→ 把该 approval_id 上报为失效。
   *  失效事实由 App 持有（同时驱动 composer 解锁与卡片只读），卡内不存第二份。 */
  goneApprovalIds?: ReadonlySet<string>;
  onApprovalGone?: (approvalId: string) => void;
  /** #420 AC2：审批决策 POST 成功 → App 对账一次（resync），保证决策后事件
   *  （resolved → … → run/completed）有一条消费路径。参数 = 会话 id。 */
  onApprovalDecided?: (sessionId: string) => void;
  /** #825（MM-04）AC7：当前所选模型的能力位（三态，见 `lib/api.ts::ModelCatalogEntry.supportsVision`）。
   *  `false` ⇒ 附图入口在 Composer 侧禁用并说明原因；**已附图的历史轮次**在该轮图旁标注
   *  「图已被省略」——因为此时后端投影确实把图换成了文本占位符（PRD D6 双保险的界面侧）。
   *  非 `false`（true / 缺失）一律不标注：把"后端没说"当成"不支持"是伪造。 */
  supportsVision?: boolean | null;
  /** #1204：`kind=ask_user` 的待回答请求——非 null 时在对话流尾部内嵌 `AskUserCard`。
   *  挂载判据在 App（暂停 run 与请求 id 对齐后给值），本组件只渲染。 */
  askUserRequest?: PendingAskUserRequest | null;
  /** 该提问的恢复是否在途（禁用提交，同 ConstraintResolutionDialog 的 resuming）。 */
  askUserResuming?: boolean;
  /** 恢复失败原文（App 的会话错误）；null = 无错误。 */
  askUserError?: string | null;
  /** 提交 `ask_user` 答案（App 侧走 resume 通道）。 */
  onAskUserAnswer?: (answer: AskUserAnswer) => void;
  /** #1356：`kind=plan_approval` 的待裁决计划——非 null 时在对话流尾部内嵌
   *  `PlanApprovalCard`。挂载判据与 `askUserRequest` 同宽（App 侧三方对齐后给值）。 */
  planApprovalRequest?: PendingPlanApprovalRequest | null;
  /** 该裁决的恢复是否在途（禁用三按钮）。 */
  planApprovalResuming?: boolean;
  /** 恢复失败原文（App 的会话错误）；null = 无错误。 */
  planApprovalError?: string | null;
  /** 提交计划裁决（App 侧走同一条 resume 通道）。 */
  onPlanApprovalAnswer?: (answer: PlanApprovalAnswer) => void;
  /** #1367（P0-3）：提交/续聊失败的**内联**呈现（只承载断网这一类）。
   *  null = 无。App 用 useMemo 保证身份稳定，不破坏本组件的 memo。 */
  submitFailure?: FailureCopy | null;
  /** 条内「重试」动作（由失败回执携带，绑死失败那次入参）。缺席 = 只呈现不给按钮。 */
  onRetrySubmit?: (() => void) | undefined;
  /** 重试在途：按钮进入禁用的忙态（同步闸在 App 侧，这里只做可见那一半）。 */
  retryPending?: boolean;
  /** `run/failed` 的内联条让位给已有的 plan-stuck 横幅（#1193，同一失败事实已
   *  带原因 + 退出 plan 动作）。 */
  suppressRunFailure?: boolean;
}

const EMPTY_TURNS: Turn[] = [];

const EXAMPLE_TASKS = [
  '写一个 FizzBuzz 脚本并运行验证',
  '创建 todo.md，写入三条今日计划',
  '列出当前目录的文件结构并总结',
];

/**
 * #1367（P0-3）：对话区内的**内联错误条**。
 *
 * 为什么长在这里而不是沿用顶部单槽横幅：`run/failed` 是**这一轮**的失败，
 * 它的事件与投影（`conversation.run_failure`）本来就落在对话上；把它抬到全局
 * 横幅会与"会话列表加载失败"之类无关错误抢同一个槽（useSession 的 error 是单槽，
 * 见其注释），且用户滚到会话中部时看不到原因。提交/续聊失败（断网）没有 run，
 * 由 App 经 `submitFailure` 传进来，同一套视觉。
 *
 * 视觉复用既有 `.approval-error` alert token（红底 8% + 2px 左实条 + 既有字号），
 * 只加单行布局与折叠详情（`.conversation-error*` 仅布局，不新增第三套 alert 样式）。
 * 默认收起 = 一行高；原始详情进 `<details>`，不默认铺满。
 */
export const ErrorBar = memo(function ErrorBar({
  copy,
  onRetry,
  retryPending = false,
}: {
  copy: FailureCopy;
  onRetry?: () => void;
  /** 重试在途：按钮禁用（防连点造重复会话，审查 P1-2）。 */
  retryPending?: boolean;
}) {
  return (
    <div className="approval-error conversation-error" role="alert">
      <AlertTriangle size={14} className="conversation-error-icon" aria-hidden="true" />
      <span className="conversation-error-title">{copy.title}</span>
      {copy.hint !== '' && <span className="conversation-error-hint">{copy.hint}</span>}
      {copy.detail !== '' && (
        <details className="conversation-error-detail">
          <summary>原始详情</summary>
          <pre>{copy.detail}</pre>
        </details>
      )}
      {copy.retryable && onRetry !== undefined && (
        <button
          type="button"
          className="conversation-error-retry"
          disabled={retryPending}
          aria-busy={retryPending}
          onClick={onRetry}
        >
          重试
        </button>
      )}
    </div>
  );
});

/**
 * F2（#272）：`memo` 包裹——挡住"与对话无关的父级提交"（打字 / hover / 拖宽 / 换焦点）。
 *
 * ⚠ **操作约束（改本组件 props 前必读）**：`memo` 的有效性完全取决于上游给的 prop 身份是否
 * 稳定。新增 prop 前必须在 `App.tsx` 侧确认它由 `useState` / `useCallback` / 原语持有；否则
 * `memo` 恒 miss。**不要**补自定义 `areEqual`：那是第二套（且更容易写错的）版本机制，
 * 一处分不清该比哪些字段就是静默吞更新。
 *
 * 逐项稳定性核对表、否决 `areEqual` 的理由、以及"该重渲染时必须重渲染"的守卫用例，
 * 见 `docs/adr/0037-projection-reference-stability-and-events-version.md` D5.3。
 */
export const Conversation = memo(function Conversation({ conversation, loadingHistory, density, disclosure, reasoningDisclosure, jumpRequest, onPresetTask, onFocusTool, onOpenSession, onInspectChild, onFork, onEditTurn, goneApprovalIds, onApprovalGone, onApprovalDecided, supportsVision = null, askUserRequest = null, askUserResuming = false, askUserError = null, onAskUserAnswer, planApprovalRequest = null, planApprovalResuming = false, planApprovalError = null, onPlanApprovalAnswer, submitFailure = null, onRetrySubmit, retryPending = false, suppressRunFailure = false }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  // Follow-mode（pi-mono TUI 语言）：贴底跟随流式增长；用户上滚即脱离跟随，
  // 出现「↓ 最新」浮标一键回归。纯视图状态，不碰投影（#22）。
  //
  // 三个状态收在 FOLLOW 原语里（lib/followLatest，ReasoningBlock / ToolCard 已
  // 在用同一份实现）：本组件此前另抄了一份内联判断（阈值硬编码 120、只在
  // effect 里现算 nearBottom），于是 follow 状态与 nearBottom 成了两套并行真相，
  // 且每个 delta 都重发 `scrollIntoView({behavior:'smooth'})` —— 动画被下一次
  // delta 反复重启，把用户的手动滚动一起吃掉（用户实测：「滑轮往下滚他就自动
  // 又上去了…必须要输出完才能看见」）。现在只有 followRef 决定要不要跟随。
  const followRef = useRef<FollowState>(FOLLOW_BOTTOM);
  const [suspended, setSuspended] = useState(false);
  /** F3（#276）挂起的「贴底」帧句柄：`null` = 本帧没有待执行的贴底。
   *  读 `scrollHeight` + 写 `scrollTop` 收进同一个 rAF 回调（见下方流式贴底 effect）。 */
  const snapFrameRef = useRef<number | null>(null);

  // PRD §20.2 / ADR-0014 D7：turns 列表窗口化（@tanstack/react-virtual）。
  // turn 是虚拟单元（user 消息 + 执行链，高度差异大）→ measureElement 动态测高；
  // overscan 6 保证滚动无白边；流式活跃 turn 高度连续变化由动态测量吸收。
  // 少 turn（<15）时与全渲染等价，统一路径避免双渲染逻辑。
  const turns = conversation?.turns ?? EMPTY_TURNS;
  const virtualizer = useVirtualizer({
    count: turns.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 240,
    overscan: 6,
    getItemKey: (i) => turns[i].step_id,
  });

  // B3：**能说「本轮之前没有历史」的那一轮**——纯函数在 projection.ts，见
  // emptyChildTurnIndex 的注释（只有第 0 轮可分叉时才是它，否则不给提示）。
  // turns 数组每个投影提交都会换引用，所以这趟扫描跟着重算（O(turns)）。
  const emptyChildTurnIdx = useMemo(() => emptyChildTurnIndex(turns), [turns]);

  // ADR-0030（#195）§5.3：最新一条用户消息可编辑（supersede 只允许最新一条，D8）。
  // 传给 TurnView 的 seq 用于动作行的启用/禁用判定。
  const latestEditableSeq = useMemo(
    () => latestEditableTurn(turns)?.user_message_seq ?? null,
    [turns],
  );

  /* #1429 打磨点 1：分组开合状态收在 Conversation 层（票面：key = 首成员
     `tool_call_id`）。虚拟化把滚出的行卸载，状态留在这里才不会「滚回就复位」。
     copy-on-write：切换一次换一次引用，TurnView 的 memo 随之重渲染（点击频率，
     不是每个 delta）。 */
  const [groupOpen, setGroupOpen] = useState<ReadonlyMap<string, boolean>>(() => new Map());
  const toggleGroup = useCallback((key: string) => {
    setGroupOpen((prev) => {
      const next = new Map(prev);
      next.set(key, !(prev.get(key) ?? true));
      return next;
    });
  }, []);

  /* #1429 打磨点 6：轮次导航 1..N + 压缩置灰（纯推断在 lib/turnNav）。 */
  const turnNavItems = useMemo(
    () => deriveTurnNav(turns, conversation?.compactions ?? []),
    [turns, conversation],
  );
  /* 轮次跳转：按 step_id 找虚拟化下标再 scrollToIndex——窗口外的行不在 DOM 里，
     不能靠 querySelector 定位。 */
  const jumpToTurn = useCallback(
    (stepId: number) => {
      const idx = turns.findIndex((t) => t.step_id === stepId);
      if (idx >= 0) virtualizer.scrollToIndex(idx, { align: 'start' });
    },
    [turns, virtualizer],
  );

  // PRD §9.2 反向联动：Inspector Timeline 点行 → 中间滚动定位 + 短促 pulse。
  // 目标可能是工具行（data-stream-key）或轮次容器（data-step-key）。
  // 虚拟化后目标行可能不在已挂载窗口内：先定位归属 turn，scrollToIndex 渲染
  // 出来后再 pulse（requestAnimationFrame 等一帧 DOM 提交）。
  // nonce 守卫：turns/virtualizer 在依赖里（窗口外兜底需要最新值），流式期间
  // 每 delta 都会变——已处理的 jumpRequest 不得重复触发 pulse。
  const processedJumpNonce = useRef<number | null>(null);
  useEffect(() => {
    if (!jumpRequest || processedJumpNonce.current === jumpRequest.nonce) return;
    processedJumpNonce.current = jumpRequest.nonce;
    const root = scrollRef.current;
    if (!root) return;
    const pulse = (el: HTMLElement) => {
      el.classList.remove('stream-jump-pulse');
      void el.offsetWidth; // 强制 reflow：同目标重复跳转也重启动画
      el.classList.add('stream-jump-pulse');
      window.setTimeout(() => el.classList.remove('stream-jump-pulse'), 900);
    };
    /* 三个 key 命名空间互不相交：`tool:`/`step:`（事件→轮次内定位）、`delegation:`、
     *  `approval:`（#184 审批卡——它在虚拟化列表**之外**，必须始终可见，所以有自己的
     *  data 属性）。一次查询按序试，命中即停。 */
    const key = jumpRequest.key;
    const approvalKey = key.startsWith('approval:') ? key.slice('approval:'.length) : null;
    const el =
      root.querySelector<HTMLElement>(`[data-stream-key="${key}"]`) ??
      root.querySelector<HTMLElement>(`[data-step-key="${key}"]`) ??
      (approvalKey === null
        ? null
        : root.querySelector<HTMLElement>(`[data-approval-key="${approvalKey}"]`));
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      pulse(el);
      return;
    }
    // 窗口外兜底：key → turn index → scrollToIndex
    const idx = turns.findIndex((t) => turnHasStreamKey(t, jumpRequest.key));
    if (idx === -1) return;
    virtualizer.scrollToIndex(idx, { align: 'center' });
    requestAnimationFrame(() => {
      const el2 =
        root.querySelector<HTMLElement>(`[data-stream-key="${jumpRequest.key}"]`) ??
        root.querySelector<HTMLElement>(`[data-step-key="${jumpRequest.key}"]`);
      if (el2) pulse(el2);
    });
  }, [jumpRequest, turns, virtualizer]);

  // 跟随态复位——两个「内容基线换了」的时刻必须回到贴底跟随：
  //  - 换会话（sessionId 变）：新会话的 follow 从底部起算（`.conversation-scroll`
  //    带 key，重挂载后 scrollTop 归 0；「打开就停在底部」不在这里做——那是
  //    历史加载完的落点问题，本 effect 只负责状态，不写 scrollTop）；
  //  - run 开始（false → true）：用户刚提交消息，要看的是新输出。
  //
  // 「run 开始」这一条是必须的，漏掉会**复现用户的原始症状**：在已结束的会话里
  // 上滚脱离（following=false、suspended=false，因为非流式期不浮现浮标），然后
  // 发一条新消息——新 run 继承 following=false，自动贴底静默，suspended 又是
  // false 所以「↓ 最新」也不出现 → 用户又一次「看不到下面」。
  //
  // 「run 结束」的悬浮态清理在这里是顺带的（复位本身覆盖了它），不是
  // useFollowResetOnStop 搬过来的——那个 hook 只服务 ReasoningBlock / ToolCard
  // 的内部滚动体。
  const sessionId = conversation?.session_id ?? null;
  const runActive = conversation?.run_status === 'running';
  // #1367：`run/failed` 的呈现层文案。只认投影事实（run_status + run_failure），
  // 不碰上报链路；取消的 run 投影里 run_failure 本来就是 null，这里再挡一道。
  const runFailureCopy = useMemo(() => {
    if (
      suppressRunFailure ||
      !conversation ||
      conversation.run_status !== 'failed' ||
      conversation.run_cancelled
    ) {
      return null;
    }
    const failure = conversation.run_failure;
    // #1367 审查 C-P2-3：run 失败的「连不上」是**后端→模型供应商**，不是浏览器断网
    // （这条事件已经送到浏览器了）。来源分开传，文案才不指错方向。
    return failure === null ? null : describeFailure(failure, { source: 'run' });
  }, [conversation, suppressRunFailure]);
  /* #1367：两种失败条**各呈现一次**（审查 P3：原先 submitFailure 会遮掉先前 run 的
     401/400 说明）。空态与非空态两个分支共用这份节点，避免同一段优先级逻辑写两遍。 */
  const failureBars = (
    <>
      {submitFailure !== null && (
        <ErrorBar
          copy={submitFailure}
          onRetry={onRetrySubmit}
          retryPending={retryPending}
        />
      )}
      {runFailureCopy !== null && <ErrorBar copy={runFailureCopy} />}
    </>
  );
  // run 结束的那一提交也会改高度：流式期间的纯文本要重渲染成 markdown、kind 行
  // 撤掉、复制按钮出现。而 runActive 已转 false，自动贴底 effect 不再跑，于是视口
  // 停在距底几十像素处；此时又不是流式（`suspended` 恒 false），连浮标都没有。
  // 所以「结束」这一拍要自己补一次瞬时贴底——但只在用户本来就在跟随时：上滚脱离
  // 的用户不能被他没要的滚动拽走。复位与补底都在同一个 effect 里，顺序是
  // 「先记下 wasFollowing → 复位 → 需要则补底」。
  const prevRunActiveRef = useRef(false);
  /* #420 AC3：回显锚定的 seen 集合宿主。{ sid, ids } 一起换，杜绝跨会话串卡。 */
  const approvalEchoSeenRef = useRef<{ sid: string; ids: Set<string> }>({ sid: '', ids: new Set<string>() });
  useEffect(() => {
    const wasActive = prevRunActiveRef.current;
    prevRunActiveRef.current = runActive;
    const wasFollowing = followRef.current.following;
    followRef.current = FOLLOW_BOTTOM;
    setSuspended(false);
    if (wasActive && !runActive && wasFollowing) {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }
  }, [sessionId, runActive]);

  // 流式期间的自动贴底——**只在 follow 为真时**，且用瞬时 `scrollTop = scrollHeight`
  // （不做 smooth 动画：动画中间态会被下一次 delta 重启，把用户的手动滚动一起吃掉）。
  //
  // F3（#276）：读 `scrollHeight` + 写 `scrollTop` **收进同一个 rAF 帧**，同一帧内多次
  // 登记只执行一次。本 effect 的依赖含整个 `conversation` ⇒ 每个 delta 提交都重跑一次，
  // 改造前是「每提交一次强制布局」（实测 1 提交 = 1 读 + 1 写）；收进单帧后每个 rAF 帧
  // 至多一次。⚠ 读写必须留在**同一个回调**里：分开会引入「读到的目标位置在写入前过期」
  // 的新竞态。鼠标释放/键盘等**用户发起**的贴底（run 结束补底、点「↓ 最新」）不在此列，
  // 仍走同步瞬时贴底——那些一拍对延迟敏感，不需要也不该等一帧。
  //
  // 依赖是有意保留整个 `conversation` 的（HANDOFF C.4.3 要求收窄，此处偏离并记录
  // 理由）：内容增长才是必须贴底的信号，而它既来自模型 delta、也来自工具输出，任何
  // 单一窄信号都接不住——`turns.length` 在纯文本 delta 时**根本不变**（漏触发、
  // 流式期间完全不贴底），`conversation.seq`/`events.length` 则每个事件都变（等频，
  // 收窄不减少运行次数）。原始症状的真正成因不是「跑太多次」，而是**没有 follow
  // 闸门 + smooth 动画**——这两个都已在别处解决，所以这条依赖不再是缺陷。
  //
  // 「用户上滚 → follow 置 false」这一步交给 scroll 监听 + wheel 监听。**只有
  // scroll 监听是不够的**（实测踩到）：scroll 事件要等下一个渲染时机才派发，而
  // delta 提交可能先到——本 effect 抢先贴底把位置拽回底部，等 scroll 事件终于派发时
  // nearBottom 已经是 true，用户的脱离意图被整个吞掉：位置每帧被拽回、浮标永不出现，
  // 即用户报的那个症状。所以脱离跟随还需一个**同步于用户意图**的信号，见 setScrollNode
  // 里的 wheel 监听。
  useEffect(() => {
    if (!runActive) return;
    if (!followRef.current.following) return;
    // 同帧去重：已有待执行的贴底帧就不再排（一帧至多一次强制布局）。
    if (snapFrameRef.current !== null) return;
    snapFrameRef.current = requestAnimationFrame(() => {
      snapFrameRef.current = null;
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }, [conversation, runActive]);

  // F3（#276）：卸载时取消挂起的贴底帧——不给已经卸载的节点写属性。
  // （`scrollRef` 在卸载时会被回调 ref 置 null，这里多一道取消是为了连回调都不再执行。）
  useEffect(
    () => () => {
      if (snapFrameRef.current !== null) {
        cancelAnimationFrame(snapFrameRef.current);
        snapFrameRef.current = null;
      }
    },
    [],
  );

  // Follow-mode：用户滚动即转移跟随态（贴底恢复跟随；上滚脱离并浮现「↓ 最新」）。
  // 流式增长本身不触发 scroll 事件，而我们写入 scrollTop 会触发——nearBottom
  // 判定天然收敛（写入后仍贴底 → followOnScroll 返回 FOLLOW_BOTTOM，状态不变，
  // 不产生额外渲染、不闪烁）。
  //
  // 监听器绑在**节点**上（回调 ref），不绑在 effect 依赖上。effect + scrollRef
  // 有个致命缺口：本次渲染返回加载态/空态时树上没有滚动容器，effect 拿到 null
  // 直接 return，而依赖（runActive/sessionId）不变就不会重跑——监听器再也补不上，
  // 于是 following 永远为真：用户上滚既不脱离跟随（视口被下一次 delta 拽回底部），
  // 也不浮现「↓ 最新」。实测复现路径：点开一个会话（加载态与 sessionId 同一次提交）
  // → 续聊 → 上滚，浮标始终不出现。
  // 依赖 `runActive`：回调 ref 的 identity 一变，React 会先以 null 调旧回调（摘监听）
  // 再以节点调新回调（挂监听），闭包里的 streaming 因此永远是最新值，不需要额外
  // 的 ref 镜像（少一个「提交后才同步」的滞后窗口）。
  const detachScrollListenerRef = useRef<(() => void) | null>(null);
  const setScrollNode = useCallback((el: HTMLDivElement | null) => {
    detachScrollListenerRef.current?.();
    detachScrollListenerRef.current = null;
    scrollRef.current = el;
    if (!el) return;
    const onScroll = () => {
      const near = nearBottom(el.scrollHeight, el.scrollTop, el.clientHeight);
      followRef.current = followOnScroll(followRef.current, near, runActive);
      setSuspended(followRef.current.suspended);
    };
    // 滚轮向上 = 「我要看上面的内容」的**用户意图**，必须在输入任务里同步处理：
    // 位置法（onScroll）有个躲不掉的竞态——scroll 事件要等下一个渲染时机才派发，
    // 而 delta 提交可能先到，自动贴底会抢先执行把位置拽回底部；等 scroll 事件终于
    // 派发时 nearBottom 已经是 true，脱离意图被整个吞掉（实测：上滚后每 300ms
    // 被拽回底部 ~2500px，浮标永不出现）。wheel 同步于用户操作，抢占不到。
    //
    // 只在**向上滚**时脱离：向下滚可能只是想滚到底，若也脱离，滚到底后位置没变、
    // 不会再有 scroll 事件来恢复跟随，跟随就再也回不来了。回到贴底由 onScroll 的
    // nearBottom 负责。
    //
    // 这个监听**不能被删**（哪怕 e2e 看起来不依赖它）：它唯一的存在理由是抢在下一次
    // delta 提交之前同步脱离。一旦上滚真的越过了阈值，onScroll 单独也能脱离——所以
    // 任何确定性测试都无法把它与「只有 scroll 监听」区分开（要复现竞态必须在 wheel
    // 之后、scroll 事件派发之前插入一次提交）。证据是真机埋点实测，见
    // docs/FRONTEND_ISSUES_LOG.md 第 19 项与 e2e/j-scroll.spec.ts 文件头。
    //
    // 触屏不做等价处理：一次触摸手势会持续产生成串 scroll 事件，位置法在其中大多数
    // 帧都能生效（只有手势第一帧可能被抢跑）；而「鼠标滚轮一次一格」是一次性的离散
    // 事件，没有后续事件来纠正，所以必须在这里接住。
    //
    // wheel 会**冒泡**：工具输出面板（`.tool-out-body`）/ reasoning 展开体本身就是
    // 独立滚动容器，在它们里面上滚也会冒到这里。那个手势的意图是看那段输出，不是
    // 离开对话底部——不排除就会在对话根本没动的情况下脱离跟随、弹出「↓ 最新」。
    //
    // 判据是**整条嵌套链能不能吃下这次上滚**。累加语义与 deltaMode 折算都在
    // lib/followLatest.ts 的纯函数里（`nestedChainAbsorbs` / `wheelDeltaPixels`，
    // 有单测）；这里只负责按 DOM 把链上可滚祖辈的余量收出来。
    // 用 `Element` 而不是 `HTMLElement`：wheel 目标可能是 lucide 图标之类的
    // `SVGElement`（非 HTMLElement），按 HTMLElement 判会在图标上短路成 false，
    // 正好漏掉要挡的那一幕。
    const nestedAbsorbsWheel = (target: EventTarget | null, wheel: WheelEvent): boolean => {
      const need = wheelDeltaPixels(wheel.deltaY, wheel.deltaMode, el.clientHeight);
      const headrooms: number[] = [];
      let node: Element | null = target instanceof Element ? target : null;
      while (node && node !== el) {
        if (node.scrollHeight > node.clientHeight) {
          const overflowY = getComputedStyle(node).overflowY;
          if (overflowY === 'auto' || overflowY === 'scroll') headrooms.push(node.scrollTop);
        }
        node = node.parentElement;
      }
      return nestedChainAbsorbs(headrooms, need);
    };
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY >= 0) return;
      // 容器自己已经在顶上（内容没超过视口，或上滚到头）：这一下上滚什么都不会动，
      // 不是「用户要离开底部」。判据缺失会让浮标为一个没发生的滚动弹出来。
      if (el.scrollTop <= 0) return;
      if (nestedAbsorbsWheel(e.target, e)) return;
      followRef.current = followOnScroll(followRef.current, false, runActive);
      setSuspended(followRef.current.suspended);
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    el.addEventListener('wheel', onWheel, { passive: true });
    detachScrollListenerRef.current = () => {
      el.removeEventListener('scroll', onScroll);
      el.removeEventListener('wheel', onWheel);
    };
  }, [runActive]);

  if (loadingHistory) {
    return <ConversationSkeleton />;
  }

  if (!conversation || conversation.turns.length === 0) {
    return (
      <div className="conversation">
        {failureBars}
        <div className="conversation-empty">
          <div className="empty-state">
            <div className="empty-logo"><OwletMark /></div>
            <div className="empty-hero">暂无对话</div>
            <div className="empty-sub">在下方提交任务，实时观看 Agent 执行。</div>
            {/* #1401：主 CTA 已删除——它实际只是把焦点交回下方 composer，
                同一屏不再放第二个主入口；示例 chip 保留，但降为低对比文字按钮。 */}
            {onPresetTask && (
              <div className="empty-examples">
                {EXAMPLE_TASKS.map((task) => (
                  <button key={task} className="example-chip" onClick={() => onPresetTask(task)}>
                    {task}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // #420 AC3：审批结果回显的 seen 集合（锚定规则见 lib/approvalEcho——只回显本次
  // 观看期间见过挂起的审批；换会话即重置，历史决策不平铺）。ref 声明在 hooks 区
  // （无条件），这里是非 hook 的派生：登记当前待决 → 收集已决回显。
  if (approvalEchoSeenRef.current.sid !== conversation.session_id) {
    approvalEchoSeenRef.current = { sid: conversation.session_id, ids: new Set<string>() };
  }
  notePendingApprovals(approvalEchoSeenRef.current.ids, conversation);
  const approvalEchoes = collectApprovalEchoes(approvalEchoSeenRef.current.ids, conversation);

  return (
    <div className="conversation">
      {/* #381（W-27）：进度清单钉在滚动区上方、虚拟化行之外——虚拟器只量 turn 行，
          外挂块不参与 measureElement（PRD 长任务 §7.5；无清单会话不渲染）。 */}
      {conversation.plan !== null && conversation.plan.length > 0 && (
        <PlanList
          key={`plan-${conversation.session_id}`}
          items={conversation.plan}
          runStatus={conversation.run_status}
          runInterrupted={conversation.run_interrupted !== null}
        />
      )}
      {/* #1429 打磨点 6：轮次导航钉在滚动区上方（虚拟化行之外，不参与测高）。 */}
      {turnNavItems.length > 1 && <TurnNav items={turnNavItems} onJump={jumpToTurn} />}
      {/* key 换 session 时整组 turn remount，触发 fade-in = 切换 crossfade 感 */}
      <div className="conversation-scroll" key={conversation.session_id} ref={setScrollNode}>
        {/* 虚拟化：绝对定位行 + 动态测高（PRD §20.2）。只有窗口内 turn 参与 DOM。
            `flexShrink: 0` 是 #1414 的修复，不是装饰：本容器是 `.conversation-scroll`
            （flex column）的 flex item，默认 `flex-shrink: 1`；它的子行全部 absolute、
            不贡献 min-content 高度，于是内容超出视口时浏览器把它从 totalSize 压到
            「视口剩余高度」，而尾部卡片（失效审批卡 / ask_user / 错误条）按**压缩后**
            的底边定位，绝对定位行再按原坐标绘制上去 → 两团文字重叠。锁定不收缩后，
            尾部卡片永远排在 totalSize 之后。 */}
        {/* 内容列（V2 `.chat-in` 800px）：虚拟化行与尾部卡片共用同一列宽。 */}
        <div className="conversation-inner">
          <div style={{ height: virtualizer.getTotalSize(), position: 'relative', flexShrink: 0 }}>
            {virtualizer.getVirtualItems().map((vi) => (
              <div
                key={vi.key}
                data-index={vi.index}
                ref={virtualizer.measureElement}
                style={{
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  width: '100%',
                  transform: `translateY(${vi.start}px)`,
                }}
              >
                <TurnView
                  turn={turns[vi.index]}
                  turnIndex={turns[vi.index].turn_index}
                  model={conversation.model}
                  density={density}
                  disclosure={disclosure}
                  reasoningDisclosure={reasoningDisclosure}
                  onFocusTool={onFocusTool}
                  onOpenSession={onOpenSession}
                  onInspectChild={onInspectChild}
                  onFork={onFork}
                  isFirstUserTurn={vi.index === emptyChildTurnIdx}
                  sessionId={conversation.session_id}
                  latestEditableSeq={latestEditableSeq}
                  onEditTurn={onEditTurn}
                  isSupersededTurn={turns[vi.index].superseded === true}
                  supportsVision={supportsVision}
                  groupOpen={groupOpen}
                  onToggleGroup={toggleGroup}
                  isHit={jumpRequest != null && turnHasStreamKey(turns[vi.index], jumpRequest.key)}
                />
              </div>
            ))}
          </div>
        </div>
        <div className="conversation-inner conversation-tail">
          <MemoryActivity key={conversation.session_id} conversation={conversation} />
          {/* #1367（P0-3）：内联错误条——run 失败（含 401/400/5xx）与提交断网
              共用这一处。虚拟化行之外（同 MemoryActivity），不参与测高。 */}
          {failureBars}
          {/* #1204：`ask_user` 通用提问的内嵌卡片——长在对话流尾部（虚拟化行之外，
              同 MemoryActivity / 内联审批卡的做法），不参与 measureElement 测高。
              `key` 按 request_id：换请求即重挂，卡片本地草稿不串题。 */}
          {/* #1356：计划审批卡——与 `ask_user` 同一条恢复通道的第三支载荷。两者都由
              「本 run 至多一个待处理请求」的准入去重，不会同时出现；顺序仍按"先计划、
              后提问"排（若将来放宽去重，计划卡更靠近暂停点）。 */}
          {planApprovalRequest !== null && onPlanApprovalAnswer && (
            <PlanApprovalCard
              key={planApprovalRequest.request_id}
              request={planApprovalRequest}
              resuming={planApprovalResuming}
              errorMessage={planApprovalError}
              onSubmit={onPlanApprovalAnswer}
            />
          )}
          {askUserRequest !== null && onAskUserAnswer && (
            <AskUserCard
              key={askUserRequest.request_id}
              request={askUserRequest}
              resuming={askUserResuming}
              errorMessage={askUserError}
              /* #1284：分钟数由投影从请求快照归一（后端落 `timeout_minutes`，旧事件由
                 `timeout_seconds` 换算）；本层只渲染，不再自算，避免与后端消息分叉。
                 缺省（旧事件无快照）⇒ null ⇒ 卡片回落不带数字的中性提示。 */
              timeoutMinutes={askUserRequest.timeout_minutes ?? null}
              onSubmit={onAskUserAnswer}
            />
          )}
          {/* #37 交互式审批 + #421 模态承载。
           *
           *  R5-B3 的根因是审批卡与虚拟化行抢同一块画布：行是 absolute 定位的绘制层，
           *  测高滞后就盖到卡片的按钮上。修法 = **第一张非失效**待决审批进模态
           *  （ApprovalModal，Portal + Overlay，指针/焦点都被 Radix 圈住，且没有
           *  决策不允许关——GitHub required-review 语义）；其余 pending 卡（失效的
           *  stale/404 孤儿、多卡并存的第 2+ 张）留在内联位置。
           *
           *  失效卡**绝不**进模态：它只剩禁用按钮和只读说明，模态化等于把用户锁死
           *  在一个关不掉的面板里（APR-01 的关闭路径必须保留）。
           *
           *  模态候选身上不挂 `data-approval-key`：Inspector PERMISSION 段的跳转
           *  （#184）在 conversation 根里 querySelector，而模态内容在 body 末尾的
           *  portal 里，跳不过去——但模态本来就常驻可见，跳转目标已在眼前，no-op
           *  是可接受的降级（内联卡的跳转不受影响）。 */}
          {(() => {
            const isInvalid = (a: (typeof conversation.pending_approvals)[number]) =>
              a.stale === true || goneApprovalIds?.has(a.approval_id) === true;
            const firstLiveIdx = conversation.pending_approvals.findIndex((a) => !isInvalid(a));
            const firstLive = firstLiveIdx >= 0 ? conversation.pending_approvals[firstLiveIdx] : null;
            return (
              <>
                {conversation.pending_approvals.map((a, i) =>
                  i === firstLiveIdx ? null : (
                    /* `data-approval-key` 是 Inspector PERMISSION 段（#184）反向联动的落点：
                       点那一行的"待审批"→ jumpRequest key `approval:<id>` → 滚到这里 + pulse。 */
                    <div key={a.approval_id} data-approval-key={a.approval_id}>
                      <ApprovalCard
                        sessionId={conversation.session_id}
                        approval={a}
                        /* 失效 = 投影判定（run 已终结）∪ 后端实证（该卡提交过且回了 404） */
                        invalid={isInvalid(a)}
                        onGone={onApprovalGone ? () => onApprovalGone(a.approval_id) : undefined}
                        onDecided={
                          onApprovalDecided
                            ? () => onApprovalDecided(conversation.session_id)
                            : undefined
                        }
                        /* UI-01 键盘安全（U-1 P1）：全局快捷键只挂在唯一 autoFocus 卡上。
                            模态候选存在时它是唯一 autoFocus 卡，内联卡一律 false。 */
                        autoFocus={false}
                      />
                    </div>
                  ),
                )}
                {firstLive && (
                  <ApprovalModal
                    sessionId={conversation.session_id}
                    approval={firstLive}
                    onGone={onApprovalGone ? () => onApprovalGone(firstLive.approval_id) : undefined}
                    onDecided={
                      onApprovalDecided
                        ? () => onApprovalDecided(conversation.session_id)
                        : undefined
                    }
                  />
                )}
                {/* #420 AC3：已决回显——本观看窗内见过挂起的审批，决出后在这里留
                    结果卡（消费投影 `approval_decisions`，不本地伪造）。模态收起后
                    结果落点就在这条队列里，与内联待决卡同一位置。 */}
                {approvalEchoes.map((d) => (
                  /* `data-approval-key` 沿用：决出后 Inspector PERMISSION 段的跳转
                      落点仍在原地（不再跳到已消失的待决卡）。 */
                  <div key={`echo-${d.approval_id}`} data-approval-key={d.approval_id}>
                    <ApprovalEchoCard decision={d} />
                  </div>
                ))}
              </>
            );
          })()}
        </div>
      </div>
      {/* Follow-mode 浮标（pi-mono "jump to latest"）：在**投影上仍是 running 的
          run** 里、用户已上滚时出现。`suspended` 由 FOLLOW 原语给出（= 已脱离跟随），
          但它只在 `streaming=true` 时被置起、却**不会**被非流式的转移清掉：
          `followOnScroll(prev, near=false, streaming=false)` 保持 `prev.suspended`
          （原语语义：非流式期没有「最新」可跳，所以不新置、也不撤销）。于是流结束后
          到达的滚动事件（settle 提交改高度、虚拟化重测高都会产生）会把粘性的
          suspended 一直带下去——只看 suspended 就会在终态留下一个点不动的浮标
          （流已结束，"最新"就是用户眼前这屏），所以渲染条件必须同时看 `runActive`。
          注意 `runActive` 读的是投影的 run_status（不变量 #22），崩溃会话可能停在
          running 且无终态，此时浮标仍会出现——这是有意的：用户可以借此回到最新内容。 */}
      {runActive && suspended && (
        <button
          className="follow-pill"
          onClick={() => {
            followRef.current = followOnJump();
            setSuspended(false);
            const el = scrollRef.current;
            if (el) el.scrollTop = el.scrollHeight;
          }}
          aria-label="滚动到最新内容"
        >
          ↓ 最新
        </button>
      )}
    </div>
  );
});

// memo + 投影层 copy-on-write（未触及 turn 引用稳定）：流式期间每个 delta 只
// 重渲染活跃轮次——已完成轮次不再重跑 deriveChain 与全量 markdown 重解析。
// 引用稳定性由 F1（#270）保证：disclosure / reasoningDisclosure 的返回值现在只在
// **自身依赖变化**时换引用（override 改动 / density 换档），流式提交期间恒等——memo 的
// 浅比较才真的会命中原注释所声明的效果。注意它**不能**做成「身份永不改变」：那样
// override 变化时 memo 也会 bail out，点击工具行的档位循环会静默无效（见
// lib/disclosure.ts 顶部注释与 Conversation.render.test.tsx 的 AC8 代理用例）。
export const TurnView = memo(function TurnView({ turn, turnIndex, model, density, disclosure, reasoningDisclosure, onFocusTool, onOpenSession, onInspectChild, onFork, isFirstUserTurn, sessionId, latestEditableSeq, onEditTurn, isSupersededTurn, supportsVision = null, groupOpen, onToggleGroup, isHit = false }: { turn: Turn; turnIndex?: number | null; model: string | null; density: TraceDensity; disclosure?: Disclosure; reasoningDisclosure?: ReasoningDisclosureApi; onFocusTool?: (tool: ToolCall) => void; onOpenSession?: (sessionId: string) => void; onInspectChild?: (child: { childSessionId: string; target: string }) => void; onFork?: (fromSeq: number) => void; isFirstUserTurn?: boolean; sessionId?: string; latestEditableSeq?: number | null; onEditTurn?: (fromSeq: number, newContent: string, attachmentIds: readonly string[]) => void; isSupersededTurn?: boolean; supportsVision?: boolean | null; /** 分组开合状态（票面：Conversation 层持有）。缺省 = 组件内兜底（独立渲染/测试）。 */ groupOpen?: ReadonlyMap<string, boolean>; onToggleGroup?: (key: string) => void; isHit?: boolean }) {
  // F1（#270）必做 2：`cycle` 回调此前在链路渲染器里**每次渲染现建一个新闭包**，
  // 作为 prop 传给 memo(ToolCard) ⇒ 浅比较恒不等，memo 恒 miss。移到组件里用
  // useCallback 建立一次（依赖 disclosure——它只在 override / density 变化时换引用，
  // 流式提交期间恒等），引用就稳定了；注册表本身不取 hook，仍按 ChainRenderCtx 的
  // 既有约定只接收透传值。
  const cycleLevel = useCallback(
    (key: string, density: TraceDensity) => {
      if (!disclosure) return;
      disclosure.setLevel(key, nextLevel(disclosure.levelFor(key, density)));
    },
    [disclosure],
  );

  // 折叠是纯手动选项（用户指令 2026-09-05，覆盖冻结决策 L48 的"默认折叠"）：
  // 完成轮一律默认展开——先让用户看到模型回答，想收起再手动点。live 与
  // 历史重挂载行为一致；流式中/无模型文本的轮次不出现折叠按钮。
  const collapsible = turn.status !== 'streaming' && turn.model.text.length > 0;
  const [collapsed, setCollapsed] = useState(false);
  /* #1429 打磨点 6：`#` 锚点只复制 `#turn-N` 文本（不做 hash 深链——不承诺
     Timeline 行级定位，也不改写 history）。 */
  const [anchorCopied, setAnchorCopied] = useState(false);
  const anchorTimer = useRef<number | null>(null);
  useEffect(() => () => {
    if (anchorTimer.current !== null) window.clearTimeout(anchorTimer.current);
  }, []);

  const duration = formatDuration(turn.started_at, turn.completed_at);
  const chain = useMemo(() => deriveChain(turn), [turn]);
  /* #1429 打磨点 1：渲染层分组。键是 `turn` 引用（与 deriveChain 同键）——
     流式 delta 不会让已完成 turn 重新分组（票面性能红线）。 */
  const entries = useMemo(() => groupChain(chain), [chain]);
  /* 分组开合的兜底宿主（与 ReasoningBlockView 的 fallbackOpen 同一模式）：
     生产路径由 Conversation 持有（虚拟化滚出/滚回不丢），独立渲染时退化为本地
     Map，组头仍是真按钮。 */
  const [localGroupOpen, setLocalGroupOpen] = useState<ReadonlyMap<string, boolean>>(() => new Map());
  const groupState = groupOpen ?? localGroupOpen;
  const toggleGroup = useCallback(
    (key: string) => {
      if (onToggleGroup) {
        onToggleGroup(key);
        return;
      }
      setLocalGroupOpen((prev) => {
        const next = new Map(prev);
        next.set(key, !(prev.get(key) ?? true));
        return next;
      });
    },
    [onToggleGroup],
  );
  // D12/PRD §10：chain 中最后一个 model 节点 = Final Answer（高对比正文），
  // 其余 done model 段是中间输出（低对比 + 语义行）。
  const lastModelIndex = useMemo(() => {
    for (let i = chain.length - 1; i >= 0; i--) {
      if (chain[i].kind === 'model') return i;
    }
    return -1;
  }, [chain]);
  // hover 时间戳（调研：完成后才展示，流式期间不打扰；title 属性最轻实现）
  const completedTitle = turn.completed_at
    ? `完成于 ${formatDateTime(turn.completed_at) ?? turn.completed_at}`
    : undefined;

  // ADR-0030 §5.3 编辑态：原地输入（该条消息变成 textarea + 保存/取消），不弹模态。
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState('');
  // D8：编辑只在**最新一条**用户消息上可用；其余置灰 + title 说明（禁用而不是
  // 隐藏：让用户知道能力存在）。被取代的轮不在 latestEditableTurn 里（自然禁用）。
  // 终审 P2 修复：streaming 中的最新消息**不可编辑**——与两行下的 fork 按钮同一
  // 守卫（ADR-0030 §5.3 的意图是编辑**已完成**的轮；流式中编辑发 queue-time
  // supersede 会命中在途轮的 409 或静默排队，意图不成立）。
  const editable =
    onEditTurn != null &&
    turn.status !== 'streaming' &&
    turn.user_message_seq !== null &&
    !turn.injected_by &&
    turn.user_message_seq === latestEditableSeq;
  /** #825（MM-04）：该轮的图片引用（缺省 = 旧版后端 / 纯文本轮）。 */
  const userImages = turn.user_attachments ?? [];

  return (
    <div className={`turn turn-${turn.status}`} data-step-key={`step:${turn.step_id}`}>
      {/* ADR-0030 §4.5.1：被取代的轮**整段从视图移除**（用户裁定：旧回答段直接
          删掉不显示，不加"已改写"标记）。由 `message/superseded` 驱动（投影层
          applySupersedeShadow 置位），不靠前端猜；事件照旧在 events 日志。
          终审 P2 修复：轮次标签**在门内**——标签在门外的话，被取代的整段只剩一个
          孤儿「第 N 轮」标签（问与答都不渲染）。 */}
      {isSupersededTurn ? null : (
        <>
      {/* T9 #139 + #1429 打磨点 6：轮次标记——一行小字 + 一条细线。
          `turn_index` 为 per-turn 事实（run/started 携带）；仅正整数显示：
          null=旧版后端缺字段，≤0=防御性不渲染。`.turn-index-label` 类名保留
          （渲染契约测试依赖它），视觉并入 `.turn-mark` 行。 */}
      {turnIndex != null && turnIndex > 0 && (
        <div className={`turn-mark${isHit ? ' is-hit' : ''}`} data-turn={turnIndex}>
          <span className="turn-index-label tm-n">{String(turnIndex).padStart(2, '0')}</span>
          <span className="tm-l">第 {turnIndex} 轮</span>
          {(duration || turn.tools.length > 0) && (
            <span className="tm-d">
              {[duration, turn.tools.length > 0 ? `${turn.tools.length} 次调用` : null]
                .filter(Boolean)
                .join(' · ')}
            </span>
          )}
          <span className="tm-line" aria-hidden="true" />
          <button
            type="button"
            className={`tm-anchor${anchorCopied ? ' is-done' : ''}`}
            title={anchorCopied ? '已复制轮次锚点' : '复制轮次锚点'}
            aria-label={anchorCopied ? '已复制轮次锚点' : '复制轮次锚点'}
            onClick={() => {
              const text = turnAnchor(turnIndex);
              const done = () => {
                setAnchorCopied(true);
                if (anchorTimer.current !== null) window.clearTimeout(anchorTimer.current);
                anchorTimer.current = window.setTimeout(() => setAnchorCopied(false), 1600);
              };
              // 非安全上下文（http 非 localhost）没有 clipboard API：退回 execCommand。
              if (navigator.clipboard?.writeText) {
                // 复制失败不能亮「已复制」——那是伪造反馈（P3）。
                navigator.clipboard.writeText(text).then(done, () => {});
              } else {
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                // execCommand 返回 false = 浏览器拒绝复制（只读文档 / 权限策略），
                // 此时亮「已复制」同样是伪造反馈——与上面 clipboard 失败分支同一条规矩。
                const ok = document.execCommand('copy');
                ta.remove();
                if (ok) done();
              }
            }}
          >
            {anchorCopied ? '已复制' : '#'}
          </button>
        </div>
      )}
      {/* User message — minimal, right-aligned；harness 注入的纠正消息
          （failure-guard soft）渲染为系统提示条而非用户气泡（不是真人说的话） */}
      {(turn.user_message || userImages.length > 0) &&
        (turn.injected_by ? (
          <div className="msg msg-system">
            <div
              className="system-notice"
              title={`注入来源：${turn.injected_by}`}
            >
              <span className="system-notice-badge">系统注入</span>
              <span className="system-notice-text">{turn.user_message}</span>
            </div>
          </div>
        ) : editing ? (
          <div className="msg msg-user msg-editing">
            <textarea
              className="msg-edit-input"
              value={editValue}
              onChange={(e) => setEditValue(e.target.value)}
              onKeyDown={(e) => {
                // IME composition 守卫（审查 P1，同 Composer）：确认拼音的
                // Enter（含误带 Ctrl）不得触发保存。
                if (e.nativeEvent.isComposing || e.keyCode === 229) return;
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  const trimmed = editValue.trim();
                  if (!trimmed || turn.user_message_seq === null) return;
                  onEditTurn?.(
                    turn.user_message_seq,
                    trimmed,
                    userImages.map((image) => image.attachment_id),
                  );
                  setEditing(false);
                }
                if (e.key === 'Escape') {
                  e.preventDefault();
                  setEditing(false);
                }
              }}
              rows={2}
              autoFocus
              aria-label="编辑消息"
            />
            <div className="msg-edit-actions">
              <button
                className="msg-edit-btn"
                onClick={() => {
                  const trimmed = editValue.trim();
                  if (!trimmed || turn.user_message_seq === null) return;
                  onEditTurn?.(
                    turn.user_message_seq,
                    trimmed,
                    userImages.map((image) => image.attachment_id),
                  );
                  setEditing(false);
                }}
                aria-label="保存修改"
              >
                保存
              </button>
              <button
                className="msg-edit-btn"
                onClick={() => setEditing(false)}
                aria-label="取消编辑"
              >
                取消
              </button>
            </div>
          </div>
        ) : (
          <div className="msg msg-user">
            {/* #825（MM-04）AC5/AC6/AC7：附图在气泡**上方**（先图后文，与主流聊天一致）。
                数据是事件的引用（`Turn.user_attachments`），字节走受控端点——刷新/重放后
                仍在；`omitted` = 当前模型不支持视觉时的「图已被省略」标注（AC7）。 */}
            <MessageImages
              sessionId={sessionId ?? null}
              images={userImages}
              omitted={supportsVision === false}
            />
            {turn.user_message && <div className="msg-bubble-user">{turn.user_message}</div>}
            {/* §5.3 动作行：复制 / 编辑 / 分叉（对齐 Codex 的三图标）。
                编辑在最新一条用户消息上可用；其余禁用（置灰 + title 说明），
                因为 supersede 只允许最新一条（D8）。 */}
            <span className="msg-actions">
              {turn.user_message && <CopyButton text={turn.user_message} label="复制消息" />}
              <button
                className="msg-action-btn"
                onClick={() => {
                  setEditValue(turn.user_message);
                  setEditing(true);
                }}
                disabled={!editable}
                title={
                  editable
                    ? '编辑这条消息（旧的一轮会被新内容取代）'
                    : '只有最新一条消息可以编辑'
                }
                aria-label={editable ? '编辑消息' : '编辑消息（仅最新一条可用）'}
              >
                编辑
              </button>
              {onFork && turn.status !== 'streaming' && turn.user_message_seq !== null && (
                <button
                  className="fork-btn"
                  title={
                    isFirstUserTurn
                      ? '这之前没有历史：新会话会是空的'
                      : '从这里另开一条新会话'
                  }
                  onClick={() => onFork(turn.user_message_seq!)}
                >
                  从这里另开一条新会话
                </button>
              )}
            </span>
          </div>
        ))}
        </>
      )}

      {/* Phase 12 白盒透明：failure-guard 事件条——soft 提示 / hard 终止标记 */}
      {turn.notices?.map((n, i) => (
        <div key={`${n.level}-${n.tool_name}-${i}`} className={`notice-strip notice-strip-${n.level}`}>
          <span className="notice-strip-badge">{n.level === 'hard' ? '终止' : '熔断'}</span>
          <span className="notice-strip-text">
            工具 {n.tool_name || '?'} 连续失败 {n.consecutive_failures} 次
          </span>
        </div>
      ))}

      {/* Execution chain — model segments and tools in true event order.
          ADR-0030 §4.5.1：被取代轮的**回答段**同样不渲染（用户裁定是
          "问与答整段删除"，与后端投影整轮 shadow 同构——Spec 审查 P1：
          只删问句块会留下孤儿回答，前后端视图不一致）。 */}
      {!isSupersededTurn && turn.activities.length > 0 && (
        <div className="msg msg-model" title={completedTitle}>
          <div className="msg-body">
            {/* 工具行：折叠按钮（手动选项）+ 模型名小标签（调研 pitfall #6：
                每条 AI 消息标注模型名，升级/降级模型时一眼可辨） */}
            {(collapsible || (model && turn.model.text)) && (
              <div className="turn-tools-row">
                {collapsible && (
                  <button className="turn-collapse-btn" onClick={() => setCollapsed((v) => !v)}>
                    {collapsed
                      ? `已折叠 · ${turn.tools.length} 个工具 · ${turn.segments.length} 轮${duration ? ` · ${duration}` : ''}`
                      : '折叠'}
                  </button>
                )}
                {model && turn.model.text && (
                  <span className="model-tag" title="本次任务使用的模型">{model}</span>
                )}
              </div>
            )}
            {!collapsed && (
              <div className="act-chain">
                {entries.map((entry) =>
                  entry.type === 'group' ? (
                    <ToolGroupView
                      key={entry.group.key}
                      group={entry.group}
                      open={groupState.get(entry.group.key) ?? true}
                      onToggle={toggleGroup}
                      density={density}
                      disclosure={disclosure}
                      onCycleLevel={cycleLevel}
                      onFocusTool={onFocusTool}
                      sessionId={sessionId}
                    />
                  ) : (
                    <ChainNodeView
                      key={chainKey(entry.node, entry.index)}
                      node={entry.node}
                      density={density}
                      disclosure={disclosure}
                      reasoningDisclosure={reasoningDisclosure}
                      isFinalModel={entry.index === lastModelIndex}
                      onCycleLevel={cycleLevel}
                      onFocusTool={onFocusTool}
                      onOpenSession={onOpenSession}
                      onInspectChild={onInspectChild}
                      sessionId={sessionId}
                    />
                  ),
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* #1017：本轮的记忆召回说明（投影 turn.recalls，正常流每 run 一条，
          投影防御路径允许多条并逐条渲染）——
          就地折叠、展开才对账 /memory-recalls。挂在执行链之后 = 「这次回答
          用了我哪条记忆」的归属行；折叠态单行，测高由虚拟化 measureElement
          动态吸收（与 notices/delegations 同一机制）。 */}
      {!isSupersededTurn && sessionId && turn.recalls?.map((recallEvent) => (
        <section className="memory-v2-activity" aria-label="记忆召回说明" key={`recalls-${recallEvent.seq ?? recallEvent.time}`}>
          <RecallNotice event={recallEvent} sessionId={sessionId} />
        </section>
      ))}
    </div>
  );
});

function chainKey(node: ChainNode, i: number): string {
  if (node.kind === 'tool') return node.tool.tool_call_id;
  if (node.kind === 'delegation') return node.delegation.child_session_id;
  if (node.kind === 'reasoning') return node.block.blockId;
  return `model-${i}`;
}

/** 反向联动窗口外兜底的 key→turn 匹配（tool:/delegation:/step: 三定位域，
 *  与 streamKeyFromEvent 的 key 词汇一一对应）。 */
function turnHasStreamKey(t: Turn, key: string): boolean {
  if (key.startsWith('tool:')) return t.tools.some((x) => `tool:${x.tool_call_id}` === key);
  if (key.startsWith('delegation:')) {
    return t.delegations?.some((x) => `delegation:${x.child_session_id}` === key) ?? false;
  }
  return `step:${t.step_id}` === key;
}

// ── 语义事件渲染注册表（spec 03 §10 Semantic Event Renderer Registry）──
// kind → 渲染器映射，禁止在单一巨型组件里硬编码全部运行时类型。
// 键由 ChainNode['kind'] 类型系统穷举——新增节点类型必须注册渲染器才过 tsc；
// 事件级未知类型在投影层已隔离（unknown_events 兜底），注册表不承担该职责。

/** 执行链节点的渲染上下文（TurnView 透传，注册表内不再取 React hook）。 */
interface ChainRenderCtx {
  node: ChainNode;
  density: TraceDensity;
  disclosure?: Disclosure;
  reasoningDisclosure?: ReasoningDisclosureApi;
  /** 该 model 段是否为 turn 最后一个模型段（final-answer 高对比）。缺省 true
   *  （与旧 ChainNodeView 默认一致——直接调用方多省略此 prop）。 */
  isFinalModel?: boolean;
  onFocusTool?: (tool: ToolCall) => void;
  /** L 级循环回调（F1/#270）：由 TurnView 用 useCallback 建立一次，跨渲染引用稳定，
   *  memo(ToolCard) 才会命中。带 (key, density) 参数而不是无参闭包——注册表不能取
   *  hook，按节点现场建的闭包必然破坏下游 memo 的浅比较。 */
  onCycleLevel?: (key: string, density: TraceDensity) => void;
  onOpenSession?: (sessionId: string) => void;
  onInspectChild?: (child: { childSessionId: string; target: string }) => void;
  /** #186：这条链属于哪个会话——工具卡的归档 diff 要按会话读 artifact 内容。 */
  sessionId?: string;
}

/** 每种 kind 的渲染器只接收窄化后的 node 类型（Extract 按 kind 收紧）。 */
type RendererFor<K extends ChainNode['kind']> = (
  ctx: Omit<ChainRenderCtx, 'node'> & { node: Extract<ChainNode, { kind: K }> },
) => ReactNode;

/** 已完成/历史 model 段的 markdown 正文（F1/#270 必做 3）。
 *  记忆化的键**必须是内容**：`segment` 对象在每次投影提交时都可能换引用，按引用记忆
 *  等于没记忆化（票面 Risks 点名的陷阱）。这里靠 memo 的 props 浅比较，键就是字符串
 *  `text`——内容未变的重复提交直接 bail out，`renderMarkdown` 一次都不再跑。
 *  注：注册表是普通函数不能取 hook，所以记忆化只能落在组件上。 */
const MarkdownBody = memo(function MarkdownBody({ text }: { text: string }) {
  return <>{renderMarkdown(text)}</>;
});

const CHAIN_RENDERERS: { [K in ChainNode['kind']]: RendererFor<K> } = {
  tool: ({ node, density, disclosure, onFocusTool, sessionId, onCycleLevel }) => {
    const key = toolEventKey(node.tool.tool_call_id);
    return (
      <ToolCard
        tool={node.tool}
        density={density}
        level={disclosure ? disclosure.levelFor(key, density) : undefined}
        onCycleLevel={onCycleLevel}
        onFocus={onFocusTool}
        sessionId={sessionId}
      />
    );
  },
  delegation: ({ node, density, onOpenSession, onInspectChild }) => (
    <DelegationNode
      delegation={node.delegation}
      density={density}
      onOpenSession={onOpenSession}
      onInspectChild={onInspectChild}
    />
  ),
  reasoning: ({ node, density, reasoningDisclosure }) => (
    <ReasoningBlockView block={node.block} density={density} disclosure={reasoningDisclosure} />
  ),
  model: ({ node, density, isFinalModel: isFinal = true }) => {
    const { segment } = node;
    const kind: RuntimeEventKind = modelKind(segment.status, isFinal);
    // Compact 档下 done 的 model 段只渲染首行摘要（渐进披露：详情留给 Inspector）
    if (density === 'compact' && segment.status !== 'streaming') {
      const first = segment.text.split('\n').find((l) => l.trim()) ?? '';
      if (!first) return null;
      return (
        <div className="model-output done model-output-compact"><MarkdownBody text={truncateForDisplay(first)} /></div>
      );
    }
    if (!segment.text && segment.status !== 'streaming') return null;
    // 模型文本与工具输出同级不可信——单行超长模型输出同样会冻结 UI，渲染前截断
    // （41e7360 只覆盖了工具路径，code-review 补齐此处）。
    const display = truncateForDisplay(segment.text);
    // 语义图标行（PRD §5.2/§10）：思考中（streaming）/ 中间输出（done 非终段）。
    // final-answer 不出行（高对比正文直接呈现，PRD §11.2）。
    const kindRow =
      kind === 'final-answer' ? null : (
        <div className={`model-kind-row model-kind-row-${kind}`}>
          <span className="model-kind-icon">{(() => { const Icon = KIND_ICON[kind]; return <Icon size={14} />; })()}</span>
          <span className="model-kind-label">{KIND_LABEL[kind]}</span>
          {kind === 'thinking' && <span className="model-kind-ellipsis" aria-hidden="true" />}
        </div>
      );
    // P0-2a 流式 markdown 增量化（HANDOFF §6）：streaming 段渲染纯文本
    // （pre-wrap 样式保留换行，标记原样透传——打字机状态本就不需要排版），
    // model/completed 置 done 后一次性 renderMarkdown。消灭流式期间每 delta
    // 全量重解析的 CPU 开销。纯文本走 React 文本节点，天然零 XSS 面。
    if (segment.status === 'streaming') {
      return (
        <div className="model-output-wrap">
          {kindRow}
          <div className="model-output streaming">
            {display}
            <span className="stream-caret" />
          </div>
        </div>
      );
    }
    // hover 复制（调研：per-message copy 是 AI chat 标配动作；仅完成段提供，
    // 流式段文本还在增长，复制半成品是噪音）。中间段低对比（D17 分层），
    // final-answer 保持正文高对比。
    return (
      <div className="model-output-wrap">
        {kindRow}
        <div className={`model-output ${segment.status}${kind === 'model' ? ' model-output-intermediate' : ''}`}>
          <MarkdownBody text={display} />
        </div>
        {segment.text && <CopyButton text={segment.text} label={kind === 'final-answer' ? '复制回答' : '复制输出'} />}
      </div>
    );
  },
};

export function ChainNodeView(ctx: ChainRenderCtx): ReactNode {
  // 单点窄化：键与 node.kind 出自同一 ctx（编译期保证一致），注册表内部
  // 已按 Extract 收紧各自 node 类型——调度处一次性还原宽签名。
  const render = CHAIN_RENDERERS[ctx.node.kind] as (c: ChainRenderCtx) => ReactNode;
  return render(ctx);
}

/** #1429 打磨点 1：连续同类工具调用的组头 + 缩进子行。
 *
 * 组头是一行字（图标 · 「N 次读取」· 工具名计数 · 状态 · 耗时 · chevron），
 * 子行复用 `ToolCard` 的同一套 L0-L2 契约——分组只改**排布**，不改工具卡的
 * 交互与密度语义。开合走 `grid-template-rows: 0fr → 1fr`（打磨点 2）。
 *
 * `memo`（#1429 F8）：开合状态是 Conversation 层的一张 Map，换一次换引用 ⇒ 所有
 * `TurnView` 都会重渲染。`open` 是逐组布尔值，memo 让**没被点的那几组**直接 bail
 * out——"开合只重渲染被点组"才是真的，而不是"每次开合重渲染整轮"。 */
const ToolGroupView = memo(function ToolGroupView({
  group,
  open,
  onToggle,
  density,
  disclosure,
  onCycleLevel,
  onFocusTool,
  sessionId,
}: {
  group: ToolGroup;
  open: boolean;
  onToggle: (key: string) => void;
  density: TraceDensity;
  disclosure?: Disclosure;
  onCycleLevel?: (key: string, density: TraceDensity) => void;
  onFocusTool?: (tool: ToolCall) => void;
  sessionId?: string;
}) {
  const bodyId = `act-group-body-${group.key}`;
  return (
    <div className={`act-group${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="act-group-head"
        aria-expanded={open}
        /* 收起态 `Collapse` 会卸载子树（id 不存在）⇒ 悬空的 aria-controls 指向
           不存在的节点，读屏器会读出一个空引用。只在真有目标时声明（#1429 P3）。 */
        aria-controls={open ? bodyId : undefined}
        onClick={() => onToggle(group.key)}
      >
        <span className="act-group-ico">{kindGlyph(group.kind, 12)}</span>
        <b>{group.members.length} 次{GROUP_LABEL[group.kind]}</b>
        <span className="act-group-kinds">
          {group.names.map((n) => `${n.name} ×${n.count}`).join(' · ')}
        </span>
        {/* 组头状态 = 成员聚合（任一失败→失败、任一运行中→运行、全完成→完成）。 */}
        <span className={`act-status act-status-${group.status} act-group-status`}>
          {group.status === 'running'
            ? <span className="status-spinner" />
            : statusGlyph(group.status)}
        </span>
        <GroupDuration
          startedAt={group.startedAt}
          completedAt={group.completedAt}
          status={group.status}
        />
        {caret('act-group-chev')}
      </button>
      <Collapse open={open} className="act-group-body" id={bodyId}>
        {group.members.map(({ tool }) => (
          <ToolCard
            key={tool.tool_call_id}
            tool={tool}
            density={density}
            level={disclosure ? disclosure.levelFor(toolEventKey(tool.tool_call_id), density) : undefined}
            onCycleLevel={onCycleLevel}
            onFocus={onFocusTool}
            sessionId={sessionId}
          />
        ))}
      </Collapse>
    </div>
  );
});

/** 组头耗时 = span（首成员开始 → 末成员结束）。
 *  末成员仍在跑时按当前时刻续算，只显示已运行时长，不造百分比。
 *  末成员被中断（stopped）时也可能没有 `completed_at`（`finalizeRun` 只翻状态）——
 *  中断的组没有终点，显式说「已中断」，**不**挂 ticker 永久续算一个已经停住的组
 *  （#1429 F7）。已收口 / 已中断的组头都不挂 ticker：那是绝大多数。 */
function GroupDuration({
  startedAt,
  completedAt,
  status,
}: {
  startedAt?: string;
  completedAt?: string;
  status: ToolGroupStatus;
}) {
  if (!startedAt) return null;
  if (completedAt) {
    const text = formatShortDuration(
      new Date(completedAt).getTime() - new Date(startedAt).getTime(),
    );
    return text === '' ? null : <span className="act-group-total">{text}</span>;
  }
  if (status === 'running') return <LiveGroupDuration startedAt={startedAt} />;
  if (status === 'stopped') return <span className="act-group-total">已中断</span>;
  return null;
}

/** 未收口组头的已运行时长（每秒续算；只给"已跑多久"，不给假百分比）。 */
function LiveGroupDuration({ startedAt }: { startedAt: string }) {
  const now = useTickingNow(1000);
  const text = formatShortDuration(now - new Date(startedAt).getTime());
  return text === '' ? null : <span className="act-group-total">{text}</span>;
}

/** #1429 打磨点 6：轮次导航（1..N 药丸 + 压缩置灰）。粘在对话列顶部——它不在
 *  滚动容器里，滚到任何位置都可见；点击走虚拟化 `scrollToIndex`，目标行即使
 *  不在已挂载窗口内也能滚到。 */
function TurnNav({
  items,
  onJump,
}: {
  items: TurnNavItem[];
  onJump: (stepId: number) => void;
}) {
  return (
    <div className="turn-nav" role="group" aria-label="轮次导航">
      {items.map((item) => (
        <button
          key={item.turnIndex}
          type="button"
          className={`turn-nav-pill${item.stepId === null ? ' is-off' : ''}`}
          disabled={item.stepId === null}
          title={item.title}
          aria-label={item.title}
          onClick={() => {
            if (item.stepId !== null) onJump(item.stepId);
          }}
        >
          {item.turnIndex}
        </button>
      ))}
    </div>
  );
}
