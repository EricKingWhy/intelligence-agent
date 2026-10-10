/**
 * RuntimeEvent 信封类型（单一事实：src/agent_harness/web/serialization.py 的
 * build_event_payload / build_session_event_payload 形状；SSE live 与重放通道同形）。
 */

export interface EventEnvelope {
  type: string;
  data: Record<string, unknown>;
  /** 幂等投影键；transient 控制帧为 null。 */
  seq: number | null;
  run_id: string | null;
  step_id: number | null;
  session_id: string;
  time: string;
  schema_version?: number;
  durability: string;
  block_id?: string;
  capability?: string;
}

/** 事件类型词汇（docs/EVENT_VOCABULARY.md 的子集；TUI 首版消费面）。 */
export const EVENT = {
  USER_MESSAGE: "user/message",
  TEXT_DELTA: "text/delta",
  MODEL_COMPLETED: "model/completed",
  REASONING_DELTA: "reasoning/delta",
  REASONING_COMPLETED: "reasoning/completed",
  TOOL_CALL: "tool/call",
  TOOL_RESULT: "tool/result",
  TOOL_OUTPUT_DELTA: "tool/output_delta",
  TOOL_APPROVAL_REQUESTED: "tool/approval-requested",
  PERMISSION_RESOLVED: "permission/resolved",
  ARTIFACT_CREATED: "artifact/created",
  ARTIFACT_EXTERNALIZED: "artifact/externalized",
  RUN_STARTED: "run/started",
  RUN_COMPLETED: "run/completed",
  RUN_FAILED: "run/failed",
  RUN_PAUSED: "run/paused",
  RUN_RESUMED: "run/resumed",
  RUN_INTERRUPTED: "run/interrupted",
  TASK_PLAN_UPDATED: "task/plan_updated",
} as const;

/** 解析 SSE data 行 -> 信封；形状不合返回 null（坏帧丢弃，不投影）。 */
export function parseEnvelope(raw: string): EventEnvelope | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const obj = parsed as Record<string, unknown>;
  if (typeof obj.type !== "string") return null;
  if (typeof obj.session_id !== "string") return null;
  if (typeof obj.time !== "string") return null;
  return {
    type: obj.type,
    data: isRecord(obj.data) ? obj.data : {},
    seq: typeof obj.seq === "number" ? obj.seq : null,
    run_id: typeof obj.run_id === "string" ? obj.run_id : null,
    step_id: typeof obj.step_id === "number" ? obj.step_id : null,
    session_id: obj.session_id,
    time: obj.time,
    durability: typeof obj.durability === "string" ? obj.durability : "durable",
    schema_version:
      typeof obj.schema_version === "number" ? obj.schema_version : undefined,
    block_id: typeof obj.block_id === "string" ? obj.block_id : undefined,
    capability: typeof obj.capability === "string" ? obj.capability : undefined,
  };
}

/**
 * `stream/truncated` 控制帧（backlog 超限）-> `data.latest_seq` 提示；不是该帧返回 null。
 *
 * 它不是运行事实（不变量 #4 边界）：服务端 `build_truncated_control` 按设计不带
 * `time`、`seq` 恒 null，所以不能过 `parseEnvelope` 的运行事实校验（缺 `time` 即丢，
 * #859）。这里只认 `type`，与 `web/src/lib/sse.ts` 的 `parseFrame` 同口径。
 */
export function parseTruncatedControl(
  raw: string,
): { latestSeq: number | null } | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(parsed) || parsed.type !== "stream/truncated") return null;
  const hint = isRecord(parsed.data) ? parsed.data.latest_seq : undefined;
  return { latestSeq: typeof hint === "number" ? hint : null };
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
