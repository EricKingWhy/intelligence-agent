/**
 * 暂停 / 恢复横幅：移植 src/agent_harness/cli.py render_pause_block 的同一合同
 * （同一份 durable data，CLI 与 TUI 必须是同一事实的同一渲染，而不是两份近似）。
 *
 * 纪律（Spec 11 第 6.1 节）：
 * - 只读事件 data（durable 投影），不读客户端本地状态；
 * - 缺键 / 形状不合一律 unavailable，永不拿 0 顶替；
 * - 分维行只渲染"有事实可说"的维度（键缺席 = 老事件 = 零行）。
 *
 * client_absent（ADR-0046 / Spec 11 第 6.2 节）：恢复依据唯一是 client_return，
 * 必须用户显式提交（不自动续跑），横幅提示按此措辞。
 */
import { asString, isRecord } from "../events.ts";
import { decimalRemaining } from "../format.ts";

/** run 作用域快照（cli.py `data.limits.run` 的同一读法；缺/畸形 = 空对象）。 */
function runScope(data: Record<string, unknown>): Record<string, unknown> {
  const limits = isRecord(data.limits) ? data.limits : {};
  return isRecord(limits.run) ? limits.run : {};
}

/** turns 维两档读数（缺键 = unavailable；缺 consumed 不能算 remaining；形状不合 = unavailable）。 */
function turnFacts(data: Record<string, unknown>): {
  consumedText: string;
  ceilingText: string;
  remainingText: string;
  localText: string;
} {
  const limits = isRecord(data.limits) ? data.limits : {};
  const runLimits = runScope(data);
  const localLimits = isRecord(limits.local) ? limits.local : {};
  const consumed = isRecord(data.consumed) ? data.consumed : {};
  const turns = consumed.agent_turns;
  const ceiling = runLimits.max_agent_turns_total;
  const turnsOk = typeof turns === "number";
  const ceilingOk = typeof ceiling === "number";
  const remaining =
    turnsOk && ceilingOk ? Math.max((ceiling as number) - (turns as number), 0) : null;
  return {
    consumedText: turnsOk ? String(turns) : "unavailable",
    ceilingText:
      ceiling === null || ceiling === undefined
        ? "unlimited"
        : ceilingOk
          ? String(ceiling)
          : "unavailable",
    remainingText:
      ceiling === null || ceiling === undefined || remaining === null
        ? "unavailable"
        : String(remaining),
    localText: typeof localLimits.max_agent_turns === "number"
      ? String(localLimits.max_agent_turns)
      : "unavailable",
  };
}

/** 额外三维（与 cli.py 的 _EXTRA_RUN_DIMENSIONS 同表）：键在才渲染。 */
const EXTRA_DIMENSIONS: Array<[string, string]> = [
  ["model_requests", "max_model_requests"],
  ["total_tokens", "max_total_tokens"],
  ["cost_usd", "max_cost_usd"],
];

function extraDimensionLines(data: Record<string, unknown>, carried: boolean): string[] {
  const limits = runScope(data);
  const consumed = isRecord(data.consumed) ? data.consumed : {};
  const lines: string[] = [];
  for (const [consumedKey, ceilingKey] of EXTRA_DIMENSIONS) {
    if (!(consumedKey in consumed) && !(ceilingKey in limits)) continue;
    const rawConsumed = consumed[consumedKey];
    const rawCeiling = limits[ceilingKey];
    lines.push(
      `  ${consumedKey}: ${carried ? "carried " : ""}consumed ` +
        `${rawConsumed === null || rawConsumed === undefined ? "unavailable" : String(rawConsumed)}` +
        ` / limit ${rawCeiling === null || rawCeiling === undefined ? "unlimited" : String(rawCeiling)}` +
        ` (remaining ${decimalRemaining(rawConsumed, rawCeiling)})`,
    );
  }
  return lines;
}

function deadlineLines(data: Record<string, unknown>, carried: boolean): string[] {
  const limits = runScope(data);
  if (!("deadline_at" in limits)) return [];
  const value = limits.deadline_at;
  const prefix = carried ? "  carried deadline: " : "  deadline: ";
  if (value === null) return [`${prefix}unlimited`];
  if (typeof value === "string" && value !== "" && value.trim() === value) {
    return [`${prefix}${value}`];
  }
  return [`${prefix}unavailable`];
}

function perToolLines(data: Record<string, unknown>, carried: boolean): string[] {
  const limits = runScope(data);
  const ceilingTable = isRecord(limits.tool_call_limits) ? limits.tool_call_limits : null;
  const consumed = isRecord(data.consumed) ? data.consumed : {};
  const callsTable = isRecord(consumed.tool_calls_by_tool) ? consumed.tool_calls_by_tool : null;
  const attemptsTable = isRecord(consumed.tool_attempts_by_tool) ? consumed.tool_attempts_by_tool : null;

  // "表在不在"判据（cli.py _per_tool_table 同款）：键缺席 = 未知；空表 = 已知 0。
  const names = new Set<string>();
  if (ceilingTable !== null) for (const n of Object.keys(ceilingTable)) names.add(n);
  if (callsTable !== null) for (const n of Object.keys(callsTable)) names.add(n);
  if (attemptsTable !== null) for (const n of Object.keys(attemptsTable)) names.add(n);

  const lines: string[] = [];
  for (const name of [...names].sort()) {
    const ceiling = ceilingTable !== null ? ceilingTable[name] : undefined;
    const used = perToolCount(callsTable, name);
    const tried = perToolCount(attemptsTable, name);
    lines.push(
      `  tool ${name}: ${carried ? "carried " : ""}consumed ` +
        `${used ?? "unavailable"} calls` +
        ` / ${tried ?? "unavailable"} attempts` +
        ` / limit ${ceiling === null || ceiling === undefined ? "unlimited" : String(ceiling)}` +
        ` (remaining ${decimalRemaining(used, ceiling)})`,
    );
  }
  return lines;
}

/** 表未知 = None（unavailable）；缺名 = 0；值畸形 = unavailable。 */
function perToolCount(
  table: Record<string, unknown> | null,
  name: string,
): number | null {
  if (table === null) return null;
  const value = table[name];
  if (value === undefined) return 0;
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) return null;
  return value;
}

function continuationLines(data: Record<string, unknown>): string[] {
  const cont = isRecord(data.continuation) ? data.continuation : null;
  if (cont === null || Object.keys(cont).length === 0) return [];
  const lines: string[] = ["  continuation:"];
  for (const [key, label] of [["completed", "completed"], ["remaining", "remaining"], ["blockers", "blockers"]] as const) {
    const items = cont[key];
    if (Array.isArray(items) && items.length > 0) {
      lines.push(`    ${label}: ${String(items[0])}`);
      for (const item of items.slice(1)) lines.push(`      ${String(item)}`);
    }
  }
  const next = cont.next_safe_action;
  if (next) lines.push(`    next: ${String(next)}`);
  return lines;
}

/** run/paused 的 data -> 暂停摘要行（不拼空行冒充信息）。 */
export function renderPauseLines(data: Record<string, unknown>): string[] {
  const facts = turnFacts(data);
  const closeout = asString(data.closeout_source);
  const lines: string[] = [
    `[run paused] reason=${asString(data.reason)} dimension=${asString(data.trigger_dimension)} version=${String(data.budget_version ?? "")}`,
    `  turns: consumed ${facts.consumedText} / limit ${facts.ceilingText}` +
      ` (remaining ${facts.remainingText}) · local fuse ${facts.localText} · closeout=${closeout}`,
  ];
  lines.push(...extraDimensionLines(data, false));
  lines.push(...deadlineLines(data, false));
  lines.push(...perToolLines(data, false));
  lines.push(...continuationLines(data));
  const requirements = data.resume_requirements;
  if (Array.isArray(requirements) && requirements.length > 0) {
    lines.push(`  resume requirements: ${requirements.map(String).join(", ")}`);
  }
  return lines;
}

/** client_absent / client_return 的显式续跑提示（TUI 按键，不自动执行）。
 *  动词先行（输入 /resume），HTTP 细节降为补充信息。 */
export function renderResumeHint(sessionId: string, data: Record<string, unknown>): string {
  const version = String(data.budget_version ?? "");
  if (asString(data.reason) === "client_absent") {
    return (
      `  resume: 输入 /resume 恢复（client_return，显式确认，不自动续跑）` +
      ` · expected_version ${version}` +
      ` · POST /api/sessions/${sessionId}/resume`
    );
  }
  return (
    `  resume: 此暂停类型不由 TUI 恢复（reason=${asString(data.reason)}）；` +
    `按 CLI 恢复流程处理（expected_version ${version}）`
  );
}
