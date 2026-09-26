/** 预算暂停的展示事实（`#312` T4 建，`#313` T5 扩到四维）——从 `RunPausedInfo`
 *  （事件真值）派生的**纯函数**。
 *
 *  为什么单列一个模块：CLI（`agent_harness.cli.render_pause_block` /
 *  `_pause_facts` / `_extra_dimension_lines`）与 Web 必须显示**同一份事实**（票面 AC：
 *  「CLI and Web display the same pause reason, consumed/limit values, version and
 *  continuation after refresh」）。这里逐条镜像 CLI 的取值口径，包括"缺数字 =
 *  unavailable，**永不**用 0 顶替"（`11 §6.1`）与 "unlimited 不是 0"。
 *
 *  `#313` 起暂停可能落在**四个** run 维度中的任何一个上（turns / requests / tokens /
 *  cost），所以本模块不再只认 turns：维度表 `RUN_DIMENSIONS` 与后端
 *  `run_budget.RUN_DIMENSIONS`（= `TRIGGER_ORDER` 去掉 local fuse）同序同值，
 *  面板与恢复输入都从它派生——**只读 turn 那一维会把"被 requests 卡住"显示成
 *  "消耗 0 轮、无上限"**，那是在陈述一件没发生过的事。
 *
 *  这里**不做**任何会话状态判断——输入就是投影出来的暂停事实，输出是要渲染的文本
 *  片段与数字。渲染组件（`components/PausedPanel.tsx`）只负责排版。
 *
 *  `#315` 起暂停多了一个**非预算**原因：`reason=deadline`（绝对截止时刻到点）。它
 *  与预算暂停共用同一份事件形状与同一条恢复路径（CAS + 绝对量），但**恢复动作**不同
 *  ——预算抬某个 ceiling，deadline 换一个新的未来时刻（`03 §3.4`）。所以本模块为它
 *  单列一组常量 + 一个 `ResumeTarget` 分支：把时刻当成"第五个数字 ceiling"来渲染
 *  会得到"已消耗不适用 / 无上限"这种自相矛盾的读数（deadline 没有 consumed 可言）。
 *
 *  `#314` 起有一维是**动态**的：per-tool 配额（`run.tool_call_limits.<工具名>`）。
 *  它不在 `RUN_DIMENSIONS` 这张定长表里，而由 `toolQuotaFacts` 按工具名排序给出
 *  （顺序确定才可复现，与后端 `run_budget.tool_dimensions` 同一取舍）。两个 counter
 *  也在这里分开报：`calls` 是**被接纳**的逻辑调用数（一次多调用批次里的每条各算一
 *  格），`attempts` 含 ToolExecutor 的 retry——报告成一对数字，是因为它们本来就不是
 *  同一个量（`02 §5.1`）。 */

import type { RunPausedInfo } from '../types';

/** 后端暂停判据里的两个常量（**只做展示口径，判定权威在后端**）。
 *
 *  `RESERVED_CLOSEOUT_TURNS` / `RESERVED_CLOSEOUT_REQUESTS` 镜像
 *  `run_budget.RESERVED_CLOSEOUT_TURNS` / `RESERVED_CLOSEOUT_REQUESTS`：turns 与
 *  requests 的暂停点在 `consumed + 预留 >= ceiling`，所以恢复至少要留出"一个可接纳
 *  单位 + 一次 closeout 预留"（后端 `resume_headroom_ok` 走 `_dimension_reached`，
 *  `ceiling > consumed + RESERVED`）。前端拿它**提示**（省一次必然 409 的往返），
 *  真正的拒绝在后端——两边口径若分叉，以后端为准，改这里对齐。 */
export const RESERVED_CLOSEOUT_TURNS = 1;
export const RESERVED_CLOSEOUT_REQUESTS = 1;

/** 恢复请求里 `budget.run` 的键名（后端 `web/app.py::RunLimitsBody` 的字段名，
 *  也是 `run_budget.RunLimits` 的字段名——三处同一个名字，别在这里发明同义词）。 */
export type RunLimitField =
  | 'max_agent_turns_total'
  | 'max_model_requests'
  | 'max_total_tokens'
  | 'max_cost_usd';

/** 四维**标量**读数的键（`data.consumed` 里那几个整数 / 十进制字段）。
 *
 *  工具配额的两张映射表（`tool_calls_by_tool` / `tool_attempts_by_tool`）刻意**不在**
 *  这张表能指的键里：它们是动态维度（工具名 → 计数），逐名断言的是 `ToolQuotaFact`；
 *  把它们放进来会让"一维"这个词同时指着两种粒度。 */
export type RunScalarConsumedKey =
  | 'agent_turns'
  | 'model_requests'
  | 'total_tokens'
  | 'cost_usd';

/** `data.limits.run` 里四维**标量** ceiling 的键（理由同上：工具配额是映射表）。 */
export type RunScalarCeilingKey =
  | 'max_agent_turns_total'
  | 'max_model_requests'
  | 'max_total_tokens'
  | 'max_cost_usd';

/** 一个 run 作用域维度在**四份**口径里的名字（事件里的触发名、消耗快照的键、
 *  ceiling 的键、恢复请求的字段、CLI 的开关）。放在一张表里是为了让"加一维"只有
 *  一处要改——散在几个 switch 里就会漏掉一个（`#313` 的 requests / tokens / cost
 *  正是这么漏的）。 */
export interface RunDimensionSpec {
  /** `run/paused.data.trigger_dimension` 的取值（后端 `run_budget.TRIGGER_*`）。 */
  dimension: string;
  /** 人话标签（与 CLI `_RESUME_FLAGS` 的开关名同源，见 `resumeFlag`）。 */
  label: string;
  /** `data.consumed` 里的键。 */
  consumedKey: RunScalarConsumedKey;
  /** `data.limits.run` 里的键。 */
  ceilingKey: RunScalarCeilingKey;
  /** 恢复请求 `budget.run` 的字段名。 */
  resumeField: RunLimitField;
  /** 抬高这一维的 CLI 开关（提示文案用；后端 `cli._RESUME_FLAGS` 的镜像）。 */
  resumeFlag: string;
  /** 准入预留（turns / requests 每次准入预留一个 closeout 位置，其余为 0）。 */
  reserved: number;
  /** 十进制计量维度（cost）：读数与差值都要按十进制字符串处理。 */
  decimal: boolean;
}

/** 四个 run 维度，顺序与后端 `run_budget.TRIGGER_ORDER` 一致（turns 最先）。 */
export const RUN_DIMENSIONS: readonly RunDimensionSpec[] = [
  {
    dimension: 'run.max_agent_turns_total',
    label: 'run 累计轮次到顶（run.max_agent_turns_total）',
    consumedKey: 'agent_turns',
    ceilingKey: 'max_agent_turns_total',
    resumeField: 'max_agent_turns_total',
    resumeFlag: '--run-turns-total',
    reserved: RESERVED_CLOSEOUT_TURNS,
    decimal: false,
  },
  {
    dimension: 'run.max_model_requests',
    label: 'run 累计模型请求到顶（run.max_model_requests）',
    consumedKey: 'model_requests',
    ceilingKey: 'max_model_requests',
    resumeField: 'max_model_requests',
    resumeFlag: '--run-model-requests',
    reserved: RESERVED_CLOSEOUT_REQUESTS,
    decimal: false,
  },
  {
    dimension: 'run.max_total_tokens',
    label: 'run 累计 token 到顶（run.max_total_tokens）',
    consumedKey: 'total_tokens',
    ceilingKey: 'max_total_tokens',
    resumeField: 'max_total_tokens',
    resumeFlag: '--run-total-tokens',
    reserved: 0,
    decimal: false,
  },
  {
    dimension: 'run.max_cost_usd',
    label: 'run 累计成本到顶（run.max_cost_usd）',
    consumedKey: 'cost_usd',
    ceilingKey: 'max_cost_usd',
    resumeField: 'max_cost_usd',
    resumeFlag: '--run-cost-usd',
    reserved: 0,
    decimal: true,
  },
];

/** `run/paused.data.reason` 的两个取值（后端 `run_budget.REASON_*` 的镜像）。
 *  `stuck` 属 `#317`，本模块不认它。 */
export const PAUSE_REASON_BUDGET_EXHAUSTED = 'budget_exhausted';
export const PAUSE_REASON_DEADLINE = 'deadline';

/** deadline 维（`#315`）：`trigger_dimension` 的取值（后端 `run_budget.TRIGGER_RUN_DEADLINE`）。
 *
 *  它**不在** `RUN_DIMENSIONS` 里，且这不是遗漏：那张表的每一项都同时给得出
 *  `consumedKey` / `ceilingKey` / `reserved`（"consumed 与 ceiling 比大小"的维度），
 *  而 deadline 判的是"当前时刻与截止时刻比先后"——没有 consumed 读数。硬塞进去会让
 *  `dimensionFact` 编出一个 `unavailable / unlimited` 的读数行。 */
export const DEADLINE_DIMENSION = 'run.deadline_at';

/** deadline 维在恢复请求里的字段名（后端 `budget.run.deadline_at`）。 */
export const DEADLINE_RESUME_FIELD = 'deadline_at';

/** 抬高 deadline 的 CLI 开关（后端 `cli._RESUME_FLAGS` 的镜像）。 */
export const DEADLINE_RESUME_FLAG = '--run-deadline';

/** 填入框里的时刻示例——形状提示，不是默认值（本模块不编"现在 + N 分钟"这种策略）。 */
export const DEADLINE_EXAMPLE = '2026-09-26T04:30:00Z';

/** 是不是 deadline 暂停（原因与触发维度任一命中即算：`reason` 是恢复动作的依据，
 *  `trigger_dimension` 是"卡在哪一维"——两者在合法载荷里同进同出，取或只是不依赖
 *  某一条恰好非空）。 */
export function isDeadlinePause(paused: RunPausedInfo): boolean {
  return (
    paused.reason === PAUSE_REASON_DEADLINE ||
    paused.trigger_dimension === DEADLINE_DIMENSION
  );
}

/** 暂停那一刻的 deadline 时刻（RFC 3339 UTC 文本）；没配 / 载荷没带 ⇒ null（不编值）。 */
export function deadlineInstant(paused: RunPausedInfo): string | null {
  return paused.run_limits?.deadline_at ?? null;
}

/** 时刻文本 → epoch 毫秒；**必须带时区**且可解析，否则 null。
 *
 *  为什么自己判时区而不是直接 `Date.parse`：后端 `parse_deadline_at` 对**朴素时间**
 *  （无时区）一律 422——同一份请求在不同机器上代表不同瞬时。`Date.parse` 会把
 *  `2026-09-26T04:30:00` 当本地时间收下，于是前端放行、后端拒——一次必然 422 的往返，
 *  且提示词还是错的（说好的格式其实不合法）。 */
function parseInstant(raw: string | null | undefined): number | null {
  if (raw === null || raw === undefined) return null;
  const text = raw.trim();
  if (!text) return null;
  if (!/(?:[Zz]|[+-]\d{2}:?\d{2})$/.test(text)) return null;
  const ms = Date.parse(text);
  return Number.isFinite(ms) ? ms : null;
}

/** deadline 草稿的预校验（**只为省一次必然拒绝的往返**，不是规则来源）。
 *
 *  判据与后端一致：形状是带时区的 RFC 3339（`parse_deadline_at`，422），且必须**严格
 *  在未来**（`resume_headroom_ok`，409）——沿用一个已到点的时刻等于恢复后立刻再停。 */
export function deadlineDraftError(paused: RunPausedInfo, draft: string): string | null {
  const text = draft.trim();
  if (!text) {
    return `请填一个未来时刻（RFC 3339 UTC，例：${DEADLINE_EXAMPLE}）`;
  }
  const ms = parseInstant(text);
  if (ms === null) {
    return `deadline 必须是带时区的 RFC 3339 时刻（例：${DEADLINE_EXAMPLE}）；朴素时间在后端会被拒（422）`;
  }
  if (ms <= Date.now()) {
    const was = deadlineInstant(paused);
    return `deadline 必须是**未来**的时刻，否则恢复后立刻再次暂停${
      was ? `（本次 ${was} 已到点）` : ''
    }`;
  }
  return null;
}

/** per-tool 配额维度的名字前缀（后端 `run_budget.TRIGGER_RUN_TOOL_PREFIX` 的镜像）。
 *  工具配额是**动态维度**：每个配了配额的工具名各占一维
 *  `run.tool_call_limits.<工具名>`，所以它不在 `RUN_DIMENSIONS` 这张定长表里。 */
export const TOOL_DIMENSION_PREFIX = 'run.tool_call_limits.';

/** 配额维度名 → 工具名；不是 per-tool 维度时 null（后端 `tool_name_of_dimension` 同判）。 */
export function toolDimensionName(dimension: string): string | null {
  if (!dimension.startsWith(TOOL_DIMENSION_PREFIX)) return null;
  const name = dimension.slice(TOOL_DIMENSION_PREFIX.length);
  return name || null;
}

/** 工具名 → 它的配额维度名（`trigger_dimension` 的那个形态）。 */
export function toolQuotaDimension(name: string): string {
  return `${TOOL_DIMENSION_PREFIX}${name}`;
}

/** 一个工具的配额读数（`#314`）：两个 counter 分开给，文本字段同 `DimensionFact`
 *  的渲染口径（`unavailable` / `unlimited`，**永不**写 0 冒充）。 */
export interface ToolQuotaFact {
  /** 工具名（后端注册名，恢复请求点名它）。 */
  name: string;
  /** `run.tool_call_limits.<name>`（`trigger_dimension` 的取值）。 */
  dimension: string;
  /** 人话标签（命中暂停时面板与提示共用同一句）。 */
  label: string;
  /** 已**接纳**的逻辑调用数；null = 账目未知（旧快照 / 载荷没带那张表）。 */
  calls: number | null;
  /** 实际尝试次数（含 retry）；null = 未知。**不是** calls 的别名。 */
  attempts: number | null;
  /** 该工具的绝对 ceiling；null = 没配配额（unlimited，不是 0）。 */
  ceiling: number | null;
  /** ceiling − calls（下限 0）；任一侧缺 ⇒ null。 */
  remaining: number | null;
  callsText: string;
  attemptsText: string;
  ceilingText: string;
  remainingText: string;
  tripped: boolean;
}

/** per-tool 表里某工具的读数：**表在不在**决定 0 还是 null（未知），不是"键在不在"。
 *
 *  表是对象 ⇒ 缺名就是 0（后端 `agent/run_budget.BudgetConsumed.calls_for` 的同一口径：
 *  `{}` = 已知且一个都没调用；表缺席 / 为 null = 未知）。反例（两轴审查共同发现）：
 *  配了 ceiling 却从未调用过的工具（`{"bash": 3}` + `{}`）曾被渲染成 `unavailable`
 *  而同一份 durable 事件的服务端投影给的是 `remaining 3`——同一事实两个互相矛盾的
 *  读数，正是 `11 §6.1` 要消灭的那种不一致。 */
function perToolReading(
  table: Record<string, number> | null | undefined,
  name: string,
): number | null {
  if (table === null || table === undefined) return null;
  return table[name] ?? 0;
}

/** 单个工具的配额读数：两个 counter 各取各的键、ceiling 取配置表里的那一条。 */
function toolQuotaFact(name: string, paused: RunPausedInfo): ToolQuotaFact {
  const calls = perToolReading(paused.consumed_dimensions?.tool_calls_by_tool, name);
  const attempts = perToolReading(paused.consumed_dimensions?.tool_attempts_by_tool, name);
  const ceiling = paused.run_limits?.tool_call_limits?.[name] ?? null;
  const dimension = toolQuotaDimension(name);
  // 复用四维那一份"剩余"的算法（十进制分支在这里取不到，收窄成整数读数；工具配额
  // 本来只按整数计量——`tool_call_limits` 在 wire 上是 `dict[str, int]`）。
  const remainingRaw = dimensionRemaining(calls, ceiling, false);
  const remaining = typeof remainingRaw === 'number' ? remainingRaw : null;
  return {
    name,
    dimension,
    label: `工具 ${name} 的本 run 调用配额到顶（${dimension}）`,
    calls,
    attempts,
    ceiling,
    remaining,
    callsText: calls === null ? 'unavailable' : String(calls),
    attemptsText: attempts === null ? 'unavailable' : String(attempts),
    ceilingText: ceiling === null ? 'unlimited' : String(ceiling),
    remainingText: remaining === null ? 'unavailable' : String(remaining),
    tripped: paused.trigger_dimension === dimension,
  };
}

/** 本 run 有事实可说的工具配额：**配了配额的 ∪ 实际调用过的**，按工具名排序。
 *
 *  为什么是两个来源的并集（与 CLI `_tool_dimension_lines` 同一取舍）：
 *  - 只看配置 ⇒ 漏掉"没配配额但调了 7 次"——那是可观测性要的事实；
 *  - 只看计数 ⇒ 漏掉"配了却一次没调"——配额确实存在，也是事实。
 *  两者都空 ⇒ 空表（不渲染这一节，同 CLI 不为空表打印表头）。
 *
 *  `calls` 表未知（null）时**不是**当 0 处理：那时只列配置了的工具名，它们的读数是
 *  null ⇒ 渲染成 unavailable（`11 §6.1`：不可得 ≠ 0）。表**已知**而某个名字不在表里
 *  则是 0（见 `perToolReading`）——"未知"与"零"是两件事。 */
export function toolQuotaFacts(paused: RunPausedInfo): ToolQuotaFact[] {
  const limits = paused.run_limits?.tool_call_limits ?? null;
  const calls = paused.consumed_dimensions?.tool_calls_by_tool ?? null;
  const names = new Set<string>([
    ...Object.keys(limits ?? {}),
    ...Object.keys(calls ?? {}),
  ]);
  return [...names].sort().map((name) => toolQuotaFact(name, paused));
}

/** 工具配额恢复的最小合法**绝对** ceiling = `calls + 1`。
 *
 *  后端 `resume_headroom_ok` 对 per-tool 维的判据是 `consumed < ceiling`（`>=` 会在
 *  下次准入立刻再次暂停），所以最小合法值就是"已接纳的调用数 + 1"。**不含** closeout
 *  预留：预留的语义是"保证暂停时还收得了口"（`02 §5.2`），而 closeout 是一次模型
 *  请求、不产生任何工具调用——给它留一格会让"配额 = 3"实际允许 4 次调用。
 *  账目未知（calls = null）⇒ null（后端必然 409，前端不编数字）。 */
export function minToolQuotaValue(fact: ToolQuotaFact): number | null {
  return fact.calls === null ? null : fact.calls + 1;
}

/** 一个维度的展示读数（数字或十进制字符串；null = 载荷没带这一维 / 该维不可得）。
 *  文本字段是渲染口径：`unavailable` / `unlimited`，**永不**写 0 冒充（`11 §6.1`）。 */
export interface DimensionFact {
  spec: RunDimensionSpec;
  /** 人话标签（该维没命中暂停时也照样给出，供清单行使用）。 */
  label: string;
  consumed: number | string | null;
  ceiling: number | string | null;
  /** ceiling − consumed（下限 0，**十进制精确**）；任一侧缺 ⇒ null。 */
  remaining: number | string | null;
  consumedText: string;
  ceilingText: string;
  remainingText: string;
  /** 是不是**这一维**触发了暂停（`trigger_dimension` 命中它）。 */
  tripped: boolean;
}

/** 十进制字符串的定点表示（整数化后的值与小数位数）。 */
interface DecimalText {
  /** 去掉小数点后的整数值（带符号）。 */
  units: bigint;
  /** 小数点后位数。 */
  scale: number;
}

/** 十进制的读数 / 比较 / 加减（cost 维）：**绝不 float 化**（`11 §6.1`：二进制浮点
 *  相等不是契约）。按小数点对齐后做整数运算，因而不引入任何近似；形状不可解析 ⇒
 *  null（= unavailable），不猜也不四舍五入。
 *
 *  入参宽容到 `undefined`（而不只是 `null`）：本模块的读数是**导出**给调用方的纯
 *  函数，非规范载荷（缺键）与"值不可得"在渲染上同一处置——都该显示 unavailable，
 *  而不是在 `raw.trim()` 上抛 TypeError 把整个面板带崩。 */
function parseDecimalText(raw: string | number | null | undefined): DecimalText | null {
  if (raw === null || raw === undefined) return null;
  const text = typeof raw === 'number' ? String(raw) : raw.trim();
  if (!text) return null;
  const match = /^([+-]?)(\d+)(?:\.(\d*))?$/.exec(text);
  if (!match) return null;
  const sign = match[1];
  const whole = match[2];
  const fraction = match[3] ?? '';
  const units = BigInt(`${whole}${fraction}`);
  return { units: sign === '-' ? -units : units, scale: fraction.length };
}

function formatDecimalText(value: DecimalText): string {
  const negative = value.units < 0n;
  const digits = (negative ? -value.units : value.units).toString();
  if (value.scale === 0) return `${negative ? '-' : ''}${digits}`;
  const padded = digits.padStart(value.scale + 1, '0');
  const whole = padded.slice(0, padded.length - value.scale);
  const fraction = padded.slice(padded.length - value.scale);
  return `${negative ? '-' : ''}${whole}.${fraction}`;
}

function alignDecimal(left: DecimalText, right: DecimalText): [bigint, bigint, number] {
  const scale = Math.max(left.scale, right.scale);
  return [
    left.units * 10n ** BigInt(scale - left.scale),
    right.units * 10n ** BigInt(scale - right.scale),
    scale,
  ];
}

/** 十进制加法（用于"最小合法 ceiling"提示）；形状不合 ⇒ null。 */
function addDecimal(raw: string | number, extra: number): string | null {
  const parsed = parseDecimalText(raw);
  if (parsed === null) return null;
  const [left, right] = alignDecimal(parsed, { units: BigInt(extra), scale: 0 });
  return formatDecimalText({ units: left + right, scale: parsed.scale });
}

/** `max(ceiling − consumed, 0)`：整数维返回 number，十进制维返回字符串。
 *
 *  cost 走十进制字符串（wire 上就是字符串），整数维走 number。任一侧不可得 / 形状
 *  不合 ⇒ null = unavailable——"算不出剩余"就如实说不知道，比编一个 0 更接近事实
 *  （与 CLI `_dimension_remaining` 同一判据）。 */
export function dimensionRemaining(
  consumed: number | string | null | undefined,
  ceiling: number | string | null | undefined,
  decimal: boolean,
): number | string | null {
  if (consumed === null || consumed === undefined) return null;
  if (ceiling === null || ceiling === undefined) return null;
  if (!decimal) {
    if (typeof consumed !== 'number' || typeof ceiling !== 'number') return null;
    return Math.max(ceiling - consumed, 0);
  }
  const left = parseDecimalText(ceiling);
  const right = parseDecimalText(consumed);
  if (left === null || right === null) return null;
  const [a, b, scale] = alignDecimal(left, right);
  const diff = a - b;
  return formatDecimalText({ units: diff < 0n ? 0n : diff, scale });
}

/** 标量比较（整数维按数值，十进制维按对齐后的整数）。任一侧不可得 ⇒ null。 */
function compareDimension(
  a: string,
  b: string | number,
  decimal: boolean,
): number | null {
  if (!decimal) {
    const left = Number(a);
    const right = Number(b);
    if (!Number.isFinite(left) || !Number.isFinite(right)) return null;
    return left - right;
  }
  const left = parseDecimalText(a);
  const right = parseDecimalText(b);
  if (left === null || right === null) return null;
  const [x, y] = alignDecimal(left, right);
  return x === y ? 0 : x < y ? -1 : 1;
}

/** 命中的 ceiling 维度 → 标签。取值域来自后端 `run_budget.TRIGGER_*`（四维 + 单实例
 *  保险丝）；未知值原样回显（不猜、也不显示成"未知维度"——原码本身对排查有用）。 */
export function dimensionLabel(dimension: string): string {
  const spec = RUN_DIMENSIONS.find((row) => row.dimension === dimension);
  if (spec) return spec.label;
  if (dimension === DEADLINE_DIMENSION) {
    return '绝对截止时刻已到（run.deadline_at）';
  }
  if (dimension === 'local.max_agent_turns') {
    return '单次执行保险丝到顶（local.max_agent_turns）';
  }
  return dimension || '预算维度未声明';
}

/** 暂停原因的人话标签（与 `dimensionLabel` 同一取舍：未知值原样回显，不编名字）。
 *
 *  两类原因**不能**合并成一句"预算问题"：`budget_exhausted` 的处置是"抬高某个
 *  ceiling"，`deadline` 的处置是"换一个新的未来时刻"——面板上的恢复输入框形状都不同
 *  （数字 vs RFC 3339 时刻），文案合并会让用户填错东西。 */
export function pauseReasonLabel(paused: RunPausedInfo): string {
  if (paused.reason === PAUSE_REASON_DEADLINE) {
    return '绝对截止时刻到点（deadline：不再接纳新的 Provider 请求 / 工具调用 / 子 Agent）';
  }
  if (paused.reason === PAUSE_REASON_BUDGET_EXHAUSTED) {
    return '预算到顶（budget_exhausted：某一维的绝对 ceiling 用尽）';
  }
  if (paused.reason === 'stuck') {
    return '疑似卡住（stuck：需要变更依据才能恢复，属 #317）';
  }
  return paused.reason || '暂停原因未声明';
}

/** 一个维度的读数。turns 维兼容 `#313` 之前的暂停载荷：那时只有 `consumed_agent_turns`
 *  / `run_limit` 两个平铺字段，没有四维组。 */
function dimensionFact(spec: RunDimensionSpec, paused: RunPausedInfo): DimensionFact {
  const snapshot = paused.consumed_dimensions;
  const limits = paused.run_limits;
  const isTurns = spec.consumedKey === 'agent_turns';
  let consumed: number | string | null;
  let ceiling: number | string | null;
  if (isTurns) {
    consumed = snapshot?.agent_turns ?? paused.consumed_agent_turns;
    ceiling = limits?.max_agent_turns_total ?? paused.run_limit;
  } else {
    consumed = snapshot ? snapshot[spec.consumedKey] : null;
    ceiling = limits ? limits[spec.ceilingKey] : null;
  }
  const remaining = dimensionRemaining(consumed, ceiling, spec.decimal);
  return {
    spec,
    label: spec.label,
    consumed,
    ceiling,
    remaining,
    // 缺失一律 unavailable / unlimited，**永不**用 0 顶替（`11 §6.1`，与 CLI 同口径）。
    consumedText: consumed === null ? 'unavailable' : String(consumed),
    ceilingText: ceiling === null ? 'unlimited' : String(ceiling),
    remainingText: remaining === null ? 'unavailable' : String(remaining),
    tripped: paused.trigger_dimension === spec.dimension,
  };
}

/** 恢复请求要抬的那一维（`#314` 起有**两种**目标）。
 *
 *  - `run`：四个 run 限额之一（恢复请求的键是 `budget.run.max_*`）；
 *  - `tool`：per-tool 配额（键是 `budget.run.tool_call_limits.<工具名>`）——配额是
 *    "一维变多维"的那一维，它的**点名单位是工具名**，所以恢复目标不能再用一个字段名
 *    表示。这正是不把它塞进 `RunDimensionSpec[]` 的原因：那张表的每一项都对应一个
 *    固定的 `max_*` 字段，而工具维的基数是无穷的。
 *
 *  命中 local fuse / 未知维度时回落到 turns 维（与后端 `cli._RESUME_FLAGS` 的回落
 *  口径一致——抬 run ceiling 是唯一一个任何 run 都读得懂的维度）。 */
export type ResumeTarget =
  | { kind: 'run'; spec: RunDimensionSpec }
  | { kind: 'tool'; quota: ToolQuotaFact }
  /** `#315`：deadline 暂停——恢复给的是**新的绝对时刻**，不是任何 ceiling 的数字。
   *  它与 run 维是同一类动作（给绝对值、不重置 counter），但值域不同（时刻文本），
   *  所以在这里分开：草稿校验、提示文案、请求体的键都由这个 kind 决定。 */
  | { kind: 'deadline' };

/** 恢复输入框那一行显示的目标名：与恢复请求的键路径同一形态
 *  （`max_model_requests` / `tool_call_limits.glob`）。 */
export function resumeTargetLabel(target: ResumeTarget): string {
  if (target.kind === 'run') return target.spec.resumeField;
  if (target.kind === 'tool') return `tool_call_limits.${target.quota.name}`;
  return DEADLINE_RESUME_FIELD;
}

/** 抬高这一维的 CLI 开关（提示文案用；后端 `cli._RESUME_FLAGS` / `_resume_command_tail`
 *  的镜像）。工具配额给的是**真实 argv 形状**（`NAME=N` 在同一个参数里），与 CLI 的
 *  `resume_hint` 逐字一致——提示必须是一条能照抄执行的命令，不是示意。 */
export function resumeTargetCliFlag(target: ResumeTarget): string {
  if (target.kind === 'run') return `${target.spec.resumeFlag} N`;
  if (target.kind === 'tool') return `--run-tool-limit ${target.quota.name}=N`;
  return `${DEADLINE_RESUME_FLAG} ${DEADLINE_EXAMPLE}`;
}

export interface PauseFacts {
  /** turns 维的读数（保留给既有消费端：面板的恢复输入默认落在这一维）。 */
  consumed: number;
  /** 绝对 ceiling；null = 未配（UI 文案 "unlimited"，不是 0）。 */
  ceiling: number | null;
  /** ceiling − consumed（下限 0）；ceiling 缺失时为 null。 */
  remaining: number | null;
  localFuseTurns: number | null;
  localFuseSource: string;
  closeoutSource: string;
  version: number;
  /** 命中维度的人话标签（未知维度原样回显，不编名字）。 */
  dimensionLabel: string;
  /** 暂停原因的原值（`budget_exhausted` / `deadline`）。 */
  reason: string;
  /** 暂停原因的人话标签（两类原因对应**不同**的恢复动作，面板据此换文案）。 */
  reasonLabel: string;
  /** `#315`：本 run 配的绝对截止时刻（RFC 3339 UTC 文本）；没配 ⇒ null。 */
  deadline: string | null;
  /** `#315`：本次暂停是 deadline 到点（`ResumeTarget.deadline` 的判据）。 */
  deadlinePause: boolean;
  /** 恢复所需的最小绝对 turn ceiling（turns 维；见 `minResumeCeiling`）。 */
  minResumeCeiling: number;
  /** 四维读数（turns 恒在，其余三维**只列有事实可说的**——与 CLI
   *  `_extra_dimension_lines` 同一取舍：老暂停只有 turns，多打三行 `unavailable /
   *  unlimited` 是噪声不是信息）。 */
  dimensions: DimensionFact[];
  /** 真正触发暂停的那一维的读数；暂停落在 local fuse（非 run 维）或某个**工具配额**
   *  上时为 null——那时没有"某一维到顶"的 run 读数可报，不能拿 turns 冒充
   *  （工具配额命中的读数在 `trippedTool` 里）。 */
  tripped: DimensionFact | null;
  /** `#314`：本 run 有事实可说的工具配额（配置过的 ∪ 调用过的，按工具名排序）。 */
  toolQuotas: ToolQuotaFact[];
  /** `#314`：真正触发暂停的那个**工具配额**；命中 run 维 / local fuse 时为 null。 */
  trippedTool: ToolQuotaFact | null;
  /** 恢复输入指向的维度：命中的 run 维、或命中的**工具配额**；local fuse / 未知维度
   *  回落到 turns（见 `ResumeTarget` 的理由）。 */
  resumeTarget: ResumeTarget;
}

/** 恢复请求的最小合法绝对 turn ceiling（**绝对值**，不是增量）。 */
export function minResumeCeiling(consumedTurns: number): number {
  return consumedTurns + RESERVED_CLOSEOUT_TURNS + 1;
}

/** 该维度"恢复后能真的干活"的**最小合法**绝对 ceiling（整数维），或计量维度
 *  （tokens / cost）的**建议默认值**（后端 `resume_headroom_ok` 的镜像）。
 *
 *  后端的判据是"这一维恢复后至少放得下一次新准入"：
 *  - 可数维度（turns / requests）每次准入预留一个 closeout 位置 ⇒ 最小 =
 *    `consumed + reserved + 1`（**存在**最小值，差一格就会被 409 拒）；
 *  - 计量维度（tokens / cost）下一轮多大不可预知 ⇒ 判据只是**严格大于**已消耗，
 *    连续域上没有"最小"可言（多一分钱都合法）⇒ 这里返回 `consumed + 1` 只作为
 *    输入框的**安全默认值**，不是下限；
 *  - 账目未知（null）⇒ 后端必然 409，这里返回 null 表示"算不出建议值"（不编数字）。 */
export function minResumeValue(
  spec: RunDimensionSpec,
  fact: DimensionFact,
): number | string | null {
  if (fact.consumed === null) return null;
  if (spec.decimal) return addDecimal(fact.consumed, 1);
  if (typeof fact.consumed !== 'number') return null;
  return fact.consumed + spec.reserved + 1;
}

/** 恢复输入框下方那行提示的文本（与 CLI `resume_hint` 同一份事实的另一种排版：
 *  点名要抬哪一维、抬到多少才算数、以及"已消耗不重置"这条语义）。
 *
 *  可数维度说"至少 N"；计量维度不能这么说——它的判据是"严格大于已消耗"，
 *  说"至少 N"会把一个虚假的下限当成规则（N 只是默认值）。工具配额（`#314`）是
 *  可数的，且最小值就是"已接纳调用数 + 1"（不留 closeout 预留）。 */
export function resumeInputHint(facts: PauseFacts): string {
  const target = facts.resumeTarget;
  if (target.kind === 'deadline') {
    // deadline 维的判据是**严格在未来**（`resume_headroom_ok`）：给不出"至少到多少"，
    // 只能点名"必须晚于现在"以及原来那个已到点的时刻（好让用户看出这是**换时刻**，
    // 不是"把同一个时刻调大"）。
    return `换一个**未来**的绝对截止时刻（CLI: ${resumeTargetCliFlag(target)}，RFC 3339 UTC）；${
      facts.deadline === null ? '本次暂停快照里没有时刻' : `本次 ${facts.deadline} 已到点`
    }——沿用会被拒（409）。已消耗不重置，恢复沿用同一 run_id`;
  }
  if (target.kind === 'tool') {
    return `抬的是 ${resumeTargetLabel(target)}（CLI: ${resumeTargetCliFlag(target)}，至少 ${String(
      minToolQuotaValue(target.quota),
    )}）；已消耗不重置，恢复沿用同一 run_id`;
  }
  const spec = target.spec;
  const fact =
    facts.dimensions.find((row) => row.spec.dimension === spec.dimension) ?? null;
  const minimum = fact === null ? null : minResumeValue(spec, fact);
  const requirement = spec.decimal
    ? `须严格大于已消耗 ${fact?.consumedText ?? 'unavailable'}（默认给到 ${String(minimum)}）`
    : `至少 ${String(minimum)}`;
  return `抬的是 ${spec.resumeField}（CLI: ${resumeTargetCliFlag(target)}，${requirement}）；已消耗不重置，恢复沿用同一 run_id`;
}

export function pauseFacts(paused: RunPausedInfo): PauseFacts {
  const facts = RUN_DIMENSIONS.map((spec) => dimensionFact(spec, paused));
  const turnsFact = facts[0];
  // turns 恒显示（CLI 的 `_pause_facts` 也恒有一行 turns）；其余三维只在有事实时列出。
  const dimensions = facts.filter(
    (fact, index) => index === 0 || fact.consumed !== null || fact.ceiling !== null,
  );
  const toolQuotas = toolQuotaFacts(paused);
  const trippedTool = toolQuotas.find((quota) => quota.tripped) ?? null;
  // 恢复目标：命中的**工具配额**优先（它是动态维度，抬 turns 解不了它的暂停）；否则
  // 按命中的 run 维取；local fuse / 未知维度回落到 turns（`ResumeTarget` 的理由）。
  const deadlinePause = isDeadlinePause(paused);
  const resumeTarget: ResumeTarget = deadlinePause
    ? { kind: 'deadline' }
    : trippedTool !== null
      ? { kind: 'tool', quota: trippedTool }
      : {
          kind: 'run',
          spec:
            RUN_DIMENSIONS.find((spec) => spec.dimension === paused.trigger_dimension) ??
            RUN_DIMENSIONS[0],
        };
  return {
    consumed: typeof turnsFact.consumed === 'number' ? turnsFact.consumed : 0,
    ceiling: typeof turnsFact.ceiling === 'number' ? turnsFact.ceiling : null,
    remaining: typeof turnsFact.remaining === 'number' ? turnsFact.remaining : null,
    localFuseTurns: paused.local_fuse?.max_agent_turns ?? null,
    localFuseSource: paused.local_fuse?.source ?? '',
    closeoutSource: paused.closeout_source,
    version: paused.version,
    dimensionLabel: dimensionLabel(paused.trigger_dimension),
    reason: paused.reason,
    reasonLabel: pauseReasonLabel(paused),
    deadline: deadlineInstant(paused),
    deadlinePause,
    minResumeCeiling: minResumeCeiling(paused.consumed_agent_turns),
    dimensions,
    tripped: facts.find((fact) => fact.tripped) ?? null,
    toolQuotas,
    trippedTool,
    resumeTarget,
  };
}

/** 工具配额草稿的预校验（判据与后端 `resume_headroom_ok` 对 per-tool 维一致：严格
 *  大于已接纳的调用数）。 */
function toolDraftError(quota: ToolQuotaFact, draft: string): string | null {
  const text = draft.trim();
  if (!text) return `请填绝对 ceiling（${quota.label}）`;
  // per-tool 配额的 wire 形状是 `dict[str, int]`（`parse_tool_call_limits` 拒 bool /
  // 非整数 / <1）⇒ 形状先按"正整数"判，再判"够不够"（顺序同 run 维）。
  if (!/^\d+$/.test(text)) return 'ceiling 必须是正整数';
  const value = Number(text);
  if (!Number.isSafeInteger(value) || value < 1) return 'ceiling 必须是正整数';
  const minimum = minToolQuotaValue(quota);
  if (minimum === null) {
    return '已消耗读数不可得：这一维配了 ceiling 而账目未知时后端会拒绝恢复（409）';
  }
  if (value < minimum) {
    return `ceiling 必须大于已消耗 ${quota.callsText} 次调用（至少 ${minimum}），否则恢复后立刻会再次暂停`;
  }
  return null;
}

/** 恢复请求的目标（App 组装请求体用）：run 维给恢复字段名，工具配额给工具名。
 *  与 `resumeTargetLabel` 是同一份事实的两种消费者（那是给人看的键路径，这是给
 *  `budget.run` 用的键）。 */
export type ResumeRequestTarget =
  | { kind: 'run'; field: RunLimitField }
  | { kind: 'tool'; tool: string }
  /** `#315`：deadline 暂停——请求体里点名的是 `budget.run.deadline_at`（时刻文本）。 */
  | { kind: 'deadline'; field: typeof DEADLINE_RESUME_FIELD };

export function resumeRequestTarget(paused: RunPausedInfo): ResumeRequestTarget {
  const target = pauseFacts(paused).resumeTarget;
  if (target.kind === 'run') return { kind: 'run', field: target.spec.resumeField };
  if (target.kind === 'tool') return { kind: 'tool', tool: target.quota.name };
  return { kind: 'deadline', field: DEADLINE_RESUME_FIELD };
}

/** 输入框草稿的默认值：卡住的那一维**恰好合法**的最小值（零点击可提交）——它只是
 *  草稿初值，不是"权威 ceiling"。算不出（账目未知）⇒ null（调用方给空串，不编数字）。 */
export function defaultResumeDraft(paused: RunPausedInfo): string | null {
  const facts = pauseFacts(paused);
  const target = facts.resumeTarget;
  if (target.kind === 'deadline') {
    // **不**编一个"现在 + N 分钟"：那是策略（跑多久算合适），不是本地能算出来的事实。
    // 后端要的是"客户端点名的未来时刻"，替用户填一个数字会让这次恢复看起来是系统的决定。
    return null;
  }
  if (target.kind === 'tool') {
    const minimum = minToolQuotaValue(target.quota);
    return minimum === null ? null : String(minimum);
  }
  const fact =
    facts.dimensions.find((row) => row.spec.dimension === target.spec.dimension) ?? null;
  if (fact === null) return String(facts.minResumeCeiling);
  const minimum = minResumeValue(target.spec, fact);
  return minimum === null ? null : String(minimum);
}

/** 绝对 ceiling 输入框的预校验（**只为省一次必然 409 的往返**，不是规则来源）。
 *
 *  `#315`：deadline 暂停的目标不是一个 ceiling 数字而是**一个未来时刻**，所以那一支
 *  分派到 `deadlineDraftError`（形状是带时区的 RFC 3339 + 严格在未来）——它与"够不够
 *  大"的判据没有共同点，硬塞进下面这条数字通道会让时刻被当成非法数字而误拒。
 *
 *  返回 null = 可以提交；返回字符串 = 就地提示的原因。
 *  判据与后端 `resume_headroom_ok`（走 `_dimension_reached`）一致，且只看**本次要改的
 *  那一维**——未点名的维度沿用暂停时的 ceiling，它们够不够由后端判（前端多一条自己的
 *  规则就会与后端分叉）。维度按 `pauseFacts().resumeTarget` 取：抬高 requests 的暂停
 *  点，填一个只够 turns 的数字是解决不了问题的，提示必须点名同一维。 */
export function ceilingDraftError(paused: RunPausedInfo, draft: string): string | null {
  const facts = pauseFacts(paused);
  const target = facts.resumeTarget;
  // `#315`：deadline 的目标是**时刻**（文本），与任何 ceiling 的"够不够"判据都不同，
  // 分派到它自己的那一份校验（形状 + 严格在未来）。
  if (target.kind === 'deadline') return deadlineDraftError(paused, draft);
  // `#314`：工具配额的判据与 run 维不同（严格大于已接纳调用数，且 wire 形状是整数），
  // 所以分派到它自己的那一份校验里。
  if (target.kind === 'tool') return toolDraftError(target.quota, draft);
  const spec = target.spec;
  const fact = facts.dimensions.find((row) => row.spec.dimension === spec.dimension) ?? null;
  const text = draft.trim();
  if (!text) return `请填绝对 ceiling（${spec.label}）`;
  if (spec.decimal) {
    if (!/^\d+(\.\d+)?$/.test(text)) return 'ceiling 必须是非负十进制数';
  } else {
    if (!/^\d+$/.test(text)) return 'ceiling 必须是正整数';
    const value = Number(text);
    // 先把"形状"判完（0 / 溢出都不是正整数），再判"够不够"——否则 0 会以
    // "必须大于已消耗"的面目出现，掩盖了它根本不是正整数这件事。
    if (!Number.isSafeInteger(value) || value < 1) return 'ceiling 必须是正整数';
  }
  const minimum = fact === null ? null : minResumeValue(spec, fact);
  // `fact.consumed` 是**可空属性**（行在、读数为未知）：它与"行不在"是同一件事的两种形状，
  // 一起收窄——否则下面的阈值仍然是 `string | number | null`，`tsc` 在赋值处报。
  if (minimum === null || fact === null || fact.consumed === null) {
    // 账目未知而该维配了 ceiling：后端必然 409（无法证明在预算内）。前端不编一个
    // 数字，如实说"预校验不了"。
    return '已消耗读数不可得：这一维配了 ceiling 而账目未知时后端会拒绝恢复（409）';
  }
  // 阈值按维度取：可数维度比"最小合法值"（consumed + 预留 + 1），计量维度比"已消耗"
  // 且要求**严格大于**（后端判据 `consumed < ceiling`；拿 consumed + 1 当阈值会把
  // 1.26 这种合法值误拒）。
  const threshold: number | string = spec.decimal ? fact.consumed : minimum;
  const comparison = compareDimension(text, threshold, spec.decimal);
  const passes =
    comparison === null ? false : spec.decimal ? comparison > 0 : comparison >= 0;
  if (!passes) {
    if (spec.decimal) {
      return `ceiling 必须严格大于已消耗 ${fact.consumedText}，否则恢复后立刻会再次暂停`;
    }
    const reserve = spec.reserved > 0 ? ` + ${spec.reserved}` : '';
    return `ceiling 必须大于已消耗 ${fact.consumedText}${reserve}（至少 ${String(minimum)}），否则恢复后立刻会再次暂停`;
  }
  return null;
}

/** 合法输入 → 提交用的值；非法返回 null（调用方据此禁用按钮）。
 *  整数维（含 `#314` 的工具配额）返回 number，十进制维返回**字符串**（保住 wire 上的
 *  十进制精度；后端 `parse_cost_ceiling` 两者都收，而工具配额只收整数）。 */
export function ceilingDraftValue(
  paused: RunPausedInfo,
  draft: string,
): number | string | null {
  if (ceilingDraftError(paused, draft) !== null) return null;
  const target = pauseFacts(paused).resumeTarget;
  const text = draft.trim();
  // deadline 维在 wire 上就是**文本**（后端 `parse_deadline_at` 只收带时区的时刻），
  // 不做任何数值化——`Number('2026-09-26T04:30:00Z')` 是 NaN。
  if (target.kind === 'deadline') return text;
  if (target.kind === 'tool') return Number(text);
  return target.spec.decimal ? text : Number(text);
}

/** continuation 的分段标签（`03 §3.4` 四键；与 CLI `_continuation_lines` 同序）。 */
export const CONTINUATION_SECTIONS = [
  { key: 'completed', label: '已完成' },
  { key: 'remaining', label: '剩余' },
  { key: 'blockers', label: '阻塞' },
] as const;
