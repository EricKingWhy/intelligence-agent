/** #1195 — Composer 内联 `/` 命令菜单：注册表与执行层（票面 §4.1/§4.2）。
 *
 * 设计（方案依据，见 PR 描述）：
 *  - local 型（抄 Claude Code slash command 的 `type:"local"`）：直接调后端 API，
 *    不经过 agent、不污染对话。
 *  - fuzzy 复用 `lib/commands.ts` 的 `fuzzyScore`（票面铁律：不另起一套）。
 *  - `/plan` 的当前 mode 从事件流投影派生（last-wins，与后端
 *    `effective_workflow_mode` 同语义），前端不记布尔值（票面 §4.2 硬性规定）。
 *    projection.ts 对 mode-changed 是 noop（审计事件），折叠逻辑落在本文件。
 *  - workflow-mode / task-definition 的 fetch 落在本文件（api.ts 不在 Scope Lock）：
 *    复用已 export 的 `getToken`（鉴权注入语义与 apiFetch 一致）与 `readErrorDetail`
 *    （错误文案与后端 detail 形状解耦）。
 *  - `/compact` `/goal 查看` 复用既有 client（compactSession / getTaskState）。
 */
import { fuzzyScore } from './commands';
import { EventType, type AgentEvent } from '../types';
import {
  compactSession,
  getTaskState,
  readErrorDetail,
  describeSessionError,
  SessionError,
} from './api';
import { getToken } from './auth';

export type SlashCommandGroup = 'add' | 'command';

export interface SlashCommandContext {
  sessionId: string;
  /** 打开现有 UI（`/permission` `/model` 只负责拉起，不改值——票面 §3.2）。 */
  openPermissionPicker: () => void;
  openModelPicker: () => void;
  /** 错误上报口（Composer 呈现为可见提示，不静默吞——票面 §4.1）。 */
  reportError: (message: string) => void;
  /** mode 投影的数据源（App 已持有 conversation；零新端点——票面 §4.2）。 */
  getSessionEvents: (sessionId: string) => Promise<AgentEvent[]>;
}

export interface SlashCommand {
  name: string;
  labelCN: string;
  labelEN: string;
  description: string;
  group: SlashCommandGroup;
  run: (args: string | undefined, ctx: SlashCommandContext) => Promise<void>;
}

/** 会话级 fetch 封装：与 apiFetch 同语义（Bearer 注入 + detail 透出），
 *  但留在本文件——api.ts 不在 Scope Lock，为两个调用改它不值（奥卡姆）。 */
async function sessionFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  return fetch(path, { ...init, headers });
}

async function sessionPostJson(sessionId: string, path: string, body: unknown): Promise<void> {
  const res = await sessionFetch(
    `/api/sessions/${encodeURIComponent(sessionId)}${path}`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
  );
  if (!res.ok) throw new SessionError(res.status, await readErrorDetail(res));
}

/** mode 投影：倒序扫 `workflow/mode-changed`，last-wins；坏值跳过继续向前
 *  （后端 `effective_workflow_mode` 的 warning-continue 语义，逐字对齐）；
 *  无事件回落 normal。 */
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
  await sessionPostJson(ctx.sessionId, '/workflow-mode', { mode: next });
}

async function runGoal(args: string | undefined, ctx: SlashCommandContext): Promise<void> {
  const text = args?.trim();
  if (text) {
    // 带参 = 设置（票面 §4.2：请求体 `{task_text}`；409=已定义、422/404 由 reportError 透出）。
    await sessionPostJson(ctx.sessionId, '/task/definition', { task_text: text });
    return;
  }
  // 无参 = 查看：404（未定义任务）不算错，如实展示「未定义」。
  try {
    const state = await getTaskState(ctx.sessionId);
    ctx.reportError(state.task_text ? `当前目标：${state.task_text}` : '当前会话未定义目标');
  } catch (e) {
    ctx.reportError(describeSessionError(e, '查看目标失败'));
  }
}

async function runCompact(_args: string | undefined, ctx: SlashCommandContext): Promise<void> {
  // 复用既有 client（180s 超时 / 409=在途 run 语义都在里面）。
  await compactSession(ctx.sessionId);
}

/** `/permission` `/model`：只拉起现有 UI（值的选择在 picker 里，本命令不改）。 */
function makeOpen(open: (ctx: SlashCommandContext) => void) {
  return async (_args: string | undefined, ctx: SlashCommandContext): Promise<void> => {
    open(ctx);
  };
}

/** 注册表：顺序 = 效果图分组顺序（添加两条 → 指令三条）。 */
export const SLASH_COMMANDS: readonly SlashCommand[] = [
  {
    name: 'goal',
    labelCN: '目标',
    labelEN: 'goal',
    description: '设置或查看长期任务目标',
    group: 'add',
    run: runGoal,
  },
  {
    name: 'plan',
    labelCN: '计划',
    labelEN: 'plan',
    description: '进入或退出计划模式',
    group: 'add',
    run: runPlan,
  },
  {
    name: 'compact',
    labelCN: '压缩',
    labelEN: 'compact',
    description: '压缩以上对话内容',
    group: 'command',
    run: runCompact,
  },
  {
    name: 'permission',
    labelCN: '权限',
    labelEN: 'permission',
    description: '切换权限预设（沙箱模式与审批策略）',
    group: 'command',
    run: makeOpen((ctx) => ctx.openPermissionPicker()),
  },
  {
    name: 'model',
    labelCN: '模型',
    labelEN: 'model',
    description: '选择本会话使用的模型',
    group: 'command',
    run: makeOpen((ctx) => ctx.openModelPicker()),
  },
];

export interface SlashTriggerMatch {
  query: string;
}

/** 触发判定（票面正则 `/(^|\s)\/(\w*)$/`）：行首或空格后半角 `/` 开尾。
 *  命中 = 菜单开着、query 为 `/` 后的词片段；`\w` 不含全角 ⇒ `／` 天然不触发。 */
export function slashTriggerMatch(value: string): SlashTriggerMatch | null {
  const m = /(?:^|\s)\/(\w*)$/.exec(value);
  return m ? { query: m[1] } : null;
}

export interface SlashSubmitMatch {
  name: string;
  args: string | undefined;
}

/** 带参提交拦截（票面正则 `^\/(\w+)(?:\s+([\s\S]+))?$`）：
 *  整条输入是一个命令调用且命令名在注册表 ⇒ preventDefault 本地执行，
 *  不走「原发送逻辑」（防止 `/goal <文本>` 被当聊天发给 agent）。 */
export function slashSubmitMatch(
  value: string,
  knownNames: readonly string[],
): SlashSubmitMatch | null {
  const m = /^\/(\w+)(?:\s+([\s\S]+))?$/.exec(value);
  if (!m || !knownNames.includes(m[1])) return null;
  return { name: m[1], args: m[2] };
}

/** 过滤：haystack = labelCN + labelEN + description 的可搜面，fuzzyScore 打分
 *  降序（与 `filterCommands` 同款稳定排序；空 query 原序全量）。 */
export function filterSlashCommands(
  items: readonly SlashCommand[],
  query: string,
): SlashCommand[] {
  if (!query.trim()) return [...items];
  return items
    .map((item) => ({
      item,
      score: fuzzyScore(query, `${item.labelCN} ${item.labelEN} ${item.description}`),
    }))
    .filter((x): x is { item: SlashCommand; score: number } => x.score !== null)
    .sort((a, b) => b.score - a.score)
    .map((x) => x.item);
}
