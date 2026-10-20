/** StepDetail — right rail Run Inspector（PRD §8，ADR-0014 D3 重排）.
 *
 * Timeline 升为 Inspector 常驻主体（PRD §8.3 "Timeline 是 Inspector 的核心"）：
 *   Timeline  → 每条事件 verbatim（seq · type · summary），默认 tab；点击行钻取
 *               事件详情，并反向定位中间主区（PRD §9.2 联动）
 *   Overview  → Run 级摘要（RUN / TOOLS / CONTEXT / MODEL / TRACE——原 Chat tab）
 *   Changes   → 文件 diff 聚合（保留，D11 不删）
 *   Terminal  → bash 调用聚合（保留）
 *   Artifacts → artifact ref 聚合（保留）
 *
 * Event-level inspector（PRD §8.4 四段）：Overview / Input / Output / Raw，
 * 顶部返回按钮回 Timeline（无弹窗——"上下文 Inspector"）。
 */

import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent, ReactNode } from 'react';
import {
  AlertTriangle, ArrowLeft, Check, ChevronDown, ChevronRight, Clock, Cpu,
  Database, FileCheck2, FileDiff, Hash, Layers, ListTree, Maximize2, Minimize2,
  Package, Pin, ShieldCheck, TerminalSquare, X, Zap,
} from 'lucide-react';
import { EventType } from '../types';
import type { AgentEvent, ConversationState, ToolCall } from '../types';
import { formatBytes, formatDateTime, formatDuration, formatShortDuration, formatTimestamp, stringifyForDisplay, truncateForDisplay } from '../lib/format';
import { groupEventsByRun, type RunGroupStatus } from '../lib/timelineGroups';
import { timelineHeadline } from '../lib/timelineHeadline';
import { allTools, displayedSessionUsage, summarizeEvent } from '../lib/projection';
import { addDecimalTexts } from '../lib/runBudget';
import { permissionView } from '../lib/permission';
import { commandOutputs, commandResult, isCommand } from '../lib/commandOutput';
import {
  INSPECTOR_MAX_W, INSPECTOR_MIN_W,
  PEEK_TAP_MS, clampInspectorWidth, escAction, eventKey, nextSelectionIndex, toolKey,
} from '../lib/inspectorPanel';
import { deriveAgentProfile, deriveReasoningEffort, deriveRunPulse, deriveRunSummary } from '../lib/runState';
import {
  barGeometry, deriveEventTimings, deriveTimelineEventKeys, eventCategory, eventMatchesFilter, filterCounts,
  formatAxisLabel, formatClock, seqGapMarkers, timelineAxis,
  type EventTiming, type SeqGap, type TimelineAxis, type TimelineFilter, type TimelineStatus,
} from '../lib/timelineView';
import { useChildConversation } from '../hooks/useChildConversation';
import { ApprovalCard } from './ApprovalCard';
import { ArtifactViewer } from './ArtifactViewer';
import { ConstraintInheritance } from './ConstraintInheritance';
import { ConstraintsTab } from './ConstraintsTab';
import { CopyButton } from './CopyButton';
import { DiffBlock } from './DiffBlock';
import { JsonTree } from './JsonTree';
import { ToolOutputStream } from './ToolOutputStream';
import { Tip } from './Tooltip';

/** Inspector focus: Run-level overview, a drilled-in event, or a child session
 *  (Phase 13 委派钻取，v2 PRD §10.5 "delegation start ↔ child run" 配对）。 */
export type InspectorFocus =
  | { kind: 'run' }
  | { kind: 'tool'; tool: ToolCall }
  | { kind: 'event'; event: AgentEvent }
  | { kind: 'child'; childSessionId: string; target: string };

type Tab = 'timeline' | 'chat' | 'changes' | 'terminal' | 'artifacts' | 'constraints';

/** Tab 图标 —— 每个 tab 一个 lucide icon，与 label 配对。
 *  Inspector 分段控件（segmented control）参考 Linear issue 面板与 VS Code
 *  侧栏：icon + label 在宽面板下并排，窄面板（<300px）降级为 icon-only + title。 */
const TAB_ICONS: Record<Tab, typeof ListTree> = {
  timeline: ListTree,
  chat: Hash,
  changes: FileDiff,
  terminal: TerminalSquare,
  artifacts: Package,
  constraints: ShieldCheck,
};

const TABS: readonly { id: Tab; label: string }[] = [
  { id: 'timeline', label: '时间线' },
  { id: 'chat', label: '概览' },
  { id: 'changes', label: '变更' },
  { id: 'terminal', label: '终端' },
  { id: 'artifacts', label: '产出文件' },
  { id: 'constraints', label: '我的要求' },
];

/** #1372 改动 8：统计行每个图标的指标口径（tooltip 用）。计数是**本会话**口径，
 *  与 tab 上的数字同源（`tabCounts`），这里只解释"这个数在数什么"。 */
const TAB_TIPS: Record<Tab, string> = {
  timeline: '时间线：本会话全部事件，按 run 分组',
  chat: '概览：run 状态、轮次、耗时、tokens 与工具计数',
  changes: '改动：产生 diff 的工具调用（本会话）',
  terminal: '输出：命令类工具调用（bash 等）',
  artifacts: '产物：本会话创建的 artifact 引用',
  constraints: '约束：本会话生效的长期规则',
};

// C4 Timeline hover 浮层布局常量（与 .tl-tooltip CSS 对齐——行高 16、内距 12、
// 与行边的 4px 间隙、上方放置需要的 16px 顶部裕量）。改一个地方即可。
const TIP_LINE_H = 16;
const TIP_PADDING = 12;
const TIP_GAP = 4;
const TIP_TOP_MARGIN = 16;

/** 「稳定的空值」——沿用 `Conversation.tsx` 的 `EMPTY_TURNS` 同款惯例：`memo` /
 *  `useMemo` 的依赖比较是**引用比较**，每次渲染新建的 `[]` 会让记忆化永久失效。 */
const EMPTY_TOOLS: ToolCall[] = [];
const EMPTY_TARGETS: { key: string; focus: () => void }[] = [];
/** 稳定空集合 / 空表——memo 与依赖比较用（每次新建 `new Set()` 会永久失效）。 */
const EMPTY_KEYS: ReadonlySet<string> = new Set();
const EMPTY_TIMINGS: ReadonlyMap<string, EventTiming> = new Map();
const EMPTY_EVENT_KEYS: ReadonlyMap<AgentEvent, string> = new Map();

/** #1419 P3-9：同时展开的卡片上限。每张展开卡都挂载完整的 `EventInspector` /
 *  JsonTree，连续展开会让 Inspector 的 DOM/内存无界增长；3 张足够对照上下文，
 *  超出时按 Set 插入顺序淘汰最早展开的一张（确定、可解释）。 */
const MAX_EXPANDED_CARDS = 3;

/** #1106：工具行的 run 归属——projection 存的 `raw_call` 是 tool/call 事件的
 *  **verbatim 整信封拷贝**（含 run_id），归属以它对账（事件真值，与 projection
 *  同口径）；缺失（旧数据）→ null，不猜。 */
function toolRunId(tool: ToolCall): string | null {
  const v = tool.raw_call?.run_id;
  return typeof v === 'string' && v ? v : null;
}

interface Props {
  conversation: ConversationState | null;
  streaming: boolean;
  focus: InspectorFocus;
  onFocusRun: () => void;
  onFocusTool: (tool: ToolCall) => void;
  onFocusEvent: (event: AgentEvent) => void;
  /** PRD §9.2 反向联动：点 Timeline 行 → 中间主区滚动定位对应事件。 */
  onJumpToStream?: (event: AgentEvent) => void;
  /** #184 反向联动：点 PERMISSION 段的待审批行 → 中间主区滚动定位审批卡。 */
  onJumpToApproval?: (approvalId: string) => void;
  /** #183 面板视图状态（钉住 / 整页 / 宽度 / peek 开合）。状态归 App——面板宽度与整页
   *  要改的是 `.app-regions` 的栅格，StepDetail 只是消费方与触发方。 */
  panel: InspectorPanelState;
  /** #1419 P2-3：面板当前是否展开（App 的 `inspectorOpen`）。展开态是视图状态，
   *  面板一旦关闭（按钮 / Esc / 会话切换 / 断点 / rail 菜单）就必须清空；这些
   *  关闭路径有一半绕过 `onPanelAction`，所以用这个**原语**统一收敛。缺省 true
   *  （独立渲染/测试不传时保持旧行为）。 */
  open?: boolean;
  /** #183 面板动作。**显式动作而不是 toggle**：Esc 要的是"退出整页"而非"翻转整页"，
   *  用一个 `toggle` 表达两件事会让"连按两次 = 回到原点"这类事故无法从类型上排除。 */
  onPanelAction: (action: InspectorPanelAction) => void;
  /** #1030：后端待裁决清单非空。与 TopBar 同源（App 的 `hasPendingDecisions`），
   *  让 Inspector 头标与顶栏对同一会话说同一个态——#312 的先例：一个独立态不许在
   *  一侧被粗化。**必须是稳定的原语**（memo 有效性依赖 prop 身份，见上方操作约束）。 */
  awaitingDecision?: boolean;
  /* #1419 O-3：总览「需要你来决定」段内嵌**真实**审批卡（`ApprovalCard`），三个按钮
   * 走既有 `postApproval` 通路，不新造第二条提交路径。这三项与中心列审批卡同源：
   * `goneApprovalIds` 是 App 记下的「后端已回 404」集合，两个回调分别上报失效与
   * 决策成功（后者触发一次对账 resync）。全部由 App 的 useState/useCallback 持有，
   * 身份稳定，`memo` 有效性不受影响。 */
  goneApprovalIds?: ReadonlySet<string>;
  onApprovalGone?: (approvalId: string) => void;
  /** 与 `Conversation` 的同名回调同签名（`useSession.resyncAfterDecision`）：
   *  决策 POST 成功后按 sessionId 对账一次，保证决策后事件有一条消费路径。 */
  onApprovalDecided?: (sessionId: string) => void;
}

/** #183 面板视图状态（全部**不持久化**，不变量 #22）。 */
export interface InspectorPanelState {
  /** 钉住：切换会话时不自动收起（AC4）。 */
  pinned: boolean;
  /** 整页：面板占满工作区宽度，长 trace / 大 diff 宽读（AC5）。 */
  expanded: boolean;
  /** 面板宽度 px（320→480，AC6）。 */
  width: number;
  /** 选中项详情（peek）是否展开。关掉只是 `hidden`，**不卸载**（AC2）。 */
  peekOpen: boolean;
}

/** #183 面板动作（单入口，App 用一个 handler 消费）。 */
export type InspectorPanelAction =
  | { type: 'pin'; value: boolean }
  | { type: 'expand'; value: boolean }
  | { type: 'close' }
  | { type: 'open-peek' }
  | { type: 'close-peek' }
  | { type: 'resize'; width: number };

/**
 * F2（#272）：`memo` 包裹——本组件**关闭时仍保持挂载**（`hidden` 而非卸载），所以"父级
 * 与对话无关的提交"（打字 / hover / 拖宽 / 换焦点）此前一直在付整棵 Inspector 子树的重跑成本。
 *
 * ⚠ **操作约束（改本组件 props 前必读）**：`memo` 的有效性完全取决于上游给的 prop 身份是否
 * 稳定。新增 prop 前必须在 `App.tsx` 侧确认它由 `useState` / `useCallback` / 原语持有；否则
 * `memo` 恒 miss。**不要**补自定义 `areEqual`（同 `Conversation.tsx`：第二套版本机制 +
 * 更易写错的静默吞更新）。
 *
 * 逐项稳定性核对表、否决 `areEqual` 的理由，见
 * `docs/adr/0037-projection-reference-stability-and-events-version.md` D5.3。
 */
export const StepDetail = memo(function StepDetail({ conversation, streaming, focus, onFocusRun, onFocusTool, onFocusEvent, onJumpToStream, onJumpToApproval, panel, open = true, onPanelAction, awaitingDecision = false, goneApprovalIds, onApprovalGone, onApprovalDecided }: Props) {
  const [tab, setTab] = useState<Tab>('timeline');
  /* 头标 run-id 列表（title + 「N runs」计数同源）。必须挂在此处——`useMemo` 不许出现在
   * 下方任何 early-return 之后（Rules of Hooks：两次渲染的 hook 数必须一致）。
   *
   * 依赖键是 `eventsVersion`，**不是** `events`——契约见 ADR-0037 D3，`exhaustive-deps`
   * 看不到这层，故定向豁免；豁免写法见本文件 `listTargets` 内的那条说明。 */
  const runIdList = useMemo(
    () => [...new Set((conversation?.events ?? []).flatMap((e) => (e.run_id ? [e.run_id] : [])))], // eslint-disable-line react-hooks/exhaustive-deps
    [conversation?.eventsVersion], // eslint-disable-line react-hooks/exhaustive-deps
  );
  /* #1419：头部标题取**第一条用户消息**（真实数据）。会话模型没有独立的
   * run_title 字段——不编一个，也不把最后一条消息当标题。 */
  const runTitle = useMemo(() => {
    const raw = conversation?.turns[0]?.user_message ?? '';
    const firstLine = raw.split('\n').find((line) => line.trim())?.trim() ?? '';
    return firstLine.length > 72 ? `${firstLine.slice(0, 72)}…` : firstLine;
  }, [conversation?.turns]);
  /* #183：面板根节点（拖宽要量出"中心列 + 面板"的实际可用宽度）与键盘计时/拖拽状态。
   * 同样是 hook——必须在 early-return 之前。 */
  const panelRef = useRef<HTMLElement>(null);
  /* #1419 AC2「快按 = 保持打开，按住 = 松手关闭」的判据：**阈值到点时键还按着吗**，
   * 不是「keydown 与 keyup 两个 `timeStamp` 之差」。
   *
   * 后者会把**我们自己**的渲染时间算进"按住时长"：keydown 里打开 peek 触发的重渲染
   * （520px 面板 + 卡片流）在并行跑 e2e / 主线程忙时能吃掉几百毫秒，于是一次零延迟的
   * `keyboard.press('Space')` 被量成 400ms+ 的长按，peek 刚开就被松手关掉（实测两
   * worker 并行 6 次复现 5 次）。计时器只在主线程**空闲**时才会走到点——那恰好就是
   * "用户真的还按着"的意思；排队中的渲染与事件不参与计量。 */
  const spaceHoldTimer = useRef<number | null>(null);
  const spaceHeldLong = useRef(false);
  const dragRef = useRef<{ startX: number; startW: number; available: number } | null>(null);
  const [resizing, setResizing] = useState(false);
  /* #1419：筛选与展开是**面板级**视图状态（不是 TimelineTab 内部状态）——
   * Enter/Esc 在面板根捕获，面板关闭要清空；会话切换按 sessionId 分槽
   * （`Map<sessionId, Set<timelineKey>>`），A 展开后切到 B 再回来不会覆盖 A。 */
  const [timelineFilter, setTimelineFilter] = useState<TimelineFilter>('all');
  /* P2-3：展开集连同「写入时的面板开合」一起存。关闭路径有一半绕过
   * `onPanelAction`（会话切换/断点/rail 菜单直接 `setInspectorOpen(false)`），
   * 所以以 `open` 这个**原语**为准：开合一变，上一轮的集合整体作废。
   * 这是 React 的「渲染期调整 state」：`open` 是 prop，派生状态与它不一致时
   * 当场重置（比 effect 少一轮提交，也不会让"关闭期间"的旧集合在重开后复活）。 */
  const [expandedStore, setExpandedStore] = useState<{
    open: boolean;
    bySession: ReadonlyMap<string, ReadonlySet<string>>;
  }>(() => ({ open, bySession: new Map() }));
  if (expandedStore.open !== open) setExpandedStore({ open, bySession: new Map() });

  /* ── F2（#272）：派生收敛 ────────────────────────────────────────────────
   *
   * ⚠ **操作约束（改下列任一 useMemo 前必读）**：
   *  1. 它们必须在两处 early-return **之前**（Rules of Hooks，同上面的 `runIdList`）。
   *  2. 依赖**细到字段，绝不写整个 `conversation`**——`applyEvent` 每次事件都返回新的顶层
   *     对象（顶层浅克隆），写整个对象是"**一次也不省**"。每个键怎么来的、为什么是它、
   *     以及本票消不掉的那部分代价，见 ADR-0037 D5.3 / D5.4。 */

  // 单一走法（#190）：与中心列「输出」面共用 projection.allTools——不在本文件内再造一份。
  const tools = useMemo(() => (conversation ? allTools(conversation) : EMPTY_TOOLS),
    [conversation?.turns]); // eslint-disable-line react-hooks/exhaustive-deps
  /* #1419：Timeline 卡片流的派生表只算一次，列表/筛选/键盘导航共用。 */
  const timelineTimings = useMemo(
    () => (conversation ? deriveEventTimings(conversation.events, tools) : EMPTY_TIMINGS),
    [conversation?.eventsVersion, tools], // eslint-disable-line react-hooks/exhaustive-deps
  );
  /* #1419 P2-1：Timeline 行身份（`eventKey` + seq-less 同键序号）。timing 仍按
   * `eventKey` 存（同一 eventKey 的事件共享同一份 timing），选中/展开/DOM id 用
   * 这张表的**唯一键**。 */
  const timelineKeys = useMemo(
    () => (conversation ? deriveTimelineEventKeys(conversation.events) : EMPTY_EVENT_KEYS),
    [conversation?.eventsVersion], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const timelineKeyOf = useCallback(
    (event: AgentEvent) => timelineKeys.get(event) ?? eventKey(event),
    [timelineKeys],
  );
  const pulse = useMemo(() => deriveRunPulse(conversation, streaming, awaitingDecision),
    [ // eslint-disable-line react-hooks/exhaustive-deps
      conversation?.run_interrupted, conversation?.run_status, conversation?.run_cancelled,
      conversation?.active_step_id, conversation?.turns, streaming, awaitingDecision,
    ]);
  /* #1106：当前聚焦 run 的归属——选中项（事件 / 工具行）自己的信封 run_id。
   * run 级视图（无选中项）与子会话视图没有聚焦 run → null → 头标徽标回退
   * 「取最后值」（单 run 会话展示不变）。 */
  const focusedRunId: string | null =
    focus.kind === 'event'
      ? focus.event.run_id
        ? focus.event.run_id
        : null
      : focus.kind === 'tool'
        ? toolRunId(focus.tool)
        : null;
  // #198：生效档位（聚焦 run 携带；无聚焦 → 最后一个 run/started；旧数据 → null →「档位未知」）。
  const agentProfile = useMemo(() => (conversation ? deriveAgentProfile(conversation.events, focusedRunId) : null),
    [conversation?.eventsVersion, focusedRunId]); // eslint-disable-line react-hooks/exhaustive-deps
  // #1093：请求侧推理档位（聚焦 run 携带；未声明 → null，整段不渲染）。
  const reasoningEffort = useMemo(
    () => (conversation ? deriveReasoningEffort(conversation.events, focusedRunId) : null),
    [conversation?.eventsVersion, focusedRunId]); // eslint-disable-line react-hooks/exhaustive-deps
  /* #1419：副行模型徽标——provider 回显优先，其次请求侧（两者都在时回显胜出，
   * 请求值进 title）。都不在 ⇒ 不渲染，不伪造模型名。 */
  const headerModel = conversation?.model ?? conversation?.requested_model ?? null;
  /* 效果图 `.live-dot`：只在真的在跑时呼吸（终态会话挂一个常亮绿点会被读成"还在跑"）。 */
  const isLive = streaming && (pulse.state === 'thinking' || pulse.state === 'tool');
  /* UI-03：tab 条目计数（与各 tab 的数据源同一判据，不建第二真相）。
   * Overview 是摘要页不计数；Changes/Terminal/Artifacts 的过滤条件与对应 Tab 组件内的
   * filter 逐字一致——收敛成三张表后 tab 计数与列表内容同源，也顺手消掉了每次提交三次
   * O(tools) 扫描。 */
  const diffTools = useMemo(() => tools.filter((t) => t.diff), [tools]);
  const commandTools = useMemo(() => tools.filter(isCommand), [tools]);
  const artifactTools = useMemo(() => tools.filter((t) => t.artifact), [tools]);
  const tabCounts: Partial<Record<Tab, number>> = {
    timeline: conversation?.events.length ?? 0,
    changes: diffTools.length,
    terminal: commandTools.length,
    artifacts: artifactTools.length,
  };

  /* #183 清单条目：↑/↓ 的移动域 = **当前 tab 里可点击选中的行**。
   *
   * 只登记"行本身可点"的三个面（Timeline 事件 / Overview 工具行 / Terminal 命令行）：
   * Changes / Artifacts 的行目前是只读卡片（不是按钮），为它们造一个只能用键盘到达的
   * 选中态，等于做出一个鼠标无法复现的选择——违反 AC3「鼠标默认可用，不做键盘唯一」。
   * 那两面的行要不要变成可选中，是它们各自票里的事。
   *
   * F2（#272）：这张表每次提交都重建成 N 个新闭包（事件多时 = O(N) 个闭包 + 一次 events
   * 线性扫描），故收进 `memo`。键含 `eventsVersion` 与两个回调——依赖逐项理由见 ADR-0037
   * D5.3 的 `listTargets` 行（两个回调进依赖是**反陈旧**保险，不是"用到了就写"）。 */
  const listTargets: { key: string; focus: () => void }[] = useMemo(() => {
    switch (tab) {
      case 'timeline': {
        /* ⚠ 本 memo 里 `conversation` 只在这一行被引用，但豁免指令**不能**写在这一行——
         * oxlint 的"判定位置"与它报出来的标签行不是同一个：挂在这一行 ⇒ 告警照旧；
         * 挂在下面依赖数组那一行 ⇒ 整条 `useMemo` 的告警（含本行这条 `missing
         * dependency: conversation`）一起被压掉。本文件所有同类豁免统一落在依赖数组行。
         *
         * 两条通用纪律（本仓一致，机制与 A/B 数字见 ADR-0037 D5.5）：
         *  1. 只能用行内 `eslint-disable-line`——`disable-next-line` / 块级会让该函数
         *     **全部** compiler 类规则一起跳过（连无关的真告警一起吞），那是放宽校验；
         *  2. 指令必须落在**依赖数组那一行**，写在回调行上是**装饰**（不生效）。 */
        const events = conversation?.events;
        if (!events) return EMPTY_TARGETS;
        /* #1419：键盘移动域与筛选后的可视列表同源——↑/↓ 不会把选中态送到
         * 当前筛选看不到的事件上（否则 Enter 会展开一个不可见卡片）。 */
        return events
          .filter((e) => eventMatchesFilter(e, timelineFilter, timelineTimings.get(eventKey(e))))
          .map((e) => ({ key: timelineKeyOf(e), focus: () => onFocusEvent(e) }));
      }
      case 'chat':
        return tools.map((t) => ({ key: toolKey(t), focus: () => onFocusTool(t) }));
      case 'terminal':
        return commandTools.map((t) => ({ key: toolKey(t), focus: () => onFocusTool(t) }));
      default:
        return EMPTY_TARGETS;
    }
  }, [ // eslint-disable-line react-hooks/exhaustive-deps
    tab, conversation?.eventsVersion, tools, commandTools, onFocusEvent, onFocusTool,
    timelineFilter, timelineTimings, timelineKeyOf,
  ]);

  /* #1419：展开键 = Timeline 行身份（`eventKey`，seq-less 同键事件追加序号），
   * 状态按 sessionId 分槽——切会话/钻取子会话时旧会话的展开不会串台；面板关闭
   * 后重开必须没有残留（判据是 `open`，见 `expandedStore` 的注释）。 */
  const toggleExpanded = useCallback((key: string) => {
    const sid = conversation?.session_id;
    if (!sid) return;
    setExpandedStore((prev) => {
      const next = new Map(prev.bySession);
      const current = new Set(next.get(sid) ?? EMPTY_KEYS);
      if (current.has(key)) current.delete(key);
      else {
        current.add(key);
        /* P3-9：超出上限时淘汰最早展开的一张（Set 保持插入顺序）。 */
        while (current.size > MAX_EXPANDED_CARDS) {
          const oldest = current.values().next().value;
          if (oldest === undefined) break;
          current.delete(oldest);
        }
      }
      if (current.size === 0) next.delete(sid);
      else next.set(sid, current);
      return { open, bySession: next };
    });
  }, [conversation?.session_id, open]);
  const clearExpanded = useCallback(() => {
    const sid = conversation?.session_id;
    if (!sid) return;
    setExpandedStore((prev) => {
      if (!prev.bySession.has(sid)) return prev;
      const next = new Map(prev.bySession);
      next.delete(sid);
      return { open, bySession: next };
    });
  }, [conversation?.session_id, open]);
  /* 点击卡片 = 选中 + 关掉 App 的自动 peek + 跳中心列 + 就地展开。顺序不能反：
   * `onFocusEvent` 会同步开 peek，随后 `close-peek` 用函数式更新把它关掉（同批提交，
   * 不闪）。 */
  const onSelectTimelineEvent = useCallback((event: AgentEvent) => {
    onFocusEvent(event);
    onPanelAction({ type: 'close-peek' });
    onJumpToStream?.(event);
  }, [onFocusEvent, onPanelAction, onJumpToStream]);

  if (!conversation) {
    return (
      <aside className="step-detail" data-panel="inspector">
        <DetailEmpty />
      </aside>
    );
  }

  // Phase 13 委派钻取（v2 PRD §10.5 "delegation start ↔ child run" 配对）：
  // child 会话在 Inspector 内原位展开——父会话上下文不丢，「返回 Run」一键回。
  // 置于 run 级派生（tools/pulse）之前——child 视图不消费它们（Standards P3）。
  // 仍是**独占**面板（不是 peek）：它是从中间列显式钻进来的另一个会话视图，
  // 与"清单 + 选中项详情"不同性质（#183 的 peek 只服务当前会话的选择）。
  if (focus.kind === 'child') {
    return (
      <aside className="step-detail" data-panel="inspector">
        <div className="detail-header">
          <button className="child-back-btn" onClick={onFocusRun} title="返回父会话的运行详情">
            <ArrowLeft size={14} /> 返回
          </button>
          <span className="panel-label">子会话 · {focus.target || '—'}</span>
          <span className="detail-run-id mono num" title={`child session ${focus.childSessionId}`}>
            {focus.childSessionId.slice(0, 8)}
          </span>
        </div>
        <div className="detail-body">
          <ChildSessionView childSessionId={focus.childSessionId} />
        </div>
      </aside>
    );
  }

  // #186：归档 diff 的「就地展开」要按会话读 artifact 内容——归属只能由 session 决定
  // （artifact_id 是内容哈希，跨会话可重名），所以从投影的会话 id 取，不另存一份。
  const sessionId = conversation.session_id;
  /* 面板开合变了 ⇒ `expandedStore` 在渲染期已整体重置（见其注释），这里只读。 */
  const expandedBySession = expandedStore.bySession;
  const expandedKeys = expandedBySession.get(sessionId) ?? EMPTY_KEYS;
  /** 面板关闭是唯一会丢掉"正在看的上下文"的动作：先清展开，再派发关闭。
   *  App 侧绕过本入口的关闭（会话切换/断点/rail 菜单）由 `open` 派生重置兜底。 */
  const dispatchPanelAction = (action: Parameters<typeof onPanelAction>[0]) => {
    if (action.type === 'close') clearExpanded();
    onPanelAction(action);
  };
  const selectedKey =
    focus.kind === 'event' ? timelineKeyOf(focus.event) : focus.kind === 'tool' ? toolKey(focus.tool) : null;
  const selectedIndex = selectedKey === null ? -1 : listTargets.findIndex((t) => t.key === selectedKey);
  const hasSelection = focus.kind === 'event' || focus.kind === 'tool';

  const moveSelection = (delta: number) => {
    if (listTargets.length === 0) return;
    const next = nextSelectionIndex(selectedIndex, delta, listTargets.length);
    if (next < 0 || next === selectedIndex) return;
    /* 选中项与 DOM 焦点一起移动（读屏"当前项"与视觉一致，Enter 也要求焦点在行上）。
       代价是一个已知的失败模式：`listTargets` 覆盖**筛选后**的全部事件，而 DOM 只有
       渲染窗口内的行——选中项落到窗口外时 `focus()` 会静默打空，焦点留在旧行上。
       这不是数据错误：`selectedKey` 已经变了，Timeline 的 `effectiveWindow` 会按它
       扩窗、`scrollIntoView` 把行带进视野；下一次 ↑/↓ 时 `focus()` 才命中。 */
    listTargets[next].focus();
    /* #1419：Timeline 的 ↑/↓ 是"选卡片"，不是"开预览"——App 的 onFocusEvent 会顺手
     * 打开 peek，这里立即关掉，保持与点击卡片同一套语义。 */
    if (tab === 'timeline') onPanelAction({ type: 'close-peek' });
  };

  /** Space 的生效范围：清单与面板 chrome。详情面板内部有自己的按钮（复制/标签条），
   *  表单域与详情内部一律不抢——否则"在详情里按空格切换标签"会变成开合预览。
   *  P2-2：按钮/链接的 Space 是**原生激活**（chips、手风琴头、钻取 tabs、CopyButton），
   *  必须与 Enter 分支同一张排除表；只有 `.timeline-row`（卡片本体）保留 Space=peek。 */
  const spaceExcluded = (target: HTMLElement) =>
    Boolean(target.closest('input, textarea, select, [contenteditable="true"], .detail-peek, button:not(.timeline-row), a'));

  /** #183 面板键位（AC2/AC5/AC7）。挂在 <aside> 上而**不是 window**：
   *  ① 只有焦点在面板内才生效——面板外的 Esc 仍归全局"中断流式"（既有行为）；
   *  ② 帧内 `stopPropagation` 让面板内的 Esc 不再被全局监听吃成"停止运行"：
   *     用户按 Esc 的意图是"关掉我正在看的这层"，而不是"杀掉正在跑的 run"。 */
  const onPanelKeyDown = (e: ReactKeyboardEvent<HTMLElement>) => {
    const target = e.target as HTMLElement;
    if (target.closest('[role="dialog"]')) return; // 弹层优先（同 App 的既有口径）
    if (e.key === 'Escape') {
      e.stopPropagation();
      /* #1419 分层 Esc：先收起展开卡片（哪怕同时有 peek/全屏），再走既有层级。
       * 这样"Esc 一下退一层"的直觉不会因为新增展开态而跳级。 */
      if (tab === 'timeline' && expandedKeys.size > 0) {
        clearExpanded();
        return;
      }
      const action = escAction({ expanded: panel.expanded, peekOpen: panel.peekOpen && hasSelection });
      if (action === 'exit-fullpage') dispatchPanelAction({ type: 'expand', value: false });
      else if (action === 'close-peek') onPanelAction({ type: 'close-peek' });
      else dispatchPanelAction({ type: 'close' });
      return;
    }
    if (e.key === 'Enter') {
      /* #1419：Enter 开合**选中卡片**。只在 Timeline 生效；详情展开区里的按钮、
       * 表单域、peek/弹层有自己的 Enter 语义，一律不抢。 */
      if (tab !== 'timeline' || focus.kind !== 'event') return;
      if (target.closest('input, textarea, select, [contenteditable="true"], .detail-peek, [role="dialog"]')) return;
      if (target.closest('button:not(.timeline-row), a')) return;
      if (selectedKey === null) return;
      /* P3-10：选中卡被当前筛选隐藏时（listTargets 只含筛选结果），Enter 不能把
       * 一个不可见的 key 塞进展开集——否则清除筛选后会"凭空"出现一张展开卡。 */
      if (!listTargets.some((t) => t.key === selectedKey)) return;
      e.preventDefault();
      toggleExpanded(selectedKey);
      return;
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (listTargets.length === 0) return;
      e.preventDefault(); // 选中与滚动不要同时发生（滚动由列表在选中后自己做）
      moveSelection(e.key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if (e.code === 'Space' || e.key === ' ') {
      if (spaceExcluded(target)) return;
      e.preventDefault(); // 不让浏览器把它当"滚动/激活当前按钮"
      if (e.repeat) return; // 长按的重复事件不重置计时
      if (spaceHoldTimer.current === null) {
        spaceHeldLong.current = false;
        spaceHoldTimer.current = window.setTimeout(() => {
          spaceHeldLong.current = true;
          spaceHoldTimer.current = null;
        }, PEEK_TAP_MS);
      }
      // 已有选中项但预览关着 → Space = 预览（Linear：Space 开 peek）。
      if (hasSelection && !panel.peekOpen) onPanelAction({ type: 'open-peek' });
    }
  };

  /** AC2：快按 = 保持打开，按住 = 松手关闭。判据见 `spaceHoldTimer` 的注释。 */
  const onPanelKeyUp = (e: ReactKeyboardEvent<HTMLElement>) => {
    if (e.code !== 'Space' && e.key !== ' ') return;
    if (spaceExcluded(e.target as HTMLElement)) return;
    if (spaceHoldTimer.current !== null) {
      window.clearTimeout(spaceHoldTimer.current);
      spaceHoldTimer.current = null;
    }
    const held = spaceHeldLong.current;
    spaceHeldLong.current = false;
    if (held) onPanelAction({ type: 'close-peek' });
  };

  /** AC6 拖宽：`available` 用**实测**的「中心列 + 面板」宽度（不含 rail——窄屏 rail
   *  会变 56px，用常量算出来的上限会随断点变化而错）。 */
  const onResizeStart = (e: ReactPointerEvent<HTMLElement>) => {
    const workspace = panelRef.current?.parentElement?.querySelector<HTMLElement>('.app-workspace');
    dragRef.current = {
      startX: e.clientX,
      startW: panel.width,
      available: (workspace?.clientWidth ?? 0) + panel.width,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
    setResizing(true);
  };
  const onResizeMove = (e: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    onPanelAction({
      type: 'resize',
      /* 面板位于三栏布局的最右列，手柄挂在它的**左缘**：指针向右移动 ⇒ 中心列变宽
         ⇒ 面板变窄。所以宽度 = `startW − Δ`（#197 曾写成 `+`：右拖变宽，与"拖动手柄"
         的直觉相反）。 */
      width: clampInspectorWidth(drag.startW - (e.clientX - drag.startX), drag.available),
    });
  };
  const onResizeEnd = (e: ReactPointerEvent<HTMLElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    setResizing(false);
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
  };
  /** 拖宽手柄的键盘通路（AC7：面板控制要么键鼠双通，要么别做成分隔条——
   *  一个 `role="separator"` 不能被键盘操作是**假**的可访问性声明）。
   *  方向与指针路径**同源**（#197）：ArrowRight = 把"手柄"向右推 ⇒ 面板变窄。
   *  只翻转拖拽、不翻转键盘，等于在同一根分隔条上留下两套矛盾方向。 */
  const onResizeKeyDown = (e: ReactKeyboardEvent<HTMLElement>) => {
    const step = e.shiftKey ? 64 : 16;
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    e.stopPropagation(); // 与清单 ↑/↓ 分开：这里左右调宽度
    const workspace = panelRef.current?.parentElement?.querySelector<HTMLElement>('.app-workspace');
    const available = (workspace?.clientWidth ?? 0) + panel.width;
    const next = panel.width + (e.key === 'ArrowRight' ? -step : step);
    onPanelAction({ type: 'resize', width: clampInspectorWidth(next, available) });
  };

  const peekVisible = hasSelection && panel.peekOpen;

  return (
    <aside
      className="step-detail"
      id="region-inspector"
      role="region"
      aria-label="Inspector 面板"
      tabIndex={-1}
      data-panel="inspector"
      data-expanded={panel.expanded ? 'true' : 'false'}
      data-resizing={resizing ? 'true' : 'false'}
      ref={panelRef}
      onKeyDown={onPanelKeyDown}
      onKeyUp={onPanelKeyUp}
    >
      {/* #183 AC6：拖宽手柄。`separator` + 当前值/范围如实上报（AC7），左右键可调。 */}
      <div
        className="detail-resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整运行详情宽度"
        aria-valuenow={panel.width}
        aria-valuemin={INSPECTOR_MIN_W}
        aria-valuemax={INSPECTOR_MAX_W}
        aria-valuetext={`${panel.width} 像素`}
        tabIndex={0}
        onPointerDown={onResizeStart}
        onPointerMove={onResizeMove}
        onPointerUp={onResizeEnd}
        onPointerCancel={onResizeEnd}
        onKeyDown={onResizeKeyDown}
      />
      <div className="detail-header">
        <div className="detail-header-main">
          <div className="ih-eyebrow">
            <Zap size={10} aria-hidden="true" />
            RUN INSPECTOR
          </div>
          <div className="ih-title">
            <h2 className="ih-title-text" title={runTitle || undefined}>
              {runTitle || '运行详情'}
            </h2>
            {/* 效果图 `.live-dot`：只在真的在跑时呼吸。 */}
            {isLive && <span className="ih-live-dot" aria-hidden="true" />}
            <span className={`run-badge run-badge-${pulse.state}`}>{pulse.label}</span>
          </div>
          <div className="ih-sub">
            {/* UI-03 头标对齐：时间线平铺全会话事件，副行给**范围**（最近 run id ·
                轮次），而不是单个 run 短码。完整 run 列表留在 title 里。 */}
            {conversation.run_id && (
              <span
                className="detail-run-id mono"
                title={`最近任务：${conversation.run_id}${runIdList.length > 1 ? `（共 ${runIdList.length} 轮：${runIdList.join(', ')}）` : ''}`}
              >
                {conversation.run_id}
              </span>
            )}
            {conversation.run_id && <span className="ih-sep">·</span>}
            {conversation.turn_index !== null ? (
              <span>第 {conversation.turn_index} 轮</span>
            ) : (
              runIdList.length > 0 && <span>共 {runIdList.length} 轮</span>
            )}
            {/* #226：模型徽标——`model` 是 provider 回显，`requested_model` 是我们
                发出去的值。两个都在时按效果图 `badge-primary` 显示实际模型，请求侧
                写进 title（"请求了 A、跑的是 B"是可解释的事实，不是矛盾）。 */}
            {headerModel !== null && (
              <>
                <span className="ih-sep">·</span>
                <span
                  className="badge ih-model-badge mono"
                  title={
                    conversation.requested_model && conversation.requested_model !== headerModel
                      ? `实际模型 ${headerModel} · 请求 ${conversation.requested_model}`
                      : `模型 ${headerModel}`
                  }
                >
                  <Cpu size={9} aria-hidden="true" />
                  {headerModel}
                </span>
              </>
            )}
            {/* #198：生效档位——#1106 起按**当前聚焦 run** 取值（选中项的信封
                run_id 对账；无聚焦回退最后一个 run/started）。#1408：头部只留
                运行状态 pill；档位改为徽标文本，未知＝不渲染（不伪造 "main"）。 */}
            {agentProfile !== null && (
              <>
                <span className="ih-sep">·</span>
                <span
                  className="detail-profile-badge mono num"
                  title={
                    focusedRunId !== null
                      ? `agent_profile: ${agentProfile}（聚焦任务的生效档位）`
                      : `agent_profile: ${agentProfile}（最近一次任务的生效档位）`
                  }
                >
                  <ShieldCheck size={9} aria-hidden="true" />
                  {agentProfile}
                </span>
              </>
            )}
            {/* #1093：请求侧推理档位——未声明时该键缺席 ⇒ 整段不渲染（不可得 ≠
                默认档，不伪造 "default"）。 */}
            {reasoningEffort !== null && (
              <>
                <span className="ih-sep">·</span>
                <span
                  className="detail-effort-badge mono num"
                  title={
                    focusedRunId !== null
                      ? `reasoning_effort: ${reasoningEffort}（聚焦任务请求侧选中的推理档位）`
                      : `reasoning_effort: ${reasoningEffort}（最近一次任务请求侧选中的推理档位）`
                  }
                >
                  推理 · {reasoningEffort}
                </span>
              </>
            )}
          </div>
        </div>
        {/* #183 AC4/AC5/AC7：钉住 / 整页 / 关闭。三个都是键盘可达的按钮，开关类用
            `aria-pressed` 如实上报（不是靠 icon 换形状暗示状态）。 */}
        <span className="detail-header-actions">
          <button
            type="button"
            className={`detail-ctrl${panel.pinned ? ' sel' : ''}`}
            aria-pressed={panel.pinned}
            /* 名与状态分离：`aria-pressed` 说状态，`aria-label` 说"这是什么"。
               名字**必须**在这里给——窄面板（<360px）隐藏 `.detail-ctrl-label`
               之后，按钮的可访问名会变成空（icon 是 aria-hidden 的）。 */
            aria-label="钉住"
            onClick={() => onPanelAction({ type: 'pin', value: !panel.pinned })}
            title="钉住：切换会话时不自动收起（视图状态，不持久化）"
          >
            <Pin size={12} aria-hidden="true" />
            <span className="detail-ctrl-label">钉住</span>
          </button>
          <button
            type="button"
            className={`detail-ctrl${panel.expanded ? ' sel' : ''}`}
            aria-pressed={panel.expanded}
            aria-label="整页"
            onClick={() => onPanelAction({ type: 'expand', value: !panel.expanded })}
            title={panel.expanded ? '退回（Esc）' : '整页打开：长 trace / 大 diff 宽读'}
          >
            {panel.expanded ? <Minimize2 size={12} aria-hidden="true" /> : <Maximize2 size={12} aria-hidden="true" />}
            <span className="detail-ctrl-label">整页</span>
          </button>
          <button
            type="button"
            className="detail-ctrl"
            aria-label="关闭运行详情"
            onClick={() => dispatchPanelAction({ type: 'close' })}
            title="关闭（Esc）"
          >
            <X size={12} aria-hidden="true" />
          </button>
        </span>
      </div>

      <div className="detail-tabs" role="tablist" aria-label="运行详情视图">
        {TABS.map((t) => {
          const Icon = TAB_ICONS[t.id];
          /* UI-03：识别而非回忆——每个视图 tab 带条目计数（Overview 是摘要页无计数）。 */
          const count = tabCounts[t.id];
          return (
            <Tip
              key={t.id}
              content={`${TAB_TIPS[t.id]}${count !== undefined ? `（当前 ${count} 条）` : ''}`}
            >
              <button
                role="tab"
                aria-selected={tab === t.id}
                className={`detail-tab ${tab === t.id ? 'sel' : ''}`}
                onClick={() => setTab(t.id)}
                /* 计数徽标是 tab 的真实文本内容：窄面板 label 隐藏（容器查询
                   icon-only）后它就成了唯一可访问名，getByRole(tab, 'Timeline')
                   会变成 name '4'。aria-label 钉住语义名，不受徽标/显隐影响。 */
                aria-label={t.label}
              >
                <Icon size={13} className="detail-tab-icon" aria-hidden="true" />
                <span className="detail-tab-label">{t.label}</span>
                {count !== undefined && (
                  <span className="detail-tab-count num" aria-hidden="true">
                    {count}
                  </span>
                )}
                {/* 效果图 `.tab .alert-dot`：有待确认的约束输入时提醒（真实数据：
                    `conversation.pending_constraint_input`）。 */}
                {t.id === 'constraints' && conversation?.pending_constraint_input != null && (
                  <span className="detail-tab-alert" aria-hidden="true" />
                )}
              </button>
            </Tip>
          );
        })}
      </div>

      {/* 结构分层：header/tabs 钉在面板顶部，只有内容滚动（用户反馈 2026-09-06：
          长内容把 tabs 滚出视口后无法切换）。

          #183 AC1：**清单与选中项详情同框**。此前 `focus` 非 run 时这里早退成
          "只有详情"，清单整个消失——用户无法"在清单里移动、看详情跟随"（Timeline
          好用的原因正是清单与详情同框）。现在两者是两个独立滚动区：清单保住自己的
          滚动位置，详情实时跟着选中项换内容（组件实例不重建）。 */}
      <div className="detail-body" data-has-peek={peekVisible ? 'on' : 'off'}>
        <div className="detail-list">
          {tab === 'chat' && (
            <ChatTab
              conversation={conversation}
              tools={tools}
              onFocusTool={onFocusTool}
              onJumpToApproval={onJumpToApproval}
              selectedKey={selectedKey}
              awaitingDecision={awaitingDecision}
              goneApprovalIds={goneApprovalIds}
              onApprovalGone={onApprovalGone}
              onApprovalDecided={onApprovalDecided}
            />
          )}
          {tab === 'timeline' && (
            <TimelineTab
              key={conversation.session_id}
              conversation={conversation}
              onFocusEvent={onFocusEvent}
              onJumpToStream={onJumpToStream}
              selectedKey={selectedKey}
              expandedKeys={expandedKeys}
              onToggleExpand={toggleExpanded}
              onSelectEvent={onSelectTimelineEvent}
              filter={timelineFilter}
              onFilterChange={setTimelineFilter}
              timings={timelineTimings}
              tools={tools}
              eventKeys={timelineKeys}
              streaming={streaming}
            />
          )}
          {tab === 'changes' && <ChangesTab tools={tools} sessionId={sessionId} />}
          {tab === 'terminal' && (
            <TerminalTab tools={tools} onFocusTool={onFocusTool} selectedKey={selectedKey} />
          )}
          {tab === 'artifacts' && <ArtifactsTab tools={tools} sessionId={sessionId} />}
          {tab === 'constraints' && <ConstraintsTab sessionId={sessionId} />}
        </div>

        {/* AC2：peek 关掉只是 `hidden`（**不卸载**）——内部标签条与滚动位置留着，
            再打开是"接着看"而不是"重新开始"。 */}
        {hasSelection && (
          <section
            className="detail-peek"
            data-peek={peekVisible ? 'on' : 'off'}
            hidden={!peekVisible}
            aria-label="选中项详情"
          >
            <div className="detail-peek-head">
              <span className="panel-label">选中项详情</span>
              <span className="detail-peek-kind mono">
                {focus.kind === 'tool' ? focus.tool.name : focus.event.type}
              </span>
              <button
                type="button"
                className="detail-ctrl detail-peek-close"
                aria-label="关闭预览（保持选中）"
                title="关闭预览（Esc）"
                onClick={() => onPanelAction({ type: 'close-peek' })}
              >
                <X size={13} aria-hidden="true" />
              </button>
            </div>
            <div className="detail-peek-body">
              <EventInspector focus={focus} />
            </div>
          </section>
        )}
      </div>
    </aside>
  );
});

/** UI-03：run 分组头状态徽章文案（与 run-badge-<state> 色域一一对应）。
 *  `cancelled` = 用户取消（`run/failed.reason=cancelled`，与顶栏脉冲同口径）——
 *  缺了它这次取消会显示成「失败」（真机审计 A-02）。 */
const RUN_GROUP_STATUS_LABEL: Record<RunGroupStatus, string> = {
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
  running: '进行中',
};

// ── Chat tab：Run 级摘要（真数据区块 + 空槽标注） ──

/** TOOLS 列表尾窗（F5 #279）。50 行 ≈3.4ms——探针 `stepdetail-list-cost.perf.test.ts`
 *  实测 @50 3.4ms / @200 17.5ms（≈0.07ms/行），与 Timeline 尾窗的 ~16% 单核量级对齐；
 *  步长 200 ≈4 屏。**不复用 `TIMELINE_WINDOW_DEFAULT`**：那是事件行，高度特征与工具行不同。 */
export const TOOLS_WINDOW_DEFAULT = 50;
export const TOOLS_WINDOW_STEP = 200;


/** #1419 Overview 指标卡的采样：只从**真实事件**取，取不到就 null（不补 0）。
 *  优先 model/request（每次调用一个点，带 usage/cost/duration），退到
 *  model/completed（部分 provider 只在完成事件回填 usage）。 */
interface ModelSample {
  prompt: number | null;
  completion: number | null;
  cost: number | null;
  duration: number | null;
}

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function recordOf(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : null;
}

function modelSamples(events: readonly AgentEvent[]): ModelSample[] {
  const requests = events.filter((e) => e.type === EventType.MODEL_REQUEST);
  const source = requests.length > 0 ? requests : events.filter((e) => e.type === EventType.MODEL_COMPLETED);
  return source.map((e) => {
    const usage = recordOf(e.data.usage);
    const cost = typeof e.data.cost_usd === 'string' ? Number(e.data.cost_usd) : finite(e.data.cost_usd);
    return {
      prompt: finite(usage?.prompt_tokens),
      completion: finite(usage?.completion_tokens),
      cost: cost !== null && Number.isFinite(cost) ? cost : null,
      duration: finite(e.data.duration_ms),
    };
  });
}

/** P2-4：会话成本 = 所有 model/request 的 cost_usd 十进制相加（wire 上是字符串，
 *  走 `runBudget` 的同一份十进制实现）。任何一笔缺失/形状不合 ⇒ null（粘性不可得，
 *  绝不把缺的一笔当 0 报一个偏小的会话总额）。 */
function sessionCostText(events: readonly AgentEvent[]): string | null {
  const requests = events.filter((e) => e.type === EventType.MODEL_REQUEST);
  const source = requests.length > 0 ? requests : events.filter((e) => e.type === EventType.MODEL_COMPLETED);
  let total = '0';
  for (const e of source) {
    const raw = typeof e.data.cost_usd === 'string'
      ? e.data.cost_usd
      : typeof e.data.cost_usd === 'number' && Number.isFinite(e.data.cost_usd)
        ? String(e.data.cost_usd)
        : null;
    if (raw === null) return null;
    const next = addDecimalTexts(total, raw);
    if (next === null) return null;
    total = next;
  }
  return source.length > 0 ? total : null;
}

function seriesOf(samples: readonly ModelSample[], key: keyof ModelSample): number[] {
  return samples.flatMap((s) => (s[key] === null ? [] : [s[key] as number]));
}

/** 采样点太多时按等距抽 24 个：只影响图形密度，不改变数值口径。 */
function sparkValues(values: readonly number[]): number[] {
  if (values.length <= 24) return [...values];
  return Array.from({ length: 24 }, (_, i) => values[Math.round((i / 23) * (values.length - 1))]);
}

/* #1419 O-2：效果图的 spark 是「面积 + 折线 + 末端圆点」（`.spark .ar/.ln/.dt`），
 * 几何按 `sparkHTML` 的口径：viewBox 0 0 100 34，左右各留 2、上下各留 4，
 * 相邻点用中点做三次贝塞尔控制点（圆滑，不插值出新数据）。 */
function Sparkline({ values, label }: { values: readonly number[]; label: string }) {
  if (values.length < 2) return null;
  const points = sparkValues(values);
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const w = 100;
  const h = 34;
  const padX = 2;
  const padY = 4;
  const pts = points.map((v, i) => [
    padX + (i * (w - padX * 2)) / (points.length - 1),
    h - padY - ((v - min) / span) * (h - padY * 2),
  ]);
  let line = `M${pts[0][0].toFixed(2)} ${pts[0][1].toFixed(2)}`;
  for (let i = 1; i < pts.length; i++) {
    const [x0, y0] = pts[i - 1];
    const [x1, y1] = pts[i];
    const cx = (x0 + x1) / 2;
    line += ` C${cx.toFixed(2)} ${y0.toFixed(2)} ${cx.toFixed(2)} ${y1.toFixed(2)} ${x1.toFixed(2)} ${y1.toFixed(2)}`;
  }
  const last = pts[pts.length - 1];
  const area = `${line} L${last[0].toFixed(2)} ${h} L${pts[0][0].toFixed(2)} ${h} Z`;
  return (
    <div className="ov-spark" role="img" aria-label={label}>
      <svg viewBox="0 0 100 34" preserveAspectRatio="none">
        <path className="ov-spark-ar" d={area} />
        <path className="ov-spark-ln" d={line} />
        <circle className="ov-spark-dt" cx={last[0].toFixed(2)} cy={last[1].toFixed(2)} r="1.6" />
      </svg>
    </div>
  );
}

function MetricCard({
  label, value, unit, sub, series, icon: Icon,
}: {
  label: string;
  value: string;
  /** 单位后缀（效果图 `.v .u`：`128,940 tok`）。缺省不渲染。 */
  unit?: string;
  sub: string;
  series: readonly number[];
  icon: typeof Hash;
}) {
  return (
    <div className="ov-metric">
      <div className="ov-metric-head">
        <Icon size={9} className="ov-metric-icon" aria-hidden="true" />
        <span className="ov-metric-label">{label}</span>
      </div>
      <div className="ov-metric-value num">
        {value}
        {unit && <span className="ov-metric-unit">{unit}</span>}
      </div>
      <div className="ov-metric-sub">{sub}</div>
      {series.length >= 2 ? (
        <Sparkline values={series} label={`${label}趋势`} />
      ) : (
        <div className="ov-metric-flat" aria-hidden="true" />
      )}
    </div>
  );
}

function OverviewMetrics({
  conversation, runDuration,
}: {
  conversation: ConversationState;
  runDuration: string | null;
}) {
  const samples = useMemo(() => modelSamples(conversation.events), [conversation.eventsVersion]); // eslint-disable-line react-hooks/exhaustive-deps
  /* P2-4：四张卡的 value / label / sparkline 必须同作用域。
   *  - tokens / 成本 = **会话级**（`session_usage_total` + 当前 run 的流式累计；
   *    成本 = 全会话 model/request 十进制相加），文案写「会话累计」；
   *  - 耗时 = **本轮**（`runDuration` 来自 deriveRunSummary 的最近 run），文案写
   *    「本轮耗时」，sparkline/最长单次都只取当前 run 的样本。 */
  const usage = displayedSessionUsage(conversation);
  const prompt = usage?.prompt_tokens ?? null;
  const completion = usage?.completion_tokens ?? null;
  const costText = useMemo(
    () => sessionCostText(conversation.events),
    [conversation.eventsVersion], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const totalTokens = usage?.total_tokens ?? null;
  const perMillion = costText !== null && totalTokens !== null && totalTokens > 0
    ? `$${((Number(costText) / totalTokens) * 1_000_000).toFixed(2)} / M tok`
    : '无完整用量，无法折算';
  const sampleCount = samples.length;
  const samplesSub = sampleCount > 0 ? `会话累计 · ${sampleCount} 次模型调用` : '尚无模型调用';
  const runSamples = useMemo(() => {
    const runEvents = conversation.run_id
      ? conversation.events.filter((e) => e.run_id === conversation.run_id)
      : conversation.events;
    return modelSamples(runEvents);
  }, [conversation.eventsVersion, conversation.run_id]); // eslint-disable-line react-hooks/exhaustive-deps
  const timeSeries = seriesOf(runSamples, 'duration');
  const longest = timeSeries.length > 0 ? Math.max(...timeSeries) : null;
  return (
    <div className="ov-metrics">
      <MetricCard
        label="输入 tokens"
        icon={Hash}
        value={prompt !== null ? prompt.toLocaleString() : '—'}
        unit={prompt !== null ? 'tok' : undefined}
        sub={samplesSub}
        series={seriesOf(samples, 'prompt')}
      />
      <MetricCard
        label="输出 tokens"
        icon={Hash}
        value={completion !== null ? completion.toLocaleString() : '—'}
        unit={completion !== null ? 'tok' : undefined}
        sub={completion !== null ? samplesSub : '等待用量回填'}
        series={seriesOf(samples, 'completion')}
      />
      <MetricCard
        label="会话成本"
        icon={Database}
        value={costText !== null ? `$${costText}` : '—'}
        sub={perMillion}
        series={seriesOf(samples, 'cost')}
      />
      <MetricCard
        label="本轮耗时"
        icon={Clock}
        value={runDuration ?? '—'}
        sub={longest !== null ? `本轮最长单次 ${formatShortDuration(longest)}` : '本轮无耗时样本'}
        series={timeSeries}
      />
    </div>
  );
}

/** Overview 折叠段：内容**始终挂载**（SSR/测试与“收起不丢数据”），关闭时
 *  `inert` + aria-hidden 阻止焦点与读屏。 */
function OverviewSection({
  id, title, icon: Icon, kv, attn = false, defaultOpen = false, className, children,
}: {
  id: string;
  title: string;
  icon: typeof Hash;
  /** 段头右侧的 mono 摘要（效果图 `.acc-kv`：`13 次 · 失败 2`）。
   *  取代旧的「收起/展开」文字钮——开合态由 chevron 旋转表达。 */
  kv?: ReactNode;
  /** 效果图 `.acc.is-attn`：有待处理事项时整段琥珀描边 + 顶部琥珀渐变，
   *  让"需要你来决定"在一列灰卡里自己跳出来。 */
  attn?: boolean;
  defaultOpen?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = `ov-body-${id}`;
  return (
    <section
      className={`ov-acc${open ? ' open' : ''}${attn ? ' is-attn' : ''}${className ? ` ${className}` : ''}`}
      data-ov-section={id}
    >
      <button
        type="button"
        className="ov-acc-head"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="ov-acc-icon"><Icon size={10} aria-hidden="true" /></span>
        <span className="ov-acc-title">{title}</span>
        {kv != null && <span className="ov-acc-kv">{kv}</span>}
        <ChevronDown size={10} className="ov-acc-chev" aria-hidden="true" />
      </button>
      <div className="ov-acc-body" id={bodyId} aria-hidden={!open} inert={!open ? true : undefined}>
        <div className="ov-acc-in">{children}</div>
      </div>
    </section>
  );
}

/** #1419 O-4：工具行的 `#seq`。序号取自 `raw_call`（投影原样保存的事件负载），
 *  缺失时不画——绝不按数组下标编一个序号出来。 */
function toolSeq(t: ToolCall): number | null {
  const seq = (t.raw_call as { seq?: unknown } | undefined)?.seq;
  return typeof seq === 'number' ? seq : null;
}

export function ChatTab({
  conversation, tools, onFocusTool, onJumpToApproval, selectedKey, awaitingDecision = false,
  goneApprovalIds, onApprovalGone, onApprovalDecided,
}: {
  conversation: ConversationState;
  tools: ToolCall[];
  /** 工具行点击回调——子会话视图等只读场景缺省：行渲染为静态行（假按钮≠诚实）。 */
  onFocusTool?: (tool: ToolCall) => void;
  /** 待审批行点击 → 中间主区滚动定位到审批卡（#184）。与 `onFocusTool` 同规则：
   *  缺省时行渲染为静态行，不画一个点不动的按钮。 */
  onJumpToApproval?: (approvalId: string) => void;
  /** #183：当前选中项身份——工具行据此标 `aria-current`（清单与详情同框后，
   *  "哪一条被选中"必须在列表里可读，否则右下方详情与列表对不上号）。 */
  selectedKey?: string | null;
  /** #1030：与顶栏/Inspector 头标同源的待裁决事实（见 Props 同名注释）。 */
  awaitingDecision?: boolean;
  /** #1419 O-3：真实审批回调（见 Props 同名注释）。缺省时段内不画按钮——
   *  只读场景（子会话视图）不渲染一个点不动的审批卡。 */
  goneApprovalIds?: ReadonlySet<string>;
  onApprovalGone?: (approvalId: string) => void;
  /** 与 `Conversation` 的同名回调同签名（`useSession.resyncAfterDecision`）：
   *  决策 POST 成功后按 sessionId 对账一次，保证决策后事件有一条消费路径。 */
  onApprovalDecided?: (sessionId: string) => void;
}) {
  // Run 状态 + 时长由 lib/runState 的 deriveRunSummary 单一提供：粗标签的
  // 「取消 ≠ 失败」语义、以及「终态集合必须含 run/interrupted」这条与顶栏脉冲
  // 共用同一份判据（此处原先自己再分支一次 run_cancelled/run_status，规则一变
  // Inspector 就会与顶栏说法不一致——T8 加 run/interrupted 时正是三处集体漂移）。
  const { label: runStatusLabel, startedAt: runStart, duration: runDuration } = deriveRunSummary(conversation, awaitingDecision);
  /* F2（#272）：TOOLS 段两个计数此前每次渲染各扫一遍工具表；键 = `tools`（调用方已
   * memo）。本 tab 只在 `tab === 'chat'` 时挂载，这一层省的是驻留期间的重渲染。 */
  const runningToolCount = useMemo(() => tools.filter((t) => t.status === 'running').length, [tools]);
  const failedToolCount = useMemo(() => tools.filter((t) => t.status === 'failed').length, [tools]);
  /* #1419 O-3：工具段的「平均耗时」= 已有起止时间的调用取均值。没有一组完整时间戳
   * 就算不出来 → 显示「—」，不拿 0 冒充（零伪造指标）。 */
  const avgToolDuration = useMemo(() => {
    const spans = tools
      .map((t) => {
        const text = formatDuration(t.started_at, t.completed_at);
        return text === null ? null : t;
      })
      .filter((t): t is ToolCall => t !== null)
      .map((t) => new Date(t.completed_at!).getTime() - new Date(t.started_at!).getTime());
    if (spans.length === 0) return null;
    return spans.reduce((a, b) => a + b, 0) / spans.length;
  }, [tools]);
  /* #1419 O-1/O-3：段头右侧的 mono 摘要与「需要你来决定」的琥珀态，都由这一份视图派生。
   * 「可操作」= 投影没标 stale（run 已终结的孤儿）且 App 没记下它已 404——这两类点了
   * 只会换来一次注定失败的 POST，段内不放按钮；它们仍在 PERMISSION 段里留痕。 */
  const permView = permissionView(conversation);
  const actionableApprovals = useMemo(
    () => permView.pending.filter((a) => a.stale !== true && !(goneApprovalIds?.has(a.approval_id) ?? false)),
    [permView.pending, goneApprovalIds],
  );
  const gaps = useMemo(() => seqGaps(conversation.events), [conversation.eventsVersion]); // eslint-disable-line react-hooks/exhaustive-deps
  const compactionCount = conversation.compactions.length;

  /* F5（#279）TOOLS 尾窗：此前全量渲染（N=500 单次 38.7ms，40fps 合帧下就是满载）。
   * 选中项必须留在窗口里——同 `TimelineTab` 的 `effectiveWindow`（#183）：键盘 ↑/↓ 的移动域
   * 是**全部**工具（见 `listTargets`），列表却只画尾窗，不派生就会「选中了却看不见」。 */
  const [toolWindow, setToolWindow] = useState(TOOLS_WINDOW_DEFAULT);
  const selectedToolIndex = useMemo(() => {
    if (!selectedKey) return -1;
    return tools.findIndex((t) => toolKey(t) === selectedKey);
  }, [tools, selectedKey]);
  const effectiveToolWindow =
    selectedToolIndex >= 0 ? Math.max(toolWindow, tools.length - selectedToolIndex) : toolWindow;
  const hiddenTools = Math.max(0, tools.length - effectiveToolWindow);
  const visibleTools = hiddenTools > 0 ? tools.slice(hiddenTools) : tools;

  return (
    <>
      <OverviewMetrics conversation={conversation} runDuration={runDuration} />
      <div className="ov-acc-list">
      {/* #1419 O-3：效果图把「需要你来决定」放在第一位——它是有待办时的唯一入口，
          埋在一列灰卡中间就等于没有。琥珀态由 `actionableApprovals` 驱动。 */}
      <OverviewSection
        id="decide"
        title="需要你来决定"
        icon={AlertTriangle}
        attn={actionableApprovals.length > 0}
        defaultOpen
        kv={
          actionableApprovals.length > 0
            ? <><b>{actionableApprovals.length}</b> 项待审批</>
            : '已处理'
        }
      >
      {actionableApprovals.length > 0 && (
        <div className="detail-section">
          {onApprovalGone && onApprovalDecided ? (
            <>
              {/* 真实审批卡（#37 的同一个组件）：三个按钮（批准 / 拒绝 / 以后都允许）
                  走既有 postApproval 通路与既有 409/404 语义，不在这里造第二条提交路径。
                  `autoFocus` 必须为 false——全局 Ctrl+Enter 只该由中心列那一张卡挂载，
                  否则一次按键会向同一个 approval_id 发两次 POST。 */}
              <ApprovalCard
                sessionId={conversation.session_id}
                approval={actionableApprovals[0]}
                onGone={() => onApprovalGone(actionableApprovals[0].approval_id)}
                onDecided={() => onApprovalDecided(conversation.session_id)}
              />
              {actionableApprovals.length > 1 && (
                <div className="ov-empty">
                  <AlertTriangle size={11} aria-hidden="true" />
                  另有 {actionableApprovals.length - 1} 项待审批 · 逐条在会话里处理
                </div>
              )}
            </>
          ) : (
            /* 只读场景（子会话视图）：不给假按钮，如实说明去哪儿处理。 */
            <div className="ov-empty">
              <AlertTriangle size={11} aria-hidden="true" />
              {actionableApprovals.length} 项待审批 · 请在主会话里处理
            </div>
          )}
        </div>
      )}
      {conversation.reconcile_queue.length > 0 ? (
        <div className="detail-section">
          <div className="detail-section-title">
            <AlertTriangle size={14} /> 需要你来决定
          </div>
          {conversation.reconcile_queue.map((r, i) => (
            <div key={i} className="detail-reconcile-row">
              <div className="detail-row">
                <span className="detail-key">工具</span>
                <span className="detail-val">{r.tool_name}</span>
              </div>
              <div className="detail-row">
                <span className="detail-key">参数身份</span>
                <code className="detail-val detail-val-mono">{r.args_identity}</code>
              </div>
              <div className="detail-row">
                <span className="detail-key">状态</span>
                <span className="detail-val detail-val-tag">{r.state}</span>
              </div>
            </div>
          ))}
        </div>
      ) : (
        actionableApprovals.length === 0 && (
          <div className="detail-empty-hint">无待裁决操作</div>
        )
      )}
      </OverviewSection>

      <OverviewSection
        id="run"
        title="任务 RUN"
        icon={Hash}
        defaultOpen
        kv={<>{runStatusLabel} · 第 {conversation.turns.length} 轮</>}
      >
      <div className="detail-section">
        <div className="detail-section-title">
          <Hash size={14} /> 任务
        </div>
        <div className="detail-row">
          <span className="detail-key">任务</span>
          <code className="detail-val detail-val-mono">{conversation.run_id ?? '—'}</code>
        </div>
        <div className="detail-row">
          <span className="detail-key">会话</span>
          <code className="detail-val detail-val-mono">{conversation.session_id.slice(0, 16)}</code>
        </div>
        <div className="detail-row">
          <span className="detail-key">状态</span>
          <span className="detail-val">{runStatusLabel}</span>
        </div>
        {/* #220：失败归因紧跟在「状态」下面——只渲染后端**给了**的部分，绝不编文案。
            三态（文案+码 / 只有码 / 都没有）与失效规则见 ADR-0033 §2.2-2.3。 */}
        {conversation.run_failure &&
          (conversation.run_failure.message || conversation.run_failure.reason) && (
            <div className="detail-row detail-row-warn">
              <span className="detail-key">失败原因</span>
              {/* 文案是完整句子（30-45 字），而 `.detail-val` 是 nowrap+ellipsis——这行必须
                  换行（run-failure-val）才能让「请到供应商控制台检查计费与配额」这类可操作
                  尾巴真的可见；title 再兜一层鼠标可达的全文。 */}
              <span
                className="detail-val run-failure-val"
                title={conversation.run_failure.message ?? conversation.run_failure.reason ?? ''}
              >
                {/* #222 真机实测：后端只给码（无文案）时这一行渲染成**空值**——码原本只在
                    `message` 同时存在时才缀出来，而 Timeline 摘要（summarizeRunFailed）早
                    就用码兜底了：同一事实两个口径。这里对齐它的规则：没有文案时，码就是这
                    一行的唯一信息。 */}
                {conversation.run_failure.message ?? (
                  <code className="detail-val-mono">{conversation.run_failure.reason}</code>
                )}
                {/* 有文案时把分类码缀在后面（等宽、次级）；没有文案时它就是唯一信息，不再重复 */}
                {conversation.run_failure.message && conversation.run_failure.reason && (
                  <>
                    {' '}
                    <code className="detail-val-mono">{conversation.run_failure.reason}</code>
                  </>
                )}
              </span>
            </div>
          )}
        <div className="detail-row">
          <span className="detail-key">轮数</span>
          <span className="detail-val">{conversation.turns.length}</span>
        </div>
        {runStart && (
          <div className="detail-row">
            <span className="detail-key">开始</span>
            <span className="detail-val">{formatDateTime(runStart) ?? runStart}</span>
          </div>
        )}
        <div className="detail-row">
          <span className="detail-key">耗时</span>
          <span className="detail-val">{runDuration ?? '—'}</span>
        </div>
        {/* #1419 O-5：fallback 胶囊——只有真的发生过切换才出现（`model_fallback`
            由 `model/fallback` 事件写入），没有这条事实就不画徽标。 */}
        {conversation.model_fallback && (
          <div className="detail-row">
            <span className="detail-key">模型切换</span>
            <span className="detail-val">
              <span className="ov-badge">
                fallback → {conversation.model_fallback.to_model}
              </span>
            </span>
          </div>
        )}
        <div className="detail-row">
          <span className="detail-key">字数</span>
          <span className="detail-val num">
            {conversation.usage_total && conversation.usage_total.total_tokens !== null
              ? conversation.usage_total.total_tokens.toLocaleString()
              : <span className="detail-val-muted">—</span>}
          </span>
        </div>
        {/* Langfuse trace 关联（ADR-0018 D7 + 契约 2d7f87a）：
            trace_url 有值 → trace_id 渲染为可点超链接直达 Langfuse dashboard；
            trace_id 有值但 trace_url 缺 → 纯 mono code（可复制，手动粘到 Langfuse 搜索）；
            两者都 null（Langfuse 未启用）→「未追踪」灰字（预期降级非故障）。 */}
        <div className="detail-row">
          <span className="detail-key">追踪</span>
          {conversation.trace_id ? (
            conversation.trace_url ? (
              <a
                className="detail-val detail-val-mono detail-trace-link"
                href={conversation.trace_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                {conversation.trace_id}
              </a>
            ) : (
              <code className="detail-val detail-val-mono">{conversation.trace_id}</code>
            )
          ) : (
            <span className="detail-val detail-val-muted">未追踪</span>
          )}
        </div>
        {/* #1419 O-5：健康注。效果图写「上下文健康 · 未触发压缩」——后端不提供窗口
            容量，所以这里只报**能证明的那一半**：压缩发生了几次。没有容量就没有百分比，
            不编一个。 */}
        <div className="ov-empty">
          <Check size={11} aria-hidden="true" />
          {compactionCount === 0
            ? '上下文健康 · 未触发压缩'
            : `已压缩 ${compactionCount} 次`}
        </div>
      </div>
      </OverviewSection>

      <OverviewSection
        id="tools"
        title="工具调用"
        icon={Package}
        defaultOpen
        kv={<><b>{tools.length}</b> 次 · 失败 {failedToolCount}</>}
      >
      <div className="detail-section">
        <div className="detail-section-title">
          <Package size={14} /> TOOLS
        </div>
        {/* #1419 O-4：效果图 2×2 统计格（总调用 / 失败 / 平均 / 进行中）。四项都是
            现有投影上的真实计数；「平均」没有完整时间戳时显示「—」而不是 0。 */}
        <div className="ov-pair">
          <div className="ov-row"><span className="k">总调用</span><span className="grow" /><span className="v">{tools.length}</span></div>
          <div className="ov-row"><span className="k">失败</span><span className="grow" /><span className="v">{failedToolCount}</span></div>
          <div className="ov-row"><span className="k">平均</span><span className="grow" /><span className="v">{avgToolDuration === null ? '—' : formatShortDuration(avgToolDuration)}</span></div>
          <div className="ov-row"><span className="k">进行中</span><span className="grow" /><span className="v">{runningToolCount}</span></div>
        </div>
        {hiddenTools > 0 && (
          /* 窗口条直接复用 Timeline 的类：`app.css` 里 `.timeline-window-bar/-earlier/-hint`
             都是无前缀的全局选择器，F5 因此**不需要动样式**（票面禁止改 app.css）。 */
          <div className="timeline-window-bar">
            <button
              className="timeline-earlier"
              onClick={() => setToolWindow(effectiveToolWindow + TOOLS_WINDOW_STEP)}
            >
              加载更早 {Math.min(TOOLS_WINDOW_STEP, hiddenTools)} 条
            </button>
            <span className="timeline-window-hint">
              显示最近 {visibleTools.length} / 共 {tools.length} 条（前段已折叠，可展开查看）
            </span>
          </div>
        )}
        {visibleTools.map((t) => {
          const selected = selectedKey != null && toolKey(t) === selectedKey;
          const rowClass = `detail-tool-row${selected ? ' sel' : ''}`;
          return onFocusTool ? (
            <button
              key={t.tool_call_id}
              className={rowClass}
              data-list-key={toolKey(t)}
              aria-current={selected ? 'true' : undefined}
              onClick={() => onFocusTool(t)}
            >
              <ChevronRight size={14} />
              <span className={`tool-status-dot tool-status-dot-${t.status}`} />
              {toolSeq(t) !== null && <span className="k">#{String(toolSeq(t)).padStart(2, '0')}</span>}
              <span className="detail-tool-name">{t.name}</span>
              {formatDuration(t.started_at, t.completed_at) !== null && (
                <span className="v">{formatDuration(t.started_at, t.completed_at)}</span>
              )}
            </button>
          ) : (
            <div key={t.tool_call_id} className={`${rowClass} detail-tool-row-static`}>
              <span className={`tool-status-dot tool-status-dot-${t.status}`} />
              {toolSeq(t) !== null && <span className="k">#{String(toolSeq(t)).padStart(2, '0')}</span>}
              <span className="detail-tool-name">{t.name}</span>
              {formatDuration(t.started_at, t.completed_at) !== null && (
                <span className="v">{formatDuration(t.started_at, t.completed_at)}</span>
              )}
            </div>
          );
        })}
      </div>
      </OverviewSection>

      <OverviewSection
        id="context"
        title="上下文"
        icon={Layers}
        kv={compactionCount > 0 ? `${compactionCount} 次压缩` : '未压缩'}
      >
      {conversation.compactions.length > 0 ? (
        <div className="detail-section">
          <div className="detail-section-title">
            <Layers size={14} /> CONTEXT
          </div>
          {conversation.compactions.map((c, i) => (
            <div key={i} className="detail-compaction-row">
              <div className="detail-row">
                <span className="detail-key">精简轮数</span>
                <span className="detail-val">{c.compacted_turn_count}</span>
              </div>
              <div className="detail-row">
                <span className="detail-key">字数估算</span>
                <span className="detail-val">{c.token_estimate.toLocaleString()}</span>
              </div>
              {c.fallback_used && (
                <div className="detail-row detail-row-warn">
                  <AlertTriangle size={14} /> <span>兜底降级（非模型摘要）</span>
                </div>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="detail-empty-hint">暂无上下文精简记录</div>
      )}
      </OverviewSection>

      <OverviewSection
        id="trace"
        title="事件轨迹"
        icon={ListTree}
        kv={<><b>{conversation.events.length}</b> 事件{gaps.length > 0 ? ` · ${gaps.length} 处缺口` : ''}</>}
      >
      <div className="detail-section">
        <div className="detail-section-title">
          <ListTree size={14} /> TRACE
        </div>
        <div className="detail-row">
          <span className="detail-key">事件数</span>
          <span className="detail-val">{conversation.events.length}</span>
        </div>
        {conversation.unknown_events.length > 0 && (
          <div className="detail-row detail-row-warn">
            <span className="detail-key">未知事件</span>
            <span className="detail-val">{conversation.unknown_events.length}</span>
          </div>
        )}
        {gaps.map((gap, i) => (
          <div key={i} className="detail-row detail-row-warn">
            <span className="detail-key">seq 缺口</span>
            <span className="detail-val detail-val-mono">{gap}</span>
          </div>
        ))}
      </div>
      </OverviewSection>

      <OverviewSection id="model" title="模型" icon={Database} kv={conversation.model ?? conversation.requested_model ?? '—'}>
      {/* MODEL：后端 Gap 1 已落地（model/completed + run/completed 观测字段）。
          全部缺失时保留空槽语义（提示而非留白），有任一真值则逐行渲染，
          缺失行显示「—」——绝不伪造 0（零伪造指标冻结决策）。
          `requested_model`（#226）也算「有真值」：provider 不回显时它是这一节
          唯一的模型事实，此时若还显示空槽提示，用户会以为连请求都没发出去。
          两个模型值的分工与「非本轮」标记的口径见 ADR-0034 §2.3。 */}
      {conversation.model === null && conversation.requested_model === null
        && conversation.usage_total === null && conversation.cost_usd === null ? (
        <div className="detail-section detail-reserved">
          <div className="detail-section-title">
            <Database size={14} /> MODEL
          </div>
          <div className="detail-empty-hint">暂无观测数据（等待 model/completed）</div>
        </div>
      ) : (
        <div className="detail-section">
          <div className="detail-section-title">
            <Database size={14} /> MODEL
          </div>
          <div className="detail-row">
            <span className="detail-key">模型</span>
            <span
              className="detail-val detail-val-mono"
              title="会话内最近一次记录的模型名——写者有四个（provider 在响应里回显 / 模型 fallback 切换 / 会话级模型切换 / 重放重建），因此它**可能是上一轮的值**；不是本轮写入时其后缀标注「非本轮」。口径见 docs/adr/0034-request-side-model-identity.md §2.3"
            >
              {conversation.model ?? '—'}
              {/* #226 review（两轴共识）：`model` 是**会话最新**、不随新 run 失效，而
                  `请求模型` 是**本轮**的。不标出来的话，"模型 A / 请求模型 B" 会被读成
                  "请求了 B 却回显 A（provider 无视请求）"，而 A 只是上一轮的值。 */}
              {conversation.model !== null && conversation.model_run_id !== conversation.run_id && (
                <span className="detail-val-muted">（非本轮）</span>
              )}
            </span>
          </div>
          {/* 只在**提供信息**时出现：请求与回显不同，或回显缺失。两者一致时
              重复一行是无信息量的噪音。 */}
          {conversation.requested_model !== null
            && conversation.requested_model !== conversation.model && (
            <div className="detail-row">
              <span className="detail-key">请求模型</span>
              <span
                className="detail-val detail-val-mono"
                title="本轮请求侧的模型标识：任务开始事件携带的 model（持久事件）= 装配给模型服务商的 model 值。本轮若一次模型调用都没发生，它仍是配置意图、不代表已发出去"
              >
                {conversation.requested_model}
              </span>
            </div>
          )}
          {conversation.model_fallback && (
            <div className="detail-row">
              <span className="detail-key">已切换</span>
              <span className="detail-val detail-val-mono model-fallback-val" title="model/fallback：主模型失稳后切换">
                {conversation.model_fallback.from_model} → {conversation.model_fallback.to_model}
                {conversation.model_fallback.reason ? ` · ${conversation.model_fallback.reason}` : ''}
              </span>
            </div>
          )}
          <div className="detail-row">
            <span className="detail-key">用量</span>
            <span className="detail-val">
              {conversation.usage_total
                ? `${conversation.usage_total.total_tokens?.toLocaleString() ?? '—'} 字（${conversation.usage_total.prompt_tokens?.toLocaleString() ?? '—'} + ${conversation.usage_total.completion_tokens?.toLocaleString() ?? '—'}）`
                : '—'}
            </span>
          </div>
          <div className="detail-row">
            <span className="detail-key">成本</span>
            <span className="detail-val">{conversation.cost_usd !== null ? `$${conversation.cost_usd}` : '—'}</span>
          </div>
        </div>
      )}
      </OverviewSection>

      <OverviewSection id="checkpoint" title="检查点" icon={Database} kv="无 API">
      <div className="detail-section detail-reserved">
        <div className="detail-section-title">
          <Database size={14} /> CHECKPOINT
        </div>
        <div className="detail-empty-hint">后端未暴露（无 API，集成阶段处理）</div>
      </div>
      </OverviewSection>

      <OverviewSection
        id="permission"
        title="权限"
        icon={ShieldCheck}
        defaultOpen
        kv={<>待审批 {permView.pending.length}</>}
      >
      <PermissionSection conversation={conversation} onJumpToApproval={onJumpToApproval} />
      </OverviewSection>
      </div>
    </>
  );
}

/** PERMISSION 段（#184，PRD §12）。
 *
 *  数据全部来自事件流投影（`tool/approval-requested` → 队列；`permission/resolved`
 *  → 裁决留痕），**没有新 API**。段里两个"档"是两件事：`session_permission_mode`
 *  （会话当下生效档；创建时声明、F18-B #283 起会话内可改）与 `permission_policy`
 *  （审批真发生那一刻实际用的阈值）。
 *  本段显示的是**后者**——它是运行时证据，会话无审批事件就无从得知，如实显示 `—` +
 *  说明原因（不拿前者冒充；#236 更正了此处"`permission_mode` 不在任何事件里"的旧前提）。
 *  整段措辞都在 `lib/permission.ts` 里定，这里只接线。
 *
 *  两处刻意的"不消失"：零待审批 → 显示「无待审批」；零裁决 → 「尚无裁决」。段本身
 *  永远渲染（除非 conversation 为空）——段消失会被读成"这个会话没有权限概念"。 */
function PermissionSection({
  conversation, onJumpToApproval,
}: {
  conversation: ConversationState;
  onJumpToApproval?: (approvalId: string) => void;
}) {
  const view = permissionView(conversation);
  return (
    <div className="detail-section" data-section="permission">
      <div className="detail-section-title">
        <ShieldCheck size={14} /> PERMISSION
      </div>
      <div className="detail-row">
        <span className="detail-key">权限档</span>
        <span className="detail-val detail-val-mono">
          {view.policy ?? <span className="detail-val-muted">—</span>}
        </span>
      </div>
      {view.policy === null && (
        // AC4：拿不到就说明为什么——「—」不带理由会被读成"没有权限约束"。
        <div className="detail-empty-hint">本会话无审批事件，生效阈值无从得知</div>
      )}
      <div className="detail-row">
        <span className="detail-key">待审批</span>
        {/* 零待审批 → 明说（AC2），但仍用静音色：这是"没有"，不是计数 0。 */}
        <span className={`detail-val${view.pending.length === 0 ? ' detail-val-muted' : ''}`}>
          {view.pendingLabel}
        </span>
      </div>
      {view.pending.map((a) =>
        onJumpToApproval ? (
          <button
            key={a.approval_id}
            className="detail-permission-row"
            onClick={() => onJumpToApproval(a.approval_id)}
          >
            <ChevronRight size={14} />
            <span className="detail-tool-name">{a.tool_name || '—'}</span>
            <span className="detail-permission-action">{a.action_type || '—'}</span>
            {/* 失效是事实，不是错误：run 已终结 → 决策永不可能再提交（APR-01）。 */}
            {a.stale === true && <span className="detail-val-tag">已失效</span>}
          </button>
        ) : (
          <div key={a.approval_id} className="detail-permission-row detail-permission-row-static">
            <span className="detail-tool-name">{a.tool_name || '—'}</span>
            <span className="detail-permission-action">{a.action_type || '—'}</span>
            {a.stale === true && <span className="detail-val-tag">已失效</span>}
          </div>
        ),
      )}
      <div className="detail-row">
        <span className="detail-key">已处理</span>
        <span className={`detail-val${view.decisions.length === 0 ? ' detail-val-muted' : ''}`}>
          {view.decisionsLabel}
        </span>
      </div>
      {view.decisions.map((d) => (
        <div key={d.approval_id} className="detail-permission-decision">
          <span className={`detail-val-tag permission-verdict-${d.tone}`}>{d.verdict}</span>
          <span className="detail-tool-name">{d.toolName}</span>
          {d.reason && <span className="detail-permission-reason" title={d.reason}>{d.reason}</span>}
        </div>
      ))}
    </div>
  );
}

// ── Timeline tab：事件真序日志（真相源 conversation.events，零过滤） ──

/** 尾窗默认大小 / 「加载更早」步长（P1-4）。200 行 ≈4ms 全量渲染（实测），
 * 40fps 合帧下余量充足；步长 500 一次多翻约 2.5 屏。 */
export const TIMELINE_WINDOW_DEFAULT = 200;
export const TIMELINE_WINDOW_STEP = 500;

/** seq 跳转检测（Inspector Scope "TRACE 事件计数 + seq 跳转"）：
 *  返回相邻可比较 seq 对之间的缺口描述（"12 → 15"），不可比较（null/乱序）则跳过。 */
export function seqGaps(events: AgentEvent[]): string[] {
  const gaps: string[] = [];
  let prev: number | null = null;
  for (const e of events) {
    if (e.seq === null) continue;
    if (prev !== null && e.seq > prev + 1) gaps.push(`${prev} → ${e.seq}`);
    prev = Math.max(prev ?? e.seq, e.seq);
  }
  return gaps;
}

/** 事件行的单行摘要——单一投影源（lib/projection.ts summarizeEvent）。 */
const eventSummary = summarizeEvent;

/** 类型图标（P3-6）：定稿原型 `.fc-lead` 是 20px 图标盒，不是 5px 类别点。
 *
 *  #1419 perf：图标是每行 SSR 成本最敏感的一处——探针口径下每多一个元素约
 *  +0.9ms/200 行。实测路径：lucide 组件 22.3ms → 逐行内联 SVG 13.1ms →
 *  `<symbol>`+`<use>` 12.9ms → **零 DOM 节点（本形态）**。图标整体下沉到 CSS：
 *  20px 盒 = `.timeline-row::before`，11px 图标 = `.tl-dur::before` 的
 *  `mask-image`（形状数据只存在于 app.css，逐字取自 lucide-react@1.40.0，ISC；
 *  颜色由 `--tl-icon-color` 给）。几何不变：20px 盒 − 2×1px 边框 − 2×3.5px
 *  内边距 = 11px 视口，与改造前 `size={11}` 一致。 */

/** P3-2：投影对 run/model 类事件没有单行摘要（`emptySummary`），Timeline 卡片
 *  标题不能因此退回原始类型串。只补这几类**可读**标题，其余仍按投影口径回退
 *  到类型名（未知事件保留原始类型是设计选择）。 */
function timelineTitle(event: AgentEvent, call?: ToolCall | null): string {
  const headline = timelineHeadline(event, call);
  if (headline.title) return headline.title;
  const summary = eventSummary(event);
  if (summary) return summary;
  const model = typeof event.data.model === 'string' && event.data.model ? event.data.model : null;
  switch (event.type) {
    case EventType.SESSION_STARTED:
      return '会话开始';
    case EventType.SESSION_RESUMED:
      return '会话恢复';
    case EventType.RUN_STARTED:
      return model ? `任务开始 · ${model}` : '任务开始';
    case EventType.MODEL_STARTED:
      return model ? `模型开始生成 · ${model}` : '模型开始生成';
    case EventType.MODEL_REQUEST:
      return model ? `模型请求 · ${model}` : '模型请求';
    case EventType.MODEL_REQUEST_STARTED:
      return model ? `模型请求开始 · ${model}` : '模型请求开始';
    case EventType.MODEL_FAILED:
      return '模型调用失败';
    case EventType.USER_INPUT_REQUESTED:
      return '等待用户输入';
    default:
      return event.type;
  }
}

/** Timeline 行 hover 浮层内容（C4）：完整时间戳（含毫秒，本地时区）+ step。
 *  纯函数导出以便 SSR 测试锁定；行内已显示 seq/type，浮层只补看不到的。
 *  step_id 语义：**缺失或 null** 都 = 无归属（后端省略值为 null 的字段 → 键缺失；
 *  recover 合成事件等用 null 哨兵），不渲染该行；数值（含 0，后端从 1 起但类型
 *  契约为 number）按合法 step 渲染。判空必须宽松——严格 `!== null` 会把缺失的键
 *  渲染成字面量 "step undefined"（BUG-008）。 */
export function formatEventTooltip(e: AgentEvent): string[] {
  const lines: string[] = [];
  const ts = formatTimestamp(e.time);
  if (ts) lines.push(ts);
  if (e.step_id != null) lines.push(`step ${e.step_id}`);
  return lines;
}

interface TipState {
  x: number;
  y: number;
  /** 事件标题（人话模板）——T-11 富信息浮层的首行。 */
  title: string;
  /** `#seq · type · 时刻`（mono 副行）。 */
  meta: string;
  /** 投影摘要；无摘要时不渲染该行。 */
  summary: string | null;
  /** 既有行（完整时间戳 + step，`formatEventTooltip` 口径）。 */
  lines: string[];
}

export function TimelineTab({
  conversation,
  onFocusEvent,
  onJumpToStream,
  selectedKey,
  expandedKeys,
  onToggleExpand,
  onSelectEvent,
  filter,
  onFilterChange,
  timings,
  tools = EMPTY_TOOLS,
  eventKeys,
  streaming = false,
}: {
  conversation: ConversationState;
  onFocusEvent: (e: AgentEvent) => void;
  onJumpToStream?: (e: AgentEvent) => void;
  selectedKey?: string | null;
  /** #1419：受控展开集合（面板级）；独立 SSR 渲染时可省略 → 组件内部自管。 */
  expandedKeys?: ReadonlySet<string>;
  onToggleExpand?: (key: string) => void;
  onSelectEvent?: (e: AgentEvent) => void;
  filter?: TimelineFilter;
  onFilterChange?: (f: TimelineFilter) => void;
  timings?: ReadonlyMap<string, EventTiming>;
  tools?: readonly ToolCall[];
  /** #1419 P2-1：事件对象 → Timeline 行身份（seq-less 同键事件带序号）。
   *  省略时退回 `eventKey`（独立渲染/旧测试兼容）。 */
  eventKeys?: ReadonlyMap<AgentEvent, string>;
  /** 底部状态条的同步态（`已同步` / `跟随中`）；独立渲染默认终态。 */
  streaming?: boolean;
}) {
  // 尾窗裁剪（P1-4，DSH "cropped client views"）：真相全量留在 conversation.events
  // （不变量 #22 不动），视图只渲染最近窗口。实测依据：2k 全量渲染 40ms、20k 359ms
  // （流式合帧 40fps 下 Timeline tab 每秒烧 14s CPU）——200 行窗口 ≈4ms，流畅。
  // #1419：筛选先于窗口——筛选结果再走 200 行窗口，展开卡片只在已渲染行内挂载。
  const total = conversation.events.length;
  const [windowSize, setWindowSize] = useState(TIMELINE_WINDOW_DEFAULT);
  const [localFilter, setLocalFilter] = useState<TimelineFilter>('all');
  const [localExpanded, setLocalExpanded] = useState<ReadonlySet<string>>(EMPTY_KEYS);
  const [flashKey, setFlashKey] = useState<string | null>(null);
  // C4：hover 时间戳浮层——单元素 fixed 浮层 + 容器事件委托（零每行 handler）。
  const [tip, setTip] = useState<TipState | null>(null);
  const visibleRef = useRef<Map<string, AgentEvent>>(new Map());
  const lastRowRef = useRef<HTMLElement | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const activeFilter = filter ?? localFilter;
  const setFilter = onFilterChange ?? setLocalFilter;
  const activeExpanded = expandedKeys ?? localExpanded;
  const keyOf = useCallback((e: AgentEvent) => eventKeys?.get(e) ?? eventKey(e), [eventKeys]);
  const toggleExpanded = onToggleExpand ?? ((key: string) => {
    setLocalExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else {
        next.add(key);
        while (next.size > MAX_EXPANDED_CARDS) {
          const oldest = next.values().next().value;
          if (oldest === undefined) break;
          next.delete(oldest);
        }
      }
      return next;
    });
  });
  const timingsMap = useMemo(
    () => timings ?? deriveEventTimings(conversation.events, tools),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [timings, conversation.eventsVersion, tools],
  );
  /* T-8：TOOL_RESULT 行标题要复用 call 的模板（result 自身内容只有 `ok`）。
   * 按 tool_call_id 建索引；`tools` 引用稳定（上游 memo），索引跟着它重建。 */
  const callById = useMemo(
    () => new Map(tools.map((t) => [t.tool_call_id, t])),
    [tools],
  );

  /* run 分组派生（N2 #271）：键用 `eventsVersion`——写 `events` 不是"省一次重算"，而是
   * **永不重算**（组头不出现、计数不涨，是正确性缺陷）。契约与代价见 ADR-0037 D3 / D5.4；
   * 豁免形式见上方 `runIdList` / `listTargets`。 */
  const runGroups = useMemo(() => groupEventsByRun(conversation.events), [conversation.eventsVersion]); // eslint-disable-line react-hooks/exhaustive-deps
  const counts = useMemo(
    () => filterCounts(conversation.events, timingsMap),
    [conversation.eventsVersion, timingsMap], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const axis = useMemo(() => timelineAxis(timingsMap), [timingsMap]);
  const gapByIndex = useMemo(() => {
    const map = new Map<number, SeqGap>();
    for (const gap of seqGapMarkers(conversation.events)) map.set(gap.afterIndex, gap);
    return map;
  }, [conversation.eventsVersion]); // eslint-disable-line react-hooks/exhaustive-deps

  const filtered = useMemo(
    () => conversation.events
      .map((e, i) => ({ e, i }))
      .filter(({ e }) => eventMatchesFilter(e, activeFilter, timingsMap.get(eventKey(e)))),
    [conversation.eventsVersion, activeFilter, timingsMap], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const selectedFilteredIndex = selectedKey
    ? filtered.findIndex(({ e }) => keyOf(e) === selectedKey)
    : -1;
  /* #183：键盘选中项必须在**渲染窗口**里，否则"选中了却看不见"（↑/↓ 到窗口外
   * 就停在原地）。窗口下界由选中项**派生**（`effectiveWindow`），不在 effect 里
   * setState：那是纯函数关系，用 effect 追会多一轮渲染（oxlint
   * `react(set-state-in-effect)` 说的就是这条）。`windowSize` 只保存用户手动
   * "加载更早"的意图，两者取大——精确到 `filtered.length - selectedFilteredIndex`，
   * 不是一把拉到全量（全量渲染正是 200 行窗口要避免的成本，见文件头的实测）。 */
  const effectiveWindow =
    selectedFilteredIndex >= 0 ? Math.max(windowSize, filtered.length - selectedFilteredIndex) : windowSize;
  const hidden = Math.max(0, filtered.length - effectiveWindow);
  const visible = hidden > 0 ? filtered.slice(hidden) : filtered;
  visibleRef.current = new Map(visible.map((v) => [keyOf(v.e), v.e]));

  /* 选中项滚进视野：`nearest` 保证已经在视野里时**不动**（否则每次 ↑/↓ 都会把
     列表拉到中间，滚动位置就"丢"了——AC1 要的正是滚动位置不被选中的副作用骚扰）。 */
  useEffect(() => {
    if (!selectedKey) return;
    listRef.current
      ?.querySelector<HTMLElement>(`[data-list-key="${selectedKey}"]`)
      ?.scrollIntoView({ block: 'nearest' });
  }, [selectedKey]);

  /* 跳转实时流：先把窗口扩到目标行，再滚到它并闪一下；不修改选中态（那是 App 的
   * focus，Timeline 不越权）。`flashKey` 的清理在 effect 里，避免渲染期定时器。 */
  const liveEvent = useMemo(() => {
    for (let i = conversation.events.length - 1; i >= 0; i--) {
      const e = conversation.events[i];
      if (timingsMap.get(eventKey(e))?.status === 'run') return e;
    }
    if (conversation.run_status === 'running') return conversation.events[conversation.events.length - 1] ?? null;
    return null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation.eventsVersion, conversation.run_status, timingsMap]);
  useEffect(() => {
    if (!flashKey) return;
    const el = listRef.current?.querySelector<HTMLElement>(`[data-list-key="${flashKey}"]`);
    el?.scrollIntoView({ block: 'center' });
    const timer = window.setTimeout(() => setFlashKey(null), 1200);
    return () => window.clearTimeout(timer);
  }, [flashKey]);
  const jumpToLive = () => {
    if (!liveEvent) return;
    const abs = conversation.events.indexOf(liveEvent);
    if (abs < 0) return;
    setWindowSize((w) => Math.max(w, total - abs));
    if (!eventMatchesFilter(liveEvent, activeFilter, timingsMap.get(eventKey(liveEvent)))) setFilter('all');
    setFlashKey(keyOf(liveEvent));
  };

  // 滚动/缩放即隐藏（fixed 定位不随容器滚动，留着会错位）。
  useEffect(() => {
    const hide = () => {
      lastRowRef.current = null;
      setTip(null);
    };
    window.addEventListener('scroll', hide, true);
    window.addEventListener('resize', hide);
    return () => {
      window.removeEventListener('scroll', hide, true);
      window.removeEventListener('resize', hide);
    };
  }, []);

  const handleOver = (e: ReactMouseEvent<HTMLDivElement>) => {
    /* P1-1：卡片行的身份属性是 `data-list-key`（旧行才有 `data-tl-i`）；
     * 用错选择器会让 tip 永远为 null，hover 浮层整条功能不可达。 */
    const btn = (e.target as HTMLElement).closest?.('[data-list-key]') as HTMLElement | null;
    if (btn === lastRowRef.current) return;
    lastRowRef.current = btn;
    if (!btn) {
      setTip(null);
      return;
    }
    const ev = btn.dataset.listKey ? visibleRef.current.get(btn.dataset.listKey) : undefined;
    const lines = ev ? formatEventTooltip(ev) : [];
    if (!ev) {
      setTip(null);
      return;
    }
    /* T-11 富信息：标题 / `#seq · type · 时刻` / 摘要 + 既有时间戳与 step 行。
     * 行数参与定位高度计算（TIP_LINE_H 每行）。 */
    const timing = timingsMap.get(eventKey(ev));
    const meta = [
      ev.seq != null ? `#${ev.seq}` : null,
      ev.type,
      formatClock(ev.time),
      timing?.durationMs != null ? formatShortDuration(timing.durationMs) : null,
    ].filter((part): part is string => Boolean(part)).join(' · ');
    const summary = eventSummary(ev) || null;
    const rows = 1 + 1 + (summary ? 1 : 0) + lines.length;
    const r = btn.getBoundingClientRect();
    const tipH = rows * TIP_LINE_H + TIP_PADDING;
    const above = r.top > tipH + TIP_TOP_MARGIN;
    setTip({
      x: Math.max(8, r.left + 6),
      y: above ? r.top - tipH - TIP_GAP : r.bottom + TIP_GAP,
      title: timelineTitle(ev),
      meta,
      summary,
      lines,
    });
  };
  const handleLeave = () => {
    lastRowRef.current = null;
    setTip(null);
  };

  /* P4-1：fallback 也要保持引用稳定——否则 standalone 渲染时每次提交都换新
   * 回调，`TimelineCard` 的 memo 被击穿（生产路径传稳定 `onSelectEvent`）。
   * 写成单一体而不是 `onSelectEvent ?? (…)`：后者会让 exhaustive-deps 判不出
   * 依赖，回调反而在每次提交时重建。 */
  const selectEvent = useCallback((e: AgentEvent) => {
    if (onSelectEvent) {
      onSelectEvent(e);
      return;
    }
    onFocusEvent(e);
    onJumpToStream?.(e);
  }, [onSelectEvent, onFocusEvent, onJumpToStream]);

  if (total === 0) {
    return <TabEmpty hint="本会话尚无事件。" icon={ListTree} />;
  }

  const lanes = buildLanes(visible, runGroups);
  const chips: { id: TimelineFilter; label: string; count: number; tone?: 'fail' }[] = [
    { id: 'all', label: '全部', count: counts.all },
    { id: 'tool', label: '工具', count: counts.tool },
    { id: 'model', label: '模型', count: counts.model },
    { id: 'fail', label: '失败', count: counts.fail, tone: 'fail' },
  ];

  return (
    <div className="detail-timeline" ref={listRef} onMouseOver={handleOver} onMouseLeave={handleLeave}>
      <div className="tl-toolbar">
        <div className="tl-filters" role="group" aria-label="Timeline 筛选">
          {chips.map((chip) => (
            <button
              key={chip.id}
              type="button"
              className={`tl-chip${chip.tone ? ` tl-chip-${chip.tone}` : ''}${activeFilter === chip.id ? ' is-on' : ''}`}
              aria-pressed={activeFilter === chip.id}
              onClick={() => setFilter(chip.id)}
            >
              <span>{chip.label}</span>
              <span className="tl-chip-count num">{chip.count}</span>
            </button>
          ))}
        </div>
        {liveEvent && (
          <>
            {/* T-12：运行态的「实时」紫钮——真实语义 = 正在跟随实时流，点击跳到最后
                一条运行中事件。效果图那颗是"插入合成事件"，不移植（不造数据）。 */}
            {streaming && (
              <button
                type="button"
                className="tl-chip tl-chip-live is-on"
                onClick={jumpToLive}
                title="正在跟随实时流 · 跳到最新事件"
              >
                <Zap size={9} aria-hidden="true" />
                实时
              </button>
            )}
            <button type="button" className="tl-live-jump" onClick={jumpToLive} title="展开窗口并定位到最后一条运行中事件">
              <span className="tl-live-dot" aria-hidden="true" />
              跳转到实时流
            </button>
          </>
        )}
      </div>

      {axis && <TimelineAxisBar axis={axis} />}

      {hidden > 0 && (
        <div className="timeline-window-bar">
          <button
            className="timeline-earlier"
            /* 以**当前实际可见**的窗口为基准再加一段：选中项派生出来的扩大部分
               不该被一次点击"缩回去"（那会让点一下反而少看几行）。 */
            onClick={() => setWindowSize(effectiveWindow + TIMELINE_WINDOW_STEP)}
          >
            加载更早 {Math.min(TIMELINE_WINDOW_STEP, hidden)} 条
          </button>
          <span className="timeline-window-hint">
            显示最近 {visible.length} / 共 {filtered.length} 条（前段已折叠，可展开查看）
          </span>
        </div>
      )}

      {filtered.length === 0 ? (
        <div className="tl-empty">
          <span className="tl-empty-title">没有匹配的事件</span>
          <span className="tl-empty-hint">当前筛选：{chips.find((c) => c.id === activeFilter)?.label}</span>
          <button type="button" className="tl-empty-reset" onClick={() => setFilter('all')}>清除筛选</button>
        </div>
      ) : lanes.map(({ group, items }) => {
        const timing = laneTiming(group, conversation.events, timingsMap);
        const failCount = items.filter(({ e }) => timingsMap.get(eventKey(e))?.status === 'fail').length;
        return (
          <Fragment key={group.start}>
            <div
              className="tl-run-header"
              data-status={group.status}
              title={group.runId ? `任务 ${group.runId}` : '会话开场（不属于任何一次任务）'}
            >
              {/* T-2：运行中泳道组头底线的扫光段（`.gh-live` 同款）。 */}
              {group.status === 'running' && (
                <span className="tl-run-live" aria-hidden="true"><i /></span>
              )}
              <span
                className="tl-run-dot"
                data-status={group.status}
                aria-hidden="true"
              />
              <span className="tl-run-name">{group.runId ? `第 ${group.ordinal} 轮执行` : '会话'}</span>
              {group.runId && (
                <span className={`run-badge run-badge-${group.status}`}>
                  {RUN_GROUP_STATUS_LABEL[group.status]}
                </span>
              )}
              <span className="tl-run-count num">
                {items.length === group.count ? `${group.count} 事件` : `${items.length}/${group.count} 事件`}
              </span>
              {/* T-1：失败数只在真有时出现，`--danger-ink` 加粗。 */}
              {failCount > 0 && <span className="tl-run-fail num">{failCount} 失败</span>}
              {timing?.start != null && (
                <span className="tl-run-time num">
                  {formatClock(new Date(timing.start).toISOString())}
                  {timing.durationMs != null ? ` · ${formatShortDuration(timing.durationMs)}` : ''}
                </span>
              )}
            </div>
            {items.map(({ e, i }) => {
              const key = keyOf(e);
              const expanded = activeExpanded.has(key);
              return (
                <Fragment key={i}>
                  <TimelineCard
                    rowKey={key}
                    event={e}
                    call={e.type === EventType.TOOL_RESULT
                      ? callById.get(String(e.data.tool_call_id ?? '')) ?? null
                      : null}
                    timing={timingsMap.get(eventKey(e))}
                    axis={axis}
                    selected={selectedKey === key}
                    expanded={expanded}
                    flash={flashKey === key}
                    onSelect={selectEvent}
                    onToggle={toggleExpanded}
                    onLocate={onJumpToStream}
                  />
                  {activeFilter === 'all' && gapByIndex.has(i) && (
                    <SeqGapBanner gap={gapByIndex.get(i)!} />
                  )}
                </Fragment>
              );
            })}
          </Fragment>
        );
      })}

      {tip && (
        <div className="tl-tooltip" role="tooltip" style={{ left: tip.x, top: tip.y }}>
          <div className="tl-tooltip-title">{tip.title}</div>
          <div className="tl-tooltip-meta mono">{tip.meta}</div>
          {tip.summary && <div className="tl-tooltip-summary">{tip.summary}</div>}
          {tip.lines.map((l, i) => (
            <div key={i} className="tl-tooltip-line">
              {l}
            </div>
          ))}
          <div className="tl-tooltip-hint">点击展开四段详情</div>
        </div>
      )}

      {/* T-14 底部状态条：左「已显示 / 总数 事件」，右键盘提示 + 同步点。
          数字全部来自当前筛选与窗口（真实状态），不是效果图的常量。 */}
      <div className="tl-foot">
        <span className="tl-foot-count mono">
          {visible.length} / {filtered.length} 事件
        </span>
        <span className="tl-foot-grow" />
        <span className="tl-foot-kbd">
          <span className="tl-kbd">↑</span>
          <span className="tl-kbd">↓</span>
          选择
        </span>
        <span className="tl-foot-kbd">
          <span className="tl-kbd">↵</span>
          展开
        </span>
        <span className="tl-foot-sync" data-live={streaming ? 'true' : undefined}>
          {streaming ? '跟随中' : '已同步'}
        </span>
      </div>
    </div>
  );
}

interface VisibleItem {
  e: AgentEvent;
  i: number;
}

type RunGroup = ReturnType<typeof groupEventsByRun>[number];

/** 把窗口内的筛选结果按 run 组桶化：组内无可见项 ⇒ 组头不渲染。 */
function buildLanes(visible: readonly VisibleItem[], groups: readonly RunGroup[]): { group: RunGroup; items: VisibleItem[] }[] {
  const out: { group: RunGroup; items: VisibleItem[] }[] = [];
  let gi = 0;
  for (const item of visible) {
    while (gi < groups.length && item.i >= groups[gi].start + groups[gi].count) gi++;
    const group = groups[gi];
    if (!group) continue;
    const last = out[out.length - 1];
    if (last && last.group.start === group.start) last.items.push(item);
    else out.push({ group, items: [item] });
  }
  return out;
}

/** run 泳道的时间跨度：组内最早 start → 最晚 finish；不可得 = null。 */
function laneTiming(
  group: RunGroup,
  events: readonly AgentEvent[],
  timings: ReadonlyMap<string, EventTiming>,
): EventTiming | null {
  let start: number | null = null;
  let end: number | null = null;
  const to = Math.min(group.start + group.count, events.length);
  for (let i = group.start; i < to; i++) {
    const timing = timings.get(eventKey(events[i]));
    if (!timing || timing.start === null) continue;
    start = start === null ? timing.start : Math.min(start, timing.start);
    const finish = timing.start + (timing.durationMs ?? 0);
    end = end === null ? finish : Math.max(end, finish);
  }
  if (start === null) return null;
  const status: TimelineStatus = group.status === 'failed' ? 'fail'
    : group.status === 'running' ? 'run'
      : group.status === 'completed' ? 'ok'
        : 'idle';
  return { start, durationMs: end !== null && end >= start ? end - start : null, status };
}

function TimelineAxisBar({ axis }: { axis: TimelineAxis }) {
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  return (
    <div className="tl-axis" aria-hidden="true">
      <span className="tl-axis-gutter" />
      <span className="tl-axis-main" />
      <span className="tl-axis-track">
        {ticks.map((t) => (
          <span key={t} className="tl-axis-tick" style={{ left: `${t * 100}%` }}>
            <span className="tl-axis-label num">{formatAxisLabel(axis.maxMs * t)}</span>
          </span>
        ))}
      </span>
      <span className="tl-axis-dur" />
      <span className="tl-axis-chev" />
    </div>
  );
}

function SeqGapBanner({ gap }: { gap: SeqGap }) {
  return (
    <div className="tl-gap" role="note">
      <AlertTriangle size={12} />
      <span className="tl-gap-txt">seq {gap.from} → {gap.to} 缺失（{gap.missing} 条）</span>
    </div>
  );
}

const TimelineCard = memo(function TimelineCard({
  rowKey,
  event,
  call,
  timing,
  axis,
  selected,
  expanded,
  flash,
  onSelect,
  onToggle,
  onLocate,
}: {
  /** P2-1：Timeline 行身份（`eventKey` + seq-less 同键序号），不是裸 `eventKey`。 */
  rowKey: string;
  event: AgentEvent;
  /** T-8：TOOL_RESULT 行要复用对应 call 的标题模板（result 自己只有 `ok`）。 */
  call?: ToolCall | null;
  timing?: EventTiming;
  axis: TimelineAxis | null;
  selected: boolean;
  expanded: boolean;
  flash: boolean;
  onSelect: (e: AgentEvent) => void;
  onToggle: (key: string) => void;
  /** T-10：展开区「定位到会话」——反向联动中心列（PRD §9.2）。 */
  onLocate?: (e: AgentEvent) => void;
}) {
  const key = rowKey;
  const status: TimelineStatus = timing?.status ?? 'idle';
  const category = eventCategory(event);
  const bar = timing && axis ? barGeometry(timing, axis) : null;
  const showBarLabel = Boolean(
    timing?.durationMs != null && axis && timing.durationMs / axis.maxMs >= 0.1,
  );
  const clock = formatClock(event.time);
  const toolName = typeof event.data.tool_name === 'string' ? event.data.tool_name : null;
  /* T-8：人话标题 + 真实副行细节（模型用量等）。纯函数、只用事件字段。 */
  const headline = timelineHeadline(event, call);
  /* 只有展开行需要 DOM id；未展开行不再每行跑一次正则替换。 */
  const expandId = expanded ? `tl-expand-${key.replace(/[^a-zA-Z0-9_-]/g, '_')}` : undefined;
  return (
    <div
      className={`timeline-card${selected ? ' sel' : ''}${expanded ? ' expanded' : ''}${flash ? ' is-new' : ''}`}
      data-state={status}
    >
      <button
        type="button"
        className={`timeline-row${selected ? ' sel' : ''}`}
        data-list-key={key}
        data-cat={category}
        aria-current={selected ? 'true' : undefined}
        aria-expanded={expanded}
        aria-controls={expandId}
        onClick={() => {
          onSelect(event);
          onToggle(key);
        }}
      >
        <span className="tl-main">
          <span className="tl-title">{timelineTitle(event, call)}</span>
          <span className="tl-meta">
            {/* P3-5：meta 文本单独包一层可收缩节点，尾部状态 tag 才不会被裁掉。 */}
            <span className="tl-meta-text">
              {`#${event.seq ?? '·'} · ${event.type}${clock ? ` · ${clock}` : ''}${event.step_id != null ? ` · step ${event.step_id}` : ''}${toolName ? ` · ${toolName}` : ''}`}
            </span>
            {/* T-8：真实可得细节（模型用量）；没有就不占位。 */}
            {headline.note && <span className="tl-note mono">{headline.note}</span>}
            {status === 'fail' && <span className="tl-tag tl-tag-fail">失败</span>}
            {status === 'run' && <span className="tl-tag tl-tag-run">运行中</span>}
          </span>
        </span>
        <span
          className={`tl-track${bar?.point ? ' is-point' : ''}${bar ? '' : ' is-empty'}`}
          data-state={bar ? status : undefined}
          style={bar ? ({
            '--bar-left': `${bar.left}%`,
            ...(bar.point ? {} : { '--bar-width': `${bar.width}%` }),
          } as CSSProperties) : undefined}
          aria-hidden="true"
        >
          {showBarLabel && timing?.durationMs != null && (
            <span className="tl-bar-label num">{formatShortDuration(timing.durationMs)}</span>
          )}
        </span>
        <span className="tl-dur num">
          {timing?.durationMs != null ? formatShortDuration(timing.durationMs) : '—'}
        </span>
      </button>
      {expanded && (
        <div className="tl-expand open" id={expandId}>
          <div className="tl-inner">
            {/* T-10 段头（效果图 `.fc-drill`）：`#seq · type` 左，时刻 · 耗时右。 */}
            <div className="tl-drill">
              <span className="tl-drill-type mono">
                {event.seq != null ? `#${event.seq}` : '#·'} · {event.type}
              </span>
              <span className="tl-drill-grow" />
              <Clock size={10} aria-hidden="true" />
              <span className="mono">
                {clock ?? '—'}
                {timing?.durationMs != null ? ` · ${formatShortDuration(timing.durationMs)}` : ''}
              </span>
            </div>
            <EventInspector focus={{ kind: 'event', event }} />
            {/* 效果图 `.fc-acts`：反向定位到中心列（PRD §9.2 的既有通路）。 */}
            {onLocate && (
              <div className="tl-acts">
                <button
                  type="button"
                  className="tl-btn"
                  onClick={() => onLocate(event)}
                  title="在会话里滚动定位到这条事件"
                >
                  <ArrowLeft size={10} aria-hidden="true" />
                  定位到会话
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
});

// ── Changes tab：文件 diff 聚合（渲染复用 `DiffBlock`——diff 只有一份渲染器） ──

/** DIFFS 列表尾窗（F5 #279）。50 项 ≈4.4ms——探针 `stepdetail-list-cost.perf.test.ts`
 *  实测 @50 4.4ms / @200 31.4ms（每项含一个 `DiffBlock`，比工具行重），故取 50 而非更大；
 *  步长 200 ≈4 屏。 */
export const CHANGES_WINDOW_DEFAULT = 50;
export const CHANGES_WINDOW_STEP = 200;

/** 导出供 SSR 测试直接渲染（同 `TimelineTab` / `ToolEventSections`：`tab` 是内部
 *  状态，从 `StepDetail` 外面进不到这个面）。 */
export function ChangesTab({ tools, sessionId }: { tools: ToolCall[]; sessionId?: string }) {
  // F2（#272）：`tools` 由调用方 memo 提供，过滤跟着它的引用走——面板关闭期间本组件
  // 仍挂载（`hidden` 不卸载），此前每次提交都要重扫一遍工具表。
  const diffs = useMemo(() => tools.filter((t) => t.diff), [tools]);
  /* F5（#279）尾窗：本面**没有选中态**（行是只读卡片，理由见 `listTargets` 的注释），
     所以不需要 Timeline 那套「选中项派生扩窗」，只留用户的「加载更早」意图。
     `useState` 必须在下面那条 early-return **之前**（Rules of Hooks）。 */
  const [diffWindow, setDiffWindow] = useState(CHANGES_WINDOW_DEFAULT);
  const hiddenDiffs = Math.max(0, diffs.length - diffWindow);
  const visibleDiffs = hiddenDiffs > 0 ? diffs.slice(hiddenDiffs) : diffs;
  if (diffs.length === 0) {
    return <TabEmpty hint="本次会话未产生文件变更。" icon={FileDiff} />;
  }
  return (
    <>
      {hiddenDiffs > 0 && (
        <div className="timeline-window-bar">
          <button
            className="timeline-earlier"
            onClick={() => setDiffWindow(diffWindow + CHANGES_WINDOW_STEP)}
          >
            加载更早 {Math.min(CHANGES_WINDOW_STEP, hiddenDiffs)} 条
          </button>
          <span className="timeline-window-hint">
            显示最近 {visibleDiffs.length} / 共 {diffs.length} 条（前段已折叠，可展开查看）
          </span>
        </div>
      )}
      {visibleDiffs.map((t) => (
        <div key={t.tool_call_id} className="detail-section detail-card">
          <div className="detail-section-title">
            <FileDiff size={14} /> {t.name}: {String(t.args.path ?? '')}
          </div>
          {/* 这里此前自己内联一份 `.diff-cols`——同一份 before/after 在 Inspector 与
              中心列各有一套渲染，且这套**认不出归档态**（before/after 已被换成
              `use read_artifact(<id>)` marker 摘要时，会把 marker 原文当 diff 正文
              渲染出来，即"显示了一段并不存在的文件内容"）。收敛到 `DiffBlock`
              （#183 AC9 / #186 AC3）后归档占位态与中心列逐字一致。 */}
          <DiffBlock diff={t.diff!} sessionId={sessionId} />
        </div>
      ))}
    </>
  );
}

// ── Terminal tab：bash 调用聚合（命令执行面） ──

function TerminalTab({ tools, onFocusTool, selectedKey }: { tools: ToolCall[]; onFocusTool: (t: ToolCall) => void; selectedKey?: string | null }) {
  // 判定与读取都来自 lib/commandOutput（票面 AC2）：
  // "什么算一次命令"只允许有一处答案——两边各写一份会各自演化。
  // F2（#272）：同上（过滤谓词仍是 lib/commandOutput 的唯一那份 `isCommand`）。
  const bashes = useMemo(() => tools.filter(isCommand), [tools]);
  if (bashes.length === 0) {
    return <TabEmpty hint="本次会话未执行命令。" icon={TerminalSquare} />;
  }
  return (
    <>
      {bashes.map((t) => {
        const result = commandResult(t);
        const selected = selectedKey != null && toolKey(t) === selectedKey;
        return (
          <button
            key={t.tool_call_id}
            className={`detail-terminal-row detail-card${selected ? ' sel' : ''}`}
            data-list-key={toolKey(t)}
            aria-current={selected ? 'true' : undefined}
            onClick={() => onFocusTool(t)}
          >
            <div className="detail-terminal-cmd">
              <span className="bash-prompt">$</span>
              <code>{String(t.args.command ?? '')}</code>
            </div>
            {result?.stdout !== undefined && <pre className="detail-terminal-out">{truncateForDisplay(result.stdout)}</pre>}
            {result?.exit_code !== undefined && (
              <span className={`exit-badge ${result.exit_code === 0 ? 'exit-ok' : 'exit-err'}`}>exit {result.exit_code}</span>
            )}
          </button>
        );
      })}
    </>
  );
}

// ── Artifacts tab：artifact 聚合页（工具挂载 ref，单一投影源——不变量 #22） ──

/** ARTIFACTS 列表尾窗（F5 #279）。20 项 ≈5ms——探针 `stepdetail-list-cost.perf.test.ts`
 *  实测 @50 12.6ms / @200 45.4ms（≈0.25ms/项；每项一个 `ArtifactViewer`，是三个列表里
 *  最重的），故窗口取 20；步长 100 ≈5 屏。 */
export const ARTIFACTS_WINDOW_DEFAULT = 20;
export const ARTIFACTS_WINDOW_STEP = 100;

/** 导出理由同 `ChangesTab`：`tab` 是内部状态，SSR 测试与 F5 成本探针进不到这个面。 */
export function ArtifactsTab({ tools, sessionId }: { tools: ToolCall[]; sessionId: string }) {
  // Artifacts only reach this tab through the projection attaching an ArtifactRef
  // to the producing ToolCall (lib/projection.ts ARTIFACT_CREATED case). If an
  // artifact/created event's tool isn't found by the projection, that's a
  // projection concern — not something to paper over here with a second truth
  // path (invariant #22).
  // F2（#272）：同上。
  const artifacts = useMemo(() => tools.filter((t) => t.artifact), [tools]);
  // F5（#279）尾窗：同 `ChangesTab`（本面也无选中态）；`useState` 必须在这条 early-return 之前。
  const [artifactWindow, setArtifactWindow] = useState(ARTIFACTS_WINDOW_DEFAULT);
  const hiddenArtifacts = Math.max(0, artifacts.length - artifactWindow);
  const visibleArtifacts = hiddenArtifacts > 0 ? artifacts.slice(hiddenArtifacts) : artifacts;
  if (artifacts.length === 0) {
    return <TabEmpty hint="本次会话没有产出文件。" icon={Package} />;
  }
  return (
    <>
      {hiddenArtifacts > 0 && (
        <div className="timeline-window-bar">
          <button
            className="timeline-earlier"
            onClick={() => setArtifactWindow(artifactWindow + ARTIFACTS_WINDOW_STEP)}
          >
            加载更早 {Math.min(ARTIFACTS_WINDOW_STEP, hiddenArtifacts)} 条
          </button>
          <span className="timeline-window-hint">
            显示最近 {visibleArtifacts.length} / 共 {artifacts.length} 条（前段已折叠，可展开查看）
          </span>
        </div>
      )}
      {visibleArtifacts.map((t) => (
        <div key={t.tool_call_id} className="detail-section detail-card">
          <div className="detail-section-title">
            <FileCheck2 size={14} /> {t.name}
          </div>
          <div className="detail-row">
            <span className="detail-key">编号</span>
            <code className="detail-val detail-val-mono">{t.artifact!.artifact_id.slice(0, 16)}</code>
          </div>
          {/* 元数据可空（AC5）：缺了就说"未知"，不拿默认值冒充。
              真后端里 size/mime_type 由 store 决定是否持久化（MinIO 不持久化
              source_tool），编一个 `0 B` / `application/octet-stream` 是在替它撒谎。 */}
          <div className="detail-row">
            <span className="detail-key">大小</span>
            <span className="detail-val">
              {t.artifact!.size === null ? '未知' : formatBytes(t.artifact!.size)}
            </span>
          </div>
          <div className="detail-row">
            <span className="detail-key">类型</span>
            <span className="detail-val detail-val-mono">
              {t.artifact!.mime_type ?? '未知'}
            </span>
          </div>
          {/* 内容按需读取（#186 AC1）——地址是 #185 的只读端点，会话归属由 URL 的
              session_id 决定（artifact_id 是内容哈希，跨会话可重名）。 */}
          <ArtifactViewer
            sessionId={sessionId}
            artifactId={t.artifact!.artifact_id}
            label="查看内容"
          />
        </div>
      ))}
    </>
  );
}

// ── 事件级 Inspector：Overview / Input / Output / Raw 四段（PRD §8.4） ──

/** 事件级 Inspector 主体——只接收已收窄的非 Run 焦点。 */
// 事件级 Inspector 只接受 tool/event——child（委派钻取）有专用面板，
// 永远不会到达这里（Exclude 与主组件的早退分支守卫保持同一形状）。
type EventFocus = Exclude<InspectorFocus, { kind: 'run' } | { kind: 'child' }>;

/** 事件级四段 tab 状态：Overview（元信息）/ Input（data）/ Output（同 data，语义入口）/
 *  Raw（完整事件）。Input 与 Output 对普通事件都是 event.data——Output 作为默认段
 *  保留"看结果优先"的旧习惯；Raw 是完整事件 JSON。 */
type EventIoTab = 'overview' | 'input' | 'output' | 'raw';

function EventInspector({ focus }: { focus: EventFocus }) {
  // hooks 规则：useState 必须在条件 return 之前（tool focus 分支也保持 hook 顺序稳定）
  const [tab, setTab] = useState<EventIoTab>('overview');
  if (focus.kind === 'tool') {
    return <ToolEventSections tool={focus.tool} />;
  }
  const event = focus.event;
  const dataJson = JSON.stringify(event.data, null, 2);
  const rawJson = JSON.stringify(event, null, 2);

  const tabs: readonly { id: EventIoTab; label: string }[] = [
    { id: 'overview', label: '概览' },
    { id: 'input', label: '输入' },
    { id: 'output', label: '输出' },
    { id: 'raw', label: '原始' },
  ];

  return (
    <>
      <div className="io-tabs" role="tablist" aria-label="事件详情段">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`io-tab${tab === t.id ? ' sel' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'overview' && (
        <div className="detail-section">
          <div className="detail-section-title">
            <Clock size={14} /> {event.type}
          </div>
          {event.seq !== null && (
            <div className="detail-row">
              <span className="detail-key">序号</span>
              <span className="detail-val detail-val-mono num">{event.seq}</span>
            </div>
          )}
          {event.time && (
            <div className="detail-row">
              <span className="detail-key">时间</span>
              <span className="detail-val detail-val-mono">{event.time}</span>
            </div>
          )}
          {/* 宽松判空（语义见 formatEventTooltip 文档）：缺失/null 都不渲染 step 行。 */}
          {event.step_id != null && (
            <div className="detail-row">
              <span className="detail-key">步骤</span>
              <span className="detail-val num">{event.step_id}</span>
            </div>
          )}
          {event.run_id && (
            <div className="detail-row">
              <span className="detail-key">任务</span>
              <span className="detail-val detail-val-mono">{event.run_id.slice(0, 16)}</span>
            </div>
          )}
          {event.event_id && (
            <div className="detail-row">
              <span className="detail-key">事件编号</span>
              <span className="detail-val detail-val-mono">{event.event_id.slice(0, 16)}</span>
            </div>
          )}
        </div>
      )}
      {tab === 'input' && (
        <div className="detail-section">
          <div className="detail-section-title">输入数据</div>
          <div className="detail-code-wrap">
            <CopyButton text={dataJson} label="复制 JSON" />
            <div className="detail-json">
              <JsonTree value={event.data} />
            </div>
          </div>
        </div>
      )}
      {tab === 'output' && (
        <div className="detail-section">
          <div className="detail-section-title">输出数据</div>
          <div className="detail-code-wrap">
            <CopyButton text={dataJson} label="复制 JSON" />
            <div className="detail-json">
              <JsonTree value={event.data} />
            </div>
          </div>
        </div>
      )}
      {tab === 'raw' && (
        <div className="detail-section">
          <div className="detail-section-title">原始</div>
          <div className="detail-code-wrap">
            <CopyButton text={rawJson} label="复制原始" />
            <div className="detail-json">
              <JsonTree value={event} />
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/** 工具事件级视图：Overview / Input(args) / Output(result) / Raw(raw_call/raw_result)
 *  四段标签条（PRD §8.4 统一）；默认 Output（运行中的工具回退 Overview）。 */
type IoTab = 'overview' | 'input' | 'output' | 'raw';

export function ToolEventSections({ tool }: { tool: ToolCall }) {
  const argsJson = JSON.stringify(tool.args, null, 2);
  const outputText = stringifyForDisplay(tool.result);
  const resultIsObject = typeof tool.result === 'object' && tool.result !== null;
  const hasOutput = tool.result !== undefined;
  const hasRaw = Boolean(tool.raw_call || tool.raw_result);
  /* #183 AC9（同一数据不得两份独立渲染）：命令输出在 Inspector 里也必须走中心列
     那一个渲染器（`ToolOutputStream`，ToolCard 与「输出」面共用）。此前这里自己
     `<pre>{truncateForDisplay(...)}</pre>` 一份——同一份 stdout 两套渲染，channels
     保真、尾窗预算、换行切换、复制口径全都会各自演化。
     `showCaret=false`（同 #190 AC5 的理由）：Inspector 是复盘面，不是对话流；
     一个闪动光标在这里读起来像"第二条正在跑的流"。 */
  /* 命令的**取数口径**也走那唯一一份（`lib/commandOutput.ts` 的终态优先级：
     有 result 就以 result 为准，没有才回落流式块）。此前这里直接拿 `tool.output`
     ——只要留过流式块就无视已到达的 result，于是同一个 bash 调用在 Inspector 里显示
     分块（大输出还可能被投影合并/重排过）、在中心列与「输出」面显示权威终态文本。
     **只在确有流式块时才需要这次纠正**：没有块的工具维持原有的"结果树"呈现——那棵树
     还带着 `exit_code` / `cancelled` 等字段，而 `ToolOutputStream` 只呈现 stdout/stderr
     文本（把它套到无块的工具上，会把 exit code 从 Inspector 里弄丢）。 */
  const hasChunks = (tool.output?.length ?? 0) > 0;
  const commandChunks = isCommand(tool) && hasChunks
    ? (commandOutputs([tool])[0]?.chunks ?? [])
    : null;
  const outputChunks = commandChunks ?? (tool.output ?? []);
  const [wantTab, setTab] = useState<IoTab>(hasOutput ? 'output' : 'overview');

  const tabs: readonly { id: IoTab; label: string }[] = [
    { id: 'overview', label: '概览' },
    { id: 'input', label: '输入' },
    ...(hasOutput ? [{ id: 'output', label: '输出' } as const] : []),
    ...(hasRaw ? [{ id: 'raw', label: '原始' } as const] : []),
  ];
  /* 渲染期收窄到仍然存在的段（同 #182 `resolveActiveTab` 的口径）：选中项在清单里
     移动时这个组件实例**不重建**（AC1），于是 `useState` 的初始值会留在上一个工具
     上——从"有输出"的工具移到"还没结果"的工具，旧 tab 指向一个不存在的段，
     面板就是空白（"详情实时跟随"当场破功）。 */
  const tab: IoTab = tabs.some((t) => t.id === wantTab)
    ? wantTab
    : (hasOutput ? 'output' : 'overview');

  return (
    <div className="detail-section">
      <div className="detail-section-title">
        <TerminalSquare size={14} /> {tool.name}
        <span className={`tool-status-dot tool-status-dot-${tool.status}`} />
        {tool.started_at && tool.completed_at && (
          <span className="io-duration">{formatDuration(tool.started_at, tool.completed_at)}</span>
        )}
      </div>

      <div className="io-tabs" role="tablist" aria-label="工具输入输出">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`io-tab${tab === t.id ? ' sel' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === 'overview' && (
        <div className="detail-overview">
          <div className="detail-row">
            <span className="detail-key">工具</span>
            <span className="detail-val detail-val-mono">{tool.name}</span>
          </div>
          <div className="detail-row">
            <span className="detail-key">调用编号</span>
            <span className="detail-val detail-val-mono">{tool.tool_call_id.slice(0, 16)}</span>
          </div>
          <div className="detail-row">
            <span className="detail-key">状态</span>
            <span className="detail-val detail-val-tag">{tool.status}</span>
          </div>
          {tool.started_at && (
            <div className="detail-row">
              <span className="detail-key">开始时间</span>
              <span className="detail-val detail-val-mono">{tool.started_at}</span>
            </div>
          )}
          {tool.started_at && tool.completed_at && (
            <div className="detail-row">
              <span className="detail-key">耗时</span>
              <span className="detail-val num">{formatDuration(tool.started_at, tool.completed_at)}</span>
            </div>
          )}
          {tool.artifact && (
            <div className="detail-row">
              <span className="detail-key">产出文件</span>
              <span className="detail-val detail-val-mono">{tool.artifact.artifact_id.slice(0, 16)}</span>
            </div>
          )}
        </div>
      )}
      {tab === 'input' && (
        <div className="detail-code-wrap">
          <CopyButton text={argsJson} label="复制 JSON" />
          <div className="detail-json">
            <JsonTree value={tool.args} />
          </div>
        </div>
      )}
      {tab === 'output' && hasOutput && outputChunks.length > 0 && (
        // 有输出块：中心列同一个渲染器（自带标签条/复制/换行与尾窗预算），
        // 不在外面再套一层 CopyButton——那会出现两个"复制"按钮说同一件事。
        <ToolOutputStream
          chunks={outputChunks}
          streaming={tool.status === 'running'}
          showCaret={false}
        />
      )}
      {tab === 'output' && hasOutput && outputChunks.length === 0 && (
        <div className="detail-code-wrap">
          <CopyButton text={outputText} label="复制输出" />
          {resultIsObject ? (
            <div className="detail-json">
              <JsonTree value={tool.result} />
            </div>
          ) : (
            <pre className="detail-code">{truncateForDisplay(outputText)}</pre>
          )}
        </div>
      )}
      {tab === 'raw' && hasRaw && (
        <div className="io-raw-panes">
          {tool.raw_call && (
            <div className="detail-code-wrap">
              <div className="io-raw-label">tool/call 原始事件</div>
              <CopyButton text={JSON.stringify(tool.raw_call, null, 2)} label="复制原始（tool/call）" />
              <div className="detail-json">
                <JsonTree value={tool.raw_call} />
              </div>
            </div>
          )}
          {tool.raw_result && (
            <div className="detail-code-wrap">
              <div className="io-raw-label">tool/result 原始事件</div>
              <CopyButton text={JSON.stringify(tool.raw_result, null, 2)} label="复制原始（tool/result）" />
              <div className="detail-json">
                <JsonTree value={tool.raw_result} />
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Tab 空状态 —— 对应 tab 的 icon + 一句话。
 *  参考 Linear/Vercel dashboard 空状态：轻量图标（非灰黄 emoji）+ 克制文案，
 *  让"没有数据"读起来是预期而非故障。icon 与 tab 条同一图标，视觉呼应。 */
function TabEmpty({ hint, icon: Icon }: { hint: string; icon: typeof ListTree }) {
  return (
    <div className="detail-tab-empty">
      <Icon size={24} className="detail-empty-icon" aria-hidden="true" />
      <div className="detail-empty-hint">{hint}</div>
    </div>
  );
}

function DetailEmpty() {
  return (
    <div className="detail-empty">
      <div className="detail-empty-title">未选择会话</div>
      <div className="detail-empty-hint">从左侧选择，或开始新任务。</div>
    </div>
  );
}

// ── Phase 13 委派钻取：child 会话视图（v2 PRD §10.5）──

/** ChildSessionView — Inspector 内原位展开 child 会话（`GET /api/sessions/{id}/events`
 *  拉取 → projectHistory 同一投影管线，不变量 #22）。只读呈现：Overview 摘要
 *  （ChatTab 复用，工具行静态化）+ child 事件尾窗——child 的完整交互仍在
 *  「打开子会话」主窗管线，这里不做第二套可操作 Inspector。 */
function ChildSessionView({ childSessionId }: { childSessionId: string }) {
  const { conversation, error } = useChildConversation(childSessionId);
  if (error) {
    return <div className="child-view-status">子会话加载失败：{error}</div>;
  }
  if (!conversation) {
    return <div className="child-view-status">正在加载子会话…</div>;
  }
  return (
    <>
      {/* #1192：约束继承快照事件在子会话自己的事件流里——子详情视图顶部
       *  只读展示（无快照事件则整区不渲染，不伪造）。 */}
      <ConstraintInheritance events={conversation.events} />
      <ChatTab conversation={conversation} tools={allTools(conversation)} />
      <ChildTimeline events={conversation.events} />
    </>
  );
}

/** child 事件只读尾窗（有界 DOM，同 Timeline 尾窗纪律）：verbatim type + 单行
 *  语义摘要。child 会话通常不长，但模型/delta 类事件可能多——tail 200 封顶。 */
const CHILD_TIMELINE_WINDOW = 200;

function ChildTimeline({ events }: { events: AgentEvent[] }) {
  const total = events.length;
  const tail = events.slice(-CHILD_TIMELINE_WINDOW);
  return (
    <div className="detail-section">
      <div className="detail-section-title">
        <ListTree size={14} /> 子任务时间线
      </div>
      {total > CHILD_TIMELINE_WINDOW && (
        <div className="detail-row detail-row-warn">显示最近 {CHILD_TIMELINE_WINDOW} 条 / 共 {total} 条</div>
      )}
      {tail.map((e, i) => (
        <div key={e.event_id ?? `${e.seq ?? 'n'}-${i}`} className="detail-row child-ev-row">
          <span className="detail-key mono">{e.type}</span>
          <span className="detail-val">{summarizeEvent(e) || '—'}</span>
        </div>
      ))}
    </div>
  );
}
