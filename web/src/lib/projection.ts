/** Event projection — pure functions turning AgentEvents into ConversationState.
 *
 * This is the ONLY place where events get interpreted into view-models.
 * No component mutates state directly; they all dispatch events here.
 * This satisfies invariant #22 (Web UI maintains no second truth) —
 * the events ARE the truth, this just projects them.
 */

import type { AgentEvent, ConversationState, Delegation, ModelSegment, ReasoningBlock, ToolCall, ToolOutputChunk, Turn, UsageStats } from '../types';
import { EventType } from '../types';
import { parseArtifactMarker } from './toolShapes';
import { quarantineRecord, validateEvent } from './eventValidate';

// ── T2（#95）reasoning 事件族——契约 C1（docs/BACKEND_PROMPT_STREAMING_UI.md）。
// generated/event-types.ts 是 backend-owned：后端 event.py 落库并重新生成后，
// 这些常量收编为 EventType.REASONING_*。此前以 fixture 驱动（fixture 先行不硬阻塞）。
const REASONING_STARTED = 'reasoning/started';
const REASONING_DELTA = 'reasoning/delta';
const REASONING_COMPLETED = 'reasoning/completed';
const REASONING_INTERRUPTED = 'reasoning/interrupted';
// ── T3（#96）工具输出流——契约 C2（同上，backend 落库后收编 EventType）。
const TOOL_OUTPUT_DELTA = 'tool/output_delta';
/** 输出 chunk 数上限：交替通道流的有界收缩触发线（spec 03 §9.4 bounded DOM）。 */
const MAX_OUTPUT_CHUNKS = 512;

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
    events: [],
    unknown_events: [],
    model: null,
    usage_total: null,
    cost_usd: null,
    trace_id: null,
    trace_url: null,
    run_id: null,
    model_fallback: null,
    seenSeqs: new Set(),
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

/** Apply one event to state, returning new state. Copy-on-write:
 *  顶层浅克隆 + 只深克隆被本事件改写的 turn/tool/数组，未触及部分保持引用稳定
 *  （渲染层 React.memo 的前提，引用契约由专项测试锁定）。
 *
 * events 日志例外（P0-1，HANDOFF_PERF_FRONTEND §4.3/§6 方案 b）：append-only
 * 共享数组，push O(1)、引用跨 state 稳定——消灭 `[...state.events, event]`
 * 每事件整体克隆的 O(N²)（20k 事件 240.9µs/事件 → <10µs）。契约：
 * 既有条目永不改写、顺序不变；旧 state 的 events 视图会随后续追加继续增长，
 * 消费端只持有最新 state（useSession 管线：局部 conv 折叠 + setConversation
 * 提交，无消费者把 events 放进 memo/useEffect 依赖），不受影响。 */
export function applyEvent(state: ConversationState, raw: AgentEvent): ConversationState {
  // T1（#94）第一道闸——帧级形状校验（spec 02 §14）：完全不可辨的帧隔离进
  // unknown_events（UnknownSurface 兜底协议，永不静默丢弃），不投影、不进轮次。
  const checked = validateEvent(raw);
  if (!checked.ok) {
    const quarantined = quarantineRecord(raw);
    state.events.push(quarantined);
    const next: ConversationState = { ...state };
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
  const next: ConversationState = { ...state };

  const { type, data } = event;

  // Run 归属（PRD §8.2 Inspector 头部 Run ID）：事件真值，最后携带者胜出
  // （run 串行）；缺失保持原值——UI 侧 null 即隐藏，不回退 session_id 冒充。
  if (typeof event.run_id === 'string' && event.run_id) next.run_id = event.run_id;

  switch (type) {
    case EventType.USER_MESSAGE: {
      const step = resolveStep(event, next);
      withTurnAt(next, step, (turn) => {
        touchTurn(turn, event);
        turn.user_message = String(data.content ?? '');
        // Phase 12（ADR-0014 #69）：failure-guard soft 注入的纠正消息带
        // injected_by 标记——渲染层据此显示为系统提示条而非用户气泡。
        if (typeof data.injected_by === 'string' && data.injected_by) {
          turn.injected_by = data.injected_by;
        }
      });
      break;
    }

    case EventType.RUN_STARTED: {
      next.run_status = 'running';
      break;
    }

    case EventType.MODEL_STARTED: {
      const step = resolveStep(event, next);
      next.active_step_id = step;
      withTurnAt(next, step, (turn) => {
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
      break;
    }

    // 文本增量（同一 append 语义双词汇）：MODEL_DELTA = legacy stream-only
    // （运行时不再发射，词汇保留兼容旧历史/fixture）；TEXT_DELTA = T-contract
    //（#116，后端契约回执 §2）durable 合帧落盘——seq 走既有 seenSeqs 去重门，
    // 重放（projectHistory）天然同构。block_id 恒无（文本按 turn/step 聚合）。
    // 回填：model/started 是 stream-only 不入历史，重放里 delta 无前驱——
    // 不回填则 activities 为空，渲染门跳过整个模型块（取消/失败轮次文本消失）。
    case EventType.MODEL_DELTA:
    case EventType.TEXT_DELTA: {
      const step = resolveStep(event, next);
      withTurnAt(next, step, (turn) => {
        touchTurn(turn, event);
        ensureModelActivity(turn);
        turn.model.text += String(data.delta ?? '');
        turn.model.status = 'streaming';
      });
      break;
    }

    case EventType.MODEL_COMPLETED: {
      const step = resolveStep(event, next);
      withTurnAt(next, step, (turn) => {
        // Final content may include consolidated text — prefer it over accumulated delta.
        turn.model.text = String(data.content ?? turn.model.text);
        turn.model.status = 'done';
        // 后端某些路径（无工具纯对话、重放的取消/失败轮次）没有 model/started
        // 前驱——回填 model activity，让 Conversation 的渲染入口存在（文本不丢）。
        ensureModelActivity(turn);
      });
      // Run-level observability（后端 Gap 1）：可选字段，缺失/畸形不伪造。
      if (typeof data.model === 'string' && data.model) next.model = data.model;
      const usage = parseUsage(data.usage);
      if (usage) {
        // run/completed 权威聚合到达前，累计各次推理 usage 作为运行中视图。
        next.usage_total = next.usage_total
          ? {
              prompt_tokens: next.usage_total.prompt_tokens + usage.prompt_tokens,
              completion_tokens: next.usage_total.completion_tokens + usage.completion_tokens,
              total_tokens: next.usage_total.total_tokens + usage.total_tokens,
            }
          : usage;
      }
      break;
    }

    case EventType.TOOL_CALL: {
      const step = resolveStep(event, next);
      withTurnAt(next, step, (turn) => {
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
      break;
    }

    case EventType.TOOL_RESULT: {
      // 配对定位两段式（性能修复：旧实现无条件 O(轮×工具) 全局扫描，正常流
      // 每个结果都白付）——定位逻辑抽 locateToolHostTurn 共享（T3 起与
      // tool/output_delta 同用）：快路径按 step、慢路径按 tool_call_id 全局。
      const callId = String(data.tool_call_id ?? '');
      const step = resolveStep(event, next);
      const hostStep = locateToolHostTurn(next, callId, step);
      withTurnAt(next, hostStep, (turn) => {
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
        // da394a9 批：>2000 字符的 before/after 变为截断摘要并内嵌
        // "use inspect_artifact(<id>)" marker——diff 已归档，视图渲染占位态而非
        // 把 marker 当 diff 内容。
        if (
          parsedData &&
          typeof parsedData.before === 'string' &&
          typeof parsedData.after === 'string'
        ) {
          const artifactId =
            parseArtifactMarker(parsedData.before) ?? parseArtifactMarker(parsedData.after);
          tool.diff = {
            before: parsedData.before,
            after: parsedData.after,
            truncated: parsedData.truncated === true,
            ...(artifactId !== null ? { archived: true as const, artifactId } : {}),
          };
        }
        tool.completed_at = event.time ?? new Date().toISOString();
        tool.raw_result = { ...event };
        turn.tools[toolIdx] = tool;
      });
      break;
    }

    case TOOL_OUTPUT_DELTA: {
      // T3（#96，契约 C2）：stdout/stderr 逐段发射——配对复用 tool/result 的
      // 两段式定位（快路径 step、慢路径全局 tool_call_id）。未知工具不伪造；
      // channel 严格两值（其余帧丢弃，不误标通道）；相邻同通道 delta 合并进
      // 尾块（数组规模有界）；result 到达后 chunks 保留（流式内容不丢弃）。
      const outCallId = String(data.tool_call_id ?? '');
      const outText = String(data.delta ?? '');
      if (!outCallId || !outText || (data.channel !== 'stdout' && data.channel !== 'stderr')) break;
      const outStep = resolveStep(event, next);
      const outTarget = locateToolHostTurn(next, outCallId, outStep);
      withTurnAt(next, outTarget, (turn) => {
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
      break;
    }

    case EventType.RUN_COMPLETED:
      // 权威聚合（后端 Gap 1/2 + trace_url 契约 2d7f87a）：事件携带的 usage_total
      // 覆盖前端累计值；cost_usd / trace_id / trace_url 缺失或 null 保持 null
      // （费率表未定义 / Langfuse 未接入）。trace_id 与 trace_url 并列不互替。
      next.run_cancelled = false;
      next.usage_total = parseUsage(data.usage_total) ?? next.usage_total;
      next.cost_usd = typeof data.cost_usd === 'number' && Number.isFinite(data.cost_usd) ? data.cost_usd : null;
      next.trace_id = typeof data.trace_id === 'string' && data.trace_id ? data.trace_id : null;
      next.trace_url = typeof data.trace_url === 'string' && data.trace_url ? data.trace_url : null;
      finalizeRun(next, 'completed', event.time);
      break;

    case EventType.RUN_FAILED:
      // 终态 reason 三值（契约回执 §4，detached-run）：'cancelled' = 显式
      // POST /cancel（用户意图中断，≠ 错误）→ Run Pulse「已取消」；
      // 'orphaned' = 孤儿回收（零订阅 300s，非用户意图，按失败展示）；
      // 缺省 = 模型/执行器异常。断连永远不出现在终态原因里（订阅者离开只
      // unsubscribe）。turn/tool 仍按失败终态 settle。
      // trace_id / trace_url 对称抽取（契约 2d7f87a——失败 run 在 Langfuse 也有
      // 可见 trace，跳转有排查价值；此前 failed 分支漏抽 trace_id 是 pre-existing bug）。
      next.run_cancelled = data.reason === 'cancelled';
      next.trace_id = typeof data.trace_id === 'string' && data.trace_id ? data.trace_id : null;
      next.trace_url = typeof data.trace_url === 'string' && data.trace_url ? data.trace_url : null;
      finalizeRun(next, 'failed', event.time);
      break;

    case EventType.ARTIFACT_CREATED: {
      // Large tool output offloaded to ArtifactStore (Phase 5, spec 06 §15).
      // Attach the ref to the producing tool call so the Inspector can fetch it.
      const toolCallId = String(data.tool_call_id ?? '');
      const turnIdx = next.turns.findIndex((t) =>
        t.tools.some((tc) => tc.tool_call_id === toolCallId),
      );
      if (turnIdx === -1) break;
      const prevTurn = next.turns[turnIdx];
      const toolIdx = prevTurn.tools.findIndex((tc) => tc.tool_call_id === toolCallId);
      const tool = cloneTool(prevTurn.tools[toolIdx]);
      tool.artifact = {
        artifact_id: String(data.artifact_id ?? ''),
        size: Number(data.size ?? 0),
        mime_type: String(data.mime_type ?? 'application/octet-stream'),
        source_tool: String(data.source_tool ?? ''),
      };
      const turn = cloneTurn(prevTurn);
      turn.tools[toolIdx] = tool; // cloneTurn 已给出新 tools 数组，原位替换即可
      replaceTurnAt(next, turnIdx, turn);
      break;
    }

    case EventType.CONTEXT_COMPACTED: {
      // Context window exceeded → older turns summarized (Phase 5, spec 06).
      // Run-level metadata for the Inspector Context panel.
      next.compactions = [...next.compactions, {
        compacted_turn_count: Number(data.compacted_turn_count ?? 0),
        summary_message_count: Number(data.summary_message_count ?? 0),
        token_estimate: Number(data.token_estimate ?? 0),
        fallback_used: data.fallback_used === true,
        time: event.time,
      }];
      break;
    }

    case EventType.OPERATION_RECONCILE_REQUIRED: {
      // A tool operation crashed mid-flight and needs human裁决 (Phase 4/5, spec 07 §13).
      // Surfaces in the Inspector as an approval queue item.
      next.reconcile_queue = [...next.reconcile_queue, {
        tool_call_id: String(data.tool_call_id ?? ''),
        tool_name: String(data.tool_name ?? ''),
        args_identity: String(data.args_identity ?? ''),
        state: String(data.state ?? 'NEED_RECONCILE'),
        time: event.time,
      }];
      break;
    }

    // #37 交互式审批（PRD §2.2）：ToolExecutor._check_approval 暂停 run，
    // 发 tool/approval-requested 事件；前端 ApprovalCard 内联渲染。
    // permission/resolved 事件从 pending 队列移除已决审批。
    case EventType.TOOL_APPROVAL_REQUESTED: {
      const approvalId = String(data.approval_id ?? '');
      if (!approvalId) break; // 契约必有 approval_id
      // 幂等：重放已存在的 approval_id 不重复入队
      if (next.pending_approvals.some((a) => a.approval_id === approvalId)) break;
      next.pending_approvals = [...next.pending_approvals, {
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
      }];
      break;
    }

    // #37 审批已决——从 pending_approvals 移除。
    case EventType.PERMISSION_RESOLVED: {
      const approvalId = String(data.approval_id ?? '');
      if (approvalId) {
        next.pending_approvals = next.pending_approvals.filter(
          (a) => a.approval_id !== approvalId,
        );
      }
      break;
    }

    // 会话生命周期事件：不投影到轮次/工具，但识别为已知事件（不进 unknown_events）。
    // Run Pulse 可消费 conversation.events 中的会话事件判断 resumed 等场景。
    // session/forked 是 Phase 14（ADR-0017）新词汇——随 backend-owned event-types
    // 入树，先识别（不伪造任何 fork 语义），钻取/分叉视图属后续票。
    case EventType.SESSION_STARTED:
    case EventType.SESSION_RESUMED:
    case EventType.SESSION_FORKED:
      break;

    // model/failed 是真实终态（df4f7d8 §1.3 收紧：零产出模型响应以
    // model/failed + run/failed 收尾，不再有"成功但空回答"）。轮次失败 settle
    // 由随后的 run/failed → finalizeRun 统一负责，这里只保证不落 unknown 兜底。
    // memory/degraded 新增 run_id 字段（可归因 run）——前端暂不渲染该归因，
    // Phase 12 白盒透明（ADR-0014）：
    // #69 RepeatedToolFailureGuard——连续同错工具调用熔断。soft 已由后端
    // 注入 user-role 纠正消息（injected_by 标记，见 USER_MESSAGE 分支）；
    // hard 意味着本轮即将 end_run 终止。事件落所在轮 notices 供渲染。
    case EventType.TOOL_FAILURE_GUARD: {
      const step = resolveStep(event, next);
      withTurnAt(next, step, (turn) => {
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
      break;
    }

    // 模型两级 fallback：主模型失稳（ModelStallError / APIConnectionError 等）
    // 切换到备选。记录最近一次切换供模型卡「已切换」态；后续推理已在
    // to_model 上——同步 state.model（下一个 model/completed 亦会确认）。
    case EventType.MODEL_FALLBACK: {
      const from = typeof data.from_model === 'string' ? data.from_model : '';
      const to = typeof data.to_model === 'string' ? data.to_model : '';
      const reason = typeof data.reason === 'string' ? data.reason : '';
      if (from && to) {
        next.model_fallback = { from_model: from, to_model: to, reason };
        next.model = to;
      }
      break;
    }

    // Phase 13 Multi-Agent（ADR-0015）：父流白盒委派事件。child 完整历史在
    // child 自己的 session（后端 Gate 4 不变量），父流只有 start/finish 锚点——
    // 节点按 child_session_id 键控（并行委派时 start/finish 按完成顺序落盘，
    // 位置不可假设，只有 child_session_id 是稳定配对键）。
    //
    // 双渲染说明（code-review Spec 轴记录）：tool/call(delegate) 与
    // agent/delegation-* 在 Trace Ladder 并存——前者是工具调用事实（ToolCard：
    // args/耗时/ToolResult 终态），后者是编排事实（DelegationNode：
    // child_session_id/summary/child 终态）。deriveChain 无过滤原则（真事件序）
    // 决定了两者并存；「一委派=一节点」的合并需要可靠的 start↔tool_call 匹配，
    // 而并行委派下两事件不携带 tool_call_id、按完成顺序落盘——匹配不存在，
    // 故保持两个真值节点，各显其职。
    case EventType.AGENT_DELEGATION_STARTED: {
      const child = typeof data.child_session_id === 'string' ? data.child_session_id : '';
      if (!child) break; // 契约必有 child_session_id；缺失不造节点（事件仍在 events 日志）
      const step = resolveStep(event, next);
      withTurnAt(next, step, (turn) => {
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
      break;
    }

    case EventType.AGENT_DELEGATION_FINISHED: {
      const child = typeof data.child_session_id === 'string' ? data.child_session_id : '';
      if (!child) break;
      // 契约冻结 status ∈ {completed, failed}；与 Phase 12 guard 模板同风格归一。
      const status: Delegation['status'] = data.status === 'failed' ? 'failed' : 'completed';
      const summary = typeof data.summary === 'string' && data.summary ? data.summary : undefined;
      const target = typeof data.target === 'string' ? data.target : '';
      const step = resolveStep(event, next);
      withTurnAt(next, step, (turn) => {
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
      break;
    }

    // ── T2（#95）Reasoning 事件族（契约 C1，fixture 先行）──
    case REASONING_STARTED:
    case REASONING_DELTA:
    case REASONING_COMPLETED:
    case REASONING_INTERRUPTED: {
      // spec 02 §15 硬边界：visibility=internal 永不投影进用户可见推理块
      //（events 日志 verbatim 保留——Inspector raw 可查，中心流不渲染）。
      if (data.visibility === 'internal') break;
      const step = resolveStep(event, next);
      withTurnAt(next, step, (turn) => {
        touchTurn(turn, event);
        applyReasoningEvent(turn, event, type);
      });
      break;
    }

    // 仅识别为已知事件；失败展示复用既有降级管线。
    case EventType.MODEL_FAILED:
    case EventType.MEMORY_DEGRADED:
      break;

    // T7 #137：会话级模型切换——更新 conversation.model 为新模型。
    // 切换不打断在途 run，下一轮 run 从事件流派生当前模型生效。
    case EventType.MODEL_CHANGED: {
      const toModel = typeof data.to_model_id === 'string' ? data.to_model_id : null;
      if (toModel) next.model = toModel;
      break;
    }

    // T8 #138：崩溃恢复——run 被进程重启打断。
    // 与 run/completed / run/failed 同属终态（RUN_TERMINAL_TYPES），
    // 但语义是「中断」而非「完成」或「失败」。finalizeRun 把 streaming
    // 段 settle 为 done，running 工具标记 stopped（中断 ≠ 错误）。
    case EventType.RUN_INTERRUPTED:
      finalizeRun(next, 'completed', event.time);
      break;

    default:
      // UnknownSurfaceNode 兜底协议（冻结决策第 69 行）：未知事件类型不静默丢弃，
      // 记录到 unknown_events 供 Timeline / Inspector 显式渲染为 raw 行。
      next.unknown_events = [...next.unknown_events, event];
      break;
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

  if (type === REASONING_STARTED) {
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
    if (!prev && type === REASONING_DELTA) {
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

  if (type === REASONING_DELTA) {
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
      status: type === REASONING_COMPLETED ? 'completed' : 'interrupted',
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
  const d = event.data;
  switch (event.type) {
    case EventType.USER_MESSAGE:
      return String(d.content ?? '').slice(0, 40);
    case EventType.MODEL_DELTA:
    case EventType.TEXT_DELTA:
      return `+${String(d.delta ?? '').length} 字符`;
    case TOOL_OUTPUT_DELTA:
      // T3（#96）：同 delta 惯例（Timeline 行 verbatim 在场，摘要只记增量）。
      return `+${String(d.delta ?? '').length} 字符`;
    case EventType.MODEL_COMPLETED: {
      // 后端 Gap 1：观测字段存在时优先展示（模型 · tokens）；否则回退内容长度。
      // 千分位等 locale 格式化归展示层（StepDetail），projection 保持确定性。
      const usage = parseUsage(d.usage);
      const model = typeof d.model === 'string' && d.model ? d.model : null;
      if (model || usage) {
        return [model, usage ? `${usage.total_tokens} tok` : null]
          .filter((p): p is string => p !== null)
          .join(' · ');
      }
      return `${String(d.content ?? '').length} 字符`;
    }
    case EventType.TOOL_CALL:
      return `${String(d.tool_name ?? '?')} ${JSON.stringify(d.args ?? {}).slice(0, 40)}`;
    case EventType.TOOL_RESULT: {
      const parsed = tryParseContent(d.content);
      if (parsed) {
        return parsed.ok === true ? 'ok' : `失败 ${String(parsed.error_code ?? '')}`;
      }
      return String(d.content ?? '').slice(0, 40);
    }
    case EventType.ARTIFACT_CREATED:
      return String(d.artifact_id ?? '').slice(0, 20);
    case EventType.CONTEXT_COMPACTED:
      return `${d.compacted_turn_count ?? '?'} 轮 · ${d.token_estimate ?? '?'} tok`;
    case EventType.OPERATION_RECONCILE_REQUIRED:
      return String(d.tool_name ?? '');
    case EventType.AGENT_DELEGATION_STARTED:
      // Phase 13（ADR-0015）：`委派 → {target}`——编排节点单行语义；
      // child_session_id 在节点 UI 可见可复制，单行不塞长 ID。
      return `委派 → ${typeof d.target === 'string' && d.target ? d.target : '?'}`;
    case EventType.AGENT_DELEGATION_FINISHED: {
      const target = typeof d.target === 'string' && d.target ? d.target : '?';
      const outcome = d.status === 'failed' ? '失败' : '完成';
      const summary = typeof d.summary === 'string' && d.summary ? ` · ${d.summary.slice(0, 40)}` : '';
      return `${target} ${outcome}${summary}`;
    }
    case EventType.TOOL_FAILURE_GUARD:
      // Phase 12（ADR-0014 #69）：`工具 ×次数 熔断 · 级别`——硬熔断即终止标记。
      return `${typeof d.tool_name === 'string' && d.tool_name ? d.tool_name : '?'} ×${
        typeof d.consecutive_failures === 'number' ? d.consecutive_failures : '?'
      } 熔断 · ${d.level === 'hard' ? 'hard' : 'soft'}`;
    case EventType.MODEL_FALLBACK: {
      // Phase 12（ADR-0014）：`from → to · 原因`；字段缺失不伪造。
      const from = typeof d.from_model === 'string' && d.from_model ? d.from_model : null;
      const to = typeof d.to_model === 'string' && d.to_model ? d.to_model : null;
      if (!from || !to) return '';
      const reason = typeof d.reason === 'string' && d.reason ? d.reason : null;
      return `${from} → ${to}${reason ? ` · ${reason}` : ''}`;
    }
    case EventType.RUN_COMPLETED: {
      // 后端 Gap 1：聚合用量/成本（缺失字段不出现，全空则空摘要——类型标签已足够）。
      const usage = parseUsage(d.usage_total);
      const parts = [
        usage ? `${usage.total_tokens} tok` : null,
        typeof d.cost_usd === 'number' && Number.isFinite(d.cost_usd) ? `$${d.cost_usd}` : null,
      ].filter((p): p is string => p !== null);
      return parts.join(' · ');
    }
    // ── T2（#95）reasoning 事件族（契约 C1）——终态一行语义；
    // delta 摘要同 model/delta 惯例（+N 字符，Timeline 行仍 verbatim 在场）。
    case REASONING_STARTED:
      return '思考开始';
    case REASONING_DELTA:
      return `+${String(d.delta ?? '').length} 字符`;
    case REASONING_COMPLETED:
      return '思考完成';
    case REASONING_INTERRUPTED:
      return '思考中断';
    // 已知生命周期事件无单行语义——空摘要，类型标签已足够。
    // 「未知事件」兜底必须只留给真正未知的类型（UnknownSurfaceNode 协议）。
    case EventType.RUN_STARTED:
    case EventType.RUN_FAILED:
    case EventType.SESSION_STARTED:
    case EventType.SESSION_RESUMED:
    case EventType.MODEL_STARTED:
    case EventType.MODEL_FAILED:
    case EventType.MEMORY_DEGRADED:
      return '';
    default:
      return `未知事件 · ${JSON.stringify(d).slice(0, 40)}`;
  }
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
