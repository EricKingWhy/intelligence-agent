/**
 * 事件 -> 视图模型（TUI 只做"事件流到组件"的投影，不持有第二份会话 JSONL，
 * 不连 Agent Runtime；不变量 #22：状态只来自 SessionEvent / 服务端投影）。
 *
 * applyEvent 是纯状态机：测试在 test/adapter.test.ts 钉住映射语义。
 */
import { EVENT, asNumber, asString, isRecord, type EventEnvelope } from "./events.ts";

export type { EventEnvelope };

export type RunStatus = "idle" | "running" | "paused" | "completed" | "failed" | "interrupted";

/**
 * 历史附图引用（M-09）：`user/message.data["attachments"]` 里一条图片引用的镜像。
 * 形状 = 服务端 `attachments/projection.py::ImageRef`（kind="image" + 引用五字段）；
 * **引用而非字节**：`bytes` 是字节数（int），图片内容经受控端点
 * `GET /api/sessions/{id}/attachments/{aid}/content` 取回（不变量 #15：事件流里没有 base64）。
 */
export interface TurnImageRef {
  attachment_id: string;
  media_type: string;
  bytes: number;
  width: number;
  height: number;
  /** 展示名；服务端写入路径恒缺省（结构性缺省），缺省时渲染端用 attachment_id 兜底。 */
  name: string | null;
}

export interface Turn {
  role: "user" | "assistant";
  text: string;
  tools: ToolCard[];
  /** 本轮 user/message 携带的图片引用（M-09）；空数组 = 无附图（旧数据 / 纯文本轮）。 */
  attachments: TurnImageRef[];
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

/** 进度清单行（#382 W-28；PRD 长任务第 7.1 节五字段，`task/plan_updated.data.items`）。
 *  与 W-27 `web/src/types.ts` 的 PlanItem 同形；TUI 与 Web 消费同一份服务端投影。 */
export interface PlanItem {
  id: string;
  content: string;
  activeForm: string;
  /** 服务端枚举 `pending|in_progress|completed`；投影不做状态机改写（脏数据原样进）。 */
  status: string;
  source: string;
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
  /** 进度清单（#382 W-28）：`task/plan_updated` 整表覆盖投影（W-26 契约）。
   *  null = 未出现过清单（不等于清空）；[] = 服务端明确清空。 */
  plan: PlanItem[] | null;
  /**
   * 本会话最近一轮 run 的**请求侧模型名**（`run/started.data.model`，ADR-0034）。
   * `null` = 还没有 run，或该轮没落该键（旧数据/无 model 路径）。MM-06 AC8 用它
   * 交叉查 `GET /api/models` 的 `supports_vision` 来判定"当前模型能不能看图"。
   */
  modelName: string | null;
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
    plan: null,
    modelName: null,
  };
}

/** 事件 -> 状态（seq 幂等由 sse.SeqCursor 在入口保证，这里不重复判）。 */
export function applyEvent(state: ConversationState, event: EventEnvelope): void {
  const data = event.data;
  switch (event.type) {
    case EVENT.USER_MESSAGE:
      state.turns.push({
        role: "user",
        text: asString(data.content),
        tools: [],
        attachments: parseImageRefs(data.attachments),
        seq: event.seq,
      });
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
      // #827 MM-06（AC8）：请求侧模型名（ADR-0034）。键缺席 => 保持上一轮的值，
      // 不把"这轮没写"误当成"没有模型"（旧数据无该键）。
      if (typeof data["model"] === "string" && data["model"]) {
        state.modelName = data["model"];
      }
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
    case EVENT.TASK_PLAN_UPDATED:
      projectPlanUpdated(state, data);
      break;
    default:
      break; // 词汇表里其余事件首版不投影（Event 不是所有都要上屏）
  }
}

/** #382（W-28）：`task/plan_updated` 投影，整表覆盖 last-wins + 行级容错。
 *
 *  契约：PRD 长任务第 7.1-7.3 节（五字段、整表覆盖、软上限 50）；后端 `session/plan.py`
 *  handler 已硬校验（单 in_progress / 状态机 / id 唯一），投影只防「手写 / 污染 JSONL
 *  的旧数据」：一行坏数据只损失该行，与 W-27 `web/src/lib/projection.ts:1245-1265`
 *  同一容错哲学。状态机级违规（如双 in_progress）不改写、原样保留，渲染端容错。
 *
 *  data.items 非数组 = 坏帧整个忽略（清单保持原值）；[] 是合法清空。 */
function projectPlanUpdated(state: ConversationState, data: Record<string, unknown>): void {
  const rawItems = data.items;
  if (!Array.isArray(rawItems)) return;
  const items: PlanItem[] = [];
  const seen = new Set<string>();
  for (const raw of rawItems) {
    if (typeof raw !== "object" || raw === null || Array.isArray(raw)) continue;
    const row = raw as Record<string, unknown>;
    if (typeof row.id !== "string" || row.id === "" || seen.has(row.id)) continue;
    seen.add(row.id);
    const content = typeof row.content === "string" ? row.content : "";
    items.push({
      id: row.id,
      content,
      activeForm: typeof row.activeForm === "string" && row.activeForm !== "" ? row.activeForm : content,
      status: typeof row.status === "string" ? row.status : "",
      source: typeof row.source === "string" ? row.source : "",
    });
  }
  state.plan = items;
}

/**
 * M-09：`user/message.data["attachments"]` -> 引用数组；坏形状**逐条**跳过（不 brick 重建）。
 * 容错口径与服务端 `attachments/projection.py::parse_image_refs` 同一纪律：投影/恢复必经
 * 节点上，一行坏数据不能拖垮整个会话。只收 `kind === "image"` 且五字段形状合格的条目。
 *
 * F3 修回：数值谓词也对齐服务端 `_is_non_negative_int`（非负整数）：负数/小数逐条跳过，
 * 不再借道 `asNumber`（它只保有限 number，形状同、数值谓词更宽）。JS 里 bool 不是
 * number（`Number.isInteger(true) === false`），与服务端排除 bool 的口径天然一致。
 */
function parseImageRefs(raw: unknown): TurnImageRef[] {
  if (!Array.isArray(raw)) return [];
  const refs: TurnImageRef[] = [];
  for (const item of raw) {
    if (!isRecord(item) || item["kind"] !== "image") continue;
    const attachmentId = item["attachment_id"];
    const mediaType = item["media_type"];
    const bytes = asNonNegativeInt(item["bytes"]);
    const width = asNonNegativeInt(item["width"]);
    const height = asNonNegativeInt(item["height"]);
    if (
      typeof attachmentId !== "string" ||
      attachmentId === "" ||
      typeof mediaType !== "string" ||
      mediaType === "" ||
      bytes === null ||
      width === null ||
      height === null
    ) {
      continue;
    }
    const name = item["name"];
    refs.push({
      attachment_id: attachmentId,
      media_type: mediaType,
      bytes,
      width,
      height,
      name: typeof name === "string" && name !== "" ? name : null,
    });
  }
  return refs;
}

/**
 * 服务端 `attachments/projection.py::_is_non_negative_int` 的镜像（F3 修回）：
 * 只收非负整数；`null` = 不合格（逐条跳过，与 parseImageRefs 的容错语义同向）。
 * `asNumber` 不动：它服务于 durationMs/size 等其它字段，全局收紧是 Scope 外改动。
 */
function asNonNegativeInt(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

function assistantTurn(state: ConversationState, seq: number | null): Turn {
  const last = state.turns[state.turns.length - 1];
  if (last && last.role === "assistant") {
    if (seq === null || last.seq === null || seq >= last.seq) last.seq = seq;
    return last;
  }
  const turn: Turn = { role: "assistant", text: "", tools: [], attachments: [], seq };
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
