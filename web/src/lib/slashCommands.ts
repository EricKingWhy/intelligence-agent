/** #1195 — Composer 内联 `/` 命令菜单：注册表与执行层（票面 §4.1/§4.2）。
 *
 * 设计（方案依据，见 PR 描述）：
 *  - local 型（抄 Claude Code slash command 的 `type:"local"`）：直接调后端 API，
 *    不经过 agent、不污染对话。
 *  - fuzzy 复用 `lib/commands.ts` 的 `fuzzyScore`（票面铁律：不另起一套）。
 *  - `/plan` 的当前 mode 从事件流投影派生（last-wins，与后端
 *    `effective_workflow_mode` 同语义），前端不记布尔值（票面 §4.2 硬性规定）。
 *    projection.ts 对 mode-changed 是 noop（审计事件），折叠逻辑落在本文件。
 *  - workflow-mode / task-definition 的传输收编进 api.ts 的
 *    `postSessionCommand`（审查 B-S-5：鉴权注入与错误映射复用 apiFetch 同一条
 *    链路，不在本文件复刻第二份 Bearer 拼装——两处一漂移，401 语义就分叉）。
 *  - `/compact` 执行反馈对齐 #1197 三态契约（压缩中/被拒/失败）：状态机宿主是
 *    `hooks/useCompactSession`（与 ContextUsagePanel 同一份），本文件只经
 *    `ctx.runCompact` 触发——不在 slash 路径另起一套压缩状态（审查 A-P1-3）。
 *  - `/goal 查看` 走 `ctx.reportInfo`（中性信息面），只有真失败才 reportError
 *    （审查 B-S-4/A-P3-2：查看结果不是错误，不该染红）。
 */
import { fuzzyScore } from './commands';
import { EventType, type AgentEvent } from '../types';
import {
  getGoalState,
  postGoalCommand,
  postSessionCommand,
  SessionError,
  type GoalSnapshot,
  type GoalStatePayload,
  type SlashRegistryCommand as SlashRegistryEntry,
} from './api';

/** #1200 Phase 3：`/` 菜单的唯一数据源（后端 `GET /api/plugins/slash-registry`）。
 *  形状定义在 api.ts（线上类型的唯一出处）；这里 re-export 一次，调用点不必双导入。 */
export type { SlashRegistryCommand as SlashRegistryEntry } from './api';

/** 菜单里显示的命令名：插件 skill 加 `namespace:` 前缀消歧（票面 §4）。 */
export function slashDisplayName(entry: SlashRegistryEntry): string {
  return entry.namespace ? `${entry.namespace}:${entry.name}` : entry.name;
}

export interface SlashCommandContext {
  sessionId: string;
  /** 打开现有 UI（`/permission` `/model` 只负责拉起，不改值——票面 §3.2）。 */
  openPermissionPicker: () => void;
  openModelPicker: () => void;
  /** 错误上报口（Composer 呈现为可见提示，不静默吞——票面 §4.1）。 */
  reportError: (message: string) => void;
  /** 中性信息上报口（查看结果 / 执行回执——不是错误，不染红；审查 B-S-4）。 */
  reportInfo: (message: string) => void;
  /** `/compact` 触发：由 Composer 驱动 `useCompactSession` 三态宿主（#1197 契约）。
   *  状态机（压缩中/被拒/失败/成功对比）归宿主，本文件不持有压缩状态。 */
  runCompact: () => Promise<void>;
  /** mode 投影的数据源（App 已持有 conversation；零新端点——票面 §4.2）。 */
  getSessionEvents: (sessionId: string) => Promise<AgentEvent[]>;
}

/** 内置命令的**执行**回调（元数据在后端 registry；前端只按 name 查表）。 */
export interface SlashCommand {
  name: string;
  run: (args: string | undefined, ctx: SlashCommandContext) => Promise<void>;
}

/** mode 投影：倒序扫 `workflow/mode-changed`，last-wins；坏值跳过继续向前
 *  （后端 `effective_workflow_mode` 的 warning-continue 语义，逐字对齐）；
 *  无事件回落 normal。
 *
 *  开销注记（审查 A-P4）：每次 `/plan` 都全量拉一遍 `GET /events`。这是**刻意**
 *  的：会话事件流是唯一真相（不变量 #22），前端不缓存 mode 副本（缓存就会与
 *  事件流分叉——票面 §4.2 明文禁止），而 /plan 是低频手动动作，一次全量拉取
 *  可接受；若未来成为热点，正确做法是订阅增量投影，不是加本地缓存。 */
export async function currentWorkflowMode(ctx: SlashCommandContext): Promise<'normal' | 'plan'> {
  const events = await ctx.getSessionEvents(ctx.sessionId);
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type !== EventType.WORKFLOW_MODE_CHANGED) continue;
    const mode = e.data.mode;
    if (mode === 'plan' || mode === 'normal') return mode;
  }
  return 'normal';
}

async function runPlan(_args: string | undefined, ctx: SlashCommandContext): Promise<void> {
  const current = await currentWorkflowMode(ctx);
  const next = current === 'plan' ? 'normal' : 'plan';
  await postSessionCommand(ctx.sessionId, '/workflow-mode', { mode: next });
}

/** `/goal` 的子命令文法（抄 dsh `command-goal` 的 `parseGoalCommand`）。
 *
 *  控制词**只有占满整个输入**才识别：`/goal pause after verification` 是
 *  objective（创建/编辑目标），不是 pause——否则一个自然语言目标会被误解析成
 *  生命周期命令。`edit` 单独出现算"缺 objective"的编辑（给可操作错误，不创建
 *  一个叫 edit 的目标）。其余一律当 objective。 */
export type GoalCommand =
  | { kind: 'show' }
  | { kind: 'create'; objective: string }
  | { kind: 'edit'; objective: string }
  | { kind: 'invalid-edit' }
  | { kind: 'pause' }
  | { kind: 'resume' }
  | { kind: 'clear' };

export function parseGoalCommand(rawInput: string | undefined): GoalCommand {
  const input = (rawInput ?? '').trim();
  if (input.length === 0) return { kind: 'show' };
  const control = input.toLowerCase();
  if (control === 'clear') return { kind: 'clear' };
  if (control === 'pause') return { kind: 'pause' };
  if (control === 'resume') return { kind: 'resume' };
  if (control === 'edit') return { kind: 'invalid-edit' };
  if (/^edit(?=\s)/i.test(input)) return { kind: 'edit', objective: input.slice(4).trim() };
  return { kind: 'create', objective: input };
}

/** `/goal` 无参回执：objective/phase/轮数/activation/可用命令（票面 AC1 与 UX）。
 *  纯函数，便于单测；blocked 时附 code + message。 */
export function formatGoalSnapshot(snapshot: GoalSnapshot): string {
  const g = snapshot.goal;
  if (!g.defined) return '当前会话未定义目标。用 /goal <objective> 创建。';
  const lines = [
    `目标：${g.objective ?? ''}`,
    `状态：${g.phase ?? '?'}`,
    `轮数：${g.rounds_started}/${g.max_goal_rounds}`,
    `自动续跑：${snapshot.activation}`,
  ];
  if (g.phase === 'blocked' && g.blocked_reason) {
    lines.push(`阻塞：${g.blocked_reason.code}: ${g.blocked_reason.message}`);
  }
  if (g.needs_interview) {
    lines.push('目标缺少 ## Success criteria 或 ## Verification 段，建议先补全再执行。');
  }
  lines.push(`可用命令：${goalCommandHint(g)}`);
  return lines.join('\n');
}

/** 当前相位下真正有意义的下一批命令（抄 dsh `commandHint`）。 */
export function goalCommandHint(g: GoalStatePayload): string {
  if (g.phase === 'complete') return '/goal <objective>，/goal clear';
  if (g.phase === 'paused' || g.phase === 'blocked') {
    return '/goal edit <objective>，/goal resume，/goal clear';
  }
  return '/goal edit <objective>，/goal pause，/goal clear';
}

/** 409 的可操作文案（AC3：把 revision 冲突翻成"当前是 rN"）。 */
function goalConflictMessage(e: SessionError): string {
  return `${e.message}。可先 /goal 查看当前 revision，再带 expected_revision 重试`;
}

async function runGoal(args: string | undefined, ctx: SlashCommandContext): Promise<void> {
  const command = parseGoalCommand(args);
  try {
    switch (command.kind) {
      case 'show':
        // 查看结果（含未定义目标）是中性信息，不染红（审查 B-S-4/A-P3-2）。
        ctx.reportInfo(formatGoalSnapshot(await getGoalState(ctx.sessionId)));
        return;
      case 'invalid-edit':
        ctx.reportError('用法：/goal edit <objective>（edit 后必须跟目标文本）');
        return;
      case 'create':
        ctx.reportInfo(
          formatGoalSnapshot(
            await postGoalCommand(ctx.sessionId, { operation: 'create', objective: command.objective }),
          ),
        );
        return;
      case 'edit':
        ctx.reportInfo(
          formatGoalSnapshot(
            await postGoalCommand(ctx.sessionId, { operation: 'edit', objective: command.objective }),
          ),
        );
        return;
      case 'pause':
      case 'resume':
      case 'clear':
        ctx.reportInfo(
          formatGoalSnapshot(await postGoalCommand(ctx.sessionId, { operation: command.kind })),
        );
        return;
    }
  } catch (e) {
    if (e instanceof SessionError && e.status === 409) {
      ctx.reportError(goalConflictMessage(e));
      return;
    }
    throw e;
  }
}

async function runCompact(_args: string | undefined, ctx: SlashCommandContext): Promise<void> {
  // 三态宿主在 Composer（useCompactSession）：压缩中 spinner+禁用、被拒就地说明、
  // 失败显示后端原因、成功展示前后对比（#1197 契约；审查 A-P1-3）。
  await ctx.runCompact();
}

/** `/permission` `/model`：只拉起现有 UI（值的选择在 picker 里，本命令不改）。 */
function makeOpen(open: (ctx: SlashCommandContext) => void) {
  return async (_args: string | undefined, ctx: SlashCommandContext): Promise<void> => {
    open(ctx);
  };
}

/** 内置命令的执行表（按 name 查）；顺序无关——展示顺序由后端 registry 决定。
 *  类型是 `Partial`：后端 registry 才是"哪些内置命令存在"的真相，前端表可能落后
 *  （漏一条 = 菜单能点但没回调）⇒ 调用方必须处理 `undefined`，不许静默吞。 */
export const BUILTIN_RUNS: Readonly<Partial<Record<string, SlashCommand['run']>>> = {
  goal: runGoal,
  plan: runPlan,
  compact: runCompact,
  permission: makeOpen((ctx) => ctx.openPermissionPicker()),
  model: makeOpen((ctx) => ctx.openModelPicker()),
};

export interface SlashTriggerMatch {
  query: string;
  /** 匹配段在输入串里的起点（含作为分隔符的前导空白）。菜单 Enter / 点选执行后
   *  用它做**定位切除**：只删命令 token，保留命令前的前缀文本（审查 B-S-3）。 */
  start: number;
}

/** 触发判定（票面正则 `/(^|\s)\/(\w*)$/`）：行首或空格后半角 `/` 开尾。
 *  命中 = 菜单开着、query 为 `/` 后的词片段；`\w` 不含全角 ⇒ `／` 天然不触发。
 *  #1200：`:` 额外进 query（插件 skill 的显示名是 `plugin:skill`）。 */
export function slashTriggerMatch(value: string): SlashTriggerMatch | null {
  // #1200：`:` 也进 query——插件 skill 的显示名是 `plugin:skill`，输入到冒号后
  // 仍要继续 fuzzy 过滤（票面 §4「继续输入 fuzzy 过滤」）。
  const m = /(?:^|\s)\/([\w:-]*)$/.exec(value);
  return m ? { query: m[1], start: m.index } : null;
}

export interface SlashSubmitMatch {
  name: string;
  args: string | undefined;
}

/** 带参提交拦截（票面正则 `^\/([\w:-]+)(?:\s+([\s\S]+))?$`；#1200 补 `:`，
 *  与触发正则同一字符集——否则 `/acme:code-review args` 菜单能插入、提交却不拦截）：
 *  整条输入是一个命令调用且命令名在注册表 ⇒ preventDefault 本地执行，
 *  不走「原发送逻辑」（防止 `/goal <文本>` 被当聊天发给 agent）。
 *  调用方传 **trim 后**的整条输入（审查 A-P2-1：`/goal␣` 尾随空格同样落到
 *  命令路径——trim 后命中纯命令形态，args 为空 ⇒ 走 §4.2「无参数则查看」）。 */
export function slashSubmitMatch(
  value: string,
  knownNames: readonly string[],
): SlashSubmitMatch | null {
  const m = /^\/([\w:-]+)(?:\s+([\s\S]+))?$/.exec(value);
  if (!m || !knownNames.includes(m[1])) return null;
  return { name: m[1], args: m[2] };
}

/** 过滤：haystack = 显示名 + 描述 + 参数提示的可搜面，fuzzyScore 打分降序
 *  （与 `filterCommands` 同款稳定排序；空 query 原序全量）。
 *  #1200：数据源是后端 registry 条目，前端不做任何"哪些命令存在"的判定。 */
export function filterSlashCommands(
  items: readonly SlashRegistryEntry[],
  query: string,
): SlashRegistryEntry[] {
  if (!query.trim()) return [...items];
  return items
    .map((item) => ({
      item,
      score: fuzzyScore(
        query,
        `${slashDisplayName(item)} ${item.description} ${item.argumentHint ?? ''}`,
      ),
    }))
    .filter((x): x is { item: SlashRegistryEntry; score: number } => x.score !== null)
    .sort((a, b) => b.score - a.score)
    .map((x) => x.item);
}
