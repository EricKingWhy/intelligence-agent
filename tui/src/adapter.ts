/**
 * 事件 -> 视图模型（TUI 只做"事件流到组件"的投影，不持有第二份会话 JSONL，
 * 不连 Agent Runtime；不变量 #22：状态只来自 SessionEvent / 服务端投影）。
 *
 * applyEvent 是纯状态机：测试在 test/adapter.test.ts 钉住映射语义。
 */
import { EVENT, asNumber, asString, isRecord, type EventEnvelope } from "./events.ts";

export type { EventEnvelope };

export type RunStatus = "idle" | "running" | "paused" | "completed" | "failed" | "interrupted";

export interface Turn {
  role: "user" | "assistant";
  text: string;
  tools: ToolCard[];
  /** 本轮关联的事件 seq 上限（排序与重建用）。 */
  seq: number | null;
}

export interface ToolCard {
  toolCallId: string;
  name: string;
  /** tool/call.data.title（部分工具带）；缺失为空串（OMP 状态行的描述位）。 */
  title: string;
  args: Record<string, unknown>;
  status: "running" | "success" | "error";
  /** tool/result.content 解析出的 ok；unparseable 为 null（不编造）。 */
  ok: boolean | null;
  /** 结果 message 或原文。 */
  message: string;
  durationMs: number | null;
  /** tool/output_delta 增量流（真实数据原样拼接）。 */
  output: string;
  artifact: ArtifactRef | null;
}

export interface ArtifactRef {
  artifact_id: string;
  size: number | null;
  mime_type: string | null;
  source_tool: string | null;
}

export interface PendingApproval {
  approvalId: string;
  toolName: string;
  toolCallId: string;
  actionType: string;
  title: string;
  description: string;
  argumentsPreview: Record<string, unknown>;
  allowedDecisions: string[];
  seq: number | null;
}

export interface PauseInfo {
  reason: string;
  triggerDimension: string;
  budgetVersion: number | null;
  /** 被暂停 run 的 id（恢复请求必须点名它（Spec 03 第 5 节：同 run 续跑））。 */
  runId: string | null;
  /** 原始 durable data；横幅渲染读它（views/pause.ts），这里不二次拆解。 */
  data: Record<string, unknown>;
  seq: number | null;
}

export interface ResumeInfo {
  basis: string;
  fromPauseSeq: number | null;
  budgetVersion: number | null;
}

export interface ConversationState {
  turns: Turn[];
  runStatus: RunStatus;
  pauseInfo: PauseInfo | null;
  lastResume: ResumeInfo | null;
  failReason: string | null;
  usageTotal: Record<string, unknown> | null;
  pendingApprovals: PendingApproval[];
  /** artifact/created 找不到宿主工具卡时的入口占位（真实 id，不伪造归属）。 */
  orphanArtifacts: ArtifactRef[];
}

export function createState(): ConversationState {
  return {
    turns: [],
    runStatus: "idle",
    pauseInfo: null,
    lastResume: null,
    failReason: null,
    usageTotal: null,
    pendingApprovals: [],
    orphanArtifacts: [],
  };
}

/** 事件 -> 状态（seq 幂等由 sse.SeqCursor 在入口保证，这里不重复判）。 */
export function applyEvent(state: ConversationState, event: EventEnvelope): void {
  const data = event.data;
  switch (event.type) {
    case EVENT.USER_MESSAGE:
      state.turns.push({ role: "user", text: asString(data.content), tools: [], seq: event.seq });
      break;
    case EVENT.TEXT_DELTA:
      assistantTurn(state, event.seq).text += asString(data.delta);
      break;
    case EVENT.MODEL_COMPLETED: {
      const turn = assistantTurn(state, event.seq);
      const content = asString(data.content);
      if (content) turn.text = content;
      break;
    }
    case EVENT.TOOL_CALL:
      assistantTurn(state, event.seq).tools.push({
        toolCallId: asString(data.tool_call_id),
        name: asString(data.tool_name) || "unknown",
        title: asString(data.title),
        args: isRecord(data.args) ? data.args : {},
        status: "running",
        ok: null,
        message: "",
        durationMs: null,
        output: "",
        artifact: null,
      });
      break;
    case EVENT.TOOL_OUTPUT_DELTA: {
      const tool = findTool(state, asString(data.tool_call_id));
      if (tool !== null) {
        tool.output += asString(data.delta);
      }
      break;
    }
    case EVENT.TOOL_RESULT: {
      const tool = findTool(state, asString(data.tool_call_id));
      if (tool === null) break;
      tool.message = asString(data.content);
      const parsed = tryParseJson(tool.message);
      if (parsed !== null) {
        tool.ok = parsed.ok === true;
        const message = parsed.message;
        if (typeof message === "string") tool.message = message;
        const duration = (parsed.metadata as Record<string, unknown> | undefined)?.duration_ms;
        const ms = asNumber(duration);
        tool.durationMs = ms === null ? null : ms / 1000;
      }
      tool.status = tool.ok === true ? "success" : "error";
      break;
    }
    case EVENT.TOOL_APPROVAL_REQUESTED: {
      const approvalId = asString(data.approval_id);
      if (!approvalId) break; // 契约必有 approval_id
      if (state.pendingApprovals.some((a) => a.approvalId === approvalId)) break; // 重放幂等
      state.pendingApprovals.push({
        approvalId,
        toolName: asString(data.tool_name),
        toolCallId: asString(data.tool_call_id),
        actionType: asString(data.action_type),
        title: asString(data.title),
        description: asString(data.description),
        argumentsPreview: isRecord(data.arguments_preview) ? data.arguments_preview : {},
        allowedDecisions: Array.isArray(data.allowed_decisions) ? data.allowed_decisions.map(String) : [],
        seq: event.seq,
      });
      break;
    }
    case EVENT.PERMISSION_RESOLVED: {
      const approvalId = asString(data.approval_id);
      if (!approvalId) break;
      state.pendingApprovals = state.pendingApprovals.filter((a) => a.approvalId !== approvalId);
      break;
    }
    case EVENT.ARTIFACT_CREATED:
    case EVENT.ARTIFACT_EXTERNALIZED: {
      const ref: ArtifactRef = {
        artifact_id: asString(data.artifact_id),
        size: asNumber(data.size),
        mime_type: typeof data.mime_type === "string" ? data.mime_type : null,
        source_tool: typeof data.source_tool === "string" ? data.source_tool : null,
      };
      const tool = findTool(state, asString(data.tool_call_id));
      if (tool !== null) tool.artifact = ref;
      else if (ref.artifact_id) state.orphanArtifacts.push(ref);
      break;
    }
    case EVENT.RUN_STARTED:
      state.runStatus = "running";
      break;
    case EVENT.RUN_PAUSED:
      state.runStatus = "paused";
      state.pauseInfo = {
        reason: asString(data.reason) || "unknown",
        triggerDimension: asString(data.trigger_dimension),
        budgetVersion: asNumber(data.budget_version),
        runId: event.run_id,
        data,
        seq: event.seq,
      };
      break;
    case EVENT.RUN_RESUMED:
      state.runStatus = "running";
      state.pauseInfo = null;
      state.lastResume = {
        basis: asString(data.resume_basis),
        fromPauseSeq: asNumber(data.from_pause_seq),
        budgetVersion: asNumber(data.budget_version),
      };
      break;
    case EVENT.RUN_COMPLETED:
      state.runStatus = "completed";
      state.pauseInfo = null;
      state.usageTotal = isRecord(data.usage_total) ? data.usage_total : null;
      break;
    case EVENT.RUN_FAILED:
      state.runStatus = "failed";
      state.pauseInfo = null;
      state.failReason = asString(data.reason) || null;
      break;
    case EVENT.RUN_INTERRUPTED:
      state.runStatus = "interrupted";
      break;
    default:
      break; // 词汇表里其余事件首版不投影（Event 不是所有都要上屏）
  }
}

function assistantTurn(state: ConversationState, seq: number | null): Turn {
  const last = state.turns[state.turns.length - 1];
  if (last && last.role === "assistant") {
    if (seq === null || last.seq === null || seq >= last.seq) last.seq = seq;
    return last;
  }
  const turn: Turn = { role: "assistant", text: "", tools: [], seq };
  state.turns.push(turn);
  return turn;
}

function findTool(state: ConversationState, toolCallId: string): ToolCard | null {
  if (!toolCallId) return null;
  for (let i = state.turns.length - 1; i >= 0; i--) {
    const tool = state.turns[i]?.tools.find((t) => t.toolCallId === toolCallId);
    if (tool !== undefined) return tool;
  }
  return null;
}

function tryParseJson(text: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(text);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}
