/** Event projection — pure functions turning AgentEvents into ConversationState.
 *
 * This is the ONLY place where events get interpreted into view-models.
 * No component mutates state directly; they all dispatch events here.
 * This satisfies invariant #22 (Web UI maintains no second truth) —
 * the events ARE the truth, this just projects them.
 */

import type { AgentEvent, AskUserAnswer, AskUserQuestion, AskUserQuestionAnswer, ConstraintInputAnswer, ConstraintInputChoice, ConversationState, Delegation, EventTypeValue, ModelSegment, PendingApproval, PendingAskUserRequest, PendingConstraintInputRequest, PendingPlanApprovalRequest, PlanApprovalAnswer, PlanItem, ReasoningBlock, RunBudgetDimensionFacts, RunContinuation, RunLimitsFacts, RunPausedInfo, ToolCall, ToolOutputChunk, Turn, UndeliveredInput, UsageStats } from '../types';
import { EventType } from '../types';
import { addDecimalTexts } from './runBudget';
import { isCancelledRunFailure } from './runCancel';
import { deadlineInstant, isDeadlinePause, toolDimensionName } from './runBudget';
import { interpretToolResult, parseArtifactMarker } from './toolShapes';
import { quarantineRecord, validateEvent } from './eventValidate';
import { parseImageRefs, type ImageAttachmentRef } from './attachmentRefs';

// 事件类型常量统一取 EventType（generated/event-types.ts，后端 event.py 生成物）。
// 此前 reasoning / tool-output 两族以本地字面量先行（fixture 驱动，不硬阻塞后端），
// 生成物落库后已收编——本地副本删除，消除双份词汇表。

/** 输出 chunk 数上限：交替通道流的有界收缩触发线（spec 03 §9.4 bounded DOM）。 */
const MAX_OUTPUT_CHUNKS = 512;

// ── ADR-0030（#196）§4.2 / §5.4：supersede 区间（与后端 derive.py 同构）─────
//
// 被取代 seq 为 s。区间 = `[s, n)`，n = s 之后第一条**未被取代**的 user/message
// 的 seq；没有就一直到底。判据只看 seq 与"该 seq 是否也被取代"，与事件到达顺序
// 无关（纯函数性质）。前端与后端**同一区间定义、同一命名**——漂移由两端的
// 测试各锁一条（后端单测 + 前端 e2e）。
//
// 被取代的整轮（问 + 答 + 工具）从**视图**移除；events 日志照旧保留（不变量
// #3 append-only），只影响渲染。用户裁定（§4.5.1）：旧回答段直接删掉不显示，
// 不加"已改写"标记。

/** supersede 区间的端点：返回每个被取代 seq 的区间 `[start, end)`（end 独占）。
 *  与后端 `derive.py` 的区间计算同构（同一判据、同一"到下一条未被取代的
 *  user 消息"边界）。 */
function supersedeRanges(events: readonly AgentEvent[]): Array<[number, number]> {
  const supersededSeqs = new Set<number>();
  for (const event of events) {
    if (event.type !== EventType.MESSAGE_SUPERSEDED) continue;
    const raw = event.data.superseded_seq;
    // 一行坏数据只损失该行（与后端 derive 同款容错）：非有限数就忽略。
    if (typeof raw === 'number' && Number.isFinite(raw)) supersededSeqs.add(raw);
  }
  if (supersededSeqs.size === 0) return [];
  const userSeqs = events
    .filter((e) => e.type === EventType.USER_MESSAGE && e.seq !== null)
    .map((e) => e.seq as number);
  // reduce 而非 Math.max(...spread)（审查 P2）：超长会话（数万事件）下
  // spread 会把整个数组当函数参数展开 → RangeError，历史装载直接崩。
  const lastSeq = events.reduce((m, e) => Math.max(m, e.seq ?? 0), 0);
  const ranges: Array<[number, number]> = [];
  for (const seq of [...supersededSeqs].sort((a, b) => a - b)) {
    const following = userSeqs.find((u) => u > seq && !supersededSeqs.has(u));
    const end = following !== undefined ? following : lastSeq + 1;
    if (end > seq) ranges.push([seq, end]);
  }
  return ranges;
}

/** 把 supersede 区间应用到已渲染的 turns：区间内的轮整段置 `superseded`。
 *  整体 reassign（与 pending_approvals 同款契约）；不删除 turns 数组元素——
 *  渲染层按标记过滤，重放确定性更好（重复应用幂等）。 */
function applySupersedeShadow(state: ConversationState): void {
  const ranges = supersedeRanges(state.events);
  if (ranges.length === 0) return;
  const shadowed = (seq: number | null): boolean =>
    seq !== null && ranges.some(([start, end]) => start <= seq && seq < end);
  state.turns = state.turns.map((turn) =>
    shadowed(turn.user_message_seq) && !turn.superseded
      ? { ...turn, superseded: true }
      : turn,
  );
}

/** 未投递输入的逐事件折叠（ADR-0030 §5.2：事件流是唯一事实）。
 *  `message/queued` / `queue/cancelled` / `steer/requested` 增量；`queue/consumed`
 *  / `steer/applied` 摘除（消费事实）。与后端 `undelivered_inputs` 同一判据。 */
function projectUndelivered(state: ConversationState, event: AgentEvent): void {
  const { type } = event;
  const data = event.data;
  // #825（MM-04）：待发送输入也带附图引用（后端 `_enqueue` 把 `user_input_metadata`
  // 原样写进 `message/queued` / `steer/requested`）。不解析它，队列条的「立即 / 编辑」
  // 重投递就不带引用，后端按本请求重建 `user/message` ⇒ 图静默消失。
  // 「非空才写」（与 `Turn.user_attachments` 同一纪律）：不制造空数组这种"看起来有话说"
  // 的取值；做成函数是为了只在**新增**两个分支里解析，摘除类事件（cancelled/consumed）
  // 不为一个用不上的字段白跑一趟。
  const refsField = (): { attachments?: ImageAttachmentRef[] } => {
    const attachments = parseImageRefs(data.attachments);
    return attachments.length > 0 ? { attachments } : {};
  };
  if (type === EventType.MESSAGE_QUEUED) {
    const id = data.queue_id;
    if (typeof id !== 'string' || !id) return;
    state.undelivered = [
      ...state.undelivered,
      {
        kind: 'queue',
        id,
        content: String(data.content ?? ''),
        seq: event.seq ?? 0,
        created_at: event.time ?? '',
        ...refsField(),
      },
    ];
    return;
  }
  if (type === EventType.STEER_REQUESTED) {
    const id = data.steer_id;
    if (typeof id !== 'string' || !id) return;
    state.undelivered = [
      ...state.undelivered,
      {
        kind: 'steer',
        id,
        content: String(data.content ?? ''),
        seq: event.seq ?? 0,
        created_at: event.time ?? '',
        ...refsField(),
      },
    ];
    return;
  }
  if (type === EventType.QUEUE_CANCELLED) {
    const id = data.queue_id;
    if (typeof id !== 'string') return;
    state.undelivered = state.undelivered.filter((u) => u.id !== id);
    return;
  }
  if (type === EventType.QUEUE_CONSUMED || type === EventType.STEER_APPLIED) {
    const id =
      type === EventType.QUEUE_CONSUMED
        ? data.queue_id
        : data.steer_id;
    if (typeof id !== 'string') return;
    state.undelivered = state.undelivered.filter((u) => u.id !== id);
  }
}

/** 首屏/重连补齐：用 `GET /queue` 的响应**替换**未投递列表（§5.2 状态源优先级：
 *  事件流是唯一事实，本端点只做补齐）。替换而非合并——重复执行幂等。
 *  由 useSession 在历史装载 / 重连时调用。
 *
 *  #825（MM-04）例外：`GET /queue` 的响应体里**没有**附件字段（后端未暴露），
 *  而事件流里的 `message/queued` / `steer/requested` 有。若按字面"替换"，一次
 *  刷新就会把"这条排队项带图"这件事抹掉 ⇒ 之后「立即 / 编辑」重投递静默丢图。
 *  故按 id 把**事件流已知**的引用带过来（内容/时间仍以本端点为准）。 */
export function restoreUndeliveredFromQueue(
  state: ConversationState,
  queue: { items: Array<{ queue_id: string; content: string; created_at: string }>; steers: Array<{ steer_id: string; content: string; created_at: string }> },
): void {
  const known = new Map(state.undelivered.map((u) => [u.id, u]));
  const items: UndeliveredInput[] = [
    ...queue.items.map((i) => ({
      kind: 'queue' as const,
      id: i.queue_id,
      content: i.content,
      seq: Number.MAX_SAFE_INTEGER,
      created_at: i.created_at,
      attachments: known.get(i.queue_id)?.attachments,
    })),
    ...queue.steers.map((s) => ({
      kind: 'steer' as const,
      id: s.steer_id,
      content: s.content,
      seq: Number.MAX_SAFE_INTEGER,
      created_at: s.created_at,
      attachments: known.get(s.steer_id)?.attachments,
    })),
  ];
  state.undelivered = items;
}

export function initConversation(session_id: string): ConversationState {
  return {
    session_id,
    turns: [],
    active_step_id: null,
    run_status: 'idle',
    run_cancelled: false,
    compaction_in_progress: false,
    compactions: [],
    reconcile_queue: [],
    pending_approvals: [],
    approval_decisions: [],
    permission_policy: null,
    session_permission_mode: null,
    session_cwd: null,
    events: [],
    // ADR-0037 D2：append 计数初值 0（与 projectHistory 的空历史产物一致）。
    eventsVersion: 0,
    unknown_events: [],
    model: null,
    usage_total: null,
    // #1313 口径 B：顶栏会话累计的两个槽，与 run 级 `usage_total` 分开维护。
    // S：已终态 run 的权威值之和（跨 run 存活，`run/started` 不清零）；
    // R：当前 run 的流式累计（`run/started` 归零，终态时被权威值替换或结转进 S）。
    session_usage_total: null,
    run_usage_live: null,
    cost_usd: null,
    plan: null,
    trace_id: null,
    trace_url: null,
    run_id: null,
    model_fallback: null,
    run_interrupted: null,
    run_paused: null,
    pending_constraint_input: null,
    pending_ask_user: null,
    pending_plan_approval: null,
    run_failure: null,
    turn_index: null,
    resumed_turn_index: null,
    requested_model: null,
    model_run_id: null,
      seenSeqs: new Set(),
      undelivered: [],
      // #537：run 预算镜像初值（无回显、无账）。
      run_budget_ceilings: null,
      run_budget_consumed: null,
    };
}

function newTurn(step_id: number): Turn {
  return {
    step_id,
    user_message: '',
    model: { text: '', status: 'streaming' },
    segments: [],
    tools: [],
    activities: [],
    status: 'streaming',
    reasoningById: {},
    turn_index: null,
    user_message_seq: null,
  };
}

/** Clone one turn for copy-on-write mutation. turn.model 与 segments[最新 model
 *  index] 是同一逻辑段：clone 会切断引用，这里按 activities 记录的 index 重新
 *  对齐，保证后续 mutation 同步（别名契约，有专项测试锁定）。 */
function cloneTurn(t: Turn): Turn {
  const turn: Turn = {
    ...t,
    model: { ...t.model },
    segments: t.segments.map((s) => ({ ...s })),
    tools: [...t.tools],
    activities: [...t.activities],
    delegations: t.delegations ? [...t.delegations] : undefined,
    recalls: t.recalls ? [...t.recalls] : undefined,
  };
  const lastModel = [...turn.activities].reverse().find((a) => a.kind === 'model');
  if (lastModel && lastModel.kind === 'model') {
    turn.segments[lastModel.index] = turn.model;
  }
  return turn;
}

/** Copy-on-write turn access: locate (or create) the turn a step belongs to,
 *  clone ONLY that turn, and hand the clone to `fn` for mutation.
 *
 * 未触及的 turn 保持引用稳定——这是渲染层 React.memo(TurnView) 的前提：
 * 流式期间每个 delta 只应重渲染活跃轮次，而不是整条会话。 */
function withTurnAt(state: ConversationState, step: number, fn: (turn: Turn) => void): void {
  // 热路径（T9 10k 基准发现）：流式与顺序历史重放的目标几乎总是最后一轮——
  // findIndex O(turns) 在长会话（数千轮）成为每事件主导成本（实测 5000 轮
  // 8.79µs/事件）。前提 = turn.step_id 唯一（resolveStep 单调递增设计不变量；
  // 退化重复步场景语义与 findIndex 首匹配可能不同，属既 broken 不变量）。
  const lastIdx = state.turns.length - 1;
  const last = lastIdx >= 0 ? state.turns[lastIdx] : undefined;
  if (last && last.step_id === step) {
    const turn = cloneTurn(last);
    replaceTurnAt(state, lastIdx, turn);
    fn(turn);
    return;
  }
  const idx = state.turns.findIndex((t) => t.step_id === step);
  if (idx === -1) {
    const turn = newTurn(step);
    // #1429 F6 / 复审 P2：恢复续轮（`run/resumed` 之后的新执行按 step_base+1 建轮）继承
    // 同一 run 的轮次号。**只交给该 run 的首个续轮**：继承槽消费即清空，且该号已被
    // 现有轮占用时不再打标（一次恢复连开多个 step 曾得到 [1,1,1]——同一 run 出现重复
    // 「第 N 轮」，导航目标被后写覆盖）。`turnNav` 的首写守卫是第二道保险。
    const inherited = state.resumed_turn_index;
    state.resumed_turn_index = null;
    if (inherited !== null && !state.turns.some((t) => t.turn_index === inherited)) {
      turn.turn_index = inherited;
    }
    state.turns = [...state.turns, turn];
    fn(turn);
    return;
  }
  const turn = cloneTurn(state.turns[idx]);
  replaceTurnAt(state, idx, turn);
  fn(turn);
}

/** Swap one cloned turn back into the turns array (array identity changes,
 *  其它元素引用不变）。withTurnAt 与 ARTIFACT_CREATED 共享的落盘路径。 */
function replaceTurnAt(state: ConversationState, idx: number, turn: Turn): void {
  const turns = [...state.turns];
  turns[idx] = turn;
  state.turns = turns;
}

/** Clone a single tool for in-place field updates (tool 级 copy-on-write：
 *  同 turn 内未触及的工具保持引用，ToolCard memo 才能跳过重渲染）。 */
function cloneTool(t: ToolCall): ToolCall {
  return { ...t };
}

/** 编排节点落盘：追加委派 + activities 编排项（STARTED 与乱序 FINISHED 防御
 *  分支共享的同构形状，Standards 轴 Duplicated Code 收敛）。整体 reassign——
 *  契约见 types.ts Delegation 的 copy-on-write 说明。 */
function pushDelegation(turn: Turn, delegation: Delegation): void {
  turn.delegations = [...(turn.delegations ?? []), delegation];
  turn.activities.push({ kind: 'delegation', child_session_id: delegation.child_session_id });
}

/** #1017：memory/recalled 归位到「事件到达时的最后一轮」（= 它所属 run 的轮次）。
 *
 * 后端该事件只带 `run_id`、不带 `step_id`（recall.py 的 context select）；而
 * append-only 流里 `user/message`（建轮）→ `run/started` → `memory/recalled`
 * 的顺序恒成立，live 流、历史重放与 fork 重放下「最后一轮」与「run 归属」等价。
 *
 * #1017 审查 P3：run_id 双向可验时才归位——两侧都在且不等 = 乱序回跳的错挂
 * 信号（ADR-0016 的乱序小窗重排 DEFER 的防御），弃挂保真（事件仍在 events
 * 日志）；任一侧缺失 = 旧数据 / 信封无 run_id，无法证伪归属，按序归位。
 *
 * 为什么不走 withTurnAt/resolveStep：recalled 无 step_id，resolveStep 的兜底链
 * （active_step_id → turns.length+1）会为它**凭空新建**一个错位空轮。这里只
 * 附着、不建轮：无轮次可归属时（defensive，正常流不会出现）事件留在 events
 * 日志，不伪造归属。verbatim 引用入 turn.recalls——展开详情仍按 seq/run_id
 * 对账 /memory-recalls（不变量 #22：不复制、不改写事件真相）。 */
function projectMemoryRecalled(state: ConversationState, event: AgentEvent): void {
  const lastIdx = state.turns.length - 1;
  if (lastIdx === -1) return;
  const last = state.turns[lastIdx];
  const eventRunId = typeof event.run_id === 'string' && event.run_id ? event.run_id : null;
  if (eventRunId !== null && last.run_id != null && last.run_id !== eventRunId) return;
  const turn = cloneTurn(last);
  turn.recalls = [...(turn.recalls ?? []), event];
  replaceTurnAt(state, lastIdx, turn);
}

/** model activity 缺位回填（MODEL_COMPLETED 与 MODEL_DELTA/TEXT_DELTA 共享）：
 *  turn 还没有任何 model activity 时，把当前 model 段挂进 segments + activities。
 *  推 turn.model 本体（别名契约：segments[最新 model index] === turn.model）。 */
function ensureModelActivity(turn: Turn): void {
  if (turn.activities.some((a) => a.kind === 'model')) return;
  turn.segments.push(turn.model);
  turn.activities.push({ kind: 'model', index: turn.segments.length - 1 });
}

// ── 事件语义注册表（架构深化 C2）──────────────────────────────────────────
//
// 此前 applyEvent 与 summarizeEvent 各有一个 switch 遍历同一份事件词汇表——
// 新增事件类型要同时改两处，漏改不会报错，只会静默落进 unknown_events
// （契约漂移）。现在两个面合成一张**穷尽注册表**：
//
//     EVENT_SEMANTICS: Record<EventTypeValue, EventSemantics>
//
// `generated/event-types.ts`（后端 `session/event.py` 的生成物）新增类型时，
// `EventTypeValue` 联合类型扩展 → 本表缺键 → `tsc` 直接失败。这把「靠人记得
// 改两处」变成编译器的强制，是本批防漂移机制的核心。
//
// 词汇表内但前端尚无投影语义的类型**显式登记**为 unhandledProjection——
// 保持既有兜底行为（进 unknown_events）不变，同时让缺口可见，而不是隐式漏掉。

/** 单个事件类型的两个语义面。 */
interface EventSemantics {
  /** 投影：把事件折叠进 ConversationState（就地改写 state，无返回值）。 */
  apply: (state: ConversationState, event: AgentEvent) => void;
  /** Timeline 单行摘要。'' = 类型标签已足够；unknownSummary = 前端未定义语义。 */
  summarize: (event: AgentEvent) => string;
}

/** 已知类型但无投影——保持「进 unknown_events」的既有兜底行为。
 *  登记它而非省略，是为了让未接线的事件类型在注册表里可见。 */
/** #1197 项 5A：`compaction/start` / `compaction/end` 是 bracket 的两端，投影成
 *  一个布尔（进行中与否）——**不**再进 unknown_events（那是未接线兜底）。
 *  `compaction/end` 到达即恢复常态（票面：自动消失，不许静默）。 */
function projectCompactionInProgress(state: ConversationState): void {
  state.compaction_in_progress = true;
}

function projectCompactionEnded(state: ConversationState): void {
  state.compaction_in_progress = false;
}

function unhandledProjection(state: ConversationState, event: AgentEvent): void {
  state.unknown_events = [...state.unknown_events, event];
}

/** 投影 no-op：识别为已知事件，但不进轮次/工具（也不进 unknown_events）。 */
function noopProjection(): void {
  /* 会话生命周期事件等——真相已在 events 日志，无视图投影 */
}

// ── 摘要器 ──

/** 已知事件但无单行语义——类型标签已足够。 */
function emptySummary(): string {
  return '';
}

/** 前端未定义语义的事件摘要（未知类型 / 词汇表内未接线的类型）。
 *  UI-04 信任裂缝：不再把 payload JSON 切片甩给用户（行标签已是 type，
 *  原始 payload 在事件详情/UnknownSurface 兜底里 verbatim 可查）。 */
function unknownSummary(_event: AgentEvent): string {
  return '未接线的类型（payload 已保留，点行看详情）';
}

/** UI-04：forked / 审批族的单行语义（此前落「未知事件」）。 */
function summarizeForked(_event: AgentEvent): string {
  return '已另开一条';
}

function summarizeApprovalRequested(event: AgentEvent): string {
  const name = event.data.tool_name;
  return typeof name === 'string' && name ? `等待审批 · ${name}` : '等待审批';
}

function summarizePermissionResolved(event: AgentEvent): string {
  const decision = event.data.decision;
  return typeof decision === 'string' && decision ? `审批已决（${decision}）` : '审批已决';
}

// #526 A2：会话级审批授权的写/撤回——审计事件，不改变投影状态。
function summarizeApprovalGranted(event: AgentEvent): string {
  const tool = event.data.tool_name;
  return typeof tool === 'string' && tool ? `已授予会话级审批（${tool}）` : '已授予会话级审批';
}

function summarizeApprovalRevoked(): string {
  return '已撤回会话级审批';
}

// #526 B1：工作流档切换——审计事件，不改变投影状态。
function summarizeWorkflowModeChanged(event: AgentEvent): string {
  return event.data.mode === 'plan' ? '已切换到 Plan 模式' : '已切换到普通模式';
}

// #1359 阶段 1：目标生命周期事件——审计行，投影状态不在这里折叠。
const GOAL_OPERATION_LABELS: Record<string, string> = {
  create: '已创建目标',
  edit: '已修改目标',
  pause: '已暂停目标',
  resume: '已恢复目标',
  complete: '已完成目标',
  block: '目标已阻塞',
  clear: '已清除目标',
};

function summarizeGoalChanged(event: AgentEvent): string {
  const operation = String(event.data.operation ?? '');
  return GOAL_OPERATION_LABELS[operation] ?? `目标变更（${operation || '未知操作'}）`;
}

/** 增量类事件共用摘要（model/delta、text/delta、tool/output_delta、
 *  reasoning/delta 同一惯例：Timeline 行仍 verbatim 在场，摘要只记增量）。 */
function summarizeDeltaChars(event: AgentEvent): string {
  return `+${String(event.data.delta ?? '').length} 字符`;
}

// ── ADR-0030（#196）：在途输入通道的 Timeline 摘要 ──

/** 术语表 §2：queue（排队）= 等当前 run 结束后接力成下一个 run。 */
function summarizeMessageQueued(event: AgentEvent): string {
  const content = String(event.data.content ?? '').trim();
  return content ? `已排队 · ${truncateQueueSummary(content)}` : '已排队';
}

/** 术语表 §2：steer（引导）= 注入当前 run 的下一个模型调用前。 */
function summarizeSteerRequested(event: AgentEvent): string {
  const content = String(event.data.content ?? '').trim();
  return content ? `引导中 · ${truncateQueueSummary(content)}` : '引导中';
}

/** §4.5.1：被取代的那一轮整段从界面消失——Timeline 摘要只描述事实。 */
function summarizeSuperseded(event: AgentEvent): string {
  const seq = event.data.superseded_seq;
  return typeof seq === 'number' ? `第 ${seq} 条输入已被新内容取代` : '输入已被取代';
}

// #1280 AC4：`error`（#1193 致命失败信号，durable）不再空摘要——data.message 是
// 失败原因的可读口径，Timeline 行给人话，全文走事件钻取。与 summarizeRunFailed
// 同规则**不截断**：文案是完整句子，截断正好切掉可操作尾巴（ADR-0033 §2.3）。
function summarizeError(event: AgentEvent): string {
  const message = event.data.message;
  const detail = typeof message === 'string' ? message.trim() : '';
  return detail ? `任务失败 · ${detail}` : '任务失败';
}

/** 队列条/摘要共用的 1 行截断（§5.2：内容摘要 1 行截断）。 */
function truncateQueueSummary(content: string, max = 40): string {
  return content.length > max ? `${content.slice(0, max)}…` : content;
}

// ── applyEvent 的 per-type 投影 ──

function projectUserMessage(state: ConversationState, event: AgentEvent): void {
  // #1429 F6：用户新输入 = 新 run 的前奏，本轮轮次号由随后的 run/started 决定——
  // 不能继承恢复续轮的号（否则新轮带着上一轮的 turn_index，导航会出现重复药丸）。
  state.resumed_turn_index = null;
  const step = resolveStep(event, state);
  withTurnAt(state, step, (turn) => {
    touchTurn(turn, event);
    turn.user_message = String(event.data.content ?? '');
    // #825（MM-04）：附图是 `content` 的**平行字段**（PRD D3），事件里只有引用。
    // 与 `user_message` 的无条件覆盖不同，附件**非空才写**：同一 step 里重复到达的
    // 空帧（既有 projection 的 broken 不变量，见 `withTurnAt` 注释）不该把已解析出的
    // 引用抹掉——"后端这帧没带"与"这轮没有图"在这里不是同一件事。
    const attachments = parseImageRefs(event.data.attachments);
    if (attachments.length > 0) turn.user_attachments = attachments;
    // BUG-001：fork 锚点需要 user/message 的 seq（持久事实），
    // 而非 turn.step_id（resolveStep 合成值）。null-seq 帧不入册。
    if (event.seq !== null) {
      turn.user_message_seq = event.seq;
    }
    // Phase 12（ADR-0014 #69）：failure-guard soft 注入的纠正消息带
    // injected_by 标记——渲染层据此显示为系统提示条而非用户气泡。
    if (typeof event.data.injected_by === 'string' && event.data.injected_by) {
      turn.injected_by = event.data.injected_by;
    }
  });

  const request = state.pending_constraint_input;
  const requestId = event.data.input_request_id;
  const answer = parseConstraintInputAnswer(event.data.input_request_answer);
  if (request && requestId === request.request_id && answer?.request_id === request.request_id) {
    state.pending_constraint_input = { ...request, answer };
  }
  // #1204：同一条恢复通道（`resume_basis: 'user_input'`）也承载 ask_user 的答案——
  // 按事件 `input_request_answer` 的形状分流，两个待回答状态各记各的答案。
  const askRequest = state.pending_ask_user;
  const askAnswer = parseAskUserAnswer(event.data.input_request_answer);
  if (askRequest && requestId === askRequest.request_id && askAnswer?.request_id === askRequest.request_id) {
    state.pending_ask_user = { ...askRequest, answer: askAnswer };
  }
  // #1356：同一条恢复通道的第三支——计划审批的裁决（`{request_id, decision, feedback?}`）。
  const planRequest = state.pending_plan_approval;
  const planAnswer = parsePlanApprovalAnswer(event.data.input_request_answer);
  if (planRequest && requestId === planRequest.request_id && planAnswer?.request_id === planRequest.request_id) {
    state.pending_plan_approval = { ...planRequest, answer: planAnswer };
  }
}

const CONSTRAINT_INPUT_CHOICES = new Set<ConstraintInputChoice>([
  'replace_persistently',
  'current_task_only',
  'keep_existing',
  'custom',
]);

function parseConstraintInputAnswer(raw: unknown): ConstraintInputAnswer | null {
  if (!isRecord(raw)) return null;
  const { request_id, choice, custom_text } = raw;
  if (
    typeof request_id !== 'string' ||
    typeof choice !== 'string' ||
    !CONSTRAINT_INPUT_CHOICES.has(choice as ConstraintInputChoice)
  ) return null;
  if (custom_text !== undefined && typeof custom_text !== 'string') return null;
  return {
    request_id,
    choice: choice as ConstraintInputChoice,
    ...(typeof custom_text === 'string' ? { custom_text } : {}),
  };
}

function projectUserInputRequested(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  // #1204：通用提问 `ask_user` 与窄场景 `protected_fact_conflict` 共用事件类型，
  // 分流判据是 `data.kind`——前者走内嵌卡片状态，后者走原约束冲突状态，互不替代。
  if (data.kind === 'ask_user') {
    const askRequest = parseAskUserRequest(data, event.run_id ?? null);
    if (askRequest !== null) state.pending_ask_user = askRequest;
    return;
  }
  // #1356：计划审批是第三种 `USER_INPUT_REQUESTED` 载荷，同款分流、互不冒充。
  if (data.kind === 'plan_approval') {
    const planRequest = parsePlanApprovalRequest(data, event.run_id ?? null);
    if (planRequest !== null) state.pending_plan_approval = planRequest;
    return;
  }
  const rawChoices = Array.isArray(data.choices) ? data.choices : [];
  const choices = rawChoices.flatMap((raw) => {
    if (!isRecord(raw)) return [];
    const { id, label } = raw;
    if (
      typeof id !== 'string' ||
      !CONSTRAINT_INPUT_CHOICES.has(id as ConstraintInputChoice) ||
      typeof label !== 'string' ||
      !label.trim()
    ) return [];
    return [{ id: id as ConstraintInputChoice, label }];
  });
  const requestId = data.request_id;
  const runId = event.run_id ?? data.run_id;
  const factId = data.fact_id;
  const oldValue = data.old_value;
  const candidate = data.candidate;
  const question = data.question;
  const sourceEventId = data.source_event_id;
  const sourceEventSeq = numberOf(data.source_event_seq);
  if (
    typeof requestId !== 'string' || !requestId ||
    typeof runId !== 'string' || !runId ||
    typeof factId !== 'string' || !factId ||
    typeof oldValue !== 'string' ||
    typeof candidate !== 'string' ||
    typeof question !== 'string' ||
    typeof sourceEventId !== 'string' || !sourceEventId ||
    sourceEventSeq === undefined ||
    choices.length === 0
  ) return;
  const request: PendingConstraintInputRequest = {
    request_id: requestId,
    run_id: runId,
    fact_id: factId,
    old_value: oldValue,
    candidate,
    question,
    source_event_id: sourceEventId,
    source_event_seq: sourceEventSeq,
    choices,
    answer: null,
  };
  state.pending_constraint_input = request;
}

/** #1204：解析 `kind == "ask_user"` 的 `USER_INPUT_REQUESTED` 载荷 → 待回答请求。
 *
 *  形状不合契约（缺 request_id / 题集为空 / 题目非法）即返回 null——**不伪造**一张
 *  空卡片：零件缺失时宁可整条不投影（与 `projectUserInputRequested` 的窄场景同款）。
 *  每题 options 2–4、header ≤12 由工具端强制；这里只做**读取侧的存活校验**，
 *  不重放工具端的全部参数校验（读了坏数据就丢，不猜）。 */
function parseAskUserRequest(data: Record<string, unknown>, envelopeRunId: string | null): PendingAskUserRequest | null {
  const requestId = data.request_id;
  const runId = envelopeRunId ?? data.run_id;
  const rawQuestions = data.questions;
  if (typeof requestId !== 'string' || !requestId) return null;
  if (typeof runId !== 'string' || !runId) return null;
  if (!Array.isArray(rawQuestions) || rawQuestions.length === 0) return null;
  const questions: AskUserQuestion[] = [];
  for (const raw of rawQuestions) {
    if (!isRecord(raw)) return null;
    const { question, header, options, recommended } = raw;
    if (typeof question !== 'string' || !question) return null;
    if (typeof header !== 'string' || !header) return null;
    if (!Array.isArray(options) || options.length === 0) return null;
    const parsedOptions = options.flatMap((option) => {
      if (!isRecord(option)) return [];
      const { label, description } = option;
      if (typeof label !== 'string' || !label.trim()) return [];
      return [{
        label,
        ...(typeof description === 'string' && description ? { description } : {}),
      }];
    });
    if (parsedOptions.length !== options.length) return null;
    const parsedRecommended =
      typeof recommended === 'number' && Number.isInteger(recommended)
      && recommended >= 0 && recommended < parsedOptions.length
        ? recommended
        : undefined;
    questions.push({
      question,
      header,
      options: parsedOptions,
      ...(parsedRecommended === undefined ? {} : { recommended: parsedRecommended }),
    });
  }
  const timeoutMinutes = askUserTimeoutMinutes(data);
  return {
    request_id: requestId,
    run_id: runId,
    questions,
    answer: null,
    // #1284：显示用分钟数（后端随快照落盘；旧事件由 `timeout_seconds` 按同一公式
    // 换算）。非法值（0 / 负数 / 非整数 / 非数）⇒ 不投影，卡片回落中性提示
    // （零伪造，不把 0 钳成 1 分钟）。
    ...(timeoutMinutes === undefined ? {} : { timeout_minutes: timeoutMinutes }),
  };
}

/** #1204：解析 `ask_user` 的持久化答案（`USER_MESSAGE.data.input_request_answer`）。
 *  与 `parseConstraintInputAnswer` 同款：形状不合即 null（宁可当作"还没回答"）。 */
function parseAskUserAnswer(raw: unknown): AskUserAnswer | null {
  if (!isRecord(raw)) return null;
  const { request_id, questions } = raw;
  if (typeof request_id !== 'string' || !request_id) return null;
  if (!Array.isArray(questions)) return null;
  const parsed: AskUserQuestionAnswer[] = [];
  for (const item of questions) {
    if (!isRecord(item)) return null;
    const text = item.question;
    if (typeof text !== 'string') return null;
    const value = item.answer;
    if (value === null) {
      parsed.push({ question: text, answer: null });
      continue;
    }
    if (!isRecord(value)) return null;
    const { label, custom_text } = value;
    if (typeof label !== 'string' && typeof custom_text !== 'string') return null;
    parsed.push({
      question: text,
      answer: {
        ...(typeof label === 'string' ? { label } : {}),
        ...(typeof custom_text === 'string' ? { custom_text } : {}),
      },
    });
  }
  return { request_id, questions: parsed };
}

/** #1356：解析 `kind == "plan_approval"` 的 `USER_INPUT_REQUESTED` 载荷 → 待裁决计划。
 *
 *  与 `parseAskUserRequest` 同款：形状不合契约（缺 request_id / run_id、正文为空、
 *  `touched_files` 不是字符串数组）即返回 null——**不伪造**一张空卡片。`artifact_id`
 *  与 `plan_sha256` 是可选展示件：缺失就不显示那一行，不猜。
 *  只做**读取侧的存活校验**，不重放工具端的 100_000 字符上限（读了坏数据就丢）。 */
function parsePlanApprovalRequest(
  data: Record<string, unknown>, envelopeRunId: string | null,
): PendingPlanApprovalRequest | null {
  const requestId = data.request_id;
  const runId = envelopeRunId ?? data.run_id;
  const planMarkdown = data.plan_markdown;
  const rawFiles = data.touched_files;
  if (typeof requestId !== 'string' || !requestId) return null;
  if (typeof runId !== 'string' || !runId) return null;
  if (typeof planMarkdown !== 'string' || !planMarkdown.trim()) return null;
  if (!Array.isArray(rawFiles) || !rawFiles.every((f) => typeof f === 'string')) return null;
  const artifactId = data.artifact_id;
  const planSha256 = data.plan_sha256;
  return {
    request_id: requestId,
    run_id: runId,
    plan_markdown: planMarkdown,
    touched_files: rawFiles as string[],
    ...(typeof artifactId === 'string' && artifactId ? { artifact_id: artifactId } : {}),
    ...(typeof planSha256 === 'string' && planSha256 ? { plan_sha256: planSha256 } : {}),
    answer: null,
  };
}

/** #1356：解析计划审批的持久化裁决（`USER_MESSAGE.data.input_request_answer`）。
 *  形状不合即 null（宁可当作"还没回答"）——与另两支同款。 */
function parsePlanApprovalAnswer(raw: unknown): PlanApprovalAnswer | null {
  if (!isRecord(raw)) return null;
  const { request_id, decision, feedback } = raw;
  if (typeof request_id !== 'string' || !request_id) return null;
  if (decision !== 'approve' && decision !== 'revise' && decision !== 'reject') return null;
  if (feedback !== undefined && typeof feedback !== 'string') return null;
  return {
    request_id,
    decision,
    ...(typeof feedback === 'string' ? { feedback } : {}),
  };
}

/** #1204：清理两个「待回答的用户输入」状态里命中 `runId` 归属规则的那一份。
 *
 *  两个状态（`pending_constraint_input` / `pending_ask_user`）同源（`USER_INPUT_REQUESTED`）
 *  但 payload 形状不同，**清理规则始终一致**——收进一处，免去在 5 个 run 生命周期点
 *  （started 用 `stale`：清掉**别的** run 的残留；终态 / resumed 用 `for-run`：清掉**本**
 *  run 已收口的那一份）逐点双写、漏一处就留下幽灵卡片。
 *  `runId` 为 null（信封未带 run_id）时 `stale` 也照清（与旧行为一致）。 */
function clearPendingUserInput(
  state: ConversationState, runId: string | null, mode: 'stale' | 'for-run',
): void {
  const matches = (pendingRun: string | undefined) =>
    mode === 'stale' ? pendingRun !== runId : pendingRun === runId;
  if (matches(state.pending_constraint_input?.run_id)) state.pending_constraint_input = null;
  if (matches(state.pending_ask_user?.run_id)) state.pending_ask_user = null;
  if (matches(state.pending_plan_approval?.run_id)) state.pending_plan_approval = null;
}

function projectRunStarted(state: ConversationState, event: AgentEvent): void {
  state.run_status = 'running';
  clearPendingUserInput(state, event.run_id ?? null, 'stale');
  // OBS-007：新 run 开始 = 用户已经接着往下跑了，「上次运行…中断」这条提示随之
  // 过期——不清掉的话它会挂到会话生命结束，与后续 run 的真实结局（比如绿色
  // 「已完成」）同屏打架。清空后 `run_interrupted` 的语义收窄为「**最近一个** run
  // 以中断收口」，deriveRunPulse 也就据此给出中性的「已中断」而不是「已完成」。
  state.run_interrupted = null;
  // #220：失败归因同属「**最近一个** run」的事实，新 run 开始即过期（同 run_interrupted）。
  state.run_failure = null;
  // #226：本轮请求侧模型标识（run/started 持久携带）——每 run 各自一个值，故是
  // 「**最近一个** run」的镜像（与 run_failure / run_interrupted 同一失效规则）：
  // 新 run 开始即重置，本 run 没带该键就归 null（**不**保留上一轮的值）。必须放在
  // 下面 turn_index 的提前 return 之前：那个 return 只跳过 turn 回填。
  // 失效规则与「与 model 的归属对照」口径见 ADR-0034 §2.3。
  const requested = event.data.model;
  state.requested_model =
    typeof requested === 'string' && requested ? requested : null;
  // #537：run 预算镜像——run/started 是「最近一个 run」账本的**重置点**：
  // ceilings 从 data.budget.run 回显解析（后端 as_run_started_budget 只在配置了
  // run ceiling 时落键 → 没配 = null = 徽标整段不渲染）；消耗账无条件归零
  // （计数不依赖有没有 ceiling，徽标只在有 ceiling 时消费它）。
  const budgetEcho = isRecord(event.data.budget) && isRecord(event.data.budget.run)
    ? event.data.budget.run
    : null;
  state.run_budget_ceilings = budgetEcho ? parseRunLimitFacts(budgetEcho) : null;
  state.run_budget_consumed = { agent_turns: 0, model_requests: 0, total_tokens: 0, cost_usd: '0' };
  // #1313 口径 B：新 run 的流式槽（R）归零；会话累计槽（S）**不清零**——
  // 顶栏口径是「会话内所有 run 累计、单调非减」，run 边界不改变已挣得的数字。
  state.run_usage_live = null;
  // T9 #139：RUN_STARTED.data.turn_index（1-based）——该 session 里第几个 run
  // （后端 session.begin_run 定义）。每次 run 各自携带自己的值，因此这是
  // per-turn 事实，必须落到当轮 turn 上——若只存会话级会被最新 run 覆盖，
  // 导致所有历史轮次显示同一个数字。
  const idx = event.data.turn_index;
  const hasIdx = typeof idx === 'number' && Number.isFinite(idx);
  if (hasIdx) state.turn_index = idx; // 会话级镜像（Langfuse / turn 元数据消费）
  // #1429 F6：新 run 自带轮次号，恢复续轮的继承意图到此作废（无论本事件有没有带值）。
  state.resumed_turn_index = null;
  // 当轮 = 最后一个 turn：生产时序为 user/message（建轮）→ run/started，
  // 故 RUN_STARTED 到达时本轮 turn 已存在。不调用 withTurnAt——它在无匹配时
  // 会新建空 turn，而 RUN_STARTED 本身不携带 step（在途 run 的 step 无法解析），
  // 会凭空多出一个错位轮次。仅在已有轮次时回填 turn_index；
  // #1017 审查 P3：同时盖 run_id 归属戳（recalled 归位的校验锚）。
  const last = state.turns[state.turns.length - 1];
  const envelopeRunId = typeof event.run_id === 'string' && event.run_id ? event.run_id : null;
  if (last && (hasIdx || envelopeRunId !== null)) {
    const turn = cloneTurn(last);
    if (hasIdx) turn.turn_index = idx;
    if (envelopeRunId !== null) turn.run_id = envelopeRunId;
    replaceTurnAt(state, state.turns.length - 1, turn);
  }
}

function projectModelStarted(state: ConversationState, event: AgentEvent): void {
  const step = resolveStep(event, state);
  state.active_step_id = step;
  withTurnAt(state, step, (turn) => {
    touchTurn(turn, event);
    // 新 burst → 新段；空段（重复 MODEL_STARTED、尚无 delta）复用不追加
    const lastActivity = turn.activities[turn.activities.length - 1];
    const isEmptyHead =
      lastActivity?.kind === 'model' &&
      turn.model.text === '' &&
      turn.model.status === 'streaming';
    if (!isEmptyHead) {
      turn.model = { text: '', status: 'streaming' };
      turn.segments.push(turn.model);
      turn.activities.push({ kind: 'model', index: turn.segments.length - 1 });
    }
    turn.status = 'streaming';
  });
}

/** 文本增量（同一 append 语义双词汇）：MODEL_DELTA = legacy stream-only
 *  （运行时不再发射，词汇保留兼容旧历史/fixture）；TEXT_DELTA = T-contract
 *  （#116，后端契约回执 §2）durable 合帧落盘——seq 走既有 seenSeqs 去重门，
 *  重放（projectHistory）天然同构。block_id 恒无（文本按 turn/step 聚合）。
 *  回填：model/started 是 stream-only 不入历史，重放里 delta 无前驱——
 *  不回填则 activities 为空，渲染门跳过整个模型块（取消/失败轮次文本消失）。 */
function projectTextDelta(state: ConversationState, event: AgentEvent): void {
  const step = resolveStep(event, state);
  withTurnAt(state, step, (turn) => {
    touchTurn(turn, event);
    ensureModelActivity(turn);
    turn.model.text += String(event.data.delta ?? '');
    turn.model.status = 'streaming';
  });
}

function projectModelCompleted(state: ConversationState, event: AgentEvent): void {
  const step = resolveStep(event, state);
  const data = event.data;
  withTurnAt(state, step, (turn) => {
    // Final content may include consolidated text — prefer it over accumulated delta.
    turn.model.text = String(data.content ?? turn.model.text);
    turn.model.status = 'done';
    // 后端某些路径（无工具纯对话、重放的取消/失败轮次）没有 model/started
    // 前驱——回填 model activity，让 Conversation 的渲染入口存在（文本不丢）。
    ensureModelActivity(turn);
  });
  // Run-level observability（后端 Gap 1）：可选字段，缺失/畸形不伪造。
  // 同时记下这个回显属于哪个 run：`model` 不随新 run 失效，而 `requested_model` 是每
  // run 重置的——没有归属就无法判断两个值能否并排比较（ADR-0034 §2.3）。
  if (typeof data.model === 'string' && data.model) {
    state.model = data.model;
    state.model_run_id = event.run_id ?? null;
  }
  const usage = parseUsage(data.usage);
  if (usage) {
    // run/completed 权威聚合到达前，累计各次推理 usage 作为运行中视图。
    // C6 口径：usage 逐维可空，累加走 addUsage 保证**未知粘性**——某维一旦转
    // `null`，不再从 0 或旧值起算（与后端 `_accumulate_usage` 同语义）。
    state.usage_total = addUsage(state.usage_total, usage);
    // #1313 口径 B：同一次推理也计入**当前 run 流式槽**（R）。上面那行是 run 级
    // 运行中视图（StepDetail 消费），这里这行喂顶栏的会话累计（S + R）。
    state.run_usage_live = addUsage(state.run_usage_live, usage);
  }
  // #537：turns 的计数点与后端 `consumed_from_events` 同一事件（MODEL_COMPLETED）。
  const consumed = state.run_budget_consumed;
  if (consumed) {
    state.run_budget_consumed = { ...consumed, agent_turns: consumed.agent_turns + 1 };
  }
}

/** #537：MODEL_REQUEST 是 requests / tokens / cost 三个维的计数点（后端
 *  `consumed_from_events` 同一事件、同一口径）。此前本事件在投影里是 no-op——
 *  徽标需要 run 作用域账，而 `usage_total` 是 run 级槽却**不在 run 边界清零**
 *  （跨 run 混用，且 cost 不在内），不能拿来当 run 徽标的消耗。
 *
 *  粘性 None（后端同语义）：某请求缺 `usage.total_tokens` / `cost_usd` ⇒ 该维从
 *  这一刻起不可得（null），**不是** 0；cost 是十进制**字符串**累加（复用
 *  runBudget 的唯一十进制实现，绝不 float 化）。 */
function projectModelRequest(state: ConversationState, event: AgentEvent): void {
  const consumed = state.run_budget_consumed;
  if (!consumed) return;
  const usage = isRecord(event.data.usage) ? event.data.usage : null;
  const reportedTokens = usage?.total_tokens;
  // 整数判定与后端 `isinstance(int) and not bool` 同口径：非整数 usage 不是
  // "截断后可用"，而是粘性不可得（null）——Math.trunc 会把 100.5 累加成 100，
  // 那是后端账本里不存在的事实。
  const tokens =
    typeof reportedTokens === 'number' && Number.isInteger(reportedTokens)
      ? reportedTokens
      : null;
  const reportedCost = typeof event.data.cost_usd === 'string' ? event.data.cost_usd : null;
  state.run_budget_consumed = {
    agent_turns: consumed.agent_turns,
    model_requests: consumed.model_requests + 1,
    total_tokens:
      consumed.total_tokens === null || tokens === null ? null : consumed.total_tokens + tokens,
    cost_usd:
      consumed.cost_usd === null || reportedCost === null
        ? null
        : addDecimalTexts(consumed.cost_usd, reportedCost),
  };
}

function projectToolCall(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  const step = resolveStep(event, state);
  withTurnAt(state, step, (turn) => {
    touchTurn(turn, event);
    const id = String(data.tool_call_id ?? '');
    if (!turn.tools.find((t) => t.tool_call_id === id)) {
      turn.tools.push({
        tool_call_id: id,
        name: String(data.tool_name ?? 'unknown'),
        args: (data.args as Record<string, unknown>) ?? {},
        status: 'running',
        // Raw 档真相源：完整源事件原样透传（type/time/step_id/data，Trace Density Raw）
        raw_call: { ...event },
        // 事件真值时间优先（历史事件带 time）；SSE 帧无 time 时回退客户端时钟
        started_at: event.time ?? new Date().toISOString(),
      });
      turn.activities.push({ kind: 'tool', tool_call_id: id });
    }
  });
}

/** 工具结果配对定位两段式（性能修复：旧实现无条件 O(轮×工具) 全局扫描，正常流
 *  每个结果都白付）——定位逻辑抽 locateToolHostTurn 共享（T3 起与
 *  tool/output_delta 同用）：快路径按 step、慢路径按 tool_call_id 全局。 */
function projectToolResult(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  const callId = String(data.tool_call_id ?? '');
  const step = resolveStep(event, state);
  const hostStep = locateToolHostTurn(state, callId, step);
  withTurnAt(state, hostStep, (turn) => {
    const toolIdx = turn.tools.findIndex((t) => t.tool_call_id === callId);
    if (toolIdx === -1) return;
    // Backend serializes the full ToolResult via model_dump_json() — so content is
    // a JSON string shaped {ok, message, data, error_code, retryable, metadata, ...}.
    // The structured payload (incl. diff for edit/write) lives under `.data`.
    const interpreted = interpretToolResult(data.content);
    const parsed = interpreted.envelope;
    const parsedData = (interpreted.data ?? null) as Record<string, unknown> | null;
    const tool = cloneTool(turn.tools[toolIdx]);
    // bash 被超时/断连取消（data.cancelled，df4f7d8 §1.3）≠ 普通失败：
    // 映射到 stopped（中断 ≠ 错误，与 finalizeRun 的 stopped 同一语义域）。
    if (interpreted.outcome === 'cancelled') {
      tool.status = 'stopped';
    } else {
      tool.status = interpreted.outcome === 'success' ? 'success' : 'failed';
    }
    tool.result = parsedData ?? parsed?.message ?? data.content;
    // #1429 F1：失败分类只在**信封**里（`data` 为空时 result 退化成 message 字符串，
    // error_code 已在上面那行丢掉）。这里解析一次落成 per-tool 事实，视图不再
    // 回头拆 `result`/`raw_result`。ok=true / 无信封 ⇒ 无 error 字段（零伪造）。
    const errorShape = tool.status === 'failed' ? interpreted.error : null;
    if (errorShape) tool.error = errorShape;
    else delete tool.error;
    // Backend edit/write/apply_patch tools spread diff fields (before/after/truncated)
    // directly into ToolResult.data — not nested under data.diff. Detect them here.
    // >2000 字符的 before/after 变为截断摘要并内嵌 "use <读回工具>(<id>)" marker
    // ——diff 已归档，视图渲染占位态而非把 marker 当 diff 内容。工具名两个都认
    // （S3 → inspect_artifact，MinIO / Local → read_artifact），并**原样带下去**：
    // 面板要按这个部署真实可调的那个名字回显与复制（#186 AC4）。
    if (
      parsedData &&
      typeof parsedData.before === 'string' &&
      typeof parsedData.after === 'string'
    ) {
      const marker =
        parseArtifactMarker(parsedData.before) ?? parseArtifactMarker(parsedData.after);
      tool.diff = {
        before: parsedData.before,
        after: parsedData.after,
        truncated: parsedData.truncated === true,
        ...(marker !== null
          ? { archived: true as const, artifactId: marker.artifactId, artifactTool: marker.toolName }
          : {}),
      };
    }
    tool.completed_at = event.time ?? new Date().toISOString();
    tool.raw_result = { ...event };
    turn.tools[toolIdx] = tool;
  });
}

/** T3（#96，契约 C2）：stdout/stderr 逐段发射——配对复用 tool/result 的
 *  两段式定位（快路径 step、慢路径全局 tool_call_id）。未知工具不伪造；
 *  channel 严格两值（其余帧丢弃，不误标通道）；相邻同通道 delta 合并进
 *  尾块（数组规模有界）；result 到达后 chunks 保留（流式内容不丢弃）。 */
function projectToolOutputDelta(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  const outCallId = String(data.tool_call_id ?? '');
  const outText = String(data.delta ?? '');
  if (!outCallId || !outText || (data.channel !== 'stdout' && data.channel !== 'stderr')) return;
  const outStep = resolveStep(event, state);
  const outTarget = locateToolHostTurn(state, outCallId, outStep);
  withTurnAt(state, outTarget, (turn) => {
    const toolIdx = turn.tools.findIndex((t) => t.tool_call_id === outCallId);
    if (toolIdx === -1) return;
    const tool = cloneTool(turn.tools[toolIdx]);
    let chunks = tool.output ?? [];
    const last = chunks[chunks.length - 1];
    const channel = data.channel === 'stderr' ? 'stderr' : 'stdout';
    chunks =
      last && last.channel === channel
        ? [...chunks.slice(0, -1), { channel, text: last.text + outText }]
        : [...chunks, { channel, text: outText }];
    // 有界收缩（spec 03 §9.4 bounded DOM）：stdout/stderr 逐行交替时相邻
    // 同通道合并永不触发，数组随行数线性膨胀、每 delta 全量拷贝退化为 O(n²)
    // ——超限把头部区域按通道聚合（文本零丢失；交错顺序仅在头部降级，
    // 尾窗与 terminal 的 result 校准不受影响——终态顺序由 result 真相恢复）。
    if (chunks.length > MAX_OUTPUT_CHUNKS) {
      const half = chunks.length >> 1;
      const merged: ToolOutputChunk[] = [];
      for (const channel of ['stdout', 'stderr'] as const) {
        const text = chunks
          .slice(0, half)
          .filter((c) => c.channel === channel)
          .map((c) => c.text)
          .join('');
        if (text) merged.push({ channel, text });
      }
      chunks = [...merged, ...chunks.slice(half)];
    }
    tool.output = chunks;
    turn.tools[toolIdx] = tool;
  });
}

/** 权威聚合（后端 Gap 1/2 + trace_url 契约 2d7f87a）：事件携带的 usage_total
 *  覆盖前端累计值；cost_usd / trace_id / trace_url 缺失或 null 保持 null
 *  （费率表未定义 / Langfuse 未接入）。trace_id 与 trace_url 并列不互替。 */
function projectRunCompleted(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  state.run_cancelled = false;
  clearPendingUserInput(state, event.run_id ?? null, 'for-run');
  // #220：失败归因是「最近一个 run 的结局」，更晚的终态一到它就过期（同 run_cancelled 复位）。
  state.run_failure = null;
  // #1313 口径 B：权威值**替换**本 run 的流式贡献（不是叠加——双计是本票的核心坑）：
  // authoritative 存在时绝不把 R 再加一次；缺权威值（事件未携带）时才用流式值兜底。
  const authoritative = parseUsage(data.usage_total);
  state.usage_total = authoritative ?? state.usage_total;
  const terminal = authoritative ?? state.run_usage_live;
  if (terminal) state.session_usage_total = addUsage(state.session_usage_total, terminal);
  state.run_usage_live = null;
  state.cost_usd =
    typeof data.cost_usd === 'number' && Number.isFinite(data.cost_usd) ? data.cost_usd : null;
  state.trace_id = typeof data.trace_id === 'string' && data.trace_id ? data.trace_id : null;
  state.trace_url = typeof data.trace_url === 'string' && data.trace_url ? data.trace_url : null;
  finalizeRun(state, 'completed', event.time);
}

/** 终态 reason（契约回执 §4，detached-run）：'cancelled' = 显式
 *  POST /cancel（用户意图中断，≠ 错误）→ Run Pulse「已取消」；
 *  'orphaned' = 孤儿回收（零订阅 300s，非用户意图，按失败展示）；
 *  其余（#222 起）**总有值**：已分类故障给 `provider_*`，未分类给异常类型名
 *  （如 `RateLimitError`）——reason 是开集，只对上面两个字面量做等值判断，
 *  别把它当枚举/白名单用（取值域与呈现见 ADR-0033 §2.1/§2.3）。缺省只会出现在
 *  #222 之前写下的历史会话里。断连永远不出现在终态原因里（订阅者离开只
 *  unsubscribe）。turn/tool 仍按失败终态 settle。
 *  trace_id / trace_url 对称抽取（契约 2d7f87a——失败 run 在 Langfuse 也有
 *  可见 trace，跳转有排查价值；此前 failed 分支漏抽 trace_id 是 pre-existing bug）。 */
function projectRunFailed(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  clearPendingUserInput(state, event.run_id ?? null, 'for-run');
  state.run_cancelled = isCancelledRunFailure(data);
  // #220：折叠失败归因。要点三条（载荷形状与呈现口径见 ADR-0033 §2.2/2.3）：
  // 取消那支不记（取消 ≠ 错误，da394a9）；两个键互相独立、都可缺；都没给就整体 null
  // ——不是 `{reason:null,message:null}`，那会让 `if (run_failure)` 为真却无内容。
  const reason = typeof data.reason === 'string' && data.reason ? data.reason : null;
  const message = typeof data.message === 'string' && data.message ? data.message : null;
  state.run_failure =
    state.run_cancelled || (reason === null && message === null) ? null : { reason, message };
  state.trace_id = typeof data.trace_id === 'string' && data.trace_id ? data.trace_id : null;
  state.trace_url = typeof data.trace_url === 'string' && data.trace_url ? data.trace_url : null;
  // #1313 口径 B（裁决记录：失败 run 计入会话累计；王浩宇可一句话推翻）：失败 run 已
  // 消耗的用量结转进 S，R 归零。口径与 `run/completed` 对齐——事件带 `usage_total`
  // 时以权威值为准（R 为 null 即客户端错过该 run 的 model/completed 时，权威值是唯
  // 一读数）；缺权威值才退回流式累计。`data.usage_total` 仍不进 run 级槽——那是既有
  // 契约（run/completed 才有权威聚合），本行只动顶栏口径的会话累计槽。
  const authoritative = parseUsage(data.usage_total) ?? state.run_usage_live;
  if (authoritative) state.session_usage_total = addUsage(state.session_usage_total, authoritative);
  state.run_usage_live = null;
  finalizeRun(state, 'failed', event.time);
}

/** T8 #138：崩溃恢复——run 被进程重启打断。与 run/completed / run/failed 同属
 *  终态（RUN_TERMINAL_TYPES），但语义是「中断」而非「完成」。finalizeRun 把
 *  streaming 段 settle 为 done，running 工具标记 stopped（中断 ≠ 错误）。 */
function projectRunInterrupted(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  clearPendingUserInput(state, event.run_id ?? null, 'for-run');
  state.run_interrupted = {
    step_id: event.step_id ?? null,
    interrupted_seq: typeof data.interrupted_seq === 'number' ? data.interrupted_seq : null,
    reason: typeof data.reason === 'string' ? data.reason : 'process_restart',
  };
  state.run_failure = null; // #220：中断是这轮 run 的**结局**终态，过期归因同 run/completed 清掉
  // #1313 口径 B：中断 run 的用量**同样结转进 S**，口径照 `run/failed`（token 已经
  // 被消耗，中断不是没发生）。不结转的后果是回归：下一次 `run/started` 清 R，这段
  // 用量永久丢失。
  const authoritative = parseUsage(data.usage_total) ?? state.run_usage_live;
  if (authoritative) state.session_usage_total = addUsage(state.session_usage_total, authoritative);
  state.run_usage_live = null;
  finalizeRun(state, 'completed', event.time);
}

/** 某个键的**整数读数**：只在真为有限数时采用，否则 null（= 该维缺席，不是 0）。 */
function dimensionNumber(raw: Record<string, unknown>, key: string): number | null {
  return numberOf(raw[key]) ?? null;
}

/** 某个键的**十进制读数**（cost 维）：wire 上是十进制**字符串**（也可能是数），
 *  原样保留字符串形态，不在前端转 float（`11 §6.1`：二进制浮点相等不是契约）；
 *  空串 / 形状不合 ⇒ null（unavailable，不是 0）。 */
function dimensionDecimalText(raw: Record<string, unknown>, key: string): string | null {
  const value = raw[key];
  if (typeof value === 'string') return value.trim() === '' ? null : value.trim();
  const num = numberOf(value);
  return num === undefined ? null : String(num);
}

/** 某个键的**时刻文本**（deadline 维，`#315`）：wire 上是 RFC 3339 UTC 的**文本**
 *  （`RunLimits.as_projection` 在 `limits.run` 里放的那个键；形状冻结在 ADR-0046 §D7）。
 *
 *  与 `dimensionDecimalText` 分开的理由是形状判据不同：数在 cost 维是合法读数
 *  （`0.3` 与 `"0.3"` 同义），在 deadline 维**不是**——`0` / epoch 毫秒都进不了后端
 *  `parse_deadline_at`，前端把它收成时刻等于编出一个任何事件里都不存在的读数。
 *
 *  只认**非空、无首尾空白**的文本：后端的**事件回读**（`_deadline_or_none`）不 strip
 *  （`fromisoformat(" 2026-…Z ")` 抛 ValueError ⇒ 那一侧读作"没配"），这里就地收下
 *  等于同一份 durable 事件两端给出互相矛盾的读数。更深的形状判据（带时区 / 严格未来）
 *  属**恢复草稿**那一关（`runBudget.deadlineDraftError` → `parseInstant`），显示链按
 *  契约原样呈现；非 ISO 文本的差异登记在 ADR-0045 §6.1。 */
function dimensionInstantText(raw: Record<string, unknown>, key: string): string | null {
  const value = raw[key];
  if (typeof value !== 'string' || value === '') return null;
  return value.trim() === value ? value : null;
}

/** 分维计数表（`#314`：工具名 → 计数）：**整键缺席 ⇒ null**（未知），空对象 ⇒ `{}`
 *  （已知，一个都没有）——两者的区别与 `cost_usd` 的"缺失 ≠ 0"同源。
 *
 *  `minimum` 由调用方给：**计数**表放行 0（"调了 0 次"是事实），**ceiling** 表要求
 *  ≥ 1（后端 `parse_tool_call_limits` 只收正整数，`0` 的形状在那边必然 422）——
 *  用同一个下限会让 `{"glob": 0}` 在前端被当成合法 ceiling 收下。表里不合形状的
 *  条目**整条丢掉**（宁少不多：一个编不出来的计数不该冒充 0）。 */
function countsByKey(
  raw: Record<string, unknown>,
  key: string,
  minimum: 0 | 1,
): Record<string, number> | null {
  const value = raw[key];
  if (!isRecord(value)) return null;
  const out: Record<string, number> = {};
  for (const [name, count] of Object.entries(value)) {
    const num = numberOf(count);
    if (num !== undefined && Number.isInteger(num) && num >= minimum) out[name] = num;
  }
  return out;
}

/** `data.consumed` → 四维读数 + 两张分维工具计数表（`#313` / `#314`）。整组缺失
 *  ⇒ null（老暂停载荷没有这组键，不是"四维都是 0"）。 */
function parseConsumedFacts(raw: unknown): RunBudgetDimensionFacts | null {
  if (!isRecord(raw)) return null;
  return {
    agent_turns: dimensionNumber(raw, 'agent_turns'),
    model_requests: dimensionNumber(raw, 'model_requests'),
    total_tokens: dimensionNumber(raw, 'total_tokens'),
    cost_usd: dimensionDecimalText(raw, 'cost_usd'),
    // `#314`：两个 counter 各自独立读（`tool_calls` 是逻辑调用，`tool_attempts`
    // 含 retry）——它们不是别名，缺一个不等于另一个也可得。
    tool_calls: dimensionNumber(raw, 'tool_calls'),
    tool_attempts: dimensionNumber(raw, 'tool_attempts'),
    tool_calls_by_tool: countsByKey(raw, 'tool_calls_by_tool', 0),
    tool_attempts_by_tool: countsByKey(raw, 'tool_attempts_by_tool', 0),
  };
}

/** `data.limits.run` → 四维 `max_*` ceiling + deadline 时刻 + per-tool 配额表
 *  （`#313` / `#314` / `#315`）。四个 `max_*` 的键名是载荷自己的形态（与恢复请求
 *  `budget.run` 的键同一套名字）；`null` = 该维没配（unlimited）。`deadline_at`
 *  不是第五个 `max_*`：它判的是时刻先后，没有 consumed/ceiling 可比。 */
function parseRunLimitFacts(raw: unknown): RunLimitsFacts | null {
  if (!isRecord(raw)) return null;
  return {
    max_agent_turns_total: dimensionNumber(raw, 'max_agent_turns_total'),
    max_model_requests: dimensionNumber(raw, 'max_model_requests'),
    max_total_tokens: dimensionNumber(raw, 'max_total_tokens'),
    max_cost_usd: dimensionDecimalText(raw, 'max_cost_usd'),
    // `#315`：deadline 维（与四个 `max_*` 同住 `limits.run`，但判的是时刻先后）。
    // 缺席 / 不是文本 ⇒ null（没配 deadline）——**不是**"值缺失 ⇒ 编一个"。
    deadline_at: dimensionInstantText(raw, 'deadline_at'),
    // `#314`：`tool_call_limits` 是**绝对** ceiling 表（不是 remaining），
    // 未配置的工具名不出现在表里（表缺席 = 没配任何工具配额）。下限是 1：
    // 后端只收正整数 ceiling（`parse_tool_call_limits`），`0` 必然在那个入口 422。
    tool_call_limits: countsByKey(raw, 'tool_call_limits', 1),
  };
}

/** `run/paused` 载荷 → `RunPausedInfo`（投影与 Timeline 摘要**共用同一解析**：
 *  两处各读一遍字段就会各有一套兜底默认值，改一处忘一处就是两条真相）。
 *
 *  读取一律"缺失即缺席"（零伪造）：`run_id` 取事件自身归属（`event.run_id` 是权威，
 *  载荷里没有同名键）；数字键仅在真为有限数时采用。 */
function parsePausedInfo(event: AgentEvent): RunPausedInfo {
  const data = event.data;
  const limits = isRecord(data.limits) ? data.limits : null;
  const runScope = limits && isRecord(limits.run) ? limits.run : null;
  const localScope = limits && isRecord(limits.local) ? limits.local : null;
  const consumed = isRecord(data.consumed) ? data.consumed : null;
  const localTurns = localScope ? numberOf(localScope.max_agent_turns) : undefined;
  return {
    run_id: typeof event.run_id === 'string' ? event.run_id : '',
    version: numberOf(data.budget_version) ?? 1,
    pause_seq: event.seq,
    reason: typeof data.reason === 'string' && data.reason ? data.reason : 'budget_exhausted',
    trigger_dimension:
      typeof data.trigger_dimension === 'string' ? data.trigger_dimension : '',
    consumed_agent_turns: numberOf(consumed?.agent_turns) ?? 0,
    run_limit: runScope ? (numberOf(runScope.max_agent_turns_total) ?? null) : null,
    consumed_dimensions: parseConsumedFacts(consumed),
    run_limits: parseRunLimitFacts(runScope),
    local_fuse:
      localTurns === undefined
        ? null
        : {
            max_agent_turns: localTurns,
            source:
              localScope && typeof localScope.source === 'string' ? localScope.source : '',
          },
    continuation: parseContinuation(data.continuation),
    closeout_source:
      typeof data.closeout_source === 'string' ? data.closeout_source : 'deterministic',
    resume_requirements: stringList(data.resume_requirements),
    trace_id: typeof data.trace_id === 'string' && data.trace_id ? data.trace_id : null,
    input_request_id:
      typeof data.input_request_id === 'string' && data.input_request_id
        ? data.input_request_id
        : null,
  };
}

/** `#312` T4：预算到顶的**非终态**暂停。
 *
 *  与三个终态投影的关键差别：`run_status` 收成 `'paused'`（不是 completed/failed/
 *  interrupted），并且**不**落 `run_failure` —— 暂停没有失败归因可言。已经跑过的
 *  这一轮按 `finalizeRun` 的共用清扫 settle（caret 停、running 工具标 stopped），
 *  因为它所属的执行区间确实结束了；恢复会从 `step_base+1` 起新的一轮执行
 *  （后端 runtime 的 `step_base = max(session.max_step_id, session.user_turn_count)`），
 *  所以历史轮次照旧可读、新工作进新轮。 */
function projectRunPaused(state: ConversationState, event: AgentEvent): void {
  state.run_paused = parsePausedInfo(event);
  // 暂停不是失败：归因面必须清空（否则同屏出现"已暂停"与上一轮的失败原因）。
  state.run_failure = null;
  finalizeRun(state, 'paused', event.time);
}

/** `#312` T4：同 run 恢复——服务层 CAS 通过后落 `run/resumed`，随后才启动执行。
 *
 *  它**不是**新的 `run/started`（后端不变量：同一逻辑 run 里只有一条），所以这里
 *  只做两件事：收起暂停事实、把 run 状态放回 running。turn 级状态不动——新执行的
 *  事件会自己建新轮（`step_base+1`），`finalizeRun('paused')` 已经 settle 过旧轮。 */
function projectRunResumed(state: ConversationState, event: AgentEvent): void {
  state.run_paused = null;
  clearPendingUserInput(state, event.run_id ?? null, 'for-run');
  state.run_status = 'running';
  // #1429 F6：恢复沿用同一逻辑 run（同一 run_id、同一 turn_index），续轮没有自己的
  // run/started 可回填 ⇒ 把会话镜像里的轮次号交给下一条新建的轮继承。
  state.resumed_turn_index = state.turn_index;
  // #537：恢复后的 ceilings 用**恢复事件自带的新快照**更新（run/resumed 载荷的
  // data.limits.run 与 run/paused 同形，值是恢复生效后的 effective limits）——
  // 徽标与暂停面板消费同一条恢复链，不 fetch 第二份。载荷没带 limits（旧会话
  // 的恢复事件）⇒ 原样保留：键缺席 ≠ 没配（零伪造）。消耗账不动（后端恢复
  // 保留 consumed，前端折叠本来就连续）。
  const runScope = isRecord(event.data.limits) && isRecord(event.data.limits.run)
    ? event.data.limits.run
    : null;
  if (runScope) {
    state.run_budget_ceilings = parseRunLimitFacts(runScope);
  }
}

/** Large tool output offloaded to ArtifactStore (Phase 5, spec 06 §15).
 *  Attach the ref to the producing tool call so the Inspector can fetch it.
 *
 *  `artifact/created` 与 `artifact/externalized` **共用**这段：两者的 payload 同构
 *  （artifact_id / tool_call_id / size / mime_type / source_tool），只是历史上一个是
 *  规格里的名字、一个是运行时真正发的名字（详见 #173）。返回 false = 找不到宿主
 *  tool_call，由调用方决定兜底。 */
function attachArtifactToTool(state: ConversationState, data: Record<string, unknown>): boolean {
  const toolCallId = String(data.tool_call_id ?? '');
  const turnIdx = state.turns.findIndex((t) => t.tools.some((tc) => tc.tool_call_id === toolCallId));
  if (turnIdx === -1) return false;
  const prevTurn = state.turns[turnIdx];
  const toolIdx = prevTurn.tools.findIndex((tc) => tc.tool_call_id === toolCallId);
  const tool = cloneTool(prevTurn.tools[toolIdx]);
  /* 元数据缺了就留 `null`（#186 AC5 / #185 AC4）：MinIO 不持久化 `source_tool`，
     一个编出来的 `''`/`0`/`'application/octet-stream'` 会变成界面上一个假的字节数与
     假的类型。`artifact_id` 是必有的（没它就没有这个产物，找不到宿主时上面已早退）。 */
  tool.artifact = {
    artifact_id: String(data.artifact_id ?? ''),
    size: typeof data.size === 'number' ? data.size : null,
    mime_type: typeof data.mime_type === 'string' ? data.mime_type : null,
    source_tool: typeof data.source_tool === 'string' ? data.source_tool : null,
  };
  const turn = cloneTurn(prevTurn);
  turn.tools[toolIdx] = tool; // cloneTurn 已给出新 tools 数组，原位替换即可
  replaceTurnAt(state, turnIdx, turn);
  return true;
}

/** 历史行为不变：找不到宿主就静默（该类型此前的语义就是如此）。 */
function projectArtifactCreated(state: ConversationState, event: AgentEvent): void {
  attachArtifactToTool(state, event.data);
}

/** 运行时**真正**发出的外置事件（`artifact/externalized`，见 `tooling/overflow.py`）。
 *
 *  此前它被登记为「词汇表内但前端尚未接线」→ 落 `unknown_events`，导致有产物的会话里
 *  Artifacts 页签恒空、页面还写「本次会话未产生 Artifact。」（第十一轮真机验收 ART-01）。
 *
 *  与 `artifact/created` 的**唯一**差别：找不到宿主 tool_call 时**不静默**——externalized
 *  自带 artifact_id，是"这里确实有一个外置产物"的独立事实，静默丢弃会让用户既看不到产物、
 *  TRACE 里也不再有任何痕迹。 */
function projectArtifactExternalized(state: ConversationState, event: AgentEvent): void {
  if (!attachArtifactToTool(state, event.data)) {
    unhandledProjection(state, event);
  }
}

/** Context window exceeded → older turns summarized (Phase 5, spec 06).
 *  Run-level metadata for the Inspector Context panel. */
function projectContextCompacted(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  state.compactions = [
    ...state.compactions,
    {
      compacted_turn_count: Number(data.compacted_turn_count ?? 0),
      summary_message_count: Number(data.summary_message_count ?? 0),
      token_estimate: Number(data.token_estimate ?? 0),
      fallback_used: data.fallback_used === true,
      time: event.time,
    },
  ];
}

/** A tool operation crashed mid-flight and needs human裁决 (Phase 4/5, spec 07 §13).
 *  Surfaces in the Inspector as an approval queue item. */
function projectOperationReconcileRequired(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  state.reconcile_queue = [
    ...state.reconcile_queue,
    {
      tool_call_id: String(data.tool_call_id ?? ''),
      tool_name: String(data.tool_name ?? ''),
      args_identity: String(data.args_identity ?? ''),
      state: String(data.state ?? 'NEED_RECONCILE'),
      time: event.time,
    },
  ];
}

/** `session/started`（会话第一条事件）：`started_data` 是会话级初始配置的**加法槽**
 *  （T7 #137）。今天这里取两个创建期定值：F15 #234 落进来的权限档
 *  （`permission_mode`）与 WS-1 / #151 的 cwd 锚（`cwd`，#1028 的归属 chip 读侧）。
 *
 *  **逐字段只认第一个带值的 `session/started`**：这些键都是**创建时**声明、写后不可变
 *  （会话内改档走 `permission/changed`，F18-B #283 起那边「最后一条胜」）——所以重放 /
 *  迟到重复投递都不得让后来的值改写先到的（与后端 `approval.py::declared_permission_mode`
 *  同一规矩——那边也是在第一条 started 上取键，缺键即 None）。实现按"该字段已非 null
 *  即不再收"达成这一点（"没带键"读作"没声明"，所以脏日志里**后**一条仍可声明：比后端
 *  "只在第一条上取键"宽一格，为这个不可达分支不再多存 seen 标志——判据由下方单测钉住）；
 *  今天每会话恰好一条 started（`session.py` 的 `Session.start` 只 append 一次），
 *  分叉 child 也是自带继承值而非复用父的 started。值不是非空字符串（老日志没有这个
 *  键、或日志被手改）→ 保持 null = "未声明"，不编默认值。
 *
 *  注意 `permission_mode` 它**不是** `permission_policy`：后者是审批请求到达时
 *  ToolExecutor 实际用的阈值，两者可以合法地不同（#236）。 */
function projectSessionStarted(state: ConversationState, event: AgentEvent): void {
  // 逐字段「只认第一条带值的 started」（不是整条事件早退）：两个键各自独立——
  // 一条 started 声明了 permission_mode 却没带 cwd（或反之）时，不能因为其中一个
  // 已定值就丢掉另一个键的首次声明。这也让下面 cwd 的「写后不可变」与 #236 的
  // 「创建声明是定值」共用同一句判据，而不是各写一份。
  const mode = event.data.permission_mode;
  if (state.session_permission_mode === null && typeof mode === 'string' && mode) {
    state.session_permission_mode = mode;
  }
  // #1028：cwd 锚（归属 chip 的数据源，WS-1 / #151 的读侧）。非空字符串才收，
  // 否则保持 null = "未声明"（老日志 / 脏值不制造假路径）。
  const cwd = event.data.cwd;
  if (state.session_cwd === null && typeof cwd === 'string' && cwd) {
    state.session_cwd = cwd;
  }
}

/** F18-B（#283）：`permission/changed` —— 会话内改档的 durable 事实（ADR-0041 D2）。
 *
 *  **最后一条胜**（ADR-0041 D3），与 `model/changed`（`projectModelChanged`）同形。这里
 *  **刻意不**复用 `projectSessionStarted` 的「已非 null 即早退」——那条规矩的存在理由正是
 *  「档位是会话属性、创建后不可变」，而本票的前提就是推翻它。
 *
 *  **坏值读作「未声明」⇒ 写 null**：后端 `effective_permission_mode`
 *  （`session/approval.py`）命中最后一条 `permission/changed` 后**不再往下找**，值不可
 *  解析时按未声明返回 None、**不回落** `session/started` 的声明值。前端逐字照抄这个
 *  优先级：两边对「当下生效的是哪一档」必须同口径，否则会重现 #236 的 P2——UI 说只读、
 *  后端按更松的档自动放行写操作。
 *
 *  **只动 `session_permission_mode`**：事件里还带 `auto_approve`，但前端今天没有它的
 *  消费者（没有「批准策略」控件）——三概念分离见 ADR-0041 D6，`permission_policy`
 *  （审批观测阈值）在这条路径上**一律不碰**。 */
function projectPermissionChanged(state: ConversationState, event: AgentEvent): void {
  const mode = event.data.permission_mode;
  state.session_permission_mode = typeof mode === 'string' && mode ? mode : null;
}

/** `permission/changed` 在时间线里的一行（对齐 `summarizeModelChanged`）：改档是用户自己
 *  发起的会话级变更，留一行真值比留一行空白更如实——与 `model/changed` 同待遇。 */
function summarizePermissionChanged(event: AgentEvent): string {
  const mode = event.data.permission_mode;
  return typeof mode === 'string' && mode ? `权限档 → ${mode}` : '权限档已变更';
}

/** #37 交互式审批（PRD §2.2）：ToolExecutor._check_approval 暂停 run，
 *  发 tool/approval-requested 事件；前端 ApprovalCard 内联渲染。 */
function projectToolApprovalRequested(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  const approvalId = String(data.approval_id ?? '');
  if (!approvalId) return; // 契约必有 approval_id
  // 生效阈值逐事件折叠（最后一条胜）——放在幂等早退**之前**：重放时它仍是同一个值，
  // 但这样就不依赖"请求只到达一次"这个假设。
  const policy = String(data.policy ?? '');
  if (policy) state.permission_policy = policy;
  // 幂等：重放已存在的 approval_id 不重复入队
  if (state.pending_approvals.some((a) => a.approval_id === approvalId)) return;
  state.pending_approvals = [
    ...state.pending_approvals,
    {
      approval_id: approvalId,
      tool_name: String(data.tool_name ?? ''),
      tool_call_id: String(data.tool_call_id ?? ''),
      action_type: String(data.action_type ?? ''),
      title: String(data.title ?? ''),
      description: String(data.description ?? ''),
      arguments_preview: (data.arguments_preview ?? {}) as Record<string, unknown>,
      permission: String(data.permission ?? ''),
      policy: String(data.policy ?? ''),
      reason: String(data.reason ?? ''),
      allowed_decisions: Array.isArray(data.allowed_decisions)
        ? data.allowed_decisions.map(String)
        : [],
      time: event.time,
    },
  ];
}

/** #37 审批已决——从 pending_approvals 移出队列，**同时留痕到 `approval_decisions`**
 *  （#184 Inspector PERMISSION 段要回答"裁决结果"，而队列语义是"决议即消失"）。
 *
 *  `tool_name` 在移除**之前**从同 id 的请求上取——队列是这条信息的唯一来源，先删就
 *  取不到了。配不上对（事件窗口从中间开始 / 未知 id）时留空，由渲染层显示 `—`；
 *  不编造工具名，也不为了"看起来完整"去 pending 之外再猜一次。 */
function projectPermissionResolved(state: ConversationState, event: AgentEvent): void {
  const approvalId = String(event.data.approval_id ?? '');
  if (!approvalId) return; // 契约必有 approval_id
  const request = state.pending_approvals.find((a) => a.approval_id === approvalId);
  state.pending_approvals = state.pending_approvals.filter((a) => a.approval_id !== approvalId);
  // 幂等：重放同一 approval_id 的决议不重复留痕（JSONL 回放会重放全部事件）。
  if (state.approval_decisions.some((d) => d.approval_id === approvalId)) return;
  state.approval_decisions = [
    ...state.approval_decisions,
    {
      approval_id: approvalId,
      decision: String(event.data.decision ?? ''),
      reason: String(event.data.reason ?? ''),
      tool_name: request?.tool_name,
      time: event.time,
    },
  ];
}

/** Inspector PERMISSION 段（#184）的三个真相在投影状态上，由 `lib/permission.ts` 的
 *  `permissionView` 组装成渲染视图——这里**不再加一层纯透传**（review 删掉了那层：
 *  它只是把三个字段抄一遍，多一层就多一处要同步的地方）。 */

/** Phase 12 白盒透明（ADR-0014 #69）：#69 RepeatedToolFailureGuard——连续同错
 *  工具调用熔断。soft 已由后端注入 user-role 纠正消息（injected_by 标记，见
 *  USER_MESSAGE 分支）；hard 意味着本轮即将 end_run 终止。事件落所在轮 notices 供渲染。 */
function projectToolFailureGuard(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  const step = resolveStep(event, state);
  withTurnAt(state, step, (turn) => {
    turn.notices = [
      ...(turn.notices ?? []),
      {
        level: data.level === 'hard' ? 'hard' : 'soft',
        tool_name: typeof data.tool_name === 'string' ? data.tool_name : '',
        consecutive_failures:
          typeof data.consecutive_failures === 'number' && Number.isFinite(data.consecutive_failures)
            ? data.consecutive_failures
            : 0,
      },
    ];
  });
}

/** 模型两级 fallback：主模型失稳（ModelStallError / APIConnectionError 等）
 *  切换到备选。记录最近一次切换供模型卡「已切换」态；后续推理已在
 *  to_model 上——同步 state.model（下一个 model/completed 亦会确认）。 */
function projectModelFallback(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  const from = typeof data.from_model === 'string' ? data.from_model : '';
  const to = typeof data.to_model === 'string' ? data.to_model : '';
  const reason = typeof data.reason === 'string' ? data.reason : '';
  if (from && to) {
    state.model_fallback = { from_model: from, to_model: to, reason };
    state.model = to;
    state.model_run_id = event.run_id ?? null; // 同 echo：记归属（切换是 run 内的事实）
  }
}

/** T7 #137：会话级模型切换——更新 conversation.model 为新模型。
 *  切换不打断在途 run，下一轮 run 从事件流派生当前模型生效。 */
// ── #381（W-27）：task/plan_updated → state.plan ─────────────────────────────
//
// 后端 handler 已硬校验（单 in_progress / 状态机 / id 唯一 / 软上限 50，
// session/plan.py，PRD 长任务 §7.2）⇒ 前端只防「手写 / 污染 JSONL 的旧数据」，
// 与后端 derive_plan 同一容错哲学：一行坏数据只损失该行，不 brick 恢复链。
//   * 行必须是非空普通对象且 id 为非空字符串（id 是覆盖对齐键与 React key，
//     缺了没法定点）；重复 id 首个胜；
//   * 其余字段防御性归一（content 非字符串取空串、activeForm 缺失回落 content、
//     status / source 非 string 记 ''）——**不改写、不猜测状态机**：双 in_progress
//     等违规原样进 state.plan，渲染端容错归 PlanList（票面）；
//   * data.items 非数组 = 坏帧整个忽略（清单保持原值）；[] 是合法清空。
function projectPlanUpdated(state: ConversationState, event: AgentEvent): void {
  const rawItems = event.data.items;
  if (!Array.isArray(rawItems)) return;
  const items: PlanItem[] = [];
  const seen = new Set<string>();
  for (const raw of rawItems) {
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) continue;
    const row = raw as Record<string, unknown>;
    if (typeof row.id !== 'string' || row.id === '' || seen.has(row.id)) continue;
    seen.add(row.id);
    const content = typeof row.content === 'string' ? row.content : '';
    items.push({
      id: row.id,
      content,
      activeForm: typeof row.activeForm === 'string' && row.activeForm !== '' ? row.activeForm : content,
      status: typeof row.status === 'string' ? row.status : '',
      source: typeof row.source === 'string' ? row.source : '',
    });
  }
  state.plan = items;
}

function projectModelChanged(state: ConversationState, event: AgentEvent): void {
  const toModel = typeof event.data.to_model_id === 'string' ? event.data.to_model_id : null;
  if (toModel) {
    state.model = toModel;
    // 会话级切换不带 run_id ⇒ 归属归 null（读作"不是任何本轮的事实"）
    state.model_run_id = event.run_id ?? null;
  }
}

/** Phase 13 Multi-Agent（ADR-0015）：父流白盒委派事件。child 完整历史在
 *  child 自己的 session（后端 Gate 4 不变量），父流只有 start/finish 锚点——
 *  节点按 child_session_id 键控（并行委派时 start/finish 按完成顺序落盘，
 *  位置不可假设，只有 child_session_id 是稳定配对键）。
 *
 *  双渲染说明（code-review Spec 轴记录）：tool/call(delegate) 与
 *  agent/delegation-* 在 Trace Ladder 并存——前者是工具调用事实（ToolCard：
 *  args/耗时/ToolResult 终态），后者是编排事实（DelegationNode：
 *  child_session_id/summary/child 终态）。deriveChain 无过滤原则（真事件序）
 *  决定了两者并存；「一委派=一节点」的合并需要可靠的 start↔tool_call 匹配，
 *  而并行委派下两事件不携带 tool_call_id、按完成顺序落盘——匹配不存在，
 *  故保持两个真值节点，各显其职。 */
function projectAgentDelegationStarted(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  const child = typeof data.child_session_id === 'string' ? data.child_session_id : '';
  if (!child) return; // 契约必有 child_session_id；缺失不造节点（事件仍在 events 日志）
  const step = resolveStep(event, state);
  withTurnAt(state, step, (turn) => {
    touchTurn(turn, event);
    if (turn.delegations?.some((d) => d.child_session_id === child)) return; // 重放幂等
    pushDelegation(turn, {
      target: typeof data.target === 'string' ? data.target : '',
      task: typeof data.task === 'string' ? data.task : '',
      child_session_id: child,
      status: 'running',
      started_at: event.time ?? new Date().toISOString(),
    });
  });
}

function projectAgentDelegationFinished(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  const child = typeof data.child_session_id === 'string' ? data.child_session_id : '';
  if (!child) return;
  // 契约冻结 status ∈ {completed, failed}；与 Phase 12 guard 模板同风格归一。
  const status: Delegation['status'] = data.status === 'failed' ? 'failed' : 'completed';
  const summary = typeof data.summary === 'string' && data.summary ? data.summary : undefined;
  const target = typeof data.target === 'string' ? data.target : '';
  const step = resolveStep(event, state);
  withTurnAt(state, step, (turn) => {
    touchTurn(turn, event);
    const idx = turn.delegations?.findIndex((d) => d.child_session_id === child) ?? -1;
    if (idx === -1) {
      // 防御：finished 先于 started 到达（截断历史/乱序持久化）——从 finish
      // 真值建终态节点（target/status/summary 事件自带），不虚构 task。
      pushDelegation(turn, {
        target,
        task: '',
        child_session_id: child,
        status,
        summary,
        completed_at: event.time ?? new Date().toISOString(),
      });
      return;
    }
    turn.delegations = (turn.delegations ?? []).map((d, i) =>
      i === idx
        ? { ...d, status, summary, completed_at: event.time ?? new Date().toISOString() }
        : d,
    );
  });
}

/** ── T2（#95）Reasoning 事件族（契约 C1）──
 *  spec 02 §15 硬边界：visibility=internal 永不投影进用户可见推理块
 *  （events 日志 verbatim 保留——Inspector raw 可查，中心流不渲染）。 */
function projectReasoning(state: ConversationState, event: AgentEvent): void {
  if (event.data.visibility === 'internal') return;
  const step = resolveStep(event, state);
  withTurnAt(state, step, (turn) => {
    touchTurn(turn, event);
    applyReasoningEvent(turn, event, event.type);
  });
}

// ── summarizeEvent 的 per-type 摘要 ──

function summarizeUserMessage(event: AgentEvent): string {
  return String(event.data.content ?? '').slice(0, 40);
}

function summarizeModelCompleted(event: AgentEvent): string {
  // 后端 Gap 1：观测字段存在时优先展示（模型 · tokens）；否则回退内容长度。
  // 千分位等 locale 格式化归展示层（StepDetail），projection 保持确定性。
  const d = event.data;
  const usage = parseUsage(d.usage);
  const model = typeof d.model === 'string' && d.model ? d.model : null;
  // 逐维可空（C6）：total_tokens 未知 ⇒ 该维不展示（绝不打印 "null tok"）；
  // 模型与 tokens 全都拿不到时回落内容长度，避免产出空摘要。
  const tokens =
    usage && usage.total_tokens !== null ? `${usage.total_tokens} 字` : null;
  if (model || tokens) {
    return [model, tokens].filter((p): p is string => p !== null).join(' · ');
  }
  return `${String(d.content ?? '').length} 字符`;
}

/** #1416：工具参数的可读化摘要——不再 `JSON.stringify(...).slice()`（裸 JSON 被
 *  硬切在引号/转义中间等同乱码）。已知语义键优先，其余按键值行拼接；嵌套值只报
 *  形状（`N 项` / `N 字段`），摘要行因此永不出现裸 JSON。 */
const TOOL_SUMMARY_DETAIL_MAX = 60;
const TOOL_SUMMARY_MAX_PAIRS = 3;
const TOOL_PATH_KEYS = ['path', 'file_path', 'file'] as const;

function singleLine(text: string, max = TOOL_SUMMARY_DETAIL_MAX): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

function argValueText(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string') return value.trim() ? singleLine(value) : null;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return null;
    const scalars = value.filter(
      (item): item is string | number | boolean =>
        typeof item === 'string' || typeof item === 'number' || typeof item === 'boolean',
    );
    return scalars.length === value.length
      ? singleLine(scalars.map(String).join(', '))
      : `${value.length} 项`;
  }
  if (isRecord(value)) return `${Object.keys(value).length} 字段`;
  return null;
}

function toolCallDetail(args: Record<string, unknown>): string {
  if (typeof args.command === 'string' && args.command.trim()) return singleLine(args.command);
  for (const key of TOOL_PATH_KEYS) {
    const value = args[key];
    if (typeof value === 'string' && value.trim()) return singleLine(value);
  }
  const pairs: string[] = [];
  for (const [key, value] of Object.entries(args)) {
    const text = argValueText(value);
    if (text === null) continue;
    pairs.push(`${key}: ${text}`);
    if (pairs.length === TOOL_SUMMARY_MAX_PAIRS) break;
  }
  return pairs.join(' · ');
}

function summarizeToolCall(event: AgentEvent): string {
  const d = event.data;
  const name = typeof d.tool_name === 'string' && d.tool_name ? d.tool_name : '—';
  const detail = isRecord(d.args) ? toolCallDetail(d.args) : '';
  return detail ? `${name} · ${detail}` : name;
}

function summarizeToolResult(event: AgentEvent): string {
  const d = event.data;
  const parsed = tryParseContent(d.content);
  if (parsed) {
    return parsed.ok === true ? 'ok' : `失败 ${String(parsed.error_code ?? '')}`;
  }
  return String(d.content ?? '').slice(0, 40);
}

function summarizeArtifactCreated(event: AgentEvent): string {
  return String(event.data.artifact_id ?? '').slice(0, 20);
}

function summarizeContextCompacted(event: AgentEvent): string {
  const d = event.data;
  return `${d.compacted_turn_count ?? '?'} 轮 · ${d.token_estimate ?? '?'} 字`;
}

function summarizeOperationReconcileRequired(event: AgentEvent): string {
  return String(event.data.tool_name ?? '');
}

function summarizeDelegationStarted(event: AgentEvent): string {
  // Phase 13（ADR-0015）：`委派 → {target}`——编排节点单行语义；
  // child_session_id 在节点 UI 可见可复制，单行不塞长 ID。
  // #1417：target 缺失按空态约定显示 `—`，不用 `?`（那是调试残留的形状）。
  const target = event.data.target;
  return `委派 → ${typeof target === 'string' && target ? target : '—'}`;
}

function summarizeDelegationFinished(event: AgentEvent): string {
  const d = event.data;
  const target = typeof d.target === 'string' && d.target ? d.target : '—';
  const outcome = d.status === 'failed' ? '失败' : '完成';
  const summary = typeof d.summary === 'string' && d.summary ? ` · ${d.summary.slice(0, 40)}` : '';
  return `${target} ${outcome}${summary}`;
}

function summarizeToolFailureGuard(event: AgentEvent): string {
  // Phase 12（ADR-0014 #69）：`工具 ×次数 熔断 · 级别`——硬熔断即终止标记。
  const d = event.data;
  return `${typeof d.tool_name === 'string' && d.tool_name ? d.tool_name : '?'} ×${
    typeof d.consecutive_failures === 'number' ? d.consecutive_failures : '?'
  } 熔断 · ${d.level === 'hard' ? 'hard' : 'soft'}`;
}

function summarizeModelFallback(event: AgentEvent): string {
  // Phase 12（ADR-0014）：`from → to · 原因`；字段缺失不伪造。
  const d = event.data;
  const from = typeof d.from_model === 'string' && d.from_model ? d.from_model : null;
  const to = typeof d.to_model === 'string' && d.to_model ? d.to_model : null;
  if (!from || !to) return '';
  const reason = typeof d.reason === 'string' && d.reason ? d.reason : null;
  return `${from} → ${to}${reason ? ` · ${reason}` : ''}`;
}

function summarizeRunCompleted(event: AgentEvent): string {
  // 后端 Gap 1：聚合用量/成本（缺失字段不出现，全空则空摘要——类型标签已足够）。
  const d = event.data;
  const usage = parseUsage(d.usage_total);
  const parts = [
    usage && usage.total_tokens !== null ? `${usage.total_tokens} 字` : null,
    typeof d.cost_usd === 'number' && Number.isFinite(d.cost_usd) ? `$${d.cost_usd}` : null,
  ].filter((p): p is string => p !== null);
  return parts.join(' · ');
}

function summarizeReasoningStarted(): string {
  return '思考开始';
}

function summarizeReasoningCompleted(): string {
  return '思考完成';
}

function summarizeReasoningInterrupted(): string {
  return '思考中断';
}

function summarizeRunInterrupted(event: AgentEvent): string {
  const step = event.step_id;
  return step !== null && step !== undefined ? `第 ${step} 步中断` : '任务中断';
}

/** `#312`：`run/paused` 行摘要 = 命中的维度 + consumed/ceiling 两个数。
 *  数字缺失就不写那一段（不编 0）——暂停行的价值正是"卡在哪、烧了多少"。
 *
 *  `#314`：命中 per-tool 配额时读的是**那个工具**的调用数与它的 ceiling（不是 turns
 *  的那一对）——工具配额卡住时显示"消耗 3 轮 / 上限 4 轮"是在陈述另一件事。
 *
 *  `#315`：deadline 到点同样没有"某一维的读数"可报（判的是时刻先后），回落到 turns
 *  那一对会把另一维的数字摆在 `run.deadline_at` 后面、却漏掉这一行唯一想说的时刻
 *  ——与 `pauseFacts().tripped === null` 同一条纪律（不拿 turns 冒充）。分支次序是
 *  per-tool → deadline，与 `pauseFacts` 的 deadline 优先**相反**：两者会分叉的载荷
 *  （`reason=deadline` + 工具维度）由写入者保证不可达（`run_budget` 里 reason 与维度
 *  同进同出）；真出现第二种非预算 reason（如 T9 的 stuck）时，回落的 `'预算到顶'`
 *  标签要改成按 `reason` 取。 */
function summarizeRunPaused(event: AgentEvent): string {
  const info = parsePausedInfo(event);
  const dimension = info.trigger_dimension || '预算到顶';
  const tool = toolDimensionName(info.trigger_dimension);
  if (tool !== null) {
    // 读数判据是"**表**在不在"（`BudgetConsumed.calls_for` 同一口径）：表在而缺名 ⇒ 0；
    // 表缺席（老载荷）才是"未知"。按"键在不在"读会把配了 ceiling 却没调用过的工具
    // 报成未知，而同一份事件的服务端投影给的是 `remaining = ceiling`。
    const table = info.consumed_dimensions?.tool_calls_by_tool;
    const calls = table === null || table === undefined ? undefined : (table[tool] ?? 0);
    const ceiling = info.run_limits?.tool_call_limits?.[tool];
    return `${dimension} · ${calls === undefined ? '未知' : calls} 次调用/${
      ceiling === undefined ? '无上限' : `上限 ${ceiling}`
    }`;
  }
  if (isDeadlinePause(info)) {
    const instant = deadlineInstant(info);
    return `${dimension} · 截止 ${instant ?? 'unavailable'}`;
  }
  const ceiling = info.run_limit === null ? '无上限' : String(info.run_limit);
  return `${dimension} · ${info.consumed_agent_turns}/${ceiling}`;
}

/** `#312`：`run/resumed` 行摘要 = 版本跃迁（CAS 的可见证据）。 */
function summarizeRunResumed(event: AgentEvent): string {
  const previous = numberOf(event.data.previous_budget_version);
  const current = numberOf(event.data.budget_version);
  if (previous === undefined || current === undefined) return '已恢复';
  return `已恢复 · 预算版本 ${previous} → ${current}`;
}

/** #220：`run/failed` 行摘要 = 随事件的失败归因文案，缺 message 时退到 reason 码
 *  （「identical_tool_failure_loop」比空白更能说明这行为什么红）。
 *  两处与相邻 summary 不同，都是刻意的：取消那支返回空串（它是 run/failed 但语义是取消，
 *  机器码 `cancelled` 不该上时间线，状态行另有「已取消」）；不做 slice(0,40)——文案是
 *  完整句子，截断正好切掉可操作尾巴，两个消费面都已有 CSS 省略（ADR-0033 §2.3）。 */
function summarizeRunFailed(event: AgentEvent): string {
  const message = event.data.message;
  if (typeof message === 'string' && message) return message;
  const reason = event.data.reason;
  if (typeof reason !== 'string' || reason === 'cancelled') return '';
  return reason;
}

function summarizeModelChanged(event: AgentEvent): string {
  const to = event.data.to_model_id;
  return typeof to === 'string' && to ? `模型 → ${to}` : '模型已切换';
}

// #1192：委派约束继承快照的单行语义（子会话事件流的独立审计事件）。
function summarizeConstraintInheritance(event: AgentEvent): string {
  const d = event.data as Record<string, unknown>;
  if (d.omit_constraints === true) return '干净启动：不沿用任何要求';
  const inherited = Array.isArray(d.inherited) ? d.inherited.length : 0;
  const notInherited = Array.isArray(d.not_inherited) ? d.not_inherited.length : 0;
  return `沿用 ${inherited} 条 · 未沿用 ${notInherited} 条`;
}

// ── 注册表（穷尽 EventTypeValue：生成物新增类型时 tsc 失败直到登记）──

const EVENT_SEMANTICS: Record<EventTypeValue, EventSemantics> = {
  [EventType.SESSION_STARTED]: { apply: projectSessionStarted, summarize: emptySummary },
  [EventType.SESSION_RESUMED]: { apply: noopProjection, summarize: emptySummary },
  // session/forked：单行语义 = 已分叉（UI-04 定案；child 指针进详情，不做截断 id）。
  [EventType.SESSION_FORKED]: { apply: noopProjection, summarize: summarizeForked },
  // #555：fork 意图标记（未完成 fork 的 durable 可见性事实）——不是时间线条目，
  // 与 SESSION_RESUMED 同形登记为 no-op（不进 unknown_events）。未完成 fork 的
  // UI 呈现属续聊对账的产品面，另行接线。
  [EventType.FORK_IN_PROGRESS]: { apply: noopProjection, summarize: emptySummary },
  [EventType.RUN_STARTED]: { apply: projectRunStarted, summarize: emptySummary },
  [EventType.RUN_COMPLETED]: { apply: projectRunCompleted, summarize: summarizeRunCompleted },
  [EventType.RUN_FAILED]: { apply: projectRunFailed, summarize: summarizeRunFailed },
  [EventType.RUN_INTERRUPTED]: { apply: projectRunInterrupted, summarize: summarizeRunInterrupted },
  // #312：暂停（非终态）+ 同 run 恢复。两者都进 EVENT_SEMANTICS 的穷尽表——
  // generated/event-types.ts 早已含这两个类型，漏登记会被 tsc 直接挡下（防漂移）。
  [EventType.RUN_PAUSED]: { apply: projectRunPaused, summarize: summarizeRunPaused },
  [EventType.RUN_RESUMED]: { apply: projectRunResumed, summarize: summarizeRunResumed },
  [EventType.USER_MESSAGE]: { apply: projectUserMessage, summarize: summarizeUserMessage },
  [EventType.USER_INPUT_REQUESTED]: { apply: projectUserInputRequested, summarize: emptySummary },
  [EventType.MODEL_STARTED]: { apply: projectModelStarted, summarize: emptySummary },
  [EventType.MODEL_DELTA]: { apply: projectTextDelta, summarize: summarizeDeltaChars },
  [EventType.MODEL_COMPLETED]: { apply: projectModelCompleted, summarize: summarizeModelCompleted },
  [EventType.MODEL_FAILED]: { apply: noopProjection, summarize: emptySummary },
  // #313（T5）：`model/request` 是**账本**事件（一次实际 Provider 请求一格），不改会话投影
  // 状态——预算读数由 `run/started` 的 budget 与 `run/paused` / `run/resumed` 承载（11 §6.1）。
  // 与 `MODEL_FAILED` 同形：登记为 no-op 意味着它**不**进 `unknown_events`（进那本账的是
  // `unhandledProjection` 那条兜底路径，别把两者混说）；
  // 不登记则 `Record<EventTypeValue, EventSemantics>` 的穷尽性被破坏，`tsc` 直接红
  // （`event-types.ts` 是生成物，加类型就必须在这里登记）。
  // #537：不再是 no-op——requests/tokens/cost 的 run 作用域计数点（徽标数据源）。
  [EventType.MODEL_REQUEST]: { apply: projectModelRequest, summarize: emptySummary },
  // #604：attempt 开始事实供重放读取，不计预算且不改变对话投影。
  [EventType.MODEL_REQUEST_STARTED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.TOOL_CALL]: { apply: projectToolCall, summarize: summarizeToolCall },
  [EventType.TOOL_RESULT]: { apply: projectToolResult, summarize: summarizeToolResult },
  [EventType.OPERATION_RECONCILE_REQUIRED]: {
    apply: projectOperationReconcileRequired,
    summarize: summarizeOperationReconcileRequired,
  },
  // #346：reconciliation confirmation is durable source evidence, not a timeline item.
  [EventType.OPERATION_RECONCILED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.ARTIFACT_CREATED]: {
    apply: projectArtifactCreated,
    summarize: summarizeArtifactCreated,
  },
  // 运行时真正发的外置事件（#173 前它是"未接线"）——与 created 同一投影、同一摘要。
  [EventType.ARTIFACT_EXTERNALIZED]: {
    apply: projectArtifactExternalized,
    summarize: summarizeArtifactCreated,
  },
  // 词汇表内但前端尚未接线——显式登记，保持既有兜底行为（进 unknown_events）。
  [EventType.CONTEXT_COMPACTED]: {
    apply: projectContextCompacted,
    summarize: summarizeContextCompacted,
  },
  // W-04（#348）：摘要尝试失败的任务可见状态——失败**不是**压缩：derive 不投影它，
  // 不带来新的投影状态。与 MEMORY_UPDATED / GUARD_STUCK 同形登记为 no-op ⇒ 已知
  // 类型、不进 `unknown_events`；失败时间线的展示面归后续票，本行只负责 `Record`
  // 的穷尽性（生成物新增类型时 tsc 失败直到登记）。
  [EventType.CONTEXT_COMPACTION_FAILED]: {
    apply: noopProjection,
    summarize: emptySummary,
  },
  // #430（W-02.1）：保护事实独立预算超限的 fail-closed 诊断事件——与
  // CONTEXT_COMPACTION_FAILED / MEMORY_DEGRADED 同形：状态/诊断面，不投影成对话
  // 消息（run 的暂停生命周期由 run/paused 承载），本行只负责 `Record` 的穷尽性。
  [EventType.CONTEXT_PROTECTED_FACTS_EXCEEDED]: {
    apply: noopProjection,
    summarize: emptySummary,
  },
  // W-26（#380）契约 + W-27（#381）接线：`task/plan_updated` 是进度清单**整表覆盖**
  // 状态事件（last-wins，PRD 长任务 §7.3）——投影进 `state.plan`，渲染面归 PlanList
  // 组件（PRD §7.5 四件套）。原 no-op 登记（只保 Record 穷尽性）由本行替换。
  [EventType.TASK_PLAN_UPDATED]: {
    apply: projectPlanUpdated,
    summarize: emptySummary,
  },
  // #351（W-07）：Task 交付状态三轴事实（定义 task/defined + task/acceptance-revised、
  // 验证 verification/updated、接受 task/accepted + task/acceptance-released）——
  // 会话级交付事实，不是对话时间线项；权威投影在后端 `derive_task_state`（GET
  // /api/sessions/{sid}/task 单源），前端不在本地再造第二套任务状态（不变量 #22）。
  // 与 SESSION_RESUMED / MEMORY_UPDATED 同形登记为 no-op ⇒ 已知类型、不进
  // `unknown_events`；任务状态 UI 呈现面归后续前端票，本组只负责 `Record` 的穷尽性
  // （生成物 `event-types.ts` 新增类型时 tsc 失败直到登记）。
  [EventType.TASK_DEFINED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.TASK_ACCEPTANCE_REVISED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.VERIFICATION_UPDATED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.TASK_ACCEPTED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.TASK_ACCEPTANCE_RELEASED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.MEMORY_DEGRADED]: { apply: noopProjection, summarize: emptySummary },
  // #298（T6 引入 `memory/updated`，T8 补登记）：与 MEMORY_DEGRADED 同形——提交型记忆变更，
  // 载荷只有 count / memory IDs / action counts / job ID，**不带内容**（PRD V2 §6.5）。
  // 它是 `Record<EventTypeValue, EventSemantics>` 的穷尽性成员：登记为 no-op 才是既有兜底
  // 行为（进 `unknown_events` 计数、不把帧当未知事件丢弃），不登记则前端 `tsc` 直接红
  // ——生成物 `event-types.ts` 由 `scripts/gen_event_types.py` 生成，加类型就必须在这里登记。
  // UI 展示面归 MEM-V2-5（#301），本行只负责穷尽性。
  [EventType.MEMORY_UPDATED]: { apply: noopProjection, summarize: emptySummary },
  // #1353 M7：`memory/human_updated` 与 MEMORY_UPDATED 同形——后台形成提交后的人面向
  // 通知事件，载荷只有 count / memory IDs / fingerprints / job ID，**不带内容**
  // （不变量 #22：内容预览只走日志通道）。登记为 no-op ⇒ 已知类型、不进
  // `unknown_events`；不登记则 `Record<EventTypeValue, EventSemantics>` 穷尽性被破坏。
  [EventType.MEMORY_HUMAN_UPDATED]: { apply: noopProjection, summarize: emptySummary },
  // #1461：`memory/human_blocked` 与 MEMORY_HUMAN_UPDATED 同形——写路径被审批门拦下时
  // 的人面向通知事件，载荷只有 count / blocked IDs / job ID，**不带内容**（不变量 #22）。
  // 登记为 no-op ⇒ 已知类型、不进 `unknown_events`；不登记则
  // `Record<EventTypeValue, EventSemantics>` 穷尽性被破坏，`tsc` 直接红。
  [EventType.MEMORY_HUMAN_BLOCKED]: { apply: noopProjection, summarize: emptySummary },
  // #1017：不再是无归属的 no-op——按「事件到达时的最后一轮」verbatim 归位（呈现层
  // 与轮次锚定，消灭尾部游离卡），事件本体照旧留在 events 日志（Inspector Timeline）。
  [EventType.MEMORY_RECALLED]: { apply: projectMemoryRecalled, summarize: emptySummary },
  // #346: protected facts are a durable context projection, not a conversation timeline item.
  [EventType.TASK_PROTECTED_FACT]: { apply: noopProjection, summarize: emptySummary },
  // #1190：约束撤销事件——与 TASK_PROTECTED_FACT 同形（durable context 投影，非对话
  // 时间线项）。撤销后的权威状态在后端 `derive_protected_facts` / GET protected-facts
  // 单源，前端不在本地再造第二套状态（不变量 #22）。登记为 no-op ⇒ 已知类型、不进
  // `unknown_events`；不登记则穷尽性 `Record<EventTypeValue, EventSemantics>` 被破坏。
  [EventType.TASK_FACT_REVOCATION]: { apply: noopProjection, summarize: emptySummary },
  // #352：evidence/recorded 是验收证据的 durable 事实，不是对话时间线项；
  // 权威投影在后端 `derive_evidence_state`（GET /api/sessions/{sid}/evidence 单源），
  // 前端不在本地再造第二套证据状态（与 TASK_* 同不变量 #22）。登记为 no-op ⇒ 已知类型、
  // 不进 `unknown_events`；证据 UI 呈现面归后续前端票，本行只负责 `Record` 的穷尽性
  // （生成物 `event-types.ts` 新增类型时 tsc 失败直到登记）。
  [EventType.EVIDENCE_RECORDED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.TOOL_FAILURE_GUARD]: {
    apply: projectToolFailureGuard,
    summarize: summarizeToolFailureGuard,
  },
  // #529：`skill/registered|updated|removed` 是 skill 沉淀闭环的治理留痕事件（durable，
  // append-only）。它们自己不带来新的投影状态——注册/更新/移除的 catalog 可见性由
  // 后端 SkillDiscovery 刷新保证，渲染层暂无展示面（第一版只读展示归后续票）。
  // 与 MEMORY_UPDATED 同形：登记为 no-op ⇒ 已知类型、不进 `unknown_events`；
  // 不登记则前端 `tsc` 直接红（`Record<EventTypeValue, EventSemantics>` 穷尽性）。
  [EventType.SKILL_REGISTERED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.SKILL_UPDATED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.SKILL_REMOVED]: { apply: noopProjection, summarize: emptySummary },
  // #317（T9）：`guard/stuck` 是循环护栏的第二条事件面，**它自己不带来新的投影状态**——
  // `level=replan` 的纠正以 `user/message`（`injected_by='stuck_guard'`）落地，渲染层照
  // `projectUserMessage` 的既有标记显示为系统提示条；`level=paused` 的那一步由紧随其后的
  // `run/paused(reason=stuck)` 承载（`run/paused.data.stuck` 是它的可读投影）。
  // 与 `MODEL_REQUEST` / `MEMORY_UPDATED` 同形：登记为 no-op ⇒ 已知类型、不进
  // `unknown_events`；护栏卡面的展示面归后续票，本行只负责 `Record` 的穷尽性。
  [EventType.GUARD_STUCK]: { apply: noopProjection, summarize: emptySummary },
  // #524：`completion/evidence-blocked` 是完成门证据策略拒绝的结构化事实，**它自己
  // 不带来新的投影状态**——纠正以紧随其后的 `user/message`
  // （`injected_by='completion_evidence_policy'`）落地，渲染层照 projectUserMessage
  // 的既有标记显示为系统提示条。与 GUARD_STUCK 同形登记为 no-op ⇒ 已知类型、不进
  // `unknown_events`；结构化卡面的展示面归后续票，本行只负责 `Record` 的穷尽性。
  [EventType.COMPLETION_EVIDENCE_BLOCKED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.MODEL_FALLBACK]: { apply: projectModelFallback, summarize: summarizeModelFallback },
  [EventType.MODEL_CHANGED]: { apply: projectModelChanged, summarize: summarizeModelChanged },
  // F18-B（#283）：会话内改档落真投影——「最后一条 permission/changed 胜」（ADR-0041 D3）。
  // F18-A（#282）曾把它登记为 no-op（只为满足 Record 穷尽性、不把帧当未知事件丢弃），
  // 那行已被本票替换——ADR-0041 §4 的那条未闭合项据此解除。
  [EventType.PERMISSION_CHANGED]: {
    apply: projectPermissionChanged,
    summarize: summarizePermissionChanged,
  },
  [EventType.AGENT_DELEGATION_STARTED]: {
    apply: projectAgentDelegationStarted,
    summarize: summarizeDelegationStarted,
  },
  [EventType.AGENT_DELEGATION_FINISHED]: {
    apply: projectAgentDelegationFinished,
    summarize: summarizeDelegationFinished,
  },
  // #1192：委派约束继承快照——只落**子会话**事件流（父流不见），不投影进
  // ConversationState：子详情「约束继承」区直接按事件流只读渲染（不变量 #22，
  // 事件流仍是唯一真相）；登记为 no-op 防进 unknown_events。
  [EventType.AGENT_CONSTRAINT_INHERITANCE]: {
    apply: noopProjection,
    summarize: summarizeConstraintInheritance,
  },
  [EventType.REASONING_STARTED]: { apply: projectReasoning, summarize: summarizeReasoningStarted },
  [EventType.REASONING_DELTA]: { apply: projectReasoning, summarize: summarizeDeltaChars },
  [EventType.REASONING_COMPLETED]: {
    apply: projectReasoning,
    summarize: summarizeReasoningCompleted,
  },
  [EventType.REASONING_INTERRUPTED]: {
    apply: projectReasoning,
    summarize: summarizeReasoningInterrupted,
  },
  [EventType.TOOL_APPROVAL_REQUESTED]: {
    apply: projectToolApprovalRequested,
    summarize: summarizeApprovalRequested,
  },
  [EventType.PERMISSION_RESOLVED]: { apply: projectPermissionResolved, summarize: summarizePermissionResolved },
  // #526 A2/B1：会话级审批授权写/撤回、工作流档切换——审计事件，投影 no-op。
  [EventType.PERMISSION_GRANTED]: { apply: noopProjection, summarize: summarizeApprovalGranted },
  [EventType.PERMISSION_REVOKED]: { apply: noopProjection, summarize: summarizeApprovalRevoked },
  [EventType.WORKFLOW_MODE_CHANGED]: { apply: noopProjection, summarize: summarizeWorkflowModeChanged },
  // #1193：run 致命异常且计划从未生成的**明确错误信号**（durable）。对话投影
  // 本身无字段可改（横幅是「带手动退出动作的错误条」，归 useSession 经
  // `deriveRunFailedBeforePlan` 从事件流派生——见该函数）。登记为 no-op ⇒
  // 已知类型、不进 `unknown_events`、时间线可摘要；不登记则穷尽性
  // `Record<EventTypeValue, EventSemantics>` 被破坏（tsc 红）。
  [EventType.ERROR]: { apply: noopProjection, summarize: summarizeError },
  [EventType.TOOL_OUTPUT_DELTA]: {
    apply: projectToolOutputDelta,
    summarize: summarizeDeltaChars,
  },
  [EventType.TEXT_DELTA]: { apply: projectTextDelta, summarize: summarizeDeltaChars },
  // #1197 项 1：`context/window-changed` 是 per-session 上下文窗口挡位的 append-only
  // 事实。窗口的**生效值**由后端 `context/window.py` 的 derive 给出（倒序 last-wins），
  // 前端不重复实现那份折叠——挡位读数走 `/api/.../context-settings`。故这里显式登记
  // 为未投影（与 COMPACTION_* 同一处理），而不是静默漏登记。
  // ⚠ 项 1 加了这个事件类型却漏登记：`EVENT_SEMANTICS` 是穷尽 `Record<EventTypeValue, …>`，
  // 缺一条 `tsc -b` 直接失败（gate0 项），而 vitest 不跑 tsc 所以当时没红。
  [EventType.CONTEXT_WINDOW_CHANGED]: { apply: unhandledProjection, summarize: unknownSummary },
  // #1359 阶段 1：`goal/changed` 是目标生命周期的 append-only 事实（每次携带变更后
  // 完整快照）。目标的**生效值**由后端 `session/goal.py::effective_goal` 倒序 last-wins
  // 派生，前端不重复实现那份折叠——读数走 `GET /api/sessions/{id}/goal`。
  // 与 WORKFLOW_MODE_CHANGED 同一处理：投影 no-op + Timeline 一行摘要。
  [EventType.GOAL_CHANGED]: { apply: noopProjection, summarize: summarizeGoalChanged },
  [EventType.COMPACTION_START]: { apply: projectCompactionInProgress, summarize: emptySummary },
  [EventType.COMPACTION_END]: { apply: projectCompactionEnded, summarize: emptySummary },
  // ADR-0030（#196）§5.2：未投递输入逐事件折叠进 state.undelivered（队列条数据源，
  // 事件流是唯一事实）。摘要给 Timeline 一行语义（不再是「未接线」）。
  [EventType.MESSAGE_QUEUED]: { apply: projectUndelivered, summarize: summarizeMessageQueued },
  [EventType.QUEUE_CANCELLED]: { apply: projectUndelivered, summarize: emptySummary },
  [EventType.STEER_REQUESTED]: { apply: projectUndelivered, summarize: summarizeSteerRequested },
  [EventType.STEER_APPLIED]: { apply: projectUndelivered, summarize: emptySummary },
  [EventType.QUEUE_CONSUMED]: { apply: projectUndelivered, summarize: emptySummary },
  // 编辑语义（§5.4）：投影只读 events 日志（shadow 在 applyEvent 里统一应用），
  // 不折叠进轮次——被取代轮的移除由 applySupersedeShadow 按 seq 区间驱动。
  [EventType.MESSAGE_SUPERSEDED]: { apply: noopProjection, summarize: summarizeSuperseded },
};

/** Apply one event to state, returning new state. Copy-on-write:
 *  顶层浅克隆 + 只深克隆被本事件改写的 turn/tool/数组，未触及部分保持引用稳定
 *  （渲染层 React.memo 的前提，引用契约由专项测试锁定）。
 *
 * events 日志例外（P0-1，HANDOFF_PERF_FRONTEND §4.3/§6 方案 b）：append-only
 * 共享数组，push O(1)、引用跨 state 稳定——消灭 `[...state.events, event]`
 * 每事件整体克隆的 O(N²)（20k 事件 240.9µs/事件 → <10µs）。契约：
 * 既有条目永不改写、顺序不变；旧 state 的 events 视图会随后续追加继续增长。
 *
 * ⚠ 消费端契约（N2 #271，ADR-0037 D3）——**引用稳定是刻意的，所以键不能用它**：
 * 需要「events 追加后重算」的 `useMemo` / `useEffect` 依赖 `eventsVersion`
 * （下面两处 push 各 +1），**不要**依赖 `events`——引用相等 ⇒ 永不重算 ⇒ 陈旧渲染。
 * 现有消费点见 `components/StepDetail.tsx` 三处（run 列表 / run 分组 / 选中项定位）。
 * `events.length` 不是替代品：去重短路那帧不 push、quarantine 分支 push，
 * 长度区分不了「长度不变而内容变」——那等于用巧合代替契约。
 * （P0-1 当时写的「无消费者把 events 放进依赖」已不属实，由本票改正。） */
export function applyEvent(state: ConversationState, raw: AgentEvent): ConversationState {
  // T1（#94）第一道闸——帧级形状校验（spec 02 §14）：完全不可辨的帧隔离进
  // unknown_events（UnknownSurface 兜底协议，永不静默丢弃），不投影、不进轮次。
  const checked = validateEvent(raw);
  if (!checked.ok) {
    const quarantined = quarantineRecord(raw);
    state.events.push(quarantined);
    // ADR-0037 D2 的边界之一：这一路**也 push 了 events**（且确实改变渲染面）⇒ 同样 +1。
    const next: ConversationState = { ...state, eventsVersion: state.eventsVersion + 1 };
    next.unknown_events = [...next.unknown_events, quarantined];
    return next;
  }
  const event = checked.event;

  // T1（#94）第二道闸——at-least-once 去重（spec 02 §6.1/6.3）：重复 seq 的
  // 持久事实整帧丢弃——不进 events 日志、不投影（重复投递不得重复任何 UI 块）。
  // null seq = ephemeral 流式信号（model/delta 等，后端契约 seq=None），永不去重。
  // 精确重复判定；乱序小窗重排 DEFER（ADR-0016）——未见过的回跳 seq 照常应用。
  // 去重键本轮取裸 seq：SSE 帧不携带 event_id（后端 _event_to_sse_dict 形状），
  // seq 每 session 单调（契约 C5）；event_id 优先键的升级路径记 ADR-0016。
  if (event.seq !== null && state.seenSeqs.has(event.seq)) return state;

  // Inspector Timeline 真相源：流经的每个事件原样追加（不含 model/delta 折叠）。
  // 先落地日志再做投影——即使投影分支抛出，事件也不从日志丢失。
  state.events.push(event);
  // ADR-0037 D2：push 成功 ⇒ 版本 +1（与上面那行 push 成对，判定依据是「是否真的 push」，
  // 不是「是否进入 applyEvent」——所以上面那条去重短路 `return state` 时不递增）。
  const next: ConversationState = { ...state, eventsVersion: state.eventsVersion + 1 };

  const { type } = event;

  // Run 归属（PRD §8.2 Inspector 头部 Run ID）：事件真值，最后携带者胜出
  // （run 串行）；缺失保持原值——UI 侧 null 即隐藏，不回退 session_id 冒充。
  if (typeof event.run_id === 'string' && event.run_id) next.run_id = event.run_id;

  // 语义分派（注册表见上方 EVENT_SEMANTICS）——穷尽覆盖词汇表；非词汇表内的
  // 类型（未来事件 / 形状可辨的畸形帧）走 UnknownSurface 兜底。
  const semantics = EVENT_SEMANTICS[type as EventTypeValue];
  if (semantics) {
    semantics.apply(next, event);
  } else {
    // UnknownSurfaceNode 兜底协议（冻结决策第 69 行）：未知事件类型不静默丢弃，
    // 记录到 unknown_events 供 Timeline / Inspector 显式渲染为 raw 行。
    next.unknown_events = [...next.unknown_events, event];
  }

  // ADR-0030 §5.4：`message/superseded` 到达后按同一区间规则把被取代的整轮从
  // 视图移除。放在语义分派**之后**：区间计算读 events 日志（上面刚 push 过），
  // 先分派再 shadow 保证新到的 user/message（B）已折叠进轮次。
  if (type === EventType.MESSAGE_SUPERSEDED) {
    applySupersedeShadow(next);
  }

  // T1（#94）幂等标记在投影成功之后（seen = applied，spec 02 §6.1）：投影分支
  // 若抛出，seq 不入册——at-least-once 重投会重新投影而非被误判为重复丢弃；
  // 帧本身已先落地 events 日志，真相无损（崩溃路径下日志可能双行，是可见痕迹
  // 而非事实丢失）。
  if (event.seq !== null) next.seenSeqs.add(event.seq);
  return next;
}

/** T2（#95）：reasoning 事件 → turn.reasoningById 单块 COW 更新。
 *
 * 目标块解析优先级：envelope block_id（#116 契约形状）→ data.block_id（legacy
 * 容错）→ 本 turn 最近一个 streaming 块（宽松契约降级）→ 合成 key
 * `r:{step}:{seq}`（重放确定性）。块一旦 completed/interrupted
 * 即不可变（spec 02 §8.1）：迟到 delta 丢弃、重复 started 幂等忽略（新分段必须
 * 换新 block_id）；无块可终结的 terminal 事件丢弃——绝不伪造块。 */
function applyReasoningEvent(turn: Turn, event: AgentEvent, type: string): void {
  const data = event.data;
  const blocks = turn.reasoningById ?? {};
  const streaming = Object.values(blocks).filter((b) => b.status === 'streaming');
  const lastStreaming = streaming[streaming.length - 1];
  // 契约形状（T-contract #116）：block_id 在 envelope 顶层（SessionEvent.block_id）；
  // data.block_id 为 legacy 容错（旧 fixture）。同 step 双块（post-tool 新段）靠它分块。
  const explicit =
    typeof event.block_id === 'string' && event.block_id
      ? event.block_id
      : typeof data.block_id === 'string' && data.block_id
        ? data.block_id
        : null;
  const key =
    explicit ?? lastStreaming?.blockId ?? `r:${event.step_id ?? turn.step_id}:${event.seq ?? 'n'}`;
  const prev = blocks[key];

  if (type === EventType.REASONING_STARTED) {
    if (prev) return;
    const block: ReasoningBlock = {
      blockId: key,
      source: data.source === 'agent' ? 'agent' : 'model',
      text: '',
      status: 'streaming',
      started_at: event.time ?? new Date().toISOString(),
    };
    turn.reasoningById = { ...blocks, [key]: block };
    turn.activities.push({ kind: 'reasoning', blockId: key });
    return;
  }

  if (!prev || prev.status !== 'streaming') {
    if (!prev && type === EventType.REASONING_DELTA) {
      // 孤儿 delta（无 started 或块丢失）：按事实建块——started_at 记本事件
      // 时间（块的可见起点，终态时长语义依赖它），其余字段不伪造
      const block: ReasoningBlock = {
        blockId: key,
        source: data.source === 'agent' ? 'agent' : 'model',
        text: String(data.delta ?? ''),
        status: 'streaming',
        started_at: event.time ?? new Date().toISOString(),
      };
      turn.reasoningById = { ...blocks, [key]: block };
      turn.activities.push({ kind: 'reasoning', blockId: key });
    }
    return;
  }

  if (type === EventType.REASONING_DELTA) {
    turn.reasoningById = {
      ...blocks,
      [key]: { ...prev, text: prev.text + String(data.delta ?? '') },
    };
    return;
  }
  // completed / interrupted：终结 + 时间戳，已聚合文本原样保留（PRD §16.4）
  turn.reasoningById = {
    ...blocks,
    [key]: {
      ...prev,
      status: type === EventType.REASONING_COMPLETED ? 'completed' : 'interrupted',
      completed_at: event.time ?? new Date().toISOString(),
    },
  };
}

/** 工具事件宿主轮定位（两段式，性能修复同 df4f7d8；TOOL_RESULT 与
 *  tool/output_delta 共享单一实现——T3 Standards 轴 Duplicated Code 收敛）：
 *  快路径——事件 step 所在轮持有此 tool_call_id（正常流 call 与后继事件同
 *  step），直接定位；慢路径——全局按 tool_call_id 配对（recover 合成的无
 *  step_id 事件、或后端 step 落点不一致；df4f7d8：无 step_id 走 resolveStep
 *  会造幽灵轮次）。找不到宿主轮时返回入参 step（withTurnAt 按 resolveStep
 *  语义处理，与 TOOL_RESULT 既有行为一致）。 */
function locateToolHostTurn(state: ConversationState, callId: string, step: number): number {
  // 热路径（T9，与 withTurnAt 同理）：工具事件的目标几乎总是最后一轮
  const last = state.turns[state.turns.length - 1];
  if (last && last.step_id === step && last.tools.some((x) => x.tool_call_id === callId)) {
    return last.step_id;
  }
  const hostIdx = state.turns.findIndex(
    (t) => t.step_id === step && t.tools.some((x) => x.tool_call_id === callId),
  );
  if (hostIdx !== -1) return state.turns[hostIdx].step_id;
  const ownerIdx = state.turns.findIndex((t) => t.tools.some((x) => x.tool_call_id === callId));
  return ownerIdx !== -1 ? state.turns[ownerIdx].step_id : step;
}

/** Resolve which step an event belongs to.
 *
 * Priority: explicit `data.step` → event `step_id` → active step → next turn index.
 * All six event cases used to inline their own variant of this chain; centralizing
 * it means a new event type can't accidentally pick a different fallback.
 *
 * 用 `!= null`（loose）而非 `!== null`（strict）：真实后端事件可能完全不带 step_id
 * 键（JSON 解析为 undefined），strict 检查会让 undefined 漏网返回，导致 user/message
 * 与后续 model/completed(step_id=number) 落入不同 turn——模型文本丢失。 */
function resolveStep(event: AgentEvent, state: ConversationState): number {
  const fromData = (event.data.step as number | undefined) ?? null;
  if (fromData != null) return fromData;
  if (event.step_id != null) return event.step_id;
  if (state.active_step_id != null) return state.active_step_id;
  return state.turns.length + 1;
}

/** 是否还有**真正欠用户决策**的审批。
 *
 *  失效审批（`stale`，见 `markPendingApprovalsStale`）不算：它所在 run 已终结，
 *  没有任何东西在等这个决策。若把它算进去，会话就被**永久锁死**——卡片只读、
 *  composer 也一直禁用，用户在这个会话里再也发不出一句话（APR-01 真机就是
 *  这个死局：卡点不动、点也只换来 404、刷新还在）。 */
export function awaitingApproval(approvals: readonly PendingApproval[]): boolean {
  return approvals.some((a) => !a.stale);
}

/** run 终结（completed/failed/interrupted）时仍未配对的审批 = 永久失效。
 *
 *  `approval_queues` 是**纯内存**的（`session/service.py:934`），run 结束时即被 GC
 *  （`service.py:1233-1241`），恢复链路（`recovery/`）完全不复现审批 →
 *  重启/中断后这条审批**不可能再被 resolve**，而它的 `tool/approval-requested` 事件
 *  永久留在 JSONL 里，于是刷新多少次都会重新渲染出来（APR-01 真机：点了只有 404）。
 *  在投影层一次判定，渲染层不必自己拼事件顺序。
 *
 *  正常流程不受影响：run 会停在审批上等待决策，只有 run 已经结束还在 pending 的
 *  才是孤儿（即 `permission/resolved` 事件缺失的那种）。
 *
 *  调用口径（#512 查证）：终态（completed/failed/interrupted）与**非终态 paused**
 *  都走这里——后者是防御：审批在 turn 中途被等待（executor → interactive callback
 *  → wait_for 阻塞），而暂停臂全部在回合边界（deadline/配额闸还在 approval 之前，
 *  executor.py 明文「注定被拒的调用不先弹审批」），审批未决时任何暂停臂都到不了；
 *  等待被取消时 fail-closed 分支会补 `permission/resolved`（session/approval.py）。
 *  即使手写/污染 JSONL 让 paused 先于未决审批到达：paused 执行的 task 完成 ⇒
 *  done_callback 已 GC 掉 approval_queues（service.py `_attach_approval_queue_gc`），
 *  resume 后旧审批无队列可 resolve——标 stale 恰好是正确语义（防 APR-01 式永久锁死），
 *  不会把「resume 后本可恢复的审批」错误锁死。 */
function markPendingApprovalsStale(state: ConversationState): void {
  if (!state.pending_approvals.some((a) => !a.stale)) return;
  state.pending_approvals = state.pending_approvals.map((a) =>
    a.stale ? a : { ...a, stale: true },
  );
}

/** Mark a run as finished and settle every in-flight turn.
 *
 * RUN_COMPLETED and RUN_FAILED share the same sweep — only the terminal
 * turn.status differs ('done' vs 'failed'). Splitting them was the source of
 * a past caret-never-stops bug; the shared helper makes the invariant
 * "run ends → no streaming turn" structural.
 * `#312` 起多一档 `'paused'`：它同样要 settle（当前执行区间确实结束了，caret 必须
 * 停、在跑工具标 stopped），但**不是**终态——`run_status` 落 'paused'，让 UI 与
 * completed / failed / interrupted 分开呈现。turn.status 与 completed 同档取
 * 'done'：那一轮的内容是完整可读的（不是半截失败的轮），后续工作由恢复执行新建
 * 的那一轮承载。
 * Copy-on-write：只克隆真正需要变更的 turn/tool，已 settle 的保持引用。 */
function finalizeRun(
  state: ConversationState,
  status: 'completed' | 'failed' | 'paused',
  time?: string,
): void {
  // #1197 项 5A：bracket 中途夭折（run 在 START 与 END 之间终结）时 flag 必须
  // 跟着收口——否则它会永久为 true，且没有任何后续事件能再把它摘掉。
  state.compaction_in_progress = false;
  state.run_status = status;
  state.active_step_id = null;
  markPendingApprovalsStale(state);
  const turnStatus = status === 'failed' ? 'failed' : 'done';
  let changed = false;
  const turns = [...state.turns];
  for (let i = 0; i < turns.length; i++) {
    const t = turns[i];
    const needsTurn = t.status === 'streaming' || t.completed_at === undefined;
    const hasStreamingSeg = t.segments.some((seg) => seg.status === 'streaming');
    const hasRunningTool = t.tools.some((tool) => tool.status === 'running');
    const hasRunningDelegation = t.delegations?.some((d) => d.status === 'running') === true;
    if (!needsTurn && !hasStreamingSeg && !hasRunningTool && !hasRunningDelegation) continue;
    const turn = cloneTurn(t);
    if (turn.status === 'streaming') turn.status = turnStatus;
    // run 终止后不再有 delta——所有段必须离开 streaming，否则 caret 永闪。
    // turn.model 与 segments[latest model index] 是同一对象（clone 后已对齐），
    // 因此遍历 segments 即同时覆盖 turn.model。
    for (const seg of turn.segments) {
      if (seg.status === 'streaming') seg.status = 'done';
    }
    // DSH 四态语义（冻结决策第 69 行 "interrupted ≠ error"）：run 结束时仍在 running
    // 的工具被中断而非失败——标记 stopped，不与 success/failed 混淆。
    turn.tools = turn.tools.map((tool) =>
      tool.status === 'running' ? { ...tool, status: 'stopped' as const } : tool,
    );
    // 同一语义域适用于未回填的委派节点：父 run 终止即 child 协作被中断
    // （阻塞语义下不该发生，发生即 kill/崩溃路径）——stopped，而非 failed。
    if (turn.delegations?.some((d) => d.status === 'running')) {
      turn.delegations = turn.delegations.map((d) =>
        d.status === 'running' ? { ...d, status: 'stopped' as const } : d,
      );
    }
    if (turn.completed_at === undefined) {
      turn.completed_at = time ?? new Date().toISOString();
    }
    turns[i] = turn;
    changed = true;
  }
  if (changed) state.turns = turns;
}

/** First touch of a turn records its true start time (event time preferred). */
function touchTurn(turn: Turn, event: AgentEvent): void {
  if (turn.started_at === undefined) {
    turn.started_at = event.time ?? new Date().toISOString();
  }
}

/** 本会话全部工具调用，按到达顺序（跨 turn 展平）。
 *
 *  单一走法：Inspector 的 run 级清单与中心列「输出」面都从这一份取（#190）。各写一遍
 *  `turns.flatMap((t) => t.tools)` 看着无害，但一旦给"工具调用"加过滤（比如只取已终态的），
 *  两份就会悄悄分叉。 */
export function allTools(state: ConversationState): ToolCall[] {
  return state.turns.flatMap((t) => t.tools);
}

/** Expand a turn's execution chain into render nodes in true event order
 *  (Trace Ladder — signature #2). Pure view over `activities` — no filtering
 *  or collapsing; empty/done segments are a rendering concern (Conversation). */
export type ChainNode =
  | { kind: 'model'; segment: ModelSegment }
  | { kind: 'tool'; tool: ToolCall }
  | { kind: 'delegation'; delegation: Delegation }
  | { kind: 'reasoning'; block: ReasoningBlock };

export function deriveChain(turn: Turn): ChainNode[] {
  return turn.activities.flatMap((a): ChainNode[] => {
    if (a.kind === 'model') {
      const segment = turn.segments[a.index];
      return segment ? [{ kind: 'model', segment }] : [];
    }
    if (a.kind === 'delegation') {
      const delegation = turn.delegations?.find((d) => d.child_session_id === a.child_session_id);
      return delegation ? [{ kind: 'delegation', delegation }] : [];
    }
    if (a.kind === 'reasoning') {
      const block = turn.reasoningById?.[a.blockId];
      return block ? [{ kind: 'reasoning', block }] : [];
    }
    const tool = turn.tools.find((t) => t.tool_call_id === a.tool_call_id);
    return tool ? [{ kind: 'tool', tool }] : [];
  });
}

/** Rebuild full conversation from a history of durable events (on page load). */
export function projectHistory(session_id: string, events: AgentEvent[]): ConversationState {
  return events.reduce(applyEvent, initConversation(session_id));
}

/** 最早可分叉的那一轮的数组下标；没有可分叉的轮时返回 -1。
 *
 *  = `user_message_seq` 最小的真实用户轮。判据必须是 seq，不是数组顺序：
 *   - harness 注入的纠正消息（`injected_by`）不是真人说的话、不渲染分叉按钮，
 *     若它排在前面就会把「本轮之前没有历史」的提示错误地挪给下一轮；
 *   - 投影按事件顺序建轮，理论上 seq 递增，但畸形日志可能乱序，取最小 seq 才
 *     真等价于「最早的用户消息」。
 *  `user_message_seq === null` 的轮不可分叉（旧版后端未带 seq），跳过。 */
export function firstForkableTurnIndex(turns: Turn[]): number {
  let bestIdx = -1;
  let bestSeq = Number.POSITIVE_INFINITY;
  turns.forEach((t, i) => {
    if (t.injected_by || t.user_message_seq === null) return;
    if (t.user_message_seq < bestSeq) {
      bestSeq = t.user_message_seq;
      bestIdx = i;
    }
  });
  return bestIdx;
}

/** 能挂「child 会话将是空会话」提示的那一轮：只有第 0 轮可分叉时才是它，否则 -1。
 *
 *  分叉按该轮的 `user_message_seq` 播种，**排在它前面的事件都会进 child**。
 *  被 `firstForkableTurnIndex` 跳过的注入轮 / 无锚点轮（旧版后端）虽不参与分叉，
 *  它们的事件仍排在锚点之前——把提示挪给下一轮就是说谎，故这里返回 -1（干脆不给
 *  提示），而不是直接返回那个「最早可分叉轮」。 */
export function emptyChildTurnIndex(turns: Turn[]): number {
  return firstForkableTurnIndex(turns) === 0 ? 0 : -1;
}

/** 最新一条用户消息的 seq（ADR-0030 D8：只有它可被 supersede/编辑）。
 *
 *  只算**非取代**的轮：被 supersede 的轮里那条问句已被新内容取代，它不再是
 *  "最新"——允许编辑它会让用户编辑一条已消失的消息（写侧 409）。注入轮
 *  （`injected_by`）不是用户发言，同样不可编辑。 */
export function latestEditableTurn(turns: Turn[]): Turn | null {
  let latest: Turn | null = null;
  let latestSeq = -1;
  for (const turn of turns) {
    if (turn.superseded) continue;
    if (!turn.user_message || turn.injected_by) continue;
    if (turn.user_message_seq === null) continue;
    if (turn.user_message_seq > latestSeq) {
      latest = turn;
      latestSeq = turn.user_message_seq;
    }
  }
  return latest;
}

/** Extract a session-title string from a single event if it's a user/message
 *  with non-empty content; '' otherwise. Shared by deriveSessionTitle (history
 *  replay) and the live SSE handler so there's one definition of "title-worthy". */
export function extractSessionTitle(event: AgentEvent): string {
  if (event.type !== EventType.USER_MESSAGE) return '';
  return String(event.data.content ?? '').trim();
}

/** Extract the first user/message content from an event stream — used as the
 *  Session Rail row title (frozen decision "Session Model E 轮" 第 73 行:
 *  "会话行 = 首条用户消息截断为标题 + 短 ID + 事件数 + 相对时间 + Run Pulse 状态点"）。
 *  Empty / no-user-message-yet → returns '' (caller falls back to short ID). */
export function deriveSessionTitle(events: AgentEvent[]): string {
  for (const e of events) {
    const title = extractSessionTitle(e);
    if (title) return title;
  }
  return '';
}

/** #1193：从事件流派生「run 在计划生成前致命失败、会话仍停在 plan 档」。
 *
 *  判定 = 三个**生命周期标记**里最新的一个是 error(run-failed-before-plan)：
 *   - error(run-failed-before-plan)：失败发生（durable——迟到接流/刷新都读得到）；
 *   - workflow/mode-changed：用户显式退出/改档 ⇒ 这次失败已被处置；
 *   - run/started：用户重试且新 run 已启动 ⇒ 失败已被更新的运行事实取代。
 *  取「最新标记」而非「存在即真」：退出 plan 或重试成功后横幅必须自行消失，
 *  再次失败（新 error 事件）又必须重新出现——两条都由同一条规则给出。
 *
 *  纯投影（不变量 #22）：横幅不另存状态。live 帧、WS 快照、刷新后的 GET /events
 *  走的是同一份事件列表，因此迟到接流（#1193 审查 F2）与刷新（F3）下结论一致。 */
export function deriveRunFailedBeforePlan(events: AgentEvent[]): {
  message: string;
  hint: string;
} | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const e = events[i];
    if (e.type === EventType.ERROR && e.data?.code === 'run-failed-before-plan') {
      const message = e.data.message;
      const hint = e.data.hint;
      return {
        message:
          typeof message === 'string' && message ? message : '任务失败（原因见后端日志）',
        hint: typeof hint === 'string' ? hint : '',
      };
    }
    if (e.type === EventType.WORKFLOW_MODE_CHANGED || e.type === EventType.RUN_STARTED) {
      return null;
    }
  }
  return null;
}

/** Try to JSON-parse a tool result `content` string; return null on failure.
 * Backend serializes the full ToolResult via model_dump_json(), so this is the
 * only way to get at ok / data / error_code without re-fetching from the API.
 */
function tryParseContent(content: unknown): Record<string, unknown> | null {
  if (typeof content !== 'string') return null;
  try {
    const parsed = JSON.parse(content);
    return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/**
 * 窄化解析 usage 形状（后端 Gap 1 契约，见 types.ts UsageStats）：**逐维独立**
 * 解析——某一维缺席 / 非数 / 非有限（`Infinity` / `NaN`）⇒ 该维 `null`（未知），
 * 其余维照常取整数，一维损坏**不连坐**另外两维（C6 口径）。
 *
 * 返回 `null` 仅当入参**根本没给对象**（无 usage 载荷）——此时调用方按「未携带」
 * 处理（RUN_COMPLETED 保留先前累计值）；一旦是对象就返回逐维可空的 UsageStats，
 * 绝不部分伪造、不补 0。逐维未知的**粘性**由累计处（`addUsage`）保证。
 */
function parseUsage(value: unknown): UsageStats | null {
  if (typeof value !== 'object' || value === null) return null;
  const v = value as Record<string, unknown>;
  return {
    prompt_tokens: numberOf(v.prompt_tokens) ?? null,
    completion_tokens: numberOf(v.completion_tokens) ?? null,
    total_tokens: numberOf(v.total_tokens) ?? null,
  };
}

/** 逐维累加 usage（run/completed 到达前的运行中视图）：未知**粘性**——任一侧为
 *  `null`（该维未知）⇒ 结果该维 `null`，绝不从 0 或另一维的旧值起算（与后端
 *  `_accumulate_usage` 同语义；「值不可采信」——INT64 越界/负数——由后端先转
 *  null 再发，前端只做粘性不做数值护栏/溢出 clamping，不伪造精度）。
 *  `a` 为空（尚无累计）时即取 `b`。 */
function addUsage(a: UsageStats | null, b: UsageStats): UsageStats {
  if (!a) return b;
  const sum = (x: number | null, y: number | null): number | null =>
    x !== null && y !== null ? x + y : null;
  return {
    prompt_tokens: sum(a.prompt_tokens, b.prompt_tokens),
    completion_tokens: sum(a.completion_tokens, b.completion_tokens),
    total_tokens: sum(a.total_tokens, b.total_tokens),
  };
}

/** #1313 口径 B（会话累计）：顶栏显示的用量 = S（已终态 run 权威值之和）+
 *  R（当前 run 流式累计）。单次连续视图内**单调非减**——`run/completed` 以权威值
 *  **替换**本 run 的流式贡献（`projectRunCompleted` 里做的），所以到达前后不出现
 *  「数字变小」的覆盖式跳变。
 *
 *  与 run 级 `usage_total` 的区别：后者是 run 级槽（`run/completed` 直接覆盖、
 *  **run 边界不清零**——跨 run 混用正是 #1313 的根因；StepDetail RUN 段的 `tokens`
 *  行消费），不是会话累计。会话累计由独立的 S 槽（`session_usage_total`）维护。
 *
 *  两槽都空 ⇒ null（无数据不显示，零伪造）；任一侧非空即返回其与另一侧的和，
 *  逐维 null 的粘性由 `addUsage` 保证（未知维不会被当成 0）。
 *
 *  **已知约束（均不在本票 AC 内，只声明不改行为）**：
 *  - 暂停→恢复切片：同一 run 内暂停再恢复后，`run/completed` 携带的 `usage_total`
 *    可能**仅为恢复后最后一次执行的切片**（后端 #T5-F1 未裁决：`_drive` 每次新建
 *    累加器、恢复时不从暂停快照播种）。前端无法区分「切片」与「全量」，此时顶栏
 *    累计可能小于恢复前——**已知约束**，不是本函数的 bug。
 *  - 回放窗口：页面刷新后投影靠事件回放重建，若回放窗口不完整，数字可能小于刷新前
 *    （单调非减的作用域只是**单次连续视图内**，见 issue #1313「期望行为」末条）。
 */
export function displayedSessionUsage(state: ConversationState): UsageStats | null {
  if (!state.session_usage_total) return state.run_usage_live;
  if (!state.run_usage_live) return state.session_usage_total;
  return addUsage(state.session_usage_total, state.run_usage_live);
}

// ── `#312` run/paused 载荷的小工具（放在投影只读路径上，一律"缺失即缺席"）──

/** 载荷对象判定：数组也是 object，这里显式排除（`[]` 不是 `{}`）。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** 有限数字才认（NaN / Infinity / 字符串数字一律 undefined = 缺席，不猜）。 */
function numberOf(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

/** 正整数才认（复用 `numberOf` 的有限数守卫）；0 / 负数 / 非整数一律缺席。
 *  用于"显示用数字"类字段：非法值必须回落中性提示，不能被钳成 1 伪造出来。 */
function positiveInteger(value: unknown): number | undefined {
  const parsed = numberOf(value);
  return parsed !== undefined && Number.isInteger(parsed) && parsed > 0
    ? parsed
    : undefined;
}

/** #1284：ask_user 超时快照 → 显示用分钟数。
 *
 *  新事件优先读后端落盘的 `timeout_minutes`（前端只渲染）；#1224 落盘的旧事件只有
 *  `timeout_seconds`，在这里按同一条公式（向下取整且至少 1 分钟）换算——换算只此一处，
 *  渲染层不再各算一遍。 */
function askUserTimeoutMinutes(data: Record<string, unknown>): number | undefined {
  const minutes = positiveInteger(data.timeout_minutes);
  if (minutes !== undefined) return minutes;
  const seconds = positiveInteger(data.timeout_seconds);
  return seconds === undefined ? undefined : Math.max(1, Math.floor(seconds / 60));
}

/** 字符串列表的宽容读：只留字符串项；不是数组就返回空列表（不编内容）。 */
function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string');
}

/** `run/paused.data.continuation` 的四键契约（`03 §3.4`）：缺任一键 ⇒ null。
 *  **不补齐**——补齐会让"模型 closeout 没产出"看起来像"产出了空计划"。 */
function parseContinuation(value: unknown): RunContinuation | null {
  if (!isRecord(value)) return null;
  const action = value.next_safe_action;
  if (typeof action !== 'string' || !action) return null;
  const lists = ['completed', 'remaining', 'blockers'] as const;
  const out: Partial<Record<(typeof lists)[number], string[]>> = {};
  for (const key of lists) {
    const raw = value[key];
    if (!Array.isArray(raw) || raw.some((item) => typeof item !== 'string')) return null;
    out[key] = raw as string[];
  }
  return {
    completed: out.completed ?? [],
    remaining: out.remaining ?? [],
    blockers: out.blockers ?? [],
    next_safe_action: action,
  };
}

/**
 * 单行事件摘要——projection 是事件→人类可读语义的唯一归属（不变量 #22）。
 * 被 Timeline / Trace / Chat 多处复用；未知事件走 UnknownSurfaceNode 兜底（冻结决策 69）。
 */
export function summarizeEvent(event: AgentEvent): string {
  // 与 applyEvent 共用同一张穷尽注册表（上方 EVENT_SEMANTICS）——两个面不再
  // 各写一个 switch，新增事件类型只需登记一次。
  const semantics = EVENT_SEMANTICS[event.type as EventTypeValue];
  return semantics ? semantics.summarize(event) : unknownSummary(event);
}

// ── Phase 13 委派 summary 溢出（后端 #86 冻结契约）──

/** 后端 summary 超 8192 字符且未配 overflow store 时，截断并追加指针后缀：
 *  `[summary 超限已截断至 8192 字符；完整输出见 child session <child_session_id>]`。
 *  识别用原文片段而非整串——child_session_id 是变量，整串匹配会碎。 */
export const DELEGATION_SUMMARY_OVERFLOW_MARKER = 'summary 超限已截断';

/** 识别委派 summary 是否携带溢出截断指针后缀（后端 #86）。 */
export function hasSummaryOverflow(summary: string): boolean {
  return summary.includes(DELEGATION_SUMMARY_OVERFLOW_MARKER);
}
