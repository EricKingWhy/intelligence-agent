/** #189 —— 「本次会话改动过的文件」的**聚合层**（纯函数，无 DOM、无 React）。
 *
 *  要回答的问题（PRD §3.3）："这次 agent 到底动了哪些文件、每个文件改成了什么样。"
 *
 *  **数据全部是事件投影的产物**（AC7 / 不变量 #22）：
 *  - `ToolCall.diff` 是投影层从 `tool/result` 的 `data.{before,after,truncated}` 建的
 *    （后端 `tooling/_diff_data.py`：编辑类工具成功后就给原料，**不在后端算 diff**——
 *    算 diff 是视图层职责）；
 *  - 文件路径取 `tool/call` 的 `args.path`。
 *
 *  **"哪些工具算写文件"与后端同一份答案**：`write` / `edit` / `apply_patch`
 *  （后端 `multiagent/provider.py::_WRITE_TOOL_NAMES` 收 `changed_files` 时用的就是
 *  这三个）。前端再写一套名字表就是第二份真相——所以这份集合只在这里出现一次，
 *  且逐字与后端对齐。
 *
 *  **统计口径（唯一）**：`+N -M` = "相对本会话**首次改动前**的原文"到"最后一次改动后的
 *  内容"的**净**行变化，不是把每次改动相加——相加会把"同一行改了三次"说成 +3 −3。
 *  行差按**出现次数**算（不是集合去重）：`a\na` → `a` 是减了一行。
 *  不做 LCS：移动的块会同时计入 + 与 -，这与多数 diff 工具的呈现一致，且 O(n)。
 *
 *  **不可得就不给数**：内容被归档（>2000 字符转 artifact，before/after 变成 marker
 *  摘要）或截断（>50KB 只留头部 5000 字节）时，手上已不是原文——在 marker/半截文本上
 *  算出来的 `+N -M` 是**假数字**，必须如实标成不可得（PRD §4 不伪造）。
 */

import type { ToolCall } from '../types';

/** 与后端 `_WRITE_TOOL_NAMES` 逐字对齐的写工具集合（见文件头）。 */
const WRITE_TOOL_NAMES: ReadonlySet<string> = new Set(['write', 'edit', 'apply_patch']);

/** 统计不可得的原因（值即渲染层要说的话；归档比截断更严重，优先上报）。 */
export type StatLimit = 'archived' | 'truncated';

export interface FileChangeEdit {
  toolCallId: string;
  toolName: string;
  before: string;
  after: string;
  truncated: boolean;
  archived: boolean;
  artifactId?: string;
}

export interface FileChange {
  path: string;
  /** 按时间序（= 投影里 tools 的顺序，即事件到达顺序）的各次改动。 */
  edits: FileChangeEdit[];
  /** 净增行数；不可得时为 `null`（**不是 0**——0 是一条"没有新增"的断言）。 */
  added: number | null;
  /** 净删行数；不可得时为 `null`。 */
  removed: number | null;
  /** 统计不可得的原因；`null` = 统计可用。 */
  limited: StatLimit | null;
}

/** 去掉行尾换行造成的幽灵空行：`"a\n"` 与 `"a"` 是同一行内容。 */
function toLines(text: string): string[] {
  if (text === '') return [];
  const lines = text.split('\n');
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
  return lines;
}

/** 行出现次数表——行差按次数算（见文件头）。 */
function counts(lines: readonly string[]): Map<string, number> {
  const map = new Map<string, number>();
  for (const line of lines) map.set(line, (map.get(line) ?? 0) + 1);
  return map;
}

/** 行级净变化：after 比 before 多出的行数 / 少掉的行数。 */
export function lineDelta(before: string, after: string): { added: number; removed: number } {
  const beforeCounts = counts(toLines(before));
  const afterCounts = counts(toLines(after));
  let added = 0;
  let removed = 0;
  for (const [line, n] of afterCounts) added += Math.max(0, n - (beforeCounts.get(line) ?? 0));
  for (const [line, n] of beforeCounts) removed += Math.max(0, n - (afterCounts.get(line) ?? 0));
  return { added, removed };
}

/** 一组改动 → 净统计（首改前的原文 → 末改后的内容）或"不可得 + 原因"。 */
export function netStatOf(
  edits: readonly { before: string; after: string; truncated?: boolean; archived?: boolean }[],
): { stat: { added: number; removed: number } | null; limited: StatLimit | null } {
  if (edits.length === 0) return { stat: null, limited: null };
  // 归档 > 截断：两者都在时说更严重的那条（归档连原文都没了）。
  if (edits.some((e) => e.archived)) return { stat: null, limited: 'archived' };
  if (edits.some((e) => e.truncated)) return { stat: null, limited: 'truncated' };
  const first = edits[0];
  const last = edits[edits.length - 1];
  return { stat: lineDelta(first.before, last.after), limited: null };
}

function readEdit(tool: ToolCall): FileChangeEdit | null {
  const diff = tool.diff;
  if (!diff) return null;
  return {
    toolCallId: tool.tool_call_id,
    toolName: tool.name,
    before: diff.before,
    after: diff.after,
    truncated: diff.truncated === true,
    archived: diff.archived === true,
    ...(diff.artifactId !== undefined ? { artifactId: diff.artifactId } : {}),
  };
}

/** 工具调用 → 文件路径；非写工具 / 无 path 都返回 null。 */
function pathOf(tool: ToolCall): string | null {
  if (!WRITE_TOOL_NAMES.has(tool.name)) return null;
  const raw = tool.args.path;
  if (typeof raw !== 'string') return null;
  const path = raw.trim();
  return path === '' ? null : path;
}

function buildFiles(tools: readonly ToolCall[]): { files: FileChange[]; unattributed: number } {
  const order: string[] = [];
  const byPath = new Map<string, FileChangeEdit[]>();
  let unattributed = 0;
  for (const tool of tools) {
    const edit = readEdit(tool);
    // 有 diff 但归属不了文件（缺 path）：不静默丢，计数上报（AC 的"不伪造"同族）。
    if (!edit) continue;
    const path = pathOf(tool);
    if (path === null) {
      unattributed += 1;
      continue;
    }
    if (!byPath.has(path)) {
      byPath.set(path, []);
      order.push(path);
    }
    byPath.get(path)!.push(edit);
  }
  const files = order.map((path) => {
    const edits = byPath.get(path)!;
    const { stat, limited } = netStatOf(edits);
    return {
      path,
      edits,
      added: stat?.added ?? null,
      removed: stat?.removed ?? null,
      limited,
    };
  });
  return { files, unattributed };
}

/** 本次会话改动过的文件（按文件聚合，首次出现顺序）。
 *
 *  `withUnattributed` 用于需要"有改动但归属不了文件"这条信息的调用方（面板的空态与
 *  脚注）——默认形态是数组，让最常见的调用点少一层解包。
 */
export function changedFiles(tools: readonly ToolCall[]): FileChange[];
export function changedFiles(
  tools: readonly ToolCall[],
  options: { withUnattributed: true },
): { files: FileChange[]; unattributed: number };
export function changedFiles(
  tools: readonly ToolCall[],
  options?: { withUnattributed?: boolean },
): FileChange[] | { files: FileChange[]; unattributed: number } {
  const built = buildFiles(tools);
  return options?.withUnattributed === true ? built : built.files;
}
