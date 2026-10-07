/**
 * W-28（#382）：进度清单 TUI 渲染（PRD 长任务第 7.5 节四件套，全行业收敛形态）。
 *
 * 纯函数 `plan state -> string[]`，不做交互编辑器（票面）；数据源仅
 * `ConversationState.plan`（`task/plan_updated` 整表覆盖投影，W-26 契约），
 * TUI 不自取数、不维护第二套清单真相（不变量 #22）。零项不渲染（不留空壳）。
 *
 * 四件套：计数行「N/M complete」；首个 `in_progress` 项取 `activeForm` + 前缀高亮；
 * 完成项删除线 + dim；完成组默认折叠，`ctrl+t` 切换（app 层持布尔开关）。
 *
 * 抄自成熟产品的结构（REPORT-2 第 9 节，只搬结构/映射表，字符换成 GBK 白名单）：
 * - 状态到样式的映射表 = Codex `codex-rs/tui/src/history_cell/plans.rs`
 *   （`completed => crossed_out().dim()` / `in_progress => fg(accent).bold()` / `pending => dim()`）；
 * - 计数行 + 步骤缩进首行 `└` = 同上（头部 `Updated Plan · {completed}/{total} complete`；
 *   注意 `└` 只落在首个步骤行，头部本身无 `└`：头部不是任何父行的子行）；
 * - 行布局 `[pointer][glyph][text]`（gap 1）+ `activeForm` 文案 + 双 glyph 表 = Claude Code plan mode；
 * - 前缀位常驻（防抖动）= Ratatui `List` 的 `HighlightSpacing::Always`；
 * - 状态判定写成有序优先级列表 = Taskwarrior `rule.precedence.color`。
 *
 * 渲染端不设行数上限：票面四件套无「行数上限」约束，软上限 50 由服务端硬校验
 * （PRD 第 7.2 节），渲染端不得静默丢项。折叠只收起完成组，`ctrl+t` 可展开（披露行
 * 只描述这一种成因，不暗示能揭开被截断的行）。
 *
 * glyph 铁律：只允许 `● ○ └ ─ │ ┌ ┐ ┘ ├ ┤ ·`（Windows 中文终端 GBK 安全），
 * 票面硬禁用的箭头/勾叉/方框字符一律不写（`test/glyphs.test.ts` 机械守门，连注释
 * 里都不能出现）。ASCII 兜底表用 `> / [ ] / [-]`（Claude Code 双表策略；终端无
 * Unicode 时降级）。
 */
import type { PlanItem } from "../adapter.ts";
import { GLYPHS, type IaTheme } from "../theme.ts";

/** 折叠/展开开关（app 层持有，`ctrl+t` 切换）。 */
export interface PlanRenderOptions {
  /** true = 展开（含完成组）；false = 折叠（完成组收起，默认）。 */
  expanded?: boolean;
  /** true = ASCII 兜底 glyph 表；false = GBK 白名单表（默认）。 */
  ascii?: boolean;
}

/** 状态分档：三态之外的脏数据一律归 `pending`（不崩、不改写）。 */
type PlanStyle = "completed" | "in_progress" | "pending";

/** 有序优先级列表：自上而下首个命中者胜（Taskwarrior `rule.precedence.color` 同款）。 */
const STYLE_RULES: ReadonlyArray<{ status: string; style: PlanStyle }> = [
  { status: "completed", style: "completed" },
  { status: "in_progress", style: "in_progress" },
];

function classify(status: string): PlanStyle {
  for (const rule of STYLE_RULES) {
    if (status === rule.status) return rule.style;
  }
  return "pending";
}

/** GBK 白名单 glyph 表（默认）。 */
const RICH_MARKERS: Record<PlanStyle, string> = {
  completed: "·",
  in_progress: GLYPHS.dot,
  pending: GLYPHS.circle,
};

/** ASCII 兜底表（终端无 Unicode 时降级；`>` 指针两表共用）。 */
const ASCII_MARKERS: Record<PlanStyle, string> = {
  completed: "[-]",
  in_progress: "[ ]",
  pending: "[ ]",
};

/** 删除线（完成项）：`\x1b[9m ... \x1b[29m`（W-28 票面硬约束）。 */
function strike(text: string): string {
  return `\x1b[9m${text}\x1b[29m`;
}

/** 粗体（进行中项）：`\x1b[1m ... \x1b[22m`。 */
function bold(text: string): string {
  return `\x1b[1m${text}\x1b[22m`;
}

/** 单行：`{branch}{pointer} {glyph} {text}`。前缀位常驻（指针列恒占 1 列，防抖动）。 */
function renderRow(
  item: PlanItem,
  index: number,
  current: boolean,
  theme: IaTheme,
  ascii: boolean,
): string {
  const branch = index === 0 ? theme.dim(`${GLYPHS.branch} `) : "  ";
  // W-27 契约（web `PlanList.test.tsx:88-94`）：脏数据双 in_progress 只认**首个**为
  // 当前项；其余 in_progress 按待办降级渲染（不指针、不高亮、文案取 content）。
  const classified = classify(item.status);
  const style: PlanStyle = classified === "in_progress" && !current ? "pending" : classified;
  // 选中/当前层与 item 状态层分离（Ratatui）：指针只在当前项（首个 in_progress）出现，
  // 但列位恒预留（前缀位常驻，防抖动）。
  const pointer = style === "in_progress" ? ">" : " ";
  const marker = (ascii ? ASCII_MARKERS : RICH_MARKERS)[style];
  // in_progress 文案取 activeForm（进行时），其余取 content；activeForm 空则回落 content。
  const label = style === "in_progress" && item.activeForm ? item.activeForm : item.content;
  const body = `${pointer} ${marker} ${label}`;
  const styled =
    style === "completed"
      ? strike(theme.dim(body))
      : style === "in_progress"
        ? bold(theme.accent(body))
        : theme.dim(body);
  return `${branch}${styled}`;
}

/** plan state -> 终端行（纯函数；零项/未出现过返回空数组，不留空壳）。 */
export function renderPlanList(
  plan: PlanItem[] | null,
  theme: IaTheme,
  options: PlanRenderOptions = {},
): string[] {
  if (plan === null || plan.length === 0) return [];
  const expanded = options.expanded ?? false;
  const ascii = options.ascii ?? false;

  const completedCount = plan.filter((item) => classify(item.status) === "completed").length;
  // 头部无 `└`：`└` 语义是「父行下的子行」，头部不是任何行的子行（Codex 头部用
  // bullet 前缀，`└` 只落在首个步骤行）。该 bullet 不在 GBK 白名单，故头部不带前缀。
  const head = theme.text("进度清单") + theme.dim(` · ${completedCount}/${plan.length} complete`);

  // 当前项 = 首个 in_progress（W-27 契约；双 in_progress 只认第一个）。
  const currentId = plan.find((item) => item.status === "in_progress")?.id ?? null;
  // 折叠 = 完成组整组收起（W-27 同款）；展开 = 全量。**不设行数上限**：票面无此约束，
  // 软上限 50 由服务端硬校验，渲染端不得静默丢项（超限行永不出现的 bug 已修）。
  const rows = expanded ? plan : plan.filter((item) => classify(item.status) !== "completed");

  const lines = [head];
  rows.forEach((item, index) =>
    lines.push(renderRow(item, index, item.id === currentId, theme, ascii)),
  );
  // 折叠时完成组被收起：给一行披露，`ctrl+t` 可展开。这是**唯一**的隐藏成因（无上限
  // 即无溢出截断），提示与实际行为一致，不暗示能揭开不存在的溢出项。
  if (!expanded && completedCount > 0) {
    lines.push(`  ${theme.dim(`· 已完成 ${completedCount} 项 (ctrl+t)`)}`);
  }
  return lines;
}
