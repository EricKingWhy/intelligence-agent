/** Event projection — pure functions turning AgentEvents into ConversationState.
 *
 * This is the ONLY place where events get interpreted into view-models.
 * No component mutates state directly; they all dispatch events here.
 * This satisfies invariant #22 (Web UI maintains no second truth) —
 * the events ARE the truth, this just projects them.
 */

import type { AgentEvent, ConversationState, Delegation, EventTypeValue, ModelSegment, PendingApproval, ReasoningBlock, ToolCall, ToolOutputChunk, Turn, UndeliveredInput, UsageStats } from '../types';
import { EventType } from '../types';
import { parseArtifactMarker } from './toolShapes';
import { quarantineRecord, validateEvent } from './eventValidate';

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
 *  由 useSession 在历史装载 / 重连时调用。 */
export function restoreUndeliveredFromQueue(
  state: ConversationState,
  queue: { items: Array<{ queue_id: string; content: string; created_at: string }>; steers: Array<{ steer_id: string; content: string; created_at: string }> },
): void {
  const items: UndeliveredInput[] = [
    ...queue.items.map((i) => ({
      kind: 'queue' as const,
      id: i.queue_id,
      content: i.content,
      seq: Number.MAX_SAFE_INTEGER,
      created_at: i.created_at,
    })),
    ...queue.steers.map((s) => ({
      kind: 'steer' as const,
      id: s.steer_id,
      content: s.content,
      seq: Number.MAX_SAFE_INTEGER,
      created_at: s.created_at,
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
    compactions: [],
    reconcile_queue: [],
    pending_approvals: [],
    approval_decisions: [],
    permission_policy: null,
    events: [],
    // ADR-0037 D2：append 计数初值 0（与 projectHistory 的空历史产物一致）。
    eventsVersion: 0,
    unknown_events: [],
    model: null,
    usage_total: null,
    cost_usd: null,
    trace_id: null,
    trace_url: null,
    run_id: null,
    model_fallback: null,
    run_interrupted: null,
    run_failure: null,
    turn_index: null,
    requested_model: null,
    model_run_id: null,
    seenSeqs: new Set(),
    undelivered: [],
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
  return '已分叉';
}

function summarizeApprovalRequested(event: AgentEvent): string {
  const name = event.data.tool_name;
  return typeof name === 'string' && name ? `等待审批 · ${name}` : '等待审批';
}

function summarizePermissionResolved(event: AgentEvent): string {
  const decision = event.data.decision;
  return typeof decision === 'string' && decision ? `审批已决（${decision}）` : '审批已决';
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

/** 队列条/摘要共用的 1 行截断（§5.2：内容摘要 1 行截断）。 */
function truncateQueueSummary(content: string, max = 40): string {
  return content.length > max ? `${content.slice(0, max)}…` : content;
}

// ── applyEvent 的 per-type 投影 ──

function projectUserMessage(state: ConversationState, event: AgentEvent): void {
  const step = resolveStep(event, state);
  withTurnAt(state, step, (turn) => {
    touchTurn(turn, event);
    turn.user_message = String(event.data.content ?? '');
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
}

function projectRunStarted(state: ConversationState, event: AgentEvent): void {
  state.run_status = 'running';
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
  // T9 #139：RUN_STARTED.data.turn_index（1-based）——该 session 里第几个 run
  // （后端 session.begin_run 定义）。每次 run 各自携带自己的值，因此这是
  // per-turn 事实，必须落到当轮 turn 上——若只存会话级会被最新 run 覆盖，
  // 导致所有历史轮次显示同一个数字。
  const idx = event.data.turn_index;
  if (typeof idx !== 'number' || !Number.isFinite(idx)) return;
  state.turn_index = idx; // 会话级镜像（Langfuse / turn 元数据消费）
  // 当轮 = 最后一个 turn：生产时序为 user/message（建轮）→ run/started，
  // 故 RUN_STARTED 到达时本轮 turn 已存在。不调用 withTurnAt——它在无匹配时
  // 会新建空 turn，而 RUN_STARTED 本身不携带 step（在途 run 的 step 无法解析），
  // 会凭空多出一个错位轮次。仅在已有轮次时回填。
  const last = state.turns[state.turns.length - 1];
  if (last) {
    const turn = cloneTurn(last);
    turn.turn_index = idx;
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
    state.usage_total = state.usage_total
      ? {
          prompt_tokens: state.usage_total.prompt_tokens + usage.prompt_tokens,
          completion_tokens: state.usage_total.completion_tokens + usage.completion_tokens,
          total_tokens: state.usage_total.total_tokens + usage.total_tokens,
        }
      : usage;
  }
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
    const parsed = tryParseContent(data.content);
    const ok = parsed?.ok === true;
    const parsedData = (parsed?.data ?? null) as Record<string, unknown> | null;
    const tool = cloneTool(turn.tools[toolIdx]);
    // bash 被超时/断连取消（data.cancelled，df4f7d8 §1.3）≠ 普通失败：
    // 映射到 stopped（中断 ≠ 错误，与 finalizeRun 的 stopped 同一语义域）。
    if (parsedData?.cancelled === true) {
      tool.status = 'stopped';
    } else {
      tool.status = ok ? 'success' : 'failed';
    }
    tool.result = parsedData ?? parsed?.message ?? data.content;
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
  // #220：失败归因是「最近一个 run 的结局」，更晚的终态一到它就过期（同 run_cancelled 复位）。
  state.run_failure = null;
  state.usage_total = parseUsage(data.usage_total) ?? state.usage_total;
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
  state.run_cancelled = data.reason === 'cancelled';
  // #220：折叠失败归因。要点三条（载荷形状与呈现口径见 ADR-0033 §2.2/2.3）：
  // 取消那支不记（取消 ≠ 错误，da394a9）；两个键互相独立、都可缺；都没给就整体 null
  // ——不是 `{reason:null,message:null}`，那会让 `if (run_failure)` 为真却无内容。
  const reason = typeof data.reason === 'string' && data.reason ? data.reason : null;
  const message = typeof data.message === 'string' && data.message ? data.message : null;
  state.run_failure =
    state.run_cancelled || (reason === null && message === null) ? null : { reason, message };
  state.trace_id = typeof data.trace_id === 'string' && data.trace_id ? data.trace_id : null;
  state.trace_url = typeof data.trace_url === 'string' && data.trace_url ? data.trace_url : null;
  finalizeRun(state, 'failed', event.time);
}

/** T8 #138：崩溃恢复——run 被进程重启打断。与 run/completed / run/failed 同属
 *  终态（RUN_TERMINAL_TYPES），但语义是「中断」而非「完成」。finalizeRun 把
 *  streaming 段 settle 为 done，running 工具标记 stopped（中断 ≠ 错误）。 */
function projectRunInterrupted(state: ConversationState, event: AgentEvent): void {
  const data = event.data;
  state.run_interrupted = {
    step_id: event.step_id ?? null,
    interrupted_seq: typeof data.interrupted_seq === 'number' ? data.interrupted_seq : null,
    reason: typeof data.reason === 'string' ? data.reason : 'process_restart',
  };
  state.run_failure = null; // #220：中断是这轮 run 的**结局**终态，过期归因同 run/completed 清掉
  finalizeRun(state, 'completed', event.time);
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
  if (model || usage) {
    return [model, usage ? `${usage.total_tokens} tok` : null]
      .filter((p): p is string => p !== null)
      .join(' · ');
  }
  return `${String(d.content ?? '').length} 字符`;
}

function summarizeToolCall(event: AgentEvent): string {
  const d = event.data;
  return `${String(d.tool_name ?? '?')} ${JSON.stringify(d.args ?? {}).slice(0, 40)}`;
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
  return `${d.compacted_turn_count ?? '?'} 轮 · ${d.token_estimate ?? '?'} tok`;
}

function summarizeOperationReconcileRequired(event: AgentEvent): string {
  return String(event.data.tool_name ?? '');
}

function summarizeDelegationStarted(event: AgentEvent): string {
  // Phase 13（ADR-0015）：`委派 → {target}`——编排节点单行语义；
  // child_session_id 在节点 UI 可见可复制，单行不塞长 ID。
  const target = event.data.target;
  return `委派 → ${typeof target === 'string' && target ? target : '?'}`;
}

function summarizeDelegationFinished(event: AgentEvent): string {
  const d = event.data;
  const target = typeof d.target === 'string' && d.target ? d.target : '?';
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
    usage ? `${usage.total_tokens} tok` : null,
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
  return step !== null && step !== undefined ? `第 ${step} 步中断` : '运行中断';
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

// ── 注册表（穷尽 EventTypeValue：生成物新增类型时 tsc 失败直到登记）──

const EVENT_SEMANTICS: Record<EventTypeValue, EventSemantics> = {
  [EventType.SESSION_STARTED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.SESSION_RESUMED]: { apply: noopProjection, summarize: emptySummary },
  // session/forked：单行语义 = 已分叉（UI-04 定案；child 指针进详情，不做截断 id）。
  [EventType.SESSION_FORKED]: { apply: noopProjection, summarize: summarizeForked },
  [EventType.RUN_STARTED]: { apply: projectRunStarted, summarize: emptySummary },
  [EventType.RUN_COMPLETED]: { apply: projectRunCompleted, summarize: summarizeRunCompleted },
  [EventType.RUN_FAILED]: { apply: projectRunFailed, summarize: summarizeRunFailed },
  [EventType.RUN_INTERRUPTED]: { apply: projectRunInterrupted, summarize: summarizeRunInterrupted },
  [EventType.USER_MESSAGE]: { apply: projectUserMessage, summarize: summarizeUserMessage },
  [EventType.MODEL_STARTED]: { apply: projectModelStarted, summarize: emptySummary },
  [EventType.MODEL_DELTA]: { apply: projectTextDelta, summarize: summarizeDeltaChars },
  [EventType.MODEL_COMPLETED]: { apply: projectModelCompleted, summarize: summarizeModelCompleted },
  [EventType.MODEL_FAILED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.TOOL_CALL]: { apply: projectToolCall, summarize: summarizeToolCall },
  [EventType.TOOL_RESULT]: { apply: projectToolResult, summarize: summarizeToolResult },
  [EventType.OPERATION_RECONCILE_REQUIRED]: {
    apply: projectOperationReconcileRequired,
    summarize: summarizeOperationReconcileRequired,
  },
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
  [EventType.MEMORY_DEGRADED]: { apply: noopProjection, summarize: emptySummary },
  [EventType.TOOL_FAILURE_GUARD]: {
    apply: projectToolFailureGuard,
    summarize: summarizeToolFailureGuard,
  },
  [EventType.MODEL_FALLBACK]: { apply: projectModelFallback, summarize: summarizeModelFallback },
  [EventType.MODEL_CHANGED]: { apply: projectModelChanged, summarize: summarizeModelChanged },
  [EventType.AGENT_DELEGATION_STARTED]: {
    apply: projectAgentDelegationStarted,
    summarize: summarizeDelegationStarted,
  },
  [EventType.AGENT_DELEGATION_FINISHED]: {
    apply: projectAgentDelegationFinished,
    summarize: summarizeDelegationFinished,
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
  [EventType.TOOL_OUTPUT_DELTA]: {
    apply: projectToolOutputDelta,
    summarize: summarizeDeltaChars,
  },
  [EventType.TEXT_DELTA]: { apply: projectTextDelta, summarize: summarizeDeltaChars },
  [EventType.COMPACTION_START]: { apply: unhandledProjection, summarize: unknownSummary },
  [EventType.COMPACTION_END]: { apply: unhandledProjection, summarize: unknownSummary },
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
 *  才是孤儿（即 `permission/resolved` 事件缺失的那种）。 */
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
 * Copy-on-write：只克隆真正需要变更的 turn/tool，已 settle 的保持引用。 */
function finalizeRun(state: ConversationState, status: 'completed' | 'failed', time?: string): void {
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
 * 窄化解析 usage 形状（后端 Gap 1 契约，见 types.ts UsageStats）：三字段必须
 * 全为有限数，否则返回 null——缺失/畸形整体按「—」处理，绝不部分伪造或补零。
 */
function parseUsage(value: unknown): UsageStats | null {
  if (typeof value !== 'object' || value === null) return null;
  const v = value as Record<string, unknown>;
  const { prompt_tokens, completion_tokens, total_tokens } = v;
  const ok =
    typeof prompt_tokens === 'number' &&
    typeof completion_tokens === 'number' &&
    typeof total_tokens === 'number' &&
    Number.isFinite(prompt_tokens) &&
    Number.isFinite(completion_tokens) &&
    Number.isFinite(total_tokens);
  return ok ? { prompt_tokens, completion_tokens, total_tokens } : null;
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
