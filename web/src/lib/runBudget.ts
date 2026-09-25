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
 *  片段与数字。渲染组件（`components/PausedPanel.tsx`）只负责排版。 */

import type {
  RunBudgetDimensionFacts,
  RunLimitsFacts,
  RunPausedInfo,
} from '../types';

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
  consumedKey: keyof RunBudgetDimensionFacts;
  /** `data.limits.run` 里的键。 */
  ceilingKey: keyof RunLimitsFacts;
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
 *  null（= unavailable），不猜也不四舍五入。 */
function parseDecimalText(raw: string | number): DecimalText | null {
  const text = typeof raw === 'number' ? String(raw) : raw.trim();
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
  consumed: number | string | null,
  ceiling: number | string | null,
  decimal: boolean,
): number | string | null {
  if (consumed === null || ceiling === null) return null;
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
  if (dimension === 'local.max_agent_turns') {
    return '单次执行保险丝到顶（local.max_agent_turns）';
  }
  return dimension || '预算维度未声明';
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
  /** 恢复所需的最小绝对 turn ceiling（turns 维；见 `minResumeCeiling`）。 */
  minResumeCeiling: number;
  /** 四维读数（turns 恒在，其余三维**只列有事实可说的**——与 CLI
   *  `_extra_dimension_lines` 同一取舍：老暂停只有 turns，多打三行 `unavailable /
   *  unlimited` 是噪声不是信息）。 */
  dimensions: DimensionFact[];
  /** 真正触发暂停的那一维的读数；暂停落在 local fuse（非 run 维）时为 null
   *  ——那时没有"某一维到顶"的 run 读数可报，不能拿 turns 冒充。 */
  tripped: DimensionFact | null;
  /** 恢复输入指向的维度：命中的 run 维；local fuse / 未知维度回落到 turns
   *  （与后端 `cli._RESUME_FLAGS` 的回落口径一致——抬 run ceiling 是唯一一个任何
   *  run 都读得懂的维度）。 */
  resumeTarget: RunDimensionSpec;
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
 *  说"至少 N"会把一个虚假的下限当成规则（N 只是默认值）。 */
export function resumeInputHint(facts: PauseFacts): string {
  const spec = facts.resumeTarget;
  const fact =
    facts.dimensions.find((row) => row.spec.dimension === spec.dimension) ?? null;
  const minimum = fact === null ? null : minResumeValue(spec, fact);
  const requirement = spec.decimal
    ? `须严格大于已消耗 ${fact?.consumedText ?? 'unavailable'}（默认给到 ${String(minimum)}）`
    : `至少 ${String(minimum)}`;
  return `抬的是 ${spec.resumeField}（CLI: ${spec.resumeFlag} N，${requirement}）；已消耗不重置，恢复沿用同一 run_id`;
}

export function pauseFacts(paused: RunPausedInfo): PauseFacts {
  const facts = RUN_DIMENSIONS.map((spec) => dimensionFact(spec, paused));
  const turnsFact = facts[0];
  // turns 恒显示（CLI 的 `_pause_facts` 也恒有一行 turns）；其余三维只在有事实时列出。
  const dimensions = facts.filter(
    (fact, index) => index === 0 || fact.consumed !== null || fact.ceiling !== null,
  );
  const resumeTarget =
    RUN_DIMENSIONS.find((spec) => spec.dimension === paused.trigger_dimension) ??
    RUN_DIMENSIONS[0];
  return {
    consumed: typeof turnsFact.consumed === 'number' ? turnsFact.consumed : 0,
    ceiling: typeof turnsFact.ceiling === 'number' ? turnsFact.ceiling : null,
    remaining: typeof turnsFact.remaining === 'number' ? turnsFact.remaining : null,
    localFuseTurns: paused.local_fuse?.max_agent_turns ?? null,
    localFuseSource: paused.local_fuse?.source ?? '',
    closeoutSource: paused.closeout_source,
    version: paused.version,
    dimensionLabel: dimensionLabel(paused.trigger_dimension),
    minResumeCeiling: minResumeCeiling(paused.consumed_agent_turns),
    dimensions,
    tripped: facts.find((fact) => fact.tripped) ?? null,
    resumeTarget,
  };
}

/** 绝对 ceiling 输入框的预校验（**只为省一次必然 409 的往返**，不是规则来源）。
 *
 *  返回 null = 可以提交；返回字符串 = 就地提示的原因。
 *  判据与后端 `resume_headroom_ok`（走 `_dimension_reached`）一致，且只看**本次要改的
 *  那一维**——未点名的维度沿用暂停时的 ceiling，它们够不够由后端判（前端多一条自己的
 *  规则就会与后端分叉）。维度按 `pauseFacts().resumeTarget` 取：抬高 requests 的暂停
 *  点，填一个只够 turns 的数字是解决不了问题的，提示必须点名同一维。 */
export function ceilingDraftError(paused: RunPausedInfo, draft: string): string | null {
  const facts = pauseFacts(paused);
  const spec = facts.resumeTarget;
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
  if (minimum === null || fact === null) {
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
 *  整数维返回 number，十进制维返回**字符串**（保住 wire 上的十进制精度；
 *  后端 `parse_cost_ceiling` 两者都收）。 */
export function ceilingDraftValue(
  paused: RunPausedInfo,
  draft: string,
): number | string | null {
  if (ceilingDraftError(paused, draft) !== null) return null;
  const spec = pauseFacts(paused).resumeTarget;
  const text = draft.trim();
  return spec.decimal ? text : Number(text);
}

/** continuation 的分段标签（`03 §3.4` 四键；与 CLI `_continuation_lines` 同序）。 */
export const CONTINUATION_SECTIONS = [
  { key: 'completed', label: '已完成' },
  { key: 'remaining', label: '剩余' },
  { key: 'blockers', label: '阻塞' },
] as const;
