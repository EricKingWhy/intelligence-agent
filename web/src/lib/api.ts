/** REST API client. All fetches go through here — single seam for base URL / auth headers.
 *
 * Auth seam (backend auth_seam fail-closed, df4f7d8): when the user has configured
 * a Bearer token (lib/auth), apiFetch injects `Authorization` on every request —
 * including the SSE POST. 401 responses are broadcast (auth.onUnauthorized) and
 * thrown as UnauthorizedError so callers surface the guidance path.
 */

import type {
  AgentEvent,
  ArtifactSlice,
  ConstraintInputChoice,
  HostDirsListing,
  MemoryDeleted,
  MemoryScope,
  MemorySummary,
  Project,
  ProjectDeleted,
  ProjectStatus,
  SessionDeleted,
  SessionArchived,
  SessionSummary,
} from '../types';
import { emitUnauthorized, getToken } from './auth';
import type { ImageIntakeLimits } from './attachments';
import type { RunLimitField } from './runBudget';
import { parseCapabilities, type CapabilityDescriptor } from './capabilities';

const BASE = ''; // relative — Vite proxy handles /api → :8000

/** Thrown for any 401 (after auth.onUnauthorized has broadcast the detail). */
export class UnauthorizedError extends Error {}

/** 404 = 目标资源不存在（会话已被删除 / 属于另一个后端实例）。
 *
 *  与「加载失败」区分开：调用方据此**清理记住的选中会话**并安静回到空态，
 *  而不是弹一条用户无法处理、每次刷新都会重演的错误横幅（BUG-005 的
 *  陈旧 id 分支）。 */
export class NotFoundError extends Error {}

/** 409 = 审批已决（幂等成功）——OBS-015 修复引入。
 *
 *  后端 `PendingApprovalQueue.resolve()` 对同一 approval_id 的第二次决策返回 409。
 *  这**不是**错误：用户的意图已经生效，卡片应翻到「已批准/已拒绝」。
 *  与网络失败 / 5xx 区分开：那些意味着决策**没有**到达后端，卡片必须保持 pending。 */
export class AlreadyResolvedError extends Error {}

/** 404 = 审批队列里没有这个 approval_id：进程重启或 run 终结后队列已被 GC，
 *  这条审批**永远不可能再被 resolve**（`app.py:1236-1240` 已把它写进契约）。
 *
 *  不复用 NotFoundError：与 deleteSession 的 404 是同一取舍（`api.test.ts:804`）——
 *  「用户想提交的东西本就不在了」与「加载路径拿不到内容」对调用方的含义不同。
 *  也不是可重试错误：重试多少次都是 404。 */
export class ApprovalGoneError extends Error {}

/** 恢复链路 409 的机器可读待裁决清单条目（#596 / 后端 #547；#357 W-13 富化）。
 *
 *  键固定为 `tool_call_id` / `tool_name` / `state`。`state` **通常** ⊆ Ledger 非终态
 *  集合 `{RUNNING, UNKNOWN, NEED_RECONCILE}`（悬空调用只认非终态）；但后端
 *  `session/service.py::_reconcile_pending` 有第二判据——非悬空行按
 *  `storage.needs_reconcile` 全量收录（#315「终态 + 副作用未证」形态），理论上可
 *  携带终态值（当前部署现实不可达）。越出三态时 `parsePendingDecisions` 整组回落
 *  undefined → 调用方展示 `message` 文案，这是任何后端版本下都正确的下限；刻意
 *  **不做 per-entry 过滤**——不制造半真半假的清单。
 *
 *  #357 W-13（修订 A）增三个**只读展示字段**，驱动「默认选中最安全项」（契约 1/2/6）：
 *  - `default_action`：由工具 `replay_safe` 决定——safe → `'RETRY'`（当作没生效，
 *    安全重做）、unsafe → `'DEFER'`（先跳过，稍后再说）。**只有这两值**（#14 安全侧）；
 *  - `risk_level`：safe → `'low'`、unsafe → `'high'`；
 *  - `probe`：逐字取工具既有 `ReconcileHint`（`verifiable` + `suggested_action`），
 *    是**建议**不是结论——前端不据此伪造「已查到/未查到」（那是用户核对后的自陈）。
 *    未知工具 fail-closed：`DEFER`/`high` + `verifiable=false`。
 *  裁决载荷即 `decisions: [{tool_call_id, verdict, source?}]`，verdict ∈
 *  `{CONFIRM_SUCCESS, CONFIRM_FAILURE, RETRY, ABANDON, DEFER}`（POST /recover 请求体）。 */
export interface PendingDecision {
  tool_call_id: string;
  tool_name: string;
  state: 'RUNNING' | 'UNKNOWN' | 'NEED_RECONCILE';
  default_action: 'RETRY' | 'DEFER';
  risk_level: 'low' | 'high';
  probe: { verifiable: boolean; suggested_action: string | null };
}

/** `pending_decisions` 的形状防御：整组合法才返回，任何一条键缺失 / 类型不对 /
 *  state 越出非终态集合 / 富化字段越枚举 ⇒ 整个字段按"没给"处理（undefined）。
 *  调用方拿 undefined 时回落到 `message` 文案展示——那在任何后端版本下都是正确的
 *  下限，不存在半真半假的清单。刻意**不做 per-entry 过滤**（一条坏行不返回其余）。 */
function parsePendingDecisions(raw: unknown): PendingDecision[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out: PendingDecision[] = [];
  for (const item of raw) {
    if (typeof item !== 'object' || item === null) return undefined;
    const { tool_call_id, tool_name, state, default_action, risk_level, probe } =
      item as Record<string, unknown>;
    if (typeof tool_call_id !== 'string' || typeof tool_name !== 'string') return undefined;
    if (state !== 'RUNNING' && state !== 'UNKNOWN' && state !== 'NEED_RECONCILE') return undefined;
    if (default_action !== 'RETRY' && default_action !== 'DEFER') return undefined;
    if (risk_level !== 'low' && risk_level !== 'high') return undefined;
    if (typeof probe !== 'object' || probe === null) return undefined;
    const { verifiable, suggested_action } = probe as Record<string, unknown>;
    if (typeof verifiable !== 'boolean') return undefined;
    // `suggested_action` 只认 string | null；缺省（undefined）与它类值一律整组回落。
    if (suggested_action !== null && typeof suggested_action !== 'string') return undefined;
    out.push({
      tool_call_id, tool_name, state, default_action, risk_level,
      probe: { verifiable, suggested_action },
    });
  }
  return out;
}

/** FastAPI 错误体 `{detail}` 读取：形状不符或 JSON 解析失败返回 ''——
 *  错误处理路径自身不再产生新错误（多处 401/409/4xx 消费共享的单一实现）。
 *
 *  `detail` 有**四种合法形状**，都要认：
 *  - `string`：端点自己 `raise HTTPException(detail=…)` —— 后端的可行动中文原因；
 *  - `Array<{loc, msg, type}>`：Pydantic 请求体校验失败的固定形状（422）。不认它
 *    就会把"path 必须是绝对路径"降级成"注册项目失败（422）"，把最该看懂的一条
 *    提示扔在门外。只取 `msg` 并剥掉 Pydantic 自己的 `Value error, ` 前缀——
 *    用户要看的是规则的结论，不是校验器的转述层。
 *  - `{code, message}`：**带机读判别字段**的错误（#225 `/api/memories` 的 503：
 *    "没配"与"配了但装配失败"必须分流，判别走 `code` 而不是匹配中文——文案会改，
 *    码不会）。`code` 由同一次读取里的 `readErrorBody` 取出（响应体只能读一次），
 *    这里只认 `message` 那个给人看的串。
 *  - `{message, pending_decisions}`：**恢复链路 409 的结构化载荷**（#596 / 后端
 *    #547：RecoveryConflict 带 `pending_decisions` 时 detail 升级为对象）。渲染取
 *    `message`（与字符串分支同一条下游），`pending_decisions` 经形状防御后随
 *    `readErrorBody` 透出（recover / resume 两路挂在错误对象上），避免直接
 *    `String(detail)` 显示成 `[object Object]`。
 */
export async function readErrorDetail(res: Response): Promise<string> {
  return (await readErrorBody(res)).message;
}

/**
 * 一次读取拿到 `{message, code, objectDetail}`（响应体只能读一次，所有消费者必须共用
 * 这次读取）。
 *
 * `objectDetail` = "后端用了 `detail` 是对象的**机读形状**回答"（#227）：用它区分三种
 * 截然不同的情况——纯字符串 detail（旧版后端）、对象 detail 带码、对象 detail **没有**
 * 合法码（新版形状却没给码 ⇒ 是后端 bug，不是旧版）。少了这个信号，调用方只能看
 * "码是不是 null"，于是会把"新版形状却没给码"误读成旧版行为——那正是本批要消灭的
 * "按缺席猜原因"（错误码只按码判：`docs/adr/0035-machine-readable-error-codes-for-503-families.md`）。
 */
async function readErrorBody(
  res: Response,
): Promise<{ message: string; code: string | null; objectDetail: boolean; pendingDecisions?: PendingDecision[] }> {
  try {
    const j = (await res.json()) as { detail?: unknown } | null;
    const detail = j?.detail;
    if (typeof detail === 'string') return { message: detail, code: null, objectDetail: false };
    if (Array.isArray(detail)) {
      const message = detail
        .flatMap((item) => {
          const msg = (item as { msg?: unknown } | null)?.msg;
          return typeof msg === 'string' && msg ? [msg.replace(/^Value error,\s*/, '')] : [];
        })
        .join('；');
      return { message, code: null, objectDetail: false };
    }
    if (typeof detail === 'object' && detail !== null) {
      const { message, code, pending_decisions } = detail as {
        message?: unknown;
        code?: unknown;
        pending_decisions?: unknown;
      };
      return {
        message: typeof message === 'string' ? message : '',
        code: typeof code === 'string' && code ? code : null,
        objectDetail: true,
        pendingDecisions: parsePendingDecisions(pending_decisions),
      };
    }
    return { message: '', code: null, objectDetail: false };
  } catch {
    return { message: '', code: null, objectDetail: false };
  }
}

/** Single request seam: auth header injection + 401 interception. */
async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (res.status === 401) {
    const detail = await readErrorDetail(res);
    emitUnauthorized(detail || 'Missing identity token');
    throw new UnauthorizedError(detail || '需要身份令牌（401）');
  }
  return res;
}

export async function getHealth(): Promise<{ status: string }> {
  const res = await apiFetch('/api/health');
  if (!res.ok) throw new Error(`health ${res.status}`);
  return res.json();
}

/** 列会话摘要。
 *
 *  `includeArchived` 默认 `false`——**与后端默认逐字一致**（#171 AC3：不带参数就不返回
 *  已归档会话）。包装层不偷偷改默认值，否则"调了同一个函数"在不同调用方手里含义不同。
 *
 *  界面侧**显式**要 `true`（`useSession.refreshSessions`），理由与后端的默认并不矛盾，
 *  是分层分工：
 *  - 后端那条默认值服务**别的客户端**（CLI / 脚本 / 未来的集成），它们只要"能用的会话"；
 *  - 本界面需要**完整一份**：归档开关要能立刻切换可见性（不重拉、不闪屏），每行要能画
 *    出「已归档」徽标，而**项目视图尤其不能缺行**——`buildRailModel` 把"账本里有 id、
 *    列表里没有"如实报成「n 条会话日志缺失」（`lib/projects.ts`），只请求默认列表会把
 *    已归档的成员说成"日志丢了"，那是假话。
 *
 *  于是可见性过滤落在**投影层**（`buildRailModel` 的 `includeArchived`）：一份载荷 +
 *  一个开关 = 一处真相（不变量 #22）。 */
export async function listSessions(
  options: { includeArchived?: boolean } = {},
): Promise<SessionSummary[]> {
  const query = options.includeArchived ? '?include_archived=true' : '';
  const res = await apiFetch(`/api/sessions${query}`);
  if (!res.ok) throw new Error(`list sessions ${res.status}`);
  return res.json();
}

export async function getSessionEvents(sessionId: string): Promise<AgentEvent[]> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/events`);
  if (res.status === 404) throw new NotFoundError(`会话不存在（${sessionId}）`);
  if (!res.ok) throw new Error(`get events ${res.status}`);
  return res.json();
}

export interface BudgetPayload {
  /** local 作用域的 turn 保险丝（`11 §6.1` / ADR-0044 D8）。
   *
   *  **缺省不发键**：不传 = 后端按 Deployment/AgentProfile 解析（默认 500）。
   *  前端刻意没有默认值——硬编码一个数字会变成请求侧覆盖：运维把 deployment
   *  ceiling 调低时，它反而让请求 422（#308 AC：产品调用方不再主动发送
   *  `max_steps`）。 */
  local?: { max_agent_turns?: number };
  /** run 作用域预算（`#312`/`#313` 落地后开放；T4 交付前注释声明"类型里也不预留"
   *  的前提已不再成立，遂随 #426 补上——只声明 UI 真正会发送的维，其余维
   *  （model_requests / cost_usd / per-tool）等到有入口再加，别预留。
   *
   *  #422：`budget.run` 与 launch=false **互斥**（422）——只在启动 run 的请求上
   *  发送。 */
  run?: {
    /** 本 run 的 Agent turn 绝对上限（正整数；`11 §6.1`）。到顶 → run/paused
     *  （非终态），PausedPanel 抬高同一维的绝对 ceiling 后同 run 恢复。 */
    max_agent_turns_total?: number;
    /** 本 run 的总 token 绝对上限（正整数；#426 起 UI 有入口，与 turns 同一
     *  「到顶 → 暂停 → 恢复抬高」闭环）。 */
    max_total_tokens?: number;
    /** 本 run 的绝对截止时刻（#315 契约）：RFC 3339 UTC 文本。datetime-local 的
     *  本地读数由映射层 `toCreateBudget` 换算成 UTC 瞬时（朴素时间后端 422）。 */
    deadline_at?: string;
  };
}

export interface StartSessionPayload {
  task: string;
  /** User explicitly declares this input as a reusable procedural rule (#298 R5). */
  remember_as_procedural_rule?: boolean;
  workspace?: string;
  /** 目录根会话（WS-6 / #169，ADR-0027 D2）：会话直接在**这个已存在的绝对路径**
   *  下运行，它同时成为会话的 workspace root（工具的相对路径都相对它解析），
   *  并在后端自动注册为项目 + attach（幂等）。
   *
   *  与 `workspace`（workspaces_root 下的单段名字，ADR-0025 D8 的旧语义）
   *  **互斥**：两个都传 → 422 `workspace 与 cwd 只能二选一`。前端入口一次只用一种，
   *  这条互斥在后端兜底而不是在这里猜（谁先谁后是可观测契约，见 PRD §4.1）。 */
  cwd?: string;
  budget?: BudgetPayload;
  auto_approve?: boolean;
  /** 可选模型选择（T10 #103，契约 C6）：GET /api/models 的 name；不传 = 默认
   *  链；未知 → 422（调用方提示重新选择并刷新目录）。 */
  model?: string;
  /** 会话级控制字段——全部在运行时真实消费（不再是 staged no-op）：
   *  - permission_mode: GET /api/permission-modes 的 id；审批阈值（非硬墙），
   *    不传 = 后端默认。
   *  - agent_profile: GET /api/agent-profiles 的 id；注入 system_prompt +
   *    按 tool_scope 收窄工具集（ADR-0020a），不传 = 后端默认。
   *  - reasoning_effort: GET /api/reasoning-efforts 的 id；经 create_chat_model
   *    注入模型原生字段（reasoning_effort 批次 `79e2860`），不传 = 后端默认。
   *  - context_providers: GET /api/context-providers 的 id 列表；按 provider
   *    name 筛选已装配子集（ADR-0021），不传 = 全部已装配 provider。
   *
   *  API 边界校验：permission_mode / reasoning_effort / agent_profile 为封闭枚举
   *  （未知值 → 422）；context_providers 做形状校验并对照 wiring 真实 id
   *  （未知 → 422）。 */
  permission_mode?: string;
  agent_profile?: string;
  reasoning_effort?: string;
  context_providers?: string[];
  /** #367 / W-23 选项 A：三档自主度（抄 Copilot Interactive/Plan/Autopilot）。
   *  不传 = 后端默认（沿用 auto_approve 既有语义）；显式声明优先于 auto_approve。
   *  - ask：逐次问；plan：先计划后执行；auto：全自动。 */
  autonomy?: 'ask' | 'plan' | 'auto';
  /** #367 / W-23 选项 A：目录冲突策略。worktree（默认）：被占时自动建
   *  worktree；queue：走既有租约排队（次选项）。不传 = 后端默认 worktree。 */
  on_conflict?: 'worktree' | 'queue';
  /** #363 / W-19：显式选择的 sandbox 后端（"local" | "docker"）；不传 = 部署默认。 */
  sandbox_backend?: string;
}

/** createEmptySession 的载荷（#204 裁定 §2）：只建会话、不启动 run。
 *  `task` **刻意不在这个形状里**——launch=false + task 是矛盾组合（给了任务却
 *  静默不执行），后端 422；类型上没有它，编译期就挡住调用方传进来。
 *  `permission_mode` 与 startSession 同词汇（弹窗选的档 = 会话级权限）；省略 =
 *  后端默认 workspace-write + auto-approve。 */
export interface CreateEmptySessionPayload {
  cwd?: string;
  budget?: BudgetPayload;
  auto_approve?: boolean;
  permission_mode?: string;
  workspace?: string;
}

/** #204：launch=false 的创建回执。只留前端**真正消费**的 `sessionId`。
 *
 *  #236：这里曾经带 `permissionMode`（#204 裁定 §3 用它初始化 composer 权限 pill）。
 *  那个消费者已随本票下线——pill 现在读会话自己的投影（`session/started` →
 *  `ConversationState.session_permission_mode`），回执驱动的本地状态正是要消灭的
 *  第二套真相。**没有消费者的字段与它的硬校验一起删掉**：留着只会让"后端哪天不返回
 *  这个键"变成一条无人受益的假报错（响应里的该键仍被忽略，不影响解析）。 */
export interface CreatedEmptySession {
  sessionId: string;
}

/** POST /api/sessions?launch=false —— 只建会话，不启动 run、不返回 SSE（#204）。
 *  响应形状 `{session_id, permission_mode}`；前端只用 `session_id`（见
 *  `CreatedEmptySession`），`permission_mode` 缺席也不再报错。 */
export async function createEmptySession(
  payload: CreateEmptySessionPayload,
): Promise<CreatedEmptySession> {
  const res = await apiFetch('/api/sessions?launch=false', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(
      buildBody(payload, {
        workspace: (p) => (p.workspace ? ['workspace', p.workspace] : null),
        cwd: (p) => (p.cwd ? ['cwd', p.cwd] : null),
        budget: (p) => (p.budget !== undefined ? ['budget', p.budget] : null),
        auto_approve: (p) => (p.auto_approve !== undefined ? ['auto_approve', p.auto_approve] : null),
        permission_mode: (p) => (p.permission_mode ? ['permission_mode', p.permission_mode] : null),
      }),
    ),
  });
  if (!res.ok) {
    const detail = await readErrorDetail(res);
    throw new SessionError(res.status, detail || `create session ${res.status}`);
  }
  const body: unknown = await res.json();
  const r = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>;
  if (typeof r.session_id !== 'string' || !r.session_id) {
    throw new SessionError(res.status, 'create 会话回执缺少 session_id');
  }
  return { sessionId: r.session_id };
}

/** #367 / W-23 选项 A：GET /api/sandbox-backends —— 可用后端 + 探针结果。
 *  不可用带 reason，前端据此置灰（不画饼）。 */
export interface SandboxBackendEntry {
  backend: string;
  available: boolean;
  reason: string | null;
  details: Record<string, string>;
}

export async function getSandboxBackends(): Promise<SandboxBackendEntry[]> {
  const res = await apiFetch('/api/sandbox-backends');
  if (!res.ok) return [];
  const body: unknown = await res.json();
  const r = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>;
  if (!Array.isArray(r.backends)) return [];
  return (r.backends as Record<string, unknown>[])
    .filter((b) => typeof b.backend === 'string')
    .map((b) => ({
      backend: b.backend as string,
      available: b.available === true,
      reason: typeof b.reason === 'string' ? b.reason : null,
      details: (typeof b.details === 'object' && b.details !== null
        ? b.details : {}) as Record<string, string>,
    }));
}

/** #362 / W-18：GET /api/mcp/servers —— 已装配 MCP server + 连接状态。 */
export interface McpServerEntry {
  name: string;
  connected: boolean;
}

export async function getMcpServers(): Promise<{ servers: McpServerEntry[]; errors: string[] }> {
  const res = await apiFetch('/api/mcp/servers');
  if (!res.ok) return { servers: [], errors: [] };
  const body: unknown = await res.json();
  const r = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>;
  const servers = (Array.isArray(r.servers) ? r.servers : []) as Record<string, unknown>[];
  const errors = (Array.isArray(r.errors) ? r.errors : []) as unknown[];
  return {
    servers: servers
      .filter((s) => typeof s.name === 'string')
      .map((s) => ({ name: s.name as string, connected: s.connected === true })),
    errors: errors.filter((e): e is string => typeof e === 'string'),
  };
}

/** #362 / W-18：POST /api/mcp/servers/{name}/disconnect —— 断开指定 server（幂等）。 */
export async function disconnectMcpServer(name: string): Promise<boolean> {
  const res = await apiFetch(`/api/mcp/servers/${encodeURIComponent(name)}/disconnect`, {
    method: 'POST',
  });
  if (!res.ok) return false;
  const body: unknown = await res.json().catch(() => ({}));
  const r = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>;
  return r.disconnected === true;
}

/** #367 / W-23 选项 A：POST /api/worktrees —— 为 git 仓库创建隔离 worktree。
 *  响应 `{worktree_path}`。目录冲突默认走这个（六家共识），"排队等"作次选项。 */
export async function createWorktree(repoPath: string): Promise<string> {
  const res = await apiFetch('/api/worktrees', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo_path: repoPath }),
  });
  if (!res.ok) {
    const detail = await readErrorDetail(res);
    throw new SessionError(res.status, detail || `create worktree ${res.status}`);
  }
  const body: unknown = await res.json();
  const r = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>;
  if (typeof r.worktree_path !== 'string' || !r.worktree_path) {
    throw new SessionError(res.status, 'create worktree 回执缺少 worktree_path');
  }
  return r.worktree_path;
}

/** B1 契约通用清单条目——{id, display_name, description}。
 *  四个清单端点（permission-modes / agent-profiles / reasoning-efforts /
 *  context-providers）共用此结构，与 /api/models 富化模式对齐。 */
export interface CatalogEntry {
  id: string;
  display_name: string;
  description: string;
  /** #214：行首图标的**语义名**（后端在后端条目上声明；取值集见
   *  `lib/catalogIcons.ts::CATALOG_ICON_NAMES`）。
   *  缺键 / `null` / 不认识的名 ⇒ 那一行留空槽——前端不编字形，也**不拿 `id` 去猜**
   *  （部署自定义档位时 id 千变万化，猜出来的字形是编的）。 */
  icon?: string | null;
  /** #201：只有 `/api/agent-profiles` 会带；其余三个清单端点没有这个字段
   *  （缺省 = 后端没说这个档位的工具面 → 前端不渲染提示，不编）。 */
  tool_scope?: ToolScope;
}

/** #201：档位的工具面披露（**只有 `/api/agent-profiles` 会带**）。
 *  `open` = 该档位开放的工具数，`total` = 全部内置档位声明工具面的并集大小，
 *  `excluded` = 并集里不在该档位的工具名（升序）。
 *  ⚠ 这是**声明面**不是运行时注册集（后端 `agent/profiles.py::tool_scope_summary`
 *  写明口径）：本部署少启用一个 capability 时，只有声明里提到、实际没注册的工具
 *  会计进 `open`——所以文案只说"该档位开放 N 个（共 M 个）"，不说"你现在能用 N 个"。 */
export interface ToolScope {
  open: number;
  total: number;
  excluded: string[];
}

/** GET /api/permission-modes —— 权限模式清单。
 *  返回 PermissionPolicy 全集 + 人类可读描述。 */
export async function getPermissionModes(): Promise<CatalogEntry[]> {
  const res = await apiFetch('/api/permission-modes');
  if (!res.ok) throw new Error(`permission-modes ${res.status}`);
  return parseCatalogEntries(await res.json(), 'modes');
}

/** GET /api/agent-profiles —— Agent Profile 清单。
 *  返回 AGENT_PROFILE_DESCRIPTIONS 全集。 */
export async function getAgentProfiles(): Promise<CatalogEntry[]> {
  const res = await apiFetch('/api/agent-profiles');
  if (!res.ok) throw new Error(`agent-profiles ${res.status}`);
  return parseCatalogEntries(await res.json(), 'profiles');
}

/** GET /api/reasoning-efforts —— Reasoning Effort 档位清单。
 *  返回 REASONING_EFFORT_DESCRIPTIONS 全集。 */
export async function getReasoningEfforts(): Promise<CatalogEntry[]> {
  const res = await apiFetch('/api/reasoning-efforts');
  if (!res.ok) throw new Error(`reasoning-efforts ${res.status}`);
  return parseCatalogEntries(await res.json(), 'efforts');
}

/** GET /api/context-providers —— Context Provider 清单。
 *  当前诚实返空数组（runtime 尚未装配任何 provider）。
 *
 *  ⚠ 当前**没有调用方**：`#201` 删掉了多选控件，UI 不再有这个入口。函数保留是因为端点本身
 *  仍在（`fixtures.ts` 的该端点 mock 也为此保留）——`#200` 看板与 `#203` 供应商管理会再评估
 *  是否要选 provider；删掉它就得连契约一起忘掉。要清理请连同这个理由一起改。 */
export async function getContextProviders(): Promise<CatalogEntry[]> {
  const res = await apiFetch('/api/context-providers');
  if (!res.ok) throw new Error(`context-providers ${res.status}`);
  return parseCatalogEntries(await res.json(), 'providers');
}

/** 窄化解析清单端点响应——仅 id/display_name/description 非空字符串的条目入选。
 *  顶层 key 用复数短名（modes/profiles/efforts/providers），调用方传入对应 key。
 *  `tool_scope`（#201）是**可选**字段：形状不完整就整块丢掉（缺省 = 后端没说，
 *  前端据此不渲染提示）——半个 tool_scope（有 open 没 total，或 excluded 混进
 *  非字符串）会被渲染成一句半真的话，比不显示更差。
 *  `icon`（#214）同样是可选字段，非空字符串才认（缺失 / `null` / 非字符串一律当"没说"）。
 *  ⚠ **本函数是白名单投影**：契约新增字段必须在这里登记，否则会被静默丢掉——后端照发、
 *  前端拿不到，是最难查的一类失效（#214 落地时实测撞到过一次：图标配好了却不显示）。 */
function parseCatalogEntries(body: unknown, key: string): CatalogEntry[] {
  const raw =
    typeof body === 'object' && body !== null && Array.isArray((body as Record<string, unknown>)[key])
      ? ((body as Record<string, unknown[]>)[key])
      : [];
  return raw.flatMap((m) => {
    if (typeof m !== 'object' || m === null) return [];
    const r = m as Record<string, unknown>;
    if (typeof r.id !== 'string' || !r.id) return [];
    if (typeof r.display_name !== 'string' || !r.display_name) return [];
    const scope = parseToolScope(r.tool_scope);
    const icon = parseIconName(r.icon);
    return [
      {
        id: r.id,
        display_name: r.display_name,
        description: typeof r.description === 'string' ? r.description : '',
        ...(icon ? { icon } : {}),
        ...(scope ? { tool_scope: scope } : {}),
      },
    ];
  });
}

/** `icon`（#214）的窄化：非空字符串才认；缺失 / `null` / 非字符串 ⇒ undefined
 *  （= 后端没说这个条目该画什么 ⇒ 渲染层留空槽，不编字形）。 */
function parseIconName(raw: unknown): string | undefined {
  return typeof raw === 'string' && raw.length > 0 ? raw : undefined;
}

/** `tool_scope` 的窄化：三个字段全部合法才返回，否则 undefined（整块丢弃）。 */
function parseToolScope(raw: unknown): ToolScope | undefined {
  if (typeof raw !== 'object' || raw === null) return undefined;
  const r = raw as Record<string, unknown>;
  const open = r.open;
  const total = r.total;
  if (!Number.isInteger(open) || !Number.isInteger(total)) return undefined;
  if ((open as number) < 0 || (total as number) < 0) return undefined;
  if (!Array.isArray(r.excluded)) return undefined;
  if (!r.excluded.every((n) => typeof n === 'string')) return undefined;
  return { open: open as number, total: total as number, excluded: r.excluded as string[] };
}

/** 请求体字段表：字段 → [契约键, 值]，返回 null = **不发键**（= 后端默认）。
 *
 *  `Record<keyof T, …>` 是编译期完整性锁：payload 类型新增字段而此处未登记
 *  → tsc 失败。此前的手写白名单会**静默**把新字段丢掉——请求照发、后端拿
 *  不到，是最难查的一类失效。字段判空规则因语义而异（falsy / undefined /
 *  非空数组），故此表只统一「构造」，不强行统一「判空」。
 *
 *  契约键的类型是 `keyof T & string` 而非裸 string：本项目的 payload 键与线上
 *  契约键同名，把这条不变量写进类型——写错键名（如 'max_step'）直接编译失败，
 *  而不是发出一条后端不认的请求。 */
type BodyEntry<T> = [keyof T & string, unknown] | null;

type BodyFields<T> = Record<keyof T, (payload: T) => BodyEntry<T>>;

/** 依字段表构造请求体（null 条目跳过）。键序 = 表内声明序。 */
function buildBody<T extends object>(payload: T, table: BodyFields<T>): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const serialize of Object.values(table) as ((p: T) => BodyEntry<T>)[]) {
    const entry = serialize(payload);
    if (entry) body[entry[0]] = entry[1];
  }
  return body;
}

/** create 路径字段表（与下方 sendMessage 的 amend 四项同词汇、各自登记）。 */
const START_SESSION_FIELDS: BodyFields<StartSessionPayload> = {
  task: (p) => ['task', p.task],
  remember_as_procedural_rule: (p) =>
    p.remember_as_procedural_rule ? ['remember_as_procedural_rule', true] : null,
  workspace: (p) => (p.workspace ? ['workspace', p.workspace] : null),
  cwd: (p) => (p.cwd ? ['cwd', p.cwd] : null),
  budget: (p) => (p.budget !== undefined ? ['budget', p.budget] : null),
  auto_approve: (p) => (p.auto_approve !== undefined ? ['auto_approve', p.auto_approve] : null),
  model: (p) => (p.model ? ['model', p.model] : null),
  permission_mode: (p) => (p.permission_mode ? ['permission_mode', p.permission_mode] : null),
  agent_profile: (p) => (p.agent_profile ? ['agent_profile', p.agent_profile] : null),
  reasoning_effort: (p) => (p.reasoning_effort ? ['reasoning_effort', p.reasoning_effort] : null),
  context_providers: (p) =>
    p.context_providers && p.context_providers.length > 0 ? ['context_providers', p.context_providers] : null,
  autonomy: (p) => (p.autonomy ? ['autonomy', p.autonomy] : null),
  on_conflict: (p) => (p.on_conflict ? ['on_conflict', p.on_conflict] : null),
  sandbox_backend: (p) => (p.sandbox_backend ? ['sandbox_backend', p.sandbox_backend] : null),
};

/** POST a new session. Returns the raw Response — SSE stream is consumed by caller.
 *  401 throws UnauthorizedError (after broadcasting) — fail fast, no empty stream.
 *
 *  「有值才带键」的**单一执行点**（与 sendMessage 同一契约，见 lib/amend.ts）：
 *  空值 / 空数组不发键 = 后端默认；调用方只做字段名映射，不判空。 */
export async function startSession(payload: StartSessionPayload): Promise<Response> {
  return apiFetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(buildBody(payload, START_SESSION_FIELDS)),
  });
}

/** #367 P3：从创建响应读 worktree 路径。后端 launch=true 走 SSE（无 JSON 体），
 *  worktree 信息走 `X-Worktree-Path` 响应头（`X-Permission-Mode` 同型先例）；
 *  launch=false 时走 JSON 体 `worktree_path`（由调用方直接读）。无头/空头 → null。
 *  #765：路径含非 latin-1 字符（中文用户名/中文目录）时后端改发
 *  `X-Worktree-Path-Encoded`（RFC 5987 ext-value `UTF-8''<percent-encoded>`，
 *  HTTP 头字段值只能 latin-1），此处按该约定兜底解码；畸形值按缺席处理——
 *  这是展示性提示，不让它炸掉创建流程。 */
export function worktreePathFromResponse(res: Response): string | null {
  const v = res.headers.get('X-Worktree-Path');
  if (v && v.length > 0) return v;
  const encoded = res.headers.get('X-Worktree-Path-Encoded');
  if (!encoded || !encoded.startsWith("UTF-8''")) return null;
  try {
    const decoded = decodeURIComponent(encoded.slice("UTF-8''".length));
    return decoded.length > 0 ? decoded : null;
  } catch {
    return null;
  }
}

/** create 会话失败时后端给的可行动原因（`{detail}` 的两种合法形状，见 readErrorDetail）。
 *
 *  单独开这个缝的原因：「在此项目中新建任务」确认面（#169 AC12）要把后端 detail
 *  **原样**留在浮层里，而 useSession 的失败通道是给用户看的一句话——它还带着
 *  "422 = 未知模型"的旧语义（App 据那句话刷新模型目录）。两者混在一起会让
 *  「cwd 目录不存在」被显示成「模型不可用」。由调用方自己读，两条语义各自成立。 */
export async function startSessionErrorDetail(res: Response): Promise<string> {
  return readErrorDetail(res);
}

/** POST /api/sessions/{id}/messages（PRD §5.3 续聊入口）。
 *  后端两种响应：
 *    - launched → SSE 流（与 POST /api/sessions 同形），返回原始 Response 供 consumeSSE 消费；
 *    - queued / steered → JSON 确认（Content-Type: application/json），调用方需检查
 *      res.headers.get('content-type') 区分。
 *  空闲会话（无在途 run）→ launched 直驱新 run，返回 SSE。
 *  在途会话 → queued 入队（JSON），消息在下个 run 自然消费。 */
export interface ProtectedFactAnnotationPayload {
  fact_type:
    | 'user_instruction'
    | 'user_goal'
    | 'constraint'
    | 'authorization'
    | 'acceptance_criterion'
    | 'exact_identifier'
    | 'confirmed_decision'
    | 'task_progress';
  value: string;
  supersedes_fact_id?: string;
}

export interface SendMessagePayload {
  content: string;
  mode?: 'queue' | 'steer';
  /** User explicitly declares this input as a reusable procedural rule (#298 R5). */
  remember_as_procedural_rule?: boolean;
  budget?: BudgetPayload;
  /** Explicit, source-bound protected facts; backend validates values against content. */
  protected_facts?: ProtectedFactAnnotationPayload[];
  /** Explicit fact/event links for user revocations and vetoes. */
  revoke_fact_id?: string;
  refutes_event_id?: string;
  /** 编辑语义（ADR-0030 §4.4，后端 #196 起接受；默认缺省不发键 = 现有行为不变）：
   *  - supersedes_seq：取代 seq 为它的那条 user/message **及其整轮**（只影响
   *    模型可见投影与界面，历史事件照旧保留）。目标必须是最新一条非注入用户
   *    消息，否则 409 `SupersedeTargetInvalid`。与 mode 正交。
   *  - queue_id：本条内容**替换**某条排队项（旧项被取消，新内容按 mode 投递）。 */
  supersedes_seq?: number;
  queue_id?: string;
  /** 续聊 amend 字段（后端 Q2 批次起 /messages 接受，与 create 路径同词汇）：
   *  - model: GET /api/models 的 name；
   *  - agent_profile: GET /api/agent-profiles 的 id；
   *  - reasoning_effort: GET /api/reasoning-efforts 的 id；
   *  - context_providers: GET /api/context-providers 的 id 列表。
   *
   *  生效范围：仅「空闲会话 → launched 新 run」时应用；在途 run 的 queued
   *  消息忽略它们（runtime 已固定，不抢断不改写）。空值不发键 = 后端默认，
   *  与 POST /api/sessions 的 create 分支同一模式。
   *
   *  ⚠ 已知 Gap：`context_providers: []` 同样不发键，但契约把 `[]` 定为「显式
   *  空集」（交接 §3.1；后端区分 None/[]）。前端选择器无法表达「零个」，
   *  改语义需先定契约。 */
  model?: string;
  agent_profile?: string;
  reasoning_effort?: string;
  context_providers?: string[];
  /** #825（MM-04）：本条消息携带的**附件引用 id**（引自 `POST …/attachments` 的回执
   *  `attachment_id`，顺序即附图顺序）。缺省 / 空数组 = 纯文本，既有行为逐字不变
   *  （「有值才带键」由 `SEND_MESSAGE_FIELDS` 单点执行）。
   *
   *  契约（后端 `web/app.py::SendMessageRequest`，基线 df2b2435 已在 main）：
   *  `attachments: list[str]`，`max_length = _MAX_ATTACHMENTS_PER_MESSAGE`，
   *  逐条校验"存在且属本会话"，不合法 → 422。 */
  attachments?: string[];
}

/** 续聊路径字段表——amend 四项与 START_SESSION_FIELDS 同词汇；`mode` 有后端默认值，
 *  故缺省在此补齐（其余键缺省即不发键，与 create 路径同款）。 */
const SEND_MESSAGE_FIELDS: BodyFields<SendMessagePayload> = {
  content: (p) => ['content', p.content],
  mode: (p) => ['mode', p.mode ?? 'queue'],
  remember_as_procedural_rule: (p) =>
    p.remember_as_procedural_rule ? ['remember_as_procedural_rule', true] : null,
  protected_facts: (p) =>
    p.protected_facts && p.protected_facts.length > 0
      ? ['protected_facts', p.protected_facts]
      : null,
  revoke_fact_id: (p) => (p.revoke_fact_id ? ['revoke_fact_id', p.revoke_fact_id] : null),
  refutes_event_id: (p) => (p.refutes_event_id ? ['refutes_event_id', p.refutes_event_id] : null),
  // budget（#308）：与 create 路径同款「有值才带键」，**没有**前端默认值。
  // 旧的 `max_steps: p.max_steps ?? 10` 正是产品侧低位默认的来源之一，随本票移除。
  budget: (p) => (p.budget !== undefined ? ['budget', p.budget] : null),
  // 编辑语义（ADR-0030）：与 amend 四项同款「有值才带键」，缺省不发键 = 后端
  // 默认 None = 现有行为逐字不变。
  supersedes_seq: (p) =>
    typeof p.supersedes_seq === 'number' && Number.isFinite(p.supersedes_seq)
      ? ['supersedes_seq', p.supersedes_seq]
      : null,
  queue_id: (p) => (p.queue_id ? ['queue_id', p.queue_id] : null),
  model: (p) => (p.model ? ['model', p.model] : null),
  agent_profile: (p) => (p.agent_profile ? ['agent_profile', p.agent_profile] : null),
  reasoning_effort: (p) => (p.reasoning_effort ? ['reasoning_effort', p.reasoning_effort] : null),
  context_providers: (p) =>
    p.context_providers && p.context_providers.length > 0 ? ['context_providers', p.context_providers] : null,
  // #825（MM-04）：与其余 amend 字段同款「有值才带键」。空数组**不发键**——后端
  // 默认即空列表，发一个显式 `[]` 只会让请求体多一个无意义字段（与
  // `context_providers` 同一纪律）。
  attachments: (p) =>
    p.attachments && p.attachments.length > 0 ? ['attachments', p.attachments] : null,
};

export async function sendMessage(sessionId: string, payload: SendMessagePayload): Promise<Response> {
  return apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // 「有值才带键」的单一执行点（与 startSession 同一契约，见 lib/amend.ts）：
    // 调用方只做字段名映射，空值 / 空数组的丢弃只在这里发生。
    body: JSON.stringify(buildBody(payload, SEND_MESSAGE_FIELDS)),
  });
}

/** GET /api/sessions/{id}/stream?after_seq=N（T4 #97，契约回执 §3）：重放 durable
 *  事件（after_seq < seq ≤ 游标，按 seq 序）后接续在途流——后端先订阅后取游标，
 *  无缝无重复；重放帧与 live 帧同形状（不含 event_id），前端一套 reducer 两条
 *  通道。返回原始 Response 由 consumeSSE 消费；404 = 会话不存在（终止重连）。 */
export async function streamSession(sessionId: string, afterSeq: number): Promise<Response> {
  return apiFetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/stream?after_seq=${Number.isFinite(afterSeq) ? afterSeq : -1}`,
  );
}

/** POST /api/sessions/{id}/approve — interactive approval decision (#37, PRD §2.2).
 *  Backend resolves the pending approval via PendingApprovalQueue.resolve().
 *  Response (200): {"status":"resolved","approval_id":"...","decision":"approve_once"}
 *  409 (`ApprovalAlreadyResolved`) = 幂等已决 → AlreadyResolvedError → 调用方翻卡片。
 *  ⚠ 404 **不是**幂等已决：后端 404 有四个来源（session 不存在 / 审批队列缺失 /
 *    `approval_id` 不在队列 / 事件过期；`web/app.py:1157-1166`），**无法**与
 *    「已解析且已出队」区分。把它当成功 = 决策其实没生效却显示「已批准」的
 *    安全假象，正是 OBS-015 要消灭的那类 bug。故 404 与其它非 2xx 同级 →
 *    plain Error → 卡片保持 pending + 可重试。
 *    真已决的兜底不靠错误码：`permission/resolved` 投影事件会把卡片移出待决队列。
 *  Other non-ok = real failure (decision did NOT reach backend) → plain Error.
 *  OBS-015 fix: the caller must distinguish these two — flipping the card to
 *  "decided" on a network error is a dangerous false positive for security.
 *
 *  #684：`decision` / `policyGranularity` 可选。省略 `decision` 时按 `approved`
 *  推导（True→approve_once，False→deny）——旧调用点逐字不变。第三档「以后都允许」
 *  传 `decision='approve_policy'` + `policyGranularity='exact'|'command'`；
 *  只有后端 requested 事件的 `allowed_decisions` 含该项时才该发（F21 显式授权）。 */
export async function postApproval(
  sessionId: string,
  approvalId: string,
  approved: boolean,
  decision?: string,
  policyGranularity?: 'exact' | 'command',
): Promise<{ status: string; approval_id: string; decision: string }> {
  const body: Record<string, unknown> = {
    approval_id: approvalId,
    approved,
    decision: decision ?? (approved ? 'approve_once' : 'deny'),
  };
  // 只有显式给了粒度才带键：普通批准/拒绝不带 policy_granularity（后端忽略它，
  // 但少一个键 = 请求体更接近旧契约，给中转/日志少一个可误读的字段）。
  if (policyGranularity !== undefined) {
    body.policy_granularity = policyGranularity;
  }
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (res.status === 409) throw new AlreadyResolvedError('审批已决（幂等）');
  if (res.status === 404) throw new ApprovalGoneError('该审批已失效（运行已中断或服务已重启）');
  if (!res.ok) throw new Error(`审批失败（${res.status}）`);
  return res.json();
}

// ── Models（后端契约回执 §3，T10 #103：多模型不写死，grill Q2 拍板）──

/** GET /api/models 目录条目。零密钥字段；name 是 POST /api/sessions 的选择键；
 *  思考能力不进元数据（显示侧由 reasoning 事件族驱动，有则显示无则不显示）。
 *
 *  #199 加法：`isAvailable` / `unavailableReason` / 三个能力位。#203 已把
 *  `is_available` 做成**真实判定**并给出 `unavailable_reason` 机器码
 *  （`provider_store.py:237-244`：`credential_unavailable` / `missing_api_key`）；
 *  能力位只在后端**声明过**时才出现（未声明的键后端不返回，前端记 null = 不猜）。
 *
 *  ⚠ `is_available` 的真实性**有边界**：只有**自定义供应商**条目是按凭据真判
 *  （`app.py:1044-1093`）；内置 preset / catalog 条目恒 `true`。所以"不可用"这一态在
 *  默认部署（没配任何自定义供应商）里根本不会出现——UI 实现了它，不等于默认部署能看到。
 */
export interface ModelReasoningEffortCapability {
  supported: string[];
  default: string;
  wireMapping: Record<string, string>;
}

const REASONING_EFFORT_WIRE_VALUES = new Set([
  'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max',
]);

function parseModelReasoningEffort(value: unknown): ModelReasoningEffortCapability | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return undefined;
  const raw = value as Record<string, unknown>;
  if (
    !Array.isArray(raw.supported) ||
    raw.supported.length === 0 ||
    raw.supported.some((level) => typeof level !== 'string' || level.length === 0) ||
    new Set(raw.supported).size !== raw.supported.length ||
    typeof raw.default !== 'string' ||
    !raw.supported.includes(raw.default) ||
    typeof raw.wire_mapping !== 'object' ||
    raw.wire_mapping === null ||
    Array.isArray(raw.wire_mapping)
  ) {
    return undefined;
  }

  const wireMapping = raw.wire_mapping as Record<string, unknown>;
  if (
    Object.keys(wireMapping).length !== raw.supported.length ||
    raw.supported.some((level) =>
      typeof wireMapping[level] !== 'string' ||
      !REASONING_EFFORT_WIRE_VALUES.has(wireMapping[level] as string)
    )
  ) {
    return undefined;
  }

  return {
    supported: raw.supported as string[],
    default: raw.default,
    wireMapping: wireMapping as Record<string, string>,
  };
}

export interface ModelCatalogEntry {
  name: string;
  provider: string | null;
  model: string | null;
  default: boolean;
  /** 已配置语义（有凭据 ⇒ true），不是网络可达（ADR-0032 D5）。
   *
   *  ⚠ 类型上**可选**且是**三态**（true / false / 缺失=后端没说），判定必须用
   *  `modelAvailability.ts::isUnavailable`（= `=== false`）：把"没说"当成"不可用"
   *  会把整份目录渲染成灰色。`getModels` 解析后**键恒存在**、值可能是 `undefined`
   *  （后端没给）——这正是三态要区分的那一态，别用 `!== false` 把它压成 `true`。 */
  isAvailable?: boolean;
  /** 机器码（`credential_unavailable` / `missing_api_key`）；无 ⇒ null。
   *  前端只在行尾给一句短文案（映射见 `lib/modelAvailability.ts`）。 */
  unavailableReason?: string | null;
  /** 能力位：true / false / 缺失（= 后端没声明，**不猜**）。 */
  supportsTools?: boolean | null;
  supportsVision?: boolean | null;
  supportsReasoningSummary?: boolean | null;
  /** Explicit backend declaration; absent or malformed means no slider for this model. */
  reasoningEffort?: ModelReasoningEffortCapability;
}

/** GET /api/models。窄化解析（零伪造）：仅 name 非空字符串的条目入选，
 *  可选字段缺失记 null；models 数组缺失/形状不符 → 空数组——调用方据此
 *  降级隐藏选择器入口，绝不伪造列表。 */
export async function getModels(): Promise<ModelCatalogEntry[]> {
  const res = await apiFetch('/api/models');
  if (!res.ok) throw new Error(`models ${res.status}`);
  const body: unknown = await res.json();
  const raw =
    typeof body === 'object' && body !== null && Array.isArray((body as { models?: unknown }).models)
      ? ((body as { models: unknown[] }).models)
      : [];
  return raw.flatMap((m) => {
    if (typeof m !== 'object' || m === null) return [];
    const r = m as Record<string, unknown>;
    if (typeof r.name !== 'string' || !r.name) return [];
    return [
      {
        name: r.name,
        provider: typeof r.provider === 'string' ? r.provider : null,
        model: typeof r.model === 'string' ? r.model : null,
        default: r.default === true,
        // 三态，与 `supports_*` 同一条纪律：true / false / **缺失=undefined**。
        // `!== false` 会把"后端没说"和"后端说 true"压成同一个值——那是伪造。
        // 判定侧唯一入口是 `modelAvailability.ts::isUnavailable`（= `=== false`），
        // 所以 undefined 落进"可用"那一支的行为不变，但类型不再说谎。
        isAvailable: typeof r.is_available === 'boolean' ? r.is_available : undefined,
        unavailableReason:
          typeof r.unavailable_reason === 'string' && r.unavailable_reason
            ? r.unavailable_reason
            : null,
        // 能力位三态：true / false / null（未声明）。**不**用 `=== true` 归一成
        // 布尔——那会把"后端说 false"与"后端没说"压成同一个值，徽标就无从判断
        // 该不该渲染（这正是 `supports_*` 与 `is_available` 语义不同的地方）。
        supportsTools: typeof r.supports_tools === 'boolean' ? r.supports_tools : null,
        supportsVision: typeof r.supports_vision === 'boolean' ? r.supports_vision : null,
        supportsReasoningSummary:
          typeof r.supports_reasoning_summary === 'boolean'
            ? r.supports_reasoning_summary
            : null,
        reasoningEffort: parseModelReasoningEffort(r.reasoning_effort),
      },
    ];
  });
}

// ── 自定义模型供应商（#203 / ADR-0032）──

/** GET /api/model-providers 条目。零密钥字段：`has_api_key` 是唯一状态通道
 *  （不回显任何 key 值/片段）；`kind` = builtin|custom|override（override = 同 id
 *  覆盖内置 preset）。`last_test` = 上次「测试连接」的结果（非密）。 */
export interface ModelProviderEntry {
  id: string;
  label: string;
  base_url: string;
  models: { model_id: string; label?: string }[];
  kind: 'custom' | 'override';
  has_api_key: boolean;
  is_available: boolean;
  unavailable_reason: string | null;
  last_test?: { ok: boolean; at: string | null; reason?: string; detail?: string } | null;
}

/** 后端 4xx/5xx → 可读错误（detail 就是后端那句话，不自己编文案）。 */
export class ProviderError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function providerFetch(path: string, init?: RequestInit): Promise<unknown> {
  const res = await apiFetch(path, init);
  if (!res.ok) {
    // 复用 `readErrorDetail`（同文件上面的单一实现），**不再自己读一遍 body**：
    // 原先这里手写的版本只认 `detail` 是**字符串**，于是把 Pydantic 422 的
    // `Array<{loc,msg}>` 形状整个丢掉 ——「ID 必须是 slug」被降级成
    // `model-providers 422`（真机证据见 `docs/LIVE_BROWSER_TEST_20260917.md` §9.2）。
    // 本文件第 53 行的注释早就把三种形状写全了，provider 这条路当时没接上去。
    const detail = await readErrorDetail(res);
    throw new ProviderError(res.status, detail || `model-providers ${res.status}`);
  }
  return res.json();
}

/** GET /api/model-providers。providers 数组缺失/形状不符 → 空数组（零伪造）。 */
export async function getModelProviders(): Promise<ModelProviderEntry[]> {
  const body = await providerFetch('/api/model-providers');
  const raw =
    typeof body === 'object' && body !== null && Array.isArray((body as { providers?: unknown }).providers)
      ? (body as { providers: unknown[] }).providers
      : [];
  return raw.flatMap((p) => {
    if (typeof p !== 'object' || p === null) return [];
    const r = p as Record<string, unknown>;
    if (typeof r.id !== 'string' || !r.id) return [];
    if (typeof r.base_url !== 'string') return [];
    const models = Array.isArray(r.models)
      ? r.models.flatMap((m) => {
          if (typeof m !== 'object' || m === null) return [];
          const mr = m as Record<string, unknown>;
          if (typeof mr.model_id !== 'string' || !mr.model_id) return [];
          return [{
            model_id: mr.model_id,
            label: typeof mr.label === 'string' && mr.label ? mr.label : undefined,
          }];
        })
      : [];
    const lastTest =
      typeof r.last_test === 'object' && r.last_test !== null
        ? (r.last_test as { ok?: unknown; at?: unknown; reason?: unknown; detail?: unknown })
        : null;
    return [{
      id: r.id,
      label: typeof r.label === 'string' ? r.label : '',
      base_url: r.base_url,
      models,
      kind: r.kind === 'override' ? 'override' as const : 'custom' as const,
      has_api_key: r.has_api_key === true,
      is_available: r.is_available === true,
      unavailable_reason: typeof r.unavailable_reason === 'string' ? r.unavailable_reason : null,
      last_test: lastTest
        ? {
            ok: lastTest.ok === true,
            at: typeof lastTest.at === 'string' ? lastTest.at : null,
            reason: typeof lastTest.reason === 'string' ? lastTest.reason : undefined,
            detail: typeof lastTest.detail === 'string' ? lastTest.detail : undefined,
          }
        : null,
    }];
  });
}

/** 创建/更新 payload。`api_key` 省略 = 不改密钥；空串 = 显式清除（ADR-0032 §7.1）。 */
export interface ProviderUpsert {
  id: string;
  label?: string;
  base_url: string;
  models: { model_id: string; label?: string }[];
  api_key?: string;
}

/** POST /api/model-providers（id 冲突 = 覆盖更新；与内置同名 = 覆盖内置）。 */
export async function createModelProvider(payload: ProviderUpsert): Promise<void> {
  await providerFetch('/api/model-providers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/** PUT /api/model-providers/{id}。api_key 省略 = 不改；空串 = 清除。 */
export async function updateModelProvider(id: string, patch: Omit<ProviderUpsert, 'id'>): Promise<void> {
  await providerFetch(`/api/model-providers/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

/** DELETE /api/model-providers/{id}。404 = 不存在；500 = 凭据删除失败（配置保留）。 */
export async function deleteModelProvider(id: string): Promise<void> {
  await providerFetch(`/api/model-providers/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/** 连接测试结果（失败归类 reason + 截断摘要 detail；零密钥）。 */
export interface ProviderTestResult {
  ok: boolean;
  message: string;
  reason?: string;
  detail?: string;
  duration_ms: number;
}

/** POST /api/model-providers/{id}/test — 走真实构造路径的最小 chat completion。 */
export async function testModelProvider(id: string): Promise<ProviderTestResult> {
  const body = await providerFetch(`/api/model-providers/${encodeURIComponent(id)}/test`, {
    method: 'POST',
  });
  const r = body as Record<string, unknown>;
  return {
    ok: r.ok === true,
    message: typeof r.message === 'string' ? r.message : (r.ok === true ? '连接正常' : '测试失败'),
    reason: typeof r.reason === 'string' ? r.reason : undefined,
    detail: typeof r.detail === 'string' ? r.detail : undefined,
    duration_ms: typeof r.duration_ms === 'number' ? r.duration_ms : 0,
  };
}

// ── 能力 manifest（#182 / PRD §3.2）──

/** GET /api/capabilities（SDD 03 §17）。
 *
 *  契约已存在但前端此前**零消费**（`src/agent_harness/web/app.py::list_capabilities`
 *  投影，条目形状定义在 `src/agent_harness/capability/manifest.py`）：
 *  `{"capabilities":[{"id":…,"surfaces":{chat,timeline,changes,terminal,artifacts},…}]}`。
 *  返回的列表**恒含一条 `core`**（内置工具集的声明，#193）——由后端保证，前端不补。
 *
 *  失败 / 端点缺席（老后端 404）时**抛错**，由调用方降级为 PRD 缺省语义——这里不
 *  静默返回缺省值：那样调用方就分不清"能力都没声明"与"压根没拿到数据"，而 PRD 要求
 *  两种情况落同一份缺省、**且 Chat 永不消失**（降级是消费方的策略，见 `App.tsx`）。 */
export async function getCapabilities(): Promise<CapabilityDescriptor[]> {
  const res = await apiFetch('/api/capabilities');
  if (!res.ok) throw new Error(`capabilities ${res.status}`);
  return parseCapabilities(await res.json());
}

// ── Cancel（后端契约回执 §3，T5 #98：detached-run 显式中断唯一入口）──

/** POST /api/sessions/{id}/cancel。200 {"status":"cancelling"|"no_active_run"}
 *  ——两者都是幂等成功（Esc 与 run 恰好刚终结的竞态是常态，契约明示不是错误）；
 *  404 = 会话不存在。非 2xx 抛 Error——调用方 cancelStream 静默降级，
 *  流终态/错误路径是 UI 收尾权威。run/failed(data.reason='cancelled') 随后
 *  经流广播（订阅者照常收到终态帧）。 */
export async function cancelSession(sessionId: string): Promise<{ status: string }> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) throw new Error(`cancel ${res.status}`);
  return res.json();
}

// ── 在途输入队列（ADR-0030 §4.6 / §5.2，后端 #196）──

/** `GET /api/sessions/{id}/queue` 的条目形状。
 *  数据源是**事件流**（不是内存队列）：只有它跨崩溃存活、也只有它同时看得见
 *  queue 与 steer 的到达顺序。前端用它做首屏/重连补齐，实时增量仍由 SSE 事件
 *  流驱动（不变量 #22：事件流是唯一事实，本端点只做补齐）。 */
export interface QueueItem {
  queue_id: string;
  content: string;
  created_at: string;
}

export interface SteerItem {
  steer_id: string;
  content: string;
  created_at: string;
}

export interface SessionQueue {
  items: QueueItem[];
  steers: SteerItem[];
}

/** GET /api/sessions/{id}/queue —— 待发送输入（ADR-0030 D11）。
 *  404 = 会话不存在。非 2xx 抛 Error（调用方静默降级：首屏补齐失败不影响
 *  实时增量通道）。 */
export async function listSessionQueue(sessionId: string): Promise<SessionQueue> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/queue`);
  if (res.status === 404) throw new NotFoundError('会话不存在');
  if (!res.ok) throw new Error(`queue ${res.status}`);
  return res.json();
}

/** POST /api/sessions/{id}/queue/flush —— 立刻投递队首的待发送输入（ADR-0030 §4.6）。
 *  空队列 → `{"status":"idle"}`；有 → 与 `/messages` 的 launched 分支同形的 SSE 流
 *  （返回原始 Response 供 consumeSSE 消费）。
 *  409 = 在途 run（轮询重试是调用方的职责：flush 的 idle 回执与在途状态不是
 *  幂等回执——投递会开新 run）。 */
export async function flushSessionQueue(sessionId: string): Promise<Response> {
  return apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/queue/flush`, {
    method: 'POST',
  });
}

/** POST /api/sessions/{id}/queue/{queue_id}/cancel —— 取消尚未消费的排队项。
 *  200 `{"status":"cancelled"}`；404 = 已取消/已消费/不存在 → NotFoundError
 *  （幂等失败语义，调用方静默）；其余非 2xx 抛 Error（网络/服务端失败必须
 *  上浮——静默会让用户以为已取消、请求根本没到服务器）。 */
export async function cancelQueueItem(sessionId: string, queueId: string): Promise<void> {
  const res = await apiFetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/queue/${encodeURIComponent(queueId)}/cancel`,
    { method: 'POST' },
  );
  if (res.status === 404) throw new NotFoundError('排队项已取消或已消费');
  if (!res.ok) throw new Error(`queue cancel ${res.status}`);
}

// ── Artifact 内容读取（#185 路由 / #186 消费）──

/** artifact 内容为什么拿不到——**三态分开**，因为对用户是三句不同的话。
 *
 *  - `gone`（404）：这个 id 不在本会话的命名空间里（不存在，或属于别的会话）。
 *    后端刻意不区分这两者——`artifact_id` 是内容哈希、跨会话可重复，区分"不存在"与
 *    "存在但不可读"会把归属变成可探测的信息（`web/app.py` 的 404 注释）。
 *  - `no-storage`（503 + `code=artifact_storage_unavailable`）：本部署**确实没有可读的
 *    存储**。不能降级成"不存在"——那会让用户以为产物丢了，而其实是这个部署没配存储。
 *  - `error`：其它失败（5xx / 网络 / 形状不符）。`detail` 是后端原文。
 *
 *  `detail` 一律是后端 `{detail}` 原文（读不到时为空串）——界面照原样显示，
 *  这比前端替它翻译一句更短的错误更有用（同 `describeSessionError` 的既有口径）。 */
export type ArtifactContentFailure = 'gone' | 'no-storage' | 'error';

/** 后端"本部署没有可读 artifact 存储"的机读码（#227，与
 *  `src/agent_harness/web/artifacts.py` 的 `ARTIFACT_STORAGE_UNAVAILABLE` 同值）。
 *  **两侧同值的唯一事实源是跨仓契约**（`docs/BACKEND_CONTRACT_STREAMING_UI.md` §3）；
 *  改名会让全门禁仍然全绿而界面把故障渲染错类——#225 的教训，故此处与 e2e 一并钉住。 */
const ARTIFACT_STORAGE_UNAVAILABLE = 'artifact_storage_unavailable';

export class ArtifactContentError extends Error {
  readonly kind: ArtifactContentFailure;
  readonly detail: string;
  readonly status: number;
  /** 后端给的机读码（无码 = null，旧版后端 / 非码化响应）。判别只读它（#227）。 */
  readonly code: string | null;
  constructor(
    kind: ArtifactContentFailure,
    detail: string,
    status: number,
    code: string | null = null,
  ) {
    super(detail || `artifact content ${status}`);
    this.kind = kind;
    this.detail = detail;
    this.status = status;
    this.code = code;
  }
}

/** 后端 422：`session_id` / `artifact_id` 形态非法，或 `start_line < 1`。
 *  属于客户端 bug（不是冲突），单独成一个 kind 便于测试与诊断。 */
export class ArtifactQueryError extends ArtifactContentError {
  constructor(detail: string, status: number) {
    super('error', detail, status);
  }
}

export interface ArtifactContentQuery {
  startLine?: number;
  endLine?: number;
  keyword?: string;
  maxLines?: number;
}

/** 只解析**后端真的会发的字段**，缺字段就抛错而不是填默认值——形状不符时编一个
 *  空的切片出来，界面会显示"内容为空"，那是在替后端撒谎（同 `parseCapabilities`
 *  的口径：解析失败必须让调用方看得见）。 */
function parseArtifactSlice(raw: unknown): ArtifactSlice {
  const o = (raw ?? {}) as Record<string, unknown>;
  const artifactId = typeof o.artifact_id === 'string' ? o.artifact_id : '';
  const lines = Array.isArray(o.lines) ? o.lines : null;
  /* `total_lines` / `returned_lines` 也**必须在场**：后端 `ArtifactSlice` 是必填模型字段
     （`storage/artifact.py`），缺失只可能是形状不符。此前静默填 0 → 界面渲染
     "共 0 行 / 没有可显示的内容"——一个编出来的"空产物"，比报错更坏（同本函数头的
     口径：不替后端撒谎）。 */
  const totalLines = typeof o.total_lines === 'number' ? o.total_lines : null;
  const returnedLines = typeof o.returned_lines === 'number' ? o.returned_lines : null;
  if (!artifactId || lines === null || totalLines === null || returnedLines === null) {
    throw new Error('artifact content: 响应形状不符');
  }
  return {
    artifact_id: artifactId,
    lines: lines.flatMap((item) => {
      const l = (item ?? {}) as Record<string, unknown>;
      if (typeof l.line_number !== 'number' || typeof l.text !== 'string') return [];
      return [
        {
          line_number: l.line_number,
          text: l.text,
          ...(l.truncated === true ? { truncated: true as const } : {}),
          ...(typeof l.full_length === 'number' ? { full_length: l.full_length } : {}),
        },
      ];
    }),
    total_lines: totalLines,
    returned_lines: returnedLines,
    truncated: o.truncated === true,
  };
}

/** GET /api/sessions/{sid}/artifacts/{aid} —— 外置产物的局部内容（#185）。
 *
 *  `session_id` 走 URL 而不是查询串：`artifact_id` 是内容哈希、跨会话可重复，
 *  归属**只能**由 session 决定（后端据此构造 store 命名空间）。 */
export async function getArtifactContent(
  sessionId: string,
  artifactId: string,
  query: ArtifactContentQuery = {},
): Promise<ArtifactSlice> {
  const params = new URLSearchParams();
  if (query.startLine !== undefined) params.set('start_line', String(query.startLine));
  if (query.endLine !== undefined) params.set('end_line', String(query.endLine));
  if (query.keyword) params.set('keyword', query.keyword);
  if (query.maxLines !== undefined) params.set('max_lines', String(query.maxLines));
  const qs = params.toString();
  const res = await apiFetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(artifactId)}` +
      (qs ? `?${qs}` : ''),
  );
  if (!res.ok) {
    const { message: detail, code, objectDetail } = await readErrorBody(res);
    if (res.status === 404) throw new ArtifactContentError('gone', detail, 404, code);
    if (res.status === 503) {
      // #227：**只按码判**。三种情形分开，别让"这一版的 503 只有一个原因"变成隐藏前提：
      //  - 码 = 无读存储 ⇒ no-storage（今天的唯一原因）；
      //  - **纯字符串 detail**（旧版后端，那时这个端点的 503 也只有这一个原因）
      //    ⇒ 同样的 no-storage；
      //  - 其余（**别的码**，或对象形状却没给合法码）⇒ 通用失败态。猜成 no-storage 会
      //    重演 #225：后端加了第二个 503 原因，界面却说是"部署没配存储"，用户照着
      //    重启/改配置。第二种尤其要小心——"码缺席"不等于"旧版后端"。
      const kind: ArtifactContentFailure =
        code === ARTIFACT_STORAGE_UNAVAILABLE || (code === null && !objectDetail)
          ? 'no-storage'
          : 'error';
      throw new ArtifactContentError(kind, detail, 503, code);
    }
    if (res.status === 422) throw new ArtifactQueryError(detail, 422);
    throw new ArtifactContentError('error', detail, res.status, code);
  }
  return parseArtifactSlice(await res.json());
}

// ── 附件上传 / 受控读取（#825 / MM-04）──
//
// 契约（`src/agent_harness/web/attachments.py`，基线 df2b2435 已在 main）：
//   POST /api/sessions/{id}/attachments?name=<file>   body = 原始字节，Content-Type: image/*
//   GET  /api/sessions/{id}/attachments/{aid}/content 原始字节 + 真 Content-Type
// 读端点的授权闸门（#823 / MM-02）：id 必须被**本会话某条 `user/message`** 引用，
// 否则 404 —— 所以**发送前**的草稿缩略图不能走这个端点（那时还没有事件引用它），
// 只能本地预览；发送后（事件已落地）才由它渲染。这不是可以简化掉的一步。

/** #937 / M-08：GET /api/attachments/limits —— 附件图片上限的**服务端权威下发**。
 *
 *  后端 `app.py::get_attachment_limits` 按 wire 惯例输出 snake_case（同附件域
 *  `attachment_id`），这里做 snake→camel 映射成前端的 `ImageIntakeLimits`
 *  （`allowed_media_types` 对应前端的 `mediaTypes`；模式与 `parseAttachmentReceipt`
 *  一致：线 snake_case、内 camelCase）。与 `getSandboxBackends`
 *  的宽容解析不同，这里**非 ok / 形状非法一律抛 Error**、不吞成默认值：「拿不到就用
 *  旧值」是调用方（`attachments.ts::loadImageLimits` → 返回调用前旧值）
 *  的决策，fetch 层无权替它做。形状防御沿用本文件惯例（`parseAttachmentReceipt` 等）：
 *  逐字段窄化，可疑数据宁可失败也不伪造。
 */
export async function getAttachmentLimits(): Promise<ImageIntakeLimits> {
  const res = await apiFetch('/api/attachments/limits');
  if (!res.ok) throw new Error(`GET /api/attachments/limits → ${res.status}`);
  const body: unknown = await res.json();
  if (typeof body !== 'object' || body === null) {
    throw new Error('GET /api/attachments/limits：响应体不是对象');
  }
  const r = body as Record<string, unknown>;
  const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0;
  if (!num(r.max_image_bytes) || !num(r.max_images_per_message) || !num(r.max_message_image_bytes)) {
    throw new Error('GET /api/attachments/limits：数值字段缺失或非法');
  }
  if (
    !Array.isArray(r.allowed_media_types) ||
    !r.allowed_media_types.every((t): t is string => typeof t === 'string')
  ) {
    throw new Error('GET /api/attachments/limits：allowed_media_types 不是字符串数组');
  }
  return {
    maxImageBytes: r.max_image_bytes,
    maxImagesPerMessage: r.max_images_per_message,
    maxMessageImageBytes: r.max_message_image_bytes,
    mediaTypes: r.allowed_media_types,
  };
}

/** `POST …/attachments` 的回执（后端 `AttachmentUploadResponse`）。 */
export interface AttachmentUploadReceipt {
  attachment_id: string;
  media_type: string;
  bytes: number;
  width: number;
  height: number;
  name?: string | null;
}

/** 一次上传的句柄：`promise` 给结局，`abort` 给"移除草稿时停掉在途上传"。 */
export interface AttachmentUpload {
  promise: Promise<AttachmentUploadReceipt>;
  abort: () => void;
}

/** 上传回执的窄化解析：形状不符即抛（不伪造 id——那会让发送带上一个不存在的引用）。 */
function parseAttachmentReceipt(raw: unknown): AttachmentUploadReceipt {
  if (typeof raw !== 'object' || raw === null) throw new Error('附件上传回执不是对象');
  const r = raw as Record<string, unknown>;
  if (typeof r.attachment_id !== 'string' || !r.attachment_id) {
    throw new Error('附件上传回执缺少 attachment_id');
  }
  if (typeof r.media_type !== 'string' || !r.media_type) {
    throw new Error('附件上传回执缺少 media_type');
  }
  const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  return {
    attachment_id: r.attachment_id,
    media_type: r.media_type,
    bytes: num(r.bytes),
    width: num(r.width),
    height: num(r.height),
    name: typeof r.name === 'string' ? r.name : null,
  };
}

/** XHR → Response：只为复用 `readErrorDetail` 那一处错误体读取（它只认 `Response`）。 */
function responseFromXhr(xhr: XMLHttpRequest): Response {
  return new Response(xhr.responseText ?? '', {
    status: xhr.status,
    headers: { 'content-type': xhr.getResponseHeader('content-type') ?? '' },
  });
}

/** 上传一张图片的字节。
 *
 *  用 `XMLHttpRequest` 而不是 `fetch`：AC4 要**上传进度**，而 `fetch` 读不到发送方向的
 *  进度（`ReadableStream` 上传在 Chromium 上仍未落地）。取 `xhr.upload.onprogress` 的
 *  `loaded/total` 直接转述，不自己估算百分比。
 *
 *  `Content-Type` 如实转述浏览器对文件的判定（不猜）：后端按字节判定并与声明比对，
 *  不符即拒（#822 AC）——客户端在这里"帮忙修正"只会把认知偏差藏起来。 */
export function uploadAttachment(
  sessionId: string,
  file: File,
  onProgress?: (loaded: number, total: number) => void,
): AttachmentUpload {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<AttachmentUploadReceipt>((resolve, reject) => {
    const query = file.name ? `?name=${encodeURIComponent(file.name)}` : '';
    xhr.open('POST', `/api/sessions/${encodeURIComponent(sessionId)}/attachments${query}`);
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
    const token = getToken();
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    if (onProgress && xhr.upload) {
      xhr.upload.onprogress = (event) => {
        if (event.total > 0) onProgress(event.loaded, event.total);
      };
    }
    xhr.onload = () => {
      if (xhr.status === 401) {
        void readErrorDetail(responseFromXhr(xhr)).then((detail) => {
          const message = detail || 'Missing identity token';
          emitUnauthorized(message);
          reject(new UnauthorizedError(message));
        });
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(parseAttachmentReceipt(JSON.parse(xhr.responseText)));
        } catch (e) {
          reject(e instanceof Error ? e : new Error('附件上传回执解析失败'));
        }
        return;
      }
      void readErrorDetail(responseFromXhr(xhr)).then((detail) =>
        reject(new Error(detail || `附件上传失败（${xhr.status}）`)),
      );
    };
    xhr.onerror = () => reject(new Error('附件上传失败：网络错误'));
    xhr.onabort = () => reject(new Error('附件上传已取消'));
    xhr.send(file);
  });
  return { promise, abort: () => xhr.abort() };
}

/** 受控读回地址（同源 `/api/...` ⇒ 现有 CSP `img-src 'self' data:` 直接放行）。
 *  装配层不在这里改语义：调用方决定是直连（无 token 部署）还是带 auth 头取字节。 */
export function attachmentContentUrl(sessionId: string, attachmentId: string): string {
  return (
    `/api/sessions/${encodeURIComponent(sessionId)}/attachments/` +
    `${encodeURIComponent(attachmentId)}/content`
  );
}

/** 带 auth 头取原始字节：配了 Bearer token 的部署里 `<img src>` 带不上头（后端
 *  auth_seam fail-closed ⇒ 401），这一条路给出可渲染的字节。无 token 时调用方
 *  应直接用 `attachmentContentUrl`（少一次全量拷贝）。 */
export async function getAttachmentBytes(sessionId: string, attachmentId: string): Promise<Blob> {
  const res = await apiFetch(attachmentContentUrl(sessionId, attachmentId));
  if (!res.ok) throw new Error((await readErrorDetail(res)) || `图片读取失败（${res.status}）`);
  return res.blob();
}

// ── Session hard delete（#172 / ADR-0029：用户显式硬删，不可恢复）──

/** 会话删除失败：状态码 + 后端 detail 原文。
 *
 *  与 `ProjectError` / `MemoryError` 同构但**分开**（同 describeMemoryError 的理由：
 *  能力各自演进，共用一个会让一侧的语义渗到另一侧）。这里的 `status` 是调用方真的
 *  会分支的字段——404（这个会话已经不存在了）与 409（后端拒绝删）导向**不同**的
 *  界面收敛，见 `deleteSession` 的注释。
 *
 *  刻意**不**复用 `NotFoundError`：那个是"加载路径拿不到内容"的信号（历史装载据此
 *  清掉记住的会话并安静回空态），而删除的 404 是"用户想删的东西本就不在了"——
 *  两者对界面的含义不同，且这里必须带上状态码。 */
export class SessionError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** 硬删会话（DELETE /api/sessions/{id}，ADR-0029）——**不可恢复**：无墓碑、无回收站。
 *  调用方必须先拿到用户的显式二次确认：ADR-0029 把"误删不可逆"的全部风险明确压在
 *  **入口层**，运行时不提供任何技术兜底。
 *
 *  状态码语义（后端 `web/app.py::delete_session` 的契约，前端不合并它们）：
 *    200 `{id, deleted:true, events, detached_from_projects}` —— 真删了；
 *    404 —— 这个会话不存在（**第二次删除就是这个**：后端刻意不伪装成"又删了一次"，
 *           所以调用方要按"列表已过期"收敛，而不是当作一次成功的删除）；
 *    409 —— 三种原因状态码相同、**只能靠 `detail` 区分**：有在途 run / 有挂起审批 /
 *           是 fork 父会话（detail 里带**真实**子会话数量）。因此这里原样保留 detail、
 *           绝不自己编文案——编了就会把"有 2 个 fork 子会话"说成一个泛泛的失败；
 *    422 —— id 形态非法（正常路径不会触发：id 来自行数据，不是用户输入）；
 *    403 —— 非本机 Origin（宿主侧不可逆操作只接受本机来源，ADR-0025 D1）。
 *
 *  跨会话的副作用后端已自洽：被删会话若被**委派**子会话指着，那条父链接会被清掉
 *  （家谱图不会出现 `(parent missing)`），前端不需要为此做任何兼容。 */
export async function deleteSession(sessionId: string): Promise<SessionDeleted> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw await sessionError(res, '删除会话失败');
  // 形状防御：非对象（null / 数组 / 字符串）一律当"没给"处理，别让读字段抛
  // TypeError——那时调用方拿到的是"读属性失败"，而不是"删掉了但回执为空"。
  // `deleted` 不从 body 读：走到 200 就是真删了（见 SessionDeleted 类型注释），
  // 后端 schema 也是 `Literal[True]`——这里没有可窄化的第二种取值。
  const raw: unknown = await res.json().catch(() => null);
  const body = (typeof raw === 'object' && raw !== null ? raw : {}) as Partial<SessionDeleted>;
  return {
    id: typeof body.id === 'string' ? body.id : sessionId,
    deleted: true,
    // 事件数缺失时给 0（与 deleteProject 的 sessions_detached 同一纪律）：这两个
    // 计数只用于回执文案，编不出真值时宁可少说，也不去猜一个像样的数字——回执那边
    // 把 events 的 0 当"没给数"处理（见 lib/sessionDelete.ts）。
    events: typeof body.events === 'number' ? body.events : 0,
    detached_from_projects:
      typeof body.detached_from_projects === 'number' ? body.detached_from_projects : 0,
  };
}

/** 非 2xx → SessionError（detail 优先，缺失时用兜底前缀 + 状态码）。 */
async function sessionError(res: Response, fallback: string): Promise<SessionError> {
  const detail = await readErrorDetail(res);
  return new SessionError(res.status, detail || `${fallback}（${res.status}）`);
}

/** 归档一个会话（#171）：把它从默认列表里收起来，**可逆**，不删任何东西。
 *
 *  与 `deleteSession`（硬删）刻意是两个函数：归档只写 `session_meta.archived` 一个标记
 *  ——事件日志、项目账本、checkpoint 全部原样保留，所以界面**不做二次确认**（可逆的动作
 *  压确认面只会让人麻木；硬删才需要 ADR-0026 那套确认）。
 *
 *  错误矩阵（后端 `web/app.py::archive_session`）：
 *    404 —— 没有这个会话（别处已删/从未存在）；
 *    409 —— 有在途 run（`detail` 就是给用户看的原因，原样上抛，不自己编）；
 *    422 / 403 —— id 形态非法 / 非本机来源（正常路径不会触发）。
 *  走到 200 就是归档态成立，回执的 `archived` 是**动作后**的真值。 */
export async function archiveSession(sessionId: string): Promise<SessionArchived> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/archive`, {
    method: 'POST',
  });
  if (!res.ok) throw await sessionError(res, '归档会话失败');
  return readArchiveReceipt(res, sessionId, true);
}

/** 取消归档（#171）：把会话放回默认列表。与 `archiveSession` 对称，**没有 409**
 *  ——把行放回列表不破坏任何人的前提，在途 run 也无所谓。 */
export async function unarchiveSession(sessionId: string): Promise<SessionArchived> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/archive`, {
    method: 'DELETE',
  });
  if (!res.ok) throw await sessionError(res, '取消归档失败');
  return readArchiveReceipt(res, sessionId, false);
}

/** 归档回执的形状防御（与 deleteSession 同一纪律）：非 2xx 已在上游抛掉，这里只防
 *  "200 但形状不对"。`archived` 是**布尔语义**——缺失时**不**用请求侧的意图去填
 *  （那会把"后端没确认"伪装成"确认了"）：直接抛，让调用方看见契约被破坏。
 *
 *  `requestedArchived` **只进诊断串、不参与判定**：名字刻意不叫 `expected`——回执
 *  里的 `archived` 是权威（后端在幂等重放/竞态下可能与请求意图不同，以它为准），
 *  所以这里**不校验**两者相等，只把它写进异常里帮人定位是哪一次调用出的问题。 */
async function readArchiveReceipt(
  res: Response,
  sessionId: string,
  requestedArchived: boolean,
): Promise<SessionArchived> {
  const raw: unknown = await res.json().catch(() => null);
  const body = (typeof raw === 'object' && raw !== null ? raw : {}) as Partial<SessionArchived>;
  if (typeof body.archived !== 'boolean') {
    throw new Error(
      `归档回执缺少 archived 布尔（请求意图 ${String(requestedArchived)}，会话 ${sessionId}）`,
    );
  }
  return { id: typeof body.id === 'string' ? body.id : sessionId, archived: body.archived };
}

/** 会话删除错误 → 展示文案：SessionError 的 message 就是后端 detail（或兜底前缀），
 *  其余异常（网络层 TypeError 等）用 message 或 fallback。与 `describeProjectError`
 *  / `describeMemoryError` 同构但分开——三处各自演进。 */
export function describeSessionError(error: unknown, fallback: string): string {
  if (error instanceof SessionError) return error.message || fallback;
  const message = (error as Error | null)?.message;
  return message || fallback;
}

// ── 证据保留 / 空间占用 / 清理（#368 W-24）──

/** 会话存储占用（GET /api/sessions/{id}/usage）——**只读投影**，不缓存。
 *  `reclaimable_bytes` 是"当前可回收"的估算（不可回收项不在此列，见 blocked）。 */
export interface SessionUsage {
  events_bytes: number;
  artifacts_bytes: number;
  progress_bytes: number;
  artifact_count: number;
  reclaimable_bytes: number;
  computed_at: string;
}

/** 一条可清理原件：`referenced_by` 是仍引用它的事件 / 证据 id（会话内多引用者）。 */
export interface CleanupAffectedItem {
  artifact_ref: string;
  size: number;
  referenced_by: string[];
}

/** 一条被挡下的原件：`reason` 是后端枚举（preview.blocked：active_task /
 *  unreconciled_operation / referenced / fork_child_reference / evidence；
 *  execute.not_deleted 另含 not_found / invalid），前端只做中文解释，不改判据。 */
export interface CleanupBlockedItem {
  artifact_ref: string;
  reason: string;
}

/** 清理预览（POST …/cleanup/preview）：`snapshot_token` 必须原样回传给 execute。 */
export interface CleanupPreview {
  snapshot_token: string;
  affected: CleanupAffectedItem[];
  evidence_invalidated: string[];
  reclaimable_bytes: number;
  blocked: CleanupBlockedItem[];
}

/** 清理执行回执（POST …/cleanup/execute）：三个字段**分开展示**，不合并成一句"完成"。
 *  `deleted` = 真删了；`failed` = 后端尝试删但失败；`not_deleted` = 执行时被挡下
 *  （与预览的 blocked 同枚举）。任何非空都必须在界面上明示，绝不谎报"全部清理"。 */
export interface CleanupResult {
  deleted: string[];
  failed: string[];
  not_deleted: CleanupBlockedItem[];
}

/** 形状防御的三个小工具（与 deleteSession 同一纪律）：类型不符即回落安全默认值，
 *  绝不让读字段抛 TypeError 把"读回执失败"伪装成"操作失败"。 */
function asNum(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0;
}
function asStr(v: unknown): string {
  return typeof v === 'string' ? v : '';
}
function asStrArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];
}
function asAffectedItem(raw: unknown): CleanupAffectedItem {
  const o = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  return { artifact_ref: asStr(o.artifact_ref), size: asNum(o.size), referenced_by: asStrArray(o.referenced_by) };
}
function asBlockedItem(raw: unknown): CleanupBlockedItem {
  const o = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  return { artifact_ref: asStr(o.artifact_ref), reason: asStr(o.reason) };
}

/** GET /api/sessions/{id}/usage —— 会话存储占用（只读）。
 *
 *  形状防御照 deleteSession：非 2xx 抛 SessionError（detail 原样，缺失用中文兜底 +
 *  状态码）；200 但字段缺失 / 类型不对 ⇒ 给安全默认值，不让调用方拿到 TypeError。 */
export async function getSessionUsage(sessionId: string): Promise<SessionUsage> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/usage`);
  if (!res.ok) throw await sessionError(res, '读取存储占用失败');
  const raw: unknown = await res.json().catch(() => null);
  const body = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  return {
    events_bytes: asNum(body.events_bytes),
    artifacts_bytes: asNum(body.artifacts_bytes),
    progress_bytes: asNum(body.progress_bytes),
    artifact_count: asNum(body.artifact_count),
    reclaimable_bytes: asNum(body.reclaimable_bytes),
    computed_at: asStr(body.computed_at),
  };
}

/** POST /api/sessions/{id}/cleanup/preview —— 取一份可清理预览（**不改任何东西**）。
 *  `mode` 默认 `"unreferenced"`（后端唯一模式）；返回的 `snapshot_token` 是执行时的
 *  并发闸门：预览与执行之间若有新事件改变可达集，execute 会以 409 拒绝（快照过期）。
 *
 *  `selectedRefs`（#368 P3-3 UX 方案 a）：勾选变化后重取时传入当前勾选集合，后端按
 *  「勾选 ∩ 可清理」生成 token，使「取消勾选 → 拿新 token 执行」不再 409。省略则按
 *  可清理全集生成（旧行为）。 */
export async function previewCleanup(
  sessionId: string,
  mode = 'unreferenced',
  selectedRefs?: string[],
): Promise<CleanupPreview> {
  const payload: { mode: string; selected_refs?: string[] } = { mode };
  if (selectedRefs !== undefined) payload.selected_refs = selectedRefs;
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/cleanup/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw await sessionError(res, '清理预览失败');
  const raw: unknown = await res.json().catch(() => null);
  const body = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  return {
    snapshot_token: asStr(body.snapshot_token),
    affected: Array.isArray(body.affected) ? body.affected.map(asAffectedItem) : [],
    evidence_invalidated: asStrArray(body.evidence_invalidated),
    reclaimable_bytes: asNum(body.reclaimable_bytes),
    blocked: Array.isArray(body.blocked) ? body.blocked.map(asBlockedItem) : [],
  };
}

/** POST /api/sessions/{id}/cleanup/execute —— 删除选中的原件（**不可恢复**）。
 *
 *  `snapshotToken` 必须是同一份预览返回的 token；过期 → 409，跨源 → 403，两者都由
 *  sessionError 带状态码上抛，调用方据此分支（不吞、不猜）。 */
export async function executeCleanup(
  sessionId: string,
  snapshotToken: string,
  artifactRefs: string[],
): Promise<CleanupResult> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/cleanup/execute`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ snapshot_token: snapshotToken, artifact_refs: artifactRefs }),
  });
  if (!res.ok) throw await sessionError(res, '清理原件失败');
  const raw: unknown = await res.json().catch(() => null);
  const body = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  return {
    deleted: asStrArray(body.deleted),
    failed: asStrArray(body.failed),
    not_deleted: Array.isArray(body.not_deleted) ? body.not_deleted.map(asBlockedItem) : [],
  };
}

// ── Recover（后端新端点，df4f7d8 §1.1）──

/** 恢复失败的可区分错误：status 404 = 会话不存在；409 = 存在需人工裁决的高风险
 *  操作（detail 说明原因，本期只展示，不做裁决交互——不变量 #14 不盲跑）。
 *  #596：后端 detail 为结构化对象时，`message` 只承载给人看的那句话，
 *  `pending_decisions` 经形状防御后挂在 `pendingDecisions`（缺省/畸形 = undefined）。 */
export class RecoverError extends Error {
  readonly status: number;
  readonly pendingDecisions?: PendingDecision[];
  constructor(status: number, message: string, pendingDecisions?: PendingDecision[]) {
    super(message);
    this.status = status;
    this.pendingDecisions = pendingDecisions;
  }
}

/** 用户对一条待裁决 Operation 的裁决值（#547 四值 + #357 W-13 修订 A §9.4-3 的
 *  `DEFER` = 先跳过、稍后再说；Ledger 保持 `NEED_RECONCILE` 可稍后重裁）。
 *  字面量与后端 `recovery/reconcile.py::ReconcileVerdict` 逐一对应。 */
export type ReconcileVerdict =
  | 'CONFIRM_SUCCESS'
  | 'CONFIRM_FAILURE'
  | 'RETRY'
  | 'ABANDON'
  | 'DEFER';

/** 提交给 POST /recover 的单条裁决（wire 形状 `{tool_call_id, verdict, source?}`）。
 *  `source`（#357 W-13 契约 3）是用户来源自陈（如「我查了外部系统」）——可选，
 *  有值才逐字进后端 `reconcile_meta`，**绝不伪造自动验证字段**。 */
export interface RecoverDecisionInput {
  tool_call_id: string;
  verdict: ReconcileVerdict;
  source?: string;
}

/** `source` 上限（**字符**），与后端 `RecoverDecisionRequest.source` 的
 *  `max_length=2000` 同口径。超限在**前端**抛错而不是截断——来源是审计留痕，
 *  静默截断等于伪造（§9.6 诚实原则）。 */
export const RECOVER_DECISION_SOURCE_MAX = 2000;

/** 组装 POST /recover 的请求体：无 decisions（或空数组）⇒ `null`（旧行为：不发
 *  body，纯恢复尝试）；有 decisions ⇒ `{decisions:[...]}`。`source` 超限在此抛错
 *  （在发请求之前，被拒请求零副作用）。 */
function buildRecoverBody(decisions?: RecoverDecisionInput[]): string | null {
  if (!decisions || decisions.length === 0) return null;
  for (const decision of decisions) {
    if (decision.source !== undefined && decision.source.length > RECOVER_DECISION_SOURCE_MAX) {
      throw new Error(
        `裁决来源过长（${decision.source.length} 字符，上限 ${RECOVER_DECISION_SOURCE_MAX}）`,
      );
    }
  }
  return JSON.stringify({
    decisions: decisions.map((d) => ({
      tool_call_id: d.tool_call_id,
      verdict: d.verdict,
      source: d.source,
    })),
  });
}

/** POST /api/sessions/{id}/recover — 幂等。200 返回该 session 全量事件数组
 *  （与 GET events 同构），调用方直接走既有 projectHistory 重建管线（不变量 #22：
 *  不引入第二套会话真相）。
 *
 *  #357 W-13（契约 4）：`decisions` 非空时发送 `{decisions:[{tool_call_id,verdict,
 *  source?}]}`——用户显式裁决覆盖全部待裁决行后结清；无 `decisions` 保持旧行为
 *  （空体）。409 载荷（`pending_decisions`）经 `RecoverError` 透出，供 UI 呈现
 *  与再提交。 */
export async function recoverSession(
  sessionId: string,
  decisions?: RecoverDecisionInput[],
): Promise<AgentEvent[]> {
  const body = buildRecoverBody(decisions);
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/recover`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    ...(body === null ? {} : { body }),
  });
  if (res.status === 404) throw new RecoverError(404, '会话不存在');
  if (res.status === 409) {
    const errBody = await readErrorBody(res);
    throw new RecoverError(
      409,
      errBody.message || '存在需要人工裁决的高风险操作',
      errBody.pendingDecisions,
    );
  }
  if (!res.ok) throw new RecoverError(res.status, `恢复失败（${res.status}）`);
  return res.json();
}

// ── 恢复列表（#357 W-13 契约 5，只读端点）──

/** GET /api/recovery/interrupted 的一行（后端 `interrupted_recovery_rows` 形状）。
 *
 *  逐字段按后端原样承载，**不改名、不补默认**。`session_cwd` 在上游 wire 上叫
 *  `workspace_root`（进度文件与 session_cwd 同一锚）；进度文件版本嵌在 `progress`
 *  里（两种互斥形状见 `InterruptedProgressVersion`）。 */
export interface InterruptedRunSummary {
  run_id: string | null;
  interrupted_seq: number;
  step_id: number | null;
  agent_id: string | null;
}

/** 进度文件版本（`progress.py::read_progress_file_version` 的两种互斥形状）：
 *  可读 ⇒ `{schema_version, source_event_seq}`；缺失/不可读/头非法 ⇒ `{status,
 *  reason}`。**如实标注，不伪造"最新"**。 */
export type InterruptedProgressVersion =
  | { schema_version: string; source_event_seq: number }
  | { status: 'missing' | 'unreadable' | 'invalid_schema'; reason: string };

export interface InterruptedRecoveryItem {
  session_id: string;
  recovery: string;
  detail: string | null;
  interrupted_runs: InterruptedRunSummary[];
  resume_available: boolean;
  task: string | null;
  workspace_root: string | null;
  progress: InterruptedProgressVersion;
}

export interface InterruptedRecoveries {
  snapshot_available: boolean;
  items: InterruptedRecoveryItem[];
}

function isStringOrNull(v: unknown): v is string | null {
  return v === null || typeof v === 'string';
}

function isNumberOrNull(v: unknown): v is number | null {
  return v === null || typeof v === 'number';
}

function parseInterruptedProgress(raw: unknown): InterruptedProgressVersion | undefined {
  if (typeof raw !== 'object' || raw === null) return undefined;
  const p = raw as Record<string, unknown>;
  if (typeof p.schema_version === 'string' && typeof p.source_event_seq === 'number') {
    return { schema_version: p.schema_version, source_event_seq: p.source_event_seq };
  }
  if (
    (p.status === 'missing' || p.status === 'unreadable' || p.status === 'invalid_schema') &&
    typeof p.reason === 'string'
  ) {
    return { status: p.status, reason: p.reason };
  }
  return undefined;
}

/** 一行恢复列表的形状防御：任一字段类型不符 ⇒ 该行不合法（返回 undefined）。 */
function parseInterruptedRecoveryItem(raw: unknown): InterruptedRecoveryItem | undefined {
  if (typeof raw !== 'object' || raw === null) return undefined;
  const r = raw as Record<string, unknown>;
  if (typeof r.session_id !== 'string' || !r.session_id) return undefined;
  if (typeof r.recovery !== 'string' || !r.recovery) return undefined;
  if (!isStringOrNull(r.detail) || !isStringOrNull(r.task) || !isStringOrNull(r.workspace_root)) {
    return undefined;
  }
  if (typeof r.resume_available !== 'boolean') return undefined;
  if (!Array.isArray(r.interrupted_runs)) return undefined;
  const runs: InterruptedRunSummary[] = [];
  for (const run of r.interrupted_runs) {
    if (typeof run !== 'object' || run === null) return undefined;
    const rn = run as Record<string, unknown>;
    if (!isStringOrNull(rn.run_id)) return undefined;
    if (typeof rn.interrupted_seq !== 'number') return undefined;
    if (!isNumberOrNull(rn.step_id) || !isStringOrNull(rn.agent_id)) return undefined;
    runs.push({
      run_id: rn.run_id,
      interrupted_seq: rn.interrupted_seq,
      step_id: rn.step_id,
      agent_id: rn.agent_id,
    });
  }
  const progress = parseInterruptedProgress(r.progress);
  if (progress === undefined) return undefined;
  return {
    session_id: r.session_id,
    recovery: r.recovery,
    detail: r.detail,
    interrupted_runs: runs,
    resume_available: r.resume_available,
    task: r.task,
    workspace_root: r.workspace_root,
    progress,
  };
}

/** 恢复列表载荷的形状防御：信封不合法 / 任一条目不合法 ⇒ 整份回落 `undefined`
 *  （与 `parsePendingDecisions` 同一条纪律——一条坏行不返回半真半假的清单）。 */
function parseInterruptedRecoveries(raw: unknown): InterruptedRecoveries | undefined {
  if (typeof raw !== 'object' || raw === null) return undefined;
  const { snapshot_available, items } = raw as Record<string, unknown>;
  if (typeof snapshot_available !== 'boolean' || !Array.isArray(items)) return undefined;
  const out: InterruptedRecoveryItem[] = [];
  for (const item of items) {
    const parsed = parseInterruptedRecoveryItem(item);
    if (parsed === undefined) return undefined;
    out.push(parsed);
  }
  return { snapshot_available, items: out };
}

/** GET /api/recovery/interrupted — 只读恢复列表（快照 + 只读富化）。
 *
 *  非 2xx 抛错（不静默当空列表）；形状不合法返回 `undefined`（整组回落）——
 *  调用方据此按"列表不可得"呈现，绝不据此点亮「继续」（`resume_available` 的
 *  fail-safe 只在后端明确给出时采信）。#22：列表由后端单点驱动，前端不缓存。 */
export async function listInterruptedRecoveries(): Promise<InterruptedRecoveries | undefined> {
  const res = await apiFetch('/api/recovery/interrupted');
  if (!res.ok) throw new Error(`list interrupted recoveries ${res.status}`);
  return parseInterruptedRecoveries(await res.json());
}

// ── 同 run 恢复（`#312` T4，PRD §3 / `11 §6.1`）──

/** 恢复被预算暂停的逻辑 run 的请求体（**不带 task**）。
 *
 *  与「带 task 的续聊恢复」是**两种形态**，后端按 `task` 是否存在区分
 *  （`web/app.py::ResumeRequest`）：给了 task = 新任务新 run_id（既有语义，逐字不变）；
 *  不给 task = 同 run 续跑，此时必须带 `run_id` + `resume_basis` +
 *  `budget.expected_version` + 绝对 ceiling（缺声明 422、状态对不上 409）。
 *
 *  `budget.run` 的键是**卡住的那一维**（`#313` / `#314`）：
 *  - 四个 `max_*` 之一（`max_agent_turns_total` / `max_model_requests` /
 *    `max_total_tokens` / `max_cost_usd`，与后端 `RunLimitsBody` 的字段名逐字相同）；
 *  - 或 `tool_call_limits`（`#314`：工具名 → 正整数**绝对** ceiling）——per-tool 配额
 *    是"一维变多维"的那一维，它的点名单位是**工具名**。
 *
 *  值是**绝对值**不是增量：后端要求这一维恢复后至少放得下一次新准入（turns /
 *  requests 要 `> consumed + 1`，tokens / cost / 工具配额要 `> consumed`），低到不能
 *  继续的 ceiling 会被 409 拒掉；未点名的维度**沿用**暂停时的 ceiling（不清空、不重置
 *  counter——ADR-0044 D1/D3），`tool_call_limits` 还是**逐键**合并（点名哪个工具就抬
 *  哪个，未点名的保留）。cost 维传十进制**字符串**（保住 wire 精度；后端
 *  `parse_cost_ceiling` 数与串都收），工具配额只收正整数。
 *  `expected_version` 与 `run` 平级（PRD §3 的冻结形状——它是"这次预算变更"的属性，
 *  不是某个作用域的 ceiling）。 */
export interface ResumeRunLimitsBody extends Partial<Record<RunLimitField, number | string>> {
  /** `#315`：deadline 维（RFC 3339 UTC 文本）。它是 `RunLimitField` 里唯一的**文本**
   *  维——后端 `parse_deadline_at` 只收带时区的时刻，数不是合法形状。 */
  deadline_at?: string;
  /** `#314`：per-tool 绝对配额（工具名 → 正整数）。 */
  tool_call_limits?: Record<string, number>;
}

export interface ConstraintInputAnswerPayload {
  request_id: string;
  choice: ConstraintInputChoice;
  custom_text?: string;
}

export type ResumePausedRunPayload =
  | {
      run_id: string;
      resume_basis: 'budget_increase';
      budget: { expected_version: number; run: ResumeRunLimitsBody };
    }
  | {
      run_id: string;
      resume_basis: 'user_input';
      budget: { expected_version: number; run: ResumeRunLimitsBody };
      input_request: ConstraintInputAnswerPayload;
    };

/** 恢复目标（`lib/runBudget.ts` 的 `pauseFacts().resumeTarget` 的 wire 形态）：run 维
 *  给字段名，工具配额给工具名。两种目标写进 `budget.run` 的键不同，所以由这里**一处**
 *  决定形状——调用方（useSession）不再自己拼键名，漂移就没有第二个地方可发生。 */
export type ResumePausedRunTarget =
  | { kind: 'run'; field: RunLimitField; value: number | string }
  | { kind: 'tool'; tool: string; value: number }
  /** `#315`：deadline 暂停的恢复目标是**新的绝对截止时刻**（RFC 3339 UTC 文本）。
   *  与 run 维分开成一个 kind，是因为值域不同（文本 vs 数）且拒绝理由不同——把时刻
   *  塞进 `field: 'deadline_at'` 会让"这个值该是数还是时刻"只能靠字段名反推。 */
  | { kind: 'deadline'; field: 'deadline_at'; value: string };

/** 恢复请求的 `budget.run`：点名的目标 → 新绝对值（`#314` 起两种目标，`#315` 加 deadline）。 */
export function resumeRunLimitsBody(target: ResumePausedRunTarget): ResumeRunLimitsBody {
  if (target.kind === 'tool') {
    return { tool_call_limits: { [target.tool]: target.value } };
  }
  return { [target.field]: target.value };
}

/** 恢复请求被拒（409/422 且**零副作用**：后端判定在任何落盘之前）。
 *  单独一个错误类型是为了让调用方能按状态分派——409 要重读日志对齐真相，
 *  422 是请求形状问题（改参数重试即可），两者对用户的下一步动作不同。 */
export class ResumeRejectionError extends Error {
  readonly status: number;
  readonly pendingDecisions?: PendingDecision[];
  constructor(status: number, message: string, pendingDecisions?: PendingDecision[]) {
    super(message);
    this.status = status;
    this.pendingDecisions = pendingDecisions;
  }
}

/** POST /api/sessions/{id}/resume（同 run 续跑）。
 *
 *  成功返回**原始 Response**：后端以 SSE 流回（`_run_stream_response`，与
 *  POST /messages 的 launched 分支同形），调用方交给既有 SSE/WS 消费机器
 *  （不在这里读 body——攒包时响应头可能被压到 run 结束才下发，读 body 就是卡住）。
 *  409/422 是**短 JSON**，当场读掉 detail 再抛（否则 detail 丢失，用户只看到一个
 *  没头没尾的"恢复失败"）。 */
export async function resumeSession(
  sessionId: string,
  payload: ResumePausedRunPayload,
): Promise<Response> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (res.status === 409 || res.status === 422) {
    const body = await readErrorBody(res);
    throw new ResumeRejectionError(
      res.status,
      body.message || (res.status === 409 ? '恢复被拒绝（状态已变化）' : '恢复请求无效'),
      body.pendingDecisions,
    );
  }
  if (res.status === 404) throw new ResumeRejectionError(404, '会话不存在');
  if (!res.ok) throw new ResumeRejectionError(res.status, `恢复失败（${res.status}）`);
  return res;
}

// ── Session-level model switch（T7 #137，PRD §2.3）──

/** POST /api/sessions/{id}/model 的响应体。
 *  ``effective_model_id`` 是 service 解析出的规范 picker id——不能回显请求值，
 *  否则上游 model_name / "default" 别名会与事件里的 to_model_id 对不上。 */
export interface ModelChangeResult {
  status: string;
  provider: string;
  model_id: string;
}

/** 切换会话当前模型并写 ``model/changed``（PRD §2.3）。
 *
 * - 切换不打断在途 run——下一轮 run 从事件流派生当前模型生效
 * - ``GET /api/models`` 的默认条目（is_default=true）也是合法 POST target = 切回默认链
 * - 响应回传规范 model_id（service 解析出的 picker id），不回显请求值
 *
 * 错误码：
 * - 404 = session 不存在
 * - 422 = provider/model_id 不在 catalog
 */
export async function changeSessionModel(
  sessionId: string,
  provider: string,
  modelId: string,
): Promise<ModelChangeResult> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/model`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, model_id: modelId }),
  });
  if (!res.ok) throw new Error(`change model ${res.status}`);
  return res.json();
}

// ── Session-level permission switch（F18-A #282 后端 / F18-B #283 前端，ADR-0041）──

/** POST /api/sessions/{id}/permission 的响应体。
 *  `permission_mode` / `auto_approve` 是 service 解析出的**改后当下生效值**（不是请求
 *  回显）——但前端**不拿它当状态**，见 `changeSessionPermission` 的第三条约定。 */
export interface PermissionChangeResult {
  status: string;
  permission_mode: string;
  auto_approve: boolean;
}

/** 会话内改权限档 + `auto_approve`（ADR-0041 D1/D2）。
 *
 * 三条调用方必须知道的约定：
 *
 * 1. **下一轮 run 生效**（D4）：只 append durable 事件、不打断在途 run ⇒ **不得**向用户
 *    承诺「立即生效」（Composer 的权限浮层底部披露同一句话）。
 * 2. **`auto_approve` 是必填**：后端刻意不给默认值，漏传即 422——否则一次「只改档位」
 *    的调用会把批准策略一并翻掉（ADR-0041 §4）。
 * 3. **不用回执写本地状态**：档位的唯一真相是投影折叠出的
 *    `ConversationState.session_permission_mode`。「回执驱动的本地状态正是要消灭的第二套
 *    真相」（同本文件 `CreatedEmptySession` 那条注释）——回执只在调用方做失败判定与
 *    「什么时候重读事件」的时机用，值本身不进任何前端状态。
 *
 * 错误码（`detail` 原样带出，调用方就地回显）：
 * - 404 = session 不存在
 * - 422 = 档位不在 `PermissionPolicy`
 * - 409 = 有未裁决审批（PendingApprovalConflict）或 seq 冲突
 */
export async function changeSessionPermission(
  sessionId: string,
  permissionMode: string,
  autoApprove: boolean,
): Promise<PermissionChangeResult> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/permission`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ permission_mode: permissionMode, auto_approve: autoApprove }),
  });
  if (!res.ok) {
    const detail = await readErrorDetail(res);
    // 带 status 的具名错误（同 createEmptySession）：409 与 422 需要被调用方分开讲，
    // 而 changeSessionModel 那种裸 `Error` 只有一句状态码，用户看不懂。
    throw new SessionError(res.status, detail || `change permission ${res.status}`);
  }
  return res.json();
}

// ── Fork（T7 #137，PRD §2.4）──

/** POST /api/sessions/{id}/forks 的响应体。 */
export interface ForkResult {
  session_id: string;
  from_seq: number;
}

/** 从历史用户消息 seq 派生 child session（PRD §2.4）。
 *
 * - 锚点消息本身不进 child seed（child 侧由用户重新发送，pi /fork 同款语义）
 * - child 继承父会话当前模型
 * - HTTP 端点不生成 tail summary（确定性、无模型调用）
 * - copy-on-fork：父 workspace 整目录复制为 child 的
 *
 * 错误码：
 * - 404 = session 不存在
 * - 409 = 在途 run（历史未 settled）
 * - 422 = from_seq 不是合法 fork 锚点（不是用户消息 seq）
 */
export async function forkSession(
  sessionId: string,
  fromSeq: number,
): Promise<ForkResult> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/forks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ from_seq: fromSeq }),
  });
  if (!res.ok) {
    // BUG-001 fix：解析后端 detail 给用户看（可用边界列表等）。
    let detail = '';
    try { detail = (await res.json())?.detail ?? ''; } catch { /* keep '' */ }
    throw new Error(detail || `fork ${res.status}`);
  }
  return res.json();
}

// ── Projects（WS-4 / #154 端点，WS-5 / #155 前端消费）──

/** 项目操作失败——带 HTTP 状态码，调用方据此给**具体**原因而不是"操作失败"。
 *
 *  状态码语义（后端 `domain_errors.py` 两张表 + `web/projects.py`）：
 *  - 403：跨源被来源闸拒绝（ADR-0025 D1）——本地信任模式下非本机 Origin；
 *  - 404：项目 id 不存在 / 注册的路径不存在（create 不会 mkdir）；
 *  - 409：请求与账本现状冲突——attach 时会话 cwd 与项目路径不一致，
 *    或重排锚点不在该项目账本里；
 *  - 422：入参形态非法——非绝对路径 / 该路径不是目录 / 标题空白。
 *
 *  `detail` 是后端给的中文原因（可能为空）：调用方优先显示它。 */
export class ProjectError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** 把项目写操作的异常翻成给用户看的一句话（对话框与侧栏错误条共用）。
 *
 *  `ProjectError` 优先用**后端 detail**——409 会给出"会话 cwd 与项目路径不一致"
 *  这类可行动原因，翻译成"操作失败"等于把它扔掉；其余异常用 `message`；都没有才
 *  用调用方的兜底文案。 */
export function describeProjectError(error: unknown, fallback: string): string {
  if (error instanceof ProjectError) return error.message || fallback;
  const message = (error as Error | null)?.message;
  return message || fallback;
}

/** 按路径把目录注册为项目（幂等：同规范路径 → 返回既有实体）。
 *  路径不存在 → ProjectError(404)；非绝对路径 → ProjectError(422)。 */
export async function createProject(path: string, title?: string | null): Promise<Project> {
  const res = await apiFetch('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(title ? { path, title } : { path }),
  });
  if (!res.ok) throw await projectError(res, '注册项目失败');
  return requireProject(await res.json());
}

/** 全部项目（**注册表顺序**，新建项目前插）。空数组 ≠ 出错：调用方据此显示
 *  「还没有项目」而不是错误横幅。 */
export async function listProjects(): Promise<Project[]> {
  const res = await apiFetch('/api/projects');
  if (!res.ok) throw await projectError(res, '加载项目失败');
  const body: unknown = await res.json();
  if (!Array.isArray(body)) return [];
  // 形状不符的单条丢弃（零伪造）——但**不因此丢掉其余项目**。
  return body.flatMap((raw) => {
    const p = parseProject(raw);
    return p ? [p] : [];
  });
}

/** 重命名项目（`setTitle`）。 */
export async function renameProject(projectId: string, title: string): Promise<Project> {
  const res = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw await projectError(res, '重命名失败');
  return requireProject(await res.json());
}

/** **软删除**项目：只摘注册记录与账本，目录/用户文件/会话日志一概不动，
 *  成员会话回到未分组。响应 `detail` 必须原样展示给用户（AC5）。 */
export async function deleteProject(projectId: string): Promise<ProjectDeleted> {
  const res = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw await projectError(res, '删除项目失败');
  // 形状防御：非对象（null / 数组 / 字符串）一律当"没给"处理，别让读字段抛
  // TypeError——那时调用方拿到的是"读属性失败"，而不是"软删除成功了但回执为空"。
  const raw: unknown = await res.json().catch(() => null);
  const body = (typeof raw === 'object' && raw !== null ? raw : {}) as Partial<ProjectDeleted>;
  return {
    id: typeof body.id === 'string' ? body.id : projectId,
    deleted: body.deleted === true,
    sessions_detached:
      typeof body.sessions_detached === 'number' ? body.sessions_detached : 0,
    detail: typeof body.detail === 'string' ? body.detail : '',
  };
}

/** 把会话加入项目（幂等）。会话不存在/无 cwd → 404；cwd 与项目路径不一致 → 409。 */
export async function attachSessionToProject(
  projectId: string,
  sessionId: string,
): Promise<Project> {
  const res = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!res.ok) throw await projectError(res, '加入项目失败');
  return requireProject(await res.json());
}

/** 把会话移出项目（幂等：不在本项目 → 无写操作；会话日志逐字节不动）。 */
export async function detachSessionFromProject(
  projectId: string,
  sessionId: string,
): Promise<Project> {
  const res = await apiFetch(
    `/api/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}`,
    { method: 'DELETE' },
  );
  if (!res.ok) throw await projectError(res, '移出项目失败');
  return requireProject(await res.json());
}

/** 项目内重排（DOM `insertBefore` 语义：`before=null` → 追加尾部）。
 *  锚点不在该项目账本里 → ProjectError(409)。 */
export async function reorderProjectSession(
  projectId: string,
  sessionId: string,
  before: string | null,
): Promise<Project> {
  const res = await apiFetch(
    `/api/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/order`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ before }),
    },
  );
  if (!res.ok) throw await projectError(res, '调整顺序失败');
  return requireProject(await res.json());
}

/** 非 2xx → ProjectError（detail 优先，缺失时用调用方给的兜底前缀 + 状态码）。 */
async function projectError(res: Response, fallback: string): Promise<ProjectError> {
  const detail = await readErrorDetail(res);
  return new ProjectError(res.status, detail || `${fallback}（${res.status}）`);
}

// ── 宿主侧目录列举（WS-7 / #170，ADR-0028）──

/** GET /api/host/dirs —— 宿主侧目录列举：只读、一层、仅目录（ADR-0028 D3–D5）。
 *
 *  `path` 省略 = 列**根**（Windows 盘符 / POSIX `/`），响应的 `path` 为 null。
 *  失败时抛 `ProjectError`（detail 优先）：403 无权限 / 404 不存在 / 422 不是目录
 *  等全部由后端 detail 说明，界面**原样显示**——PRD §4.4 的错误矩阵就是按"前端不
 *  翻译"设计的，这里多加一句自己的话就会把矩阵里的措辞盖掉。
 *
 *  形状窄化：非法条目丢弃但**不牵连其余**（与 listProjects / listMemories 同一条
 *  纪律）；`path`/`parent` 只认字符串，其余一律当"没有"（那时界面显示的是"没有当前
 *  目录"而不是一个编造的路径）。 */
export async function getHostDirs(path?: string | null): Promise<HostDirsListing> {
  const query = path ? `?path=${encodeURIComponent(path)}` : '';
  const res = await apiFetch(`/api/host/dirs${query}`);
  if (!res.ok) throw await projectError(res, '加载目录失败');
  const raw: unknown = await res.json().catch(() => null);
  const body = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  const entries = Array.isArray(body.entries)
    ? body.entries.flatMap((raw) => {
        if (typeof raw !== 'object' || raw === null) return [];
        const entry = raw as Record<string, unknown>;
        if (typeof entry.name !== 'string' || !entry.name) return [];
        if (typeof entry.path !== 'string' || !entry.path) return [];
        return [{ name: entry.name, path: entry.path }];
      })
    : [];
  return {
    path: typeof body.path === 'string' && body.path ? body.path : null,
    parent: typeof body.parent === 'string' && body.parent ? body.parent : null,
    truncated: body.truncated === true,
    entries,
  };
}

/** 记忆请求失败：状态码 + 后端 detail 原文（detail 优先，是 AC 要展示的那句话）
 *  + 机读判别码（`detail.code`，老后端没有则为 null）。 */
export class MemoryError extends Error {
  readonly status: number;
  readonly code: string | null;
  constructor(status: number, message: string, code: string | null = null) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** 记忆列表（GET /api/memories，**按创建时间倒序**分页）。
 *
 *  `limit` 由后端夹在 1..200（本函数不猜上界，越界由后端 422 说话）；`offset` 是
 *  「跳过的条数」——"加载更多"传已显示的条数。空数组 ≠ 出错：调用方据此显示
 *  「还没有记忆」而不是错误横幅（与 `listProjects` 同一条纪律）。
 *
 *  形状不符的单条**丢弃但不牵连其余**：一条坏行不能把整页变成"加载失败"。
 */
export async function listMemories(limit = 50, offset = 0): Promise<MemorySummary[]> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  const res = await apiFetch(`/api/memories?${params.toString()}`);
  if (!res.ok) throw await memoryError(res, '加载记忆失败');
  const body: unknown = await res.json();
  if (!Array.isArray(body)) return [];
  return body.flatMap((raw) => {
    const memory = parseMemory(raw);
    return memory ? [memory] : [];
  });
}

/** 硬删一条记忆（DELETE /api/memories/{id}）——**不可恢复**，调用方必须先二次确认。
 *
 *  状态码语义（后端 `web/memory.py` 的契约，前端不合并它们）：
 *    200 `{id, deleted:true}` / 404 id 不存在 / 403 不属于当前入口 / 503 记忆未装配。
 *  404 与 403 都**不**当成功：用户对着具体一条点删除，"已经不在了"与"不给删"是
 *  两种不同结果，UI 要分别说（这也是前端不维护第二套真相的必然要求——本地删掉
 *  而后端拒绝会直接违背不变量 #22）。
 */
export async function deleteMemory(memoryId: string): Promise<MemoryDeleted> {
  const res = await apiFetch(`/api/memories/${encodeURIComponent(memoryId)}`, { method: 'DELETE' });
  if (!res.ok) throw await memoryError(res, '删除记忆失败');
  const raw: unknown = await res.json().catch(() => null);
  const body = (typeof raw === 'object' && raw !== null ? raw : {}) as Partial<MemoryDeleted>;
  return {
    id: typeof body.id === 'string' ? body.id : memoryId,
    deleted: body.deleted === true,
  };
}

/** 非 2xx → MemoryError（detail 优先，缺失时用兜底前缀 + 状态码）。 */
async function memoryError(res: Response, fallback: string): Promise<MemoryError> {
  const { message, code } = await readErrorBody(res);
  return new MemoryError(res.status, message || `${fallback}（${res.status}）`, code);
}

/** 装配期就失败（`DegradeReason.INIT_FAILED`）的机读码。与"没配"共用一个 503 状态码，
 *  但**是故障不是配置状态**：要报错、要给重试（#225）。 */
const MEMORY_INIT_FAILED = 'init_failed';

/** 记忆能力**装配失败**（外部依赖故障，503 + `code=init_failed`）：**这是故障**——
 *  用户改配置没用，要给错误条 + 重试，而不是"记忆未启用"那句配置态文案。
 *  真机症状（#225）：后端明明返回了"初始化失败"的降级原因，前端却一律按配置状态渲染，
 *  还自己加了一句"这不是故障"，把用户推去改一个本来就配好的开关。 */
export function isMemoryFault(error: unknown): boolean {
  return (
    error instanceof MemoryError && error.status === 503 && error.code === MEMORY_INIT_FAILED
  );
}

/** 记忆能力**未装配**（配置状态，503 且不是装配失败）：UI 要显示「记忆未启用」而不是
 *  "加载失败/重试"，否则用户会一直点重试去修一个不存在的故障（不变量 #21）。
 *  **老后端没有 `code`**（detail 是纯字符串）时按配置状态处理——那是这以前的唯一语义。 */
export function isMemoryDisabled(error: unknown): boolean {
  return error instanceof MemoryError && error.status === 503 && !isMemoryFault(error);
}

/** 记忆错误 → 展示文案：MemoryError 的 message 就是后端 detail（或兜底前缀），
 *  其余异常（网络层 TypeError 等）用 message 或 fallback。与 `describeProjectError`
 *  同构但**分开**：两个能力各自演进，共用一个会让某一侧的语义渗到另一侧。 */
export function describeMemoryError(error: unknown, fallback: string): string {
  if (error instanceof MemoryError) return error.message || fallback;
  const message = (error as Error | null)?.message;
  return message || fallback;
}

/** 窄化解析一条记忆（零伪造）：id/content/created_at 必须是非空字符串、scope 必须是
 *  已知取值、metadata 必须是对象；任一不符 → null。**不补默认值**：`scope` 猜错会让
 *  用户以为这条记在别的名下，`created_at` 补空串会让"记于何时"变成谎言。 */
function parseMemory(raw: unknown): MemorySummary | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const row = raw as Record<string, unknown>;
  const { id, content, created_at: createdAt, scope, metadata } = row;
  if (typeof id !== 'string' || !id) return null;
  if (typeof content !== 'string') return null;
  if (typeof createdAt !== 'string' || !createdAt) return null;
  if (scope !== 'user' && scope !== 'session') return null;
  return {
    id,
    content,
    scope: scope as MemoryScope,
    metadata: typeof metadata === 'object' && metadata !== null
      ? (metadata as Record<string, unknown>)
      : {},
    created_at: createdAt,
  };
}

/** 窄化解析项目（零伪造）：id/path/title/session_ids 形状不符 → null（调用方丢弃该条）。
 *
 *  `status` 是**例外**：只有明确等于 `'missing-dir'` 才取该值，其余（含未知字符串、
 *  缺失）一律 `'ok'`。理由——它是"目录可能不见了"的**告警标志**，不是存在性断言；
 *  把未知值当告警会让每条项目都亮黄点（假告警），而把项目整条丢掉更糟：项目会从
 *  UI 消失、它的会话被误判成未分组。未知状态值不值得付出这两个代价。 */
function parseProject(raw: unknown): Project | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.id !== 'string' || !r.id) return null;
  if (typeof r.path !== 'string' || !r.path) return null;
  if (typeof r.title !== 'string') return null;
  const sessionIds = Array.isArray(r.session_ids)
    ? r.session_ids.filter((v): v is string => typeof v === 'string' && v.length > 0)
    : [];
  return {
    id: r.id,
    path: r.path,
    title: r.title,
    status: normalizeProjectStatus(r.status),
    session_ids: sessionIds,
    created_at: typeof r.created_at === 'string' ? r.created_at : '',
    updated_at: typeof r.updated_at === 'string' ? r.updated_at : '',
  };
}

/** 单实体端点：200 但形状不符 → **响亮失败**而不是返回伪造项目。
 *  （列表端点相反：丢掉坏条目、保留其余，见 `listProjects`——一条坏数据不该让整
 *  个侧栏空掉，但一个"注册成功"的假实体更糟：它会让 UI 显示一个后端并不存在的项目。） */
function requireProject(raw: unknown): Project {
  const project = parseProject(raw);
  if (!project) {
    throw new Error('项目响应形状不符（后端契约可能已变更）');
  }
  return project;
}

function normalizeProjectStatus(raw: unknown): ProjectStatus {
  return raw === 'missing-dir' ? 'missing-dir' : 'ok';
}


// ── 上下文容量看板（#200）──

/** 六桶分类 + 缓存命中率的端点形状（后端 GET /api/sessions/{id}/context-usage）。 */
export interface ContextUsage {
  estimated: boolean;
  window_tokens: number;
  used_tokens: number;
  thresholds: { auto_compact: number; hard_guard: number };
  breakdown: {
    messages: number;
    system_prompt: number;
    skills: number;
    other: number;
    tools: { system: number; mcp: number };
  };
  cache: {
    state: 'ok' | 'partial' | 'not_collected';
    reported_calls: number;
    total_calls: number;
    avg_hit_rate: number | null;
  };
  /** `ok` = 有 builder 快照（六桶可分解）；`usage_only` = 快照缺席但事件流有用量
   *  （#212：`used_tokens` 是**窗口占用下界**，六桶如实为 0，来源见 `usage_source`）；
   *  `no_data` = 两者都没有。**不得**把 `usage_only` 也说成"没有数据"，
   *  两者对用户是两句不同的话（"分类缺了" vs "什么都没上报"）。 */
  state: 'ok' | 'usage_only' | 'no_data';
  /** 仅 `usage_only` 下发：`used_tokens` 的取数事实（后端算，前端不推算，#22）。 */
  usage_source?: {
    /** 取数口径的机器码。已知值 `'last_call_prompt_tokens'` = 最近一次
     *  `model/completed` 的 `prompt_tokens`（窗口占用下界）。
     *
     *  **故意不写成字面量联合**：`getContextUsage` 没做字段窄化（`res.json()` 直出），
     *  写成 `'last_call_prompt_tokens'` 只会在"后端换口径"时让编译期继续点头——
     *  而设计稿 §3.4 明说翻案只改一行取值、不动契约形状。所以类型如实写成 string，
     *  由 `ContextUsagePanel` 按值选措辞、未知码退中性说法（有单测）。 */
    kind: string;
    calls_with_usage: number;
    last_prompt_tokens: number;
    last_total_tokens: number | null;
  };
}

/** GET /api/sessions/{id}/context-usage —— 上下文容量（只读，无副作用）。
 *  404 = 会话不存在；非 2xx 抛 Error（调用方降级为空态，不影响会话）。 */
export async function getContextUsage(sessionId: string): Promise<ContextUsage> {
  const res = await apiFetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/context-usage`,
  );
  if (res.status === 404) throw new NotFoundError('会话不存在');
  if (!res.ok) throw new Error(`context-usage ${res.status}`);
  return res.json();
}

/** 手动压缩回执（后端 `POST /api/sessions/{id}/context/compact`，`#635`）。
 *
 *  形状是服务层 DTO 的字段透传。`bracket_id` 指向新落的 bracket；低水位
 *  （无可压缩早期轮 / 校验闸门未过）时 `bracket_id=null`、`compacted_turn_count=0`，
 *  **仍 200 且零写入**——"没有可压的"不是错误。`tokens_before/after` 同一口径，
 *  可直接相减展示。 */
export interface SessionContextCompacted {
  bracket_id: string | null;
  source_seq_start: number | null;
  source_seq_end: number | null;
  tokens_before: number;
  tokens_after: number;
  compacted_turn_count: number;
  summary_model: string | null;
}

/** 压缩是单次同步 POST，网关实测有 44s 送达延迟（`web/app.py` 注释），且不引入
 *  SSE/轮询。给一个**显式**超时上限，把"永远转圈"变成一个明确的错误提示；正常
 *  10–60s 远在阈值内。**不是**服务端超时——只是前端停止等待。 */
const COMPACT_TIMEOUT_MS = 180_000;

/** 手动触发一次上下文压缩（`#635`）：每次调用都是用户显式请求的一次**新**压缩，
 *  追加新 bracket；重复调用安全但**非 no-op**（与 `purge-stale-tools` 的幂等不同）。
 *
 *  错误矩阵（后端 `web/app.py::compact_session_context`）：
 *    404 —— 没有这个会话；
 *    409 —— 在途 run 或该会话已有压缩在途（`detail` 原样上抛，不自己编文案）；
 *    422 —— id 形态非法 / 非法 `?model=`（`detail` 原样上抛）；
 *    403 —— 非本机来源（宿主侧管理动作只接受本机来源，ADR-0025 D1）。
 *  超时 → 明确的 Error（前端不再无限等待）。 */
export async function compactSession(
  sessionId: string,
  model?: string,
): Promise<SessionContextCompacted> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), COMPACT_TIMEOUT_MS);
  try {
    const query = model ? `?model=${encodeURIComponent(model)}` : '';
    const res = await apiFetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/context/compact${query}`,
      { method: 'POST', signal: controller.signal },
    );
    if (!res.ok) throw await sessionError(res, '压缩会话上下文失败');
    return res.json();
  } catch (error) {
    if (controller.signal.aborted) {
      throw new Error('压缩请求超时（180s）——仍可稍后在上下文容量面板查看结果');
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}


// ── 持久审批规则（#684 Phase 2）──

/** 一条项目级持久审批规则（后端 GET /api/approve-policy/rules）。
 *
 *  规则内容由 Runtime 在审批流里从已校验参数精确派生（用户只能选粒度，不能手写
 *  规则文本）；前端只读展示与原样撤销，**不提供创建入口**——唯一安装路径是用户在
 *  审批卡上主动选「以后都允许」（F21：禁止静默创建）。 */
export interface ApprovePolicyRule {
  id: string;
  tool: string;
  /** exact 档 = 完整身份键（含 args hash）；command 档 = 去 args hash 的命令串。 */
  key: string;
  granularity: 'exact' | 'command';
  /** 批准时工具权限；命中时会重验（工具权限被抬高则旧规则失效，防提权）。 */
  permission_at_approval: string;
  created_at: string;
}

/** 撤销回执（POST /api/approve-policy/rules/{id}/revoke）。`revoked` 是**动作后**
 *  的真值：幂等——重复撤销 / id 不存在仍 200，`revoked=false` 表示当时本就不存在。 */
export interface ApprovePolicyRuleRevoked {
  id: string;
  revoked: boolean;
}

/** 白名单投影：契约字段必须在这里登记，否则静默丢弃（api.ts 的通用纪律）。
 *  `granularity` 只认 exact / command 两档（F22：系统不猜第三档）。 */
function parseApprovePolicyRule(raw: unknown): ApprovePolicyRule | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.id !== 'string' || !r.id) return null;
  if (typeof r.tool !== 'string') return null;
  if (typeof r.key !== 'string') return null;
  const granularity =
    r.granularity === 'exact' || r.granularity === 'command' ? r.granularity : null;
  if (granularity === null) return null;
  if (typeof r.permission_at_approval !== 'string') return null;
  if (typeof r.created_at !== 'string') return null;
  return {
    id: r.id,
    tool: r.tool,
    key: r.key,
    granularity,
    permission_at_approval: r.permission_at_approval,
    created_at: r.created_at,
  };
}

/** GET /api/approve-policy/rules —— 列出本项目持久审批规则（只读）。
 *  无规则 / 文件缺失 / 文件损坏后端的 fail-closed 都是 `{"rules": []}`；非 2xx 抛 Error。 */
export async function listApprovePolicyRules(): Promise<ApprovePolicyRule[]> {
  const res = await apiFetch('/api/approve-policy/rules');
  if (!res.ok) throw new Error(`approve-policy rules ${res.status}`);
  const body = (await res.json().catch(() => null)) as { rules?: unknown } | null;
  const raw = body && Array.isArray(body.rules) ? body.rules : [];
  return raw.flatMap((entry) => {
    const rule = parseApprovePolicyRule(entry);
    return rule ? [rule] : [];
  });
}

/** POST /api/approve-policy/rules/{id}/revoke —— 撤销一条持久规则（幂等）。
 *  200 `{id, revoked}`；422 id 形态非法。调用方**必须先二次确认**（F21：持久
 *  "always allow" 是全系统攻击面最大的单点，撤销是不可静默触发的显式动作）。 */
export async function revokeApprovePolicyRule(
  ruleId: string,
): Promise<ApprovePolicyRuleRevoked> {
  const res = await apiFetch(
    `/api/approve-policy/rules/${encodeURIComponent(ruleId)}/revoke`,
    { method: 'POST' },
  );
  if (!res.ok) throw new Error(`revoke approve-policy rule ${res.status}`);
  const body = (await res.json().catch(() => null)) as Partial<ApprovePolicyRuleRevoked> | null;
  return {
    id: typeof body?.id === 'string' ? body.id : ruleId,
    revoked: body?.revoked === true,
  };
}

// ── #353 W-09 任务审阅：Task / Evidence / Lease / workspace-git 只读+命令面 ──
//
// 数据源全部是服务端投影（`session/task.py::derive_task_state`、
// `session/evidence.py::derive_evidence_state` 的 REST 搬运）。前端**不**合并
// `product_state` 与 `freshness`、不推断 UNKNOWN（不变量 #22）——只如实透传。

/** GET /task、GET /evidence 回 404：**未定义任务**（后端契约：`state is None` → 404，
 *  与"会话不存在"同码）。调用方据此渲染"未定义任务"空态，而不是加载失败横幅。 */
export class TaskNotDefinedError extends Error {}

/** 任务审阅相关命令的非 2xx（除 404 未定义）：`status` 保留原始 HTTP 码，
 *  `message` = 服务端 `detail` 原文（409=已接受过/版本冲突，422=形状非法）。
 *  与 `AlreadyResolvedError`（审批幂等）区分：task acceptance 的 409 语义由调用方
 *  结合最新 `GET /task` 结果判定，不做字符串匹配。 */
export class TaskReviewRequestError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** 逐项验证值六态（后端 `VERIFICATION_VALUES`；中文展示名由组件映射）。 */
export type VerificationValue =
  | 'not_started'
  | 'in_progress'
  | 'passed'
  | 'failed'
  | 'blocked'
  | 'incomplete';

/** 产品交付四态（后端 `PRODUCT_STATES`；未定义任务为 ""）。 */
export type ProductState = 'executing' | 'pending_verification' | 'deliverable' | 'accepted' | '';

export interface TaskCriterionPayload {
  item_id: string;
  text: string;
  origin: 'user' | 'agent';
  confirmed: boolean;
}

export interface TaskVerificationEntryPayload {
  value: VerificationValue;
  evidence: string | null;
}

export interface TaskAcceptancePayload {
  decision: 'accepted' | 'accepted_with_gaps';
  reason: string | null;
}

/** `GET /api/sessions/{id}/task` 的 `task` 对象（`TaskState.to_payload()` 逐字段透传）。 */
export interface TaskState {
  defined: boolean;
  task_text: string | null;
  read_write_intent: string | null;
  cwd: string | null;
  /** 会话创建时显式声明的权限档（后端 `TaskState.to_payload` 的 permission_mode 投影）；
   *  未声明（历史会话 / 用户没选）为 null，不替用户猜档位。 */
  authorization: string | null;
  criteria: TaskCriterionPayload[];
  verification: Record<string, TaskVerificationEntryPayload>;
  acceptance: TaskAcceptancePayload | null;
  version: number;
  open_run_ids: string[];
  product_state: ProductState;
}

export type EvidenceResult = 'pass' | 'fail' | 'blocked';

export interface EvidenceManifestFile {
  path: string;
  sha256: string;
}

/** 记录时的覆盖清单（快照）：显式文件 + sha256；`progress.md` hash 独立单列。 */
export interface EvidenceManifest {
  files: EvidenceManifestFile[];
  manifest_hash: string;
  progress_md_sha256: string | null;
}

/** 一条结构化证据（票面 14 字段 + 服务端读取时求值的 `freshness`）。 */
export interface EvidenceRecord {
  evidence_id: string;
  task_session_id: string;
  run_id: string;
  criterion_id: string;
  kind: 'test' | 'ui' | 'diff' | 'external';
  source_event_seq: number | null;
  tool_call_id: string | null;
  captured_at: string;
  result: EvidenceResult;
  command_or_action: string | null;
  exit_code_or_observation: number | string | null;
  artifact_ref: string | null;
  base_head: string | null;
  workspace_manifest: EvidenceManifest;
  /** 服务端 fail-closed 新鲜度判定：`stale` 时 `reasons` 逐条列明过期原因。 */
  freshness: { status: 'fresh' | 'stale'; reasons: string[] };
}

/** `GET /api/sessions/{id}/evidence` → `evidence`（criterion_id → 记录数组）。 */
export type EvidenceByCriterion = Record<string, EvidenceRecord[]>;

export interface LeaseReleaseResult {
  released: boolean;
  promoted_to: string | null;
}

/** 只读 git 命令结果（后端 `GitCommandResult`；`exit_code` 非零**不是** HTTP 错误）。 */
export interface GitCommandResult {
  exit_code: number;
  stdout: string;
  stderr: string;
  artifact_ref: string | null;
}

async function taskReviewError(res: Response, fallback: string): Promise<TaskReviewRequestError> {
  const detail = await readErrorDetail(res);
  return new TaskReviewRequestError(res.status, detail || `${fallback}（${res.status}）`);
}

/** 任务审阅 API 响应的通用解析（P2-1 抽取的公共模式）。
 *  - map404=true 时：404 → TaskNotDefinedError（未定义任务空态）；
 *  - 其余非 2xx → taskReviewError(fallback)；
 *  - JSON 解析失败 → null，由 extract 决定回退值。
 *  行为与抽取前四处内联代码逐字一致。 */
async function parseTaskResponse<T>(
  res: Response,
  fallback: string,
  extract: (body: { task?: unknown; evidence?: unknown } | null) => T,
  map404 = false,
): Promise<T> {
  if (map404 && res.status === 404) throw new TaskNotDefinedError('未定义任务');
  if (!res.ok) throw await taskReviewError(res, fallback);
  const body = (await res.json().catch(() => null)) as { task?: unknown; evidence?: unknown } | null;
  return extract(body);
}

/** GET /api/sessions/{id}/task —— 任务交付状态投影（只读）。
 *  404 = 未定义任务（TaskNotDefinedError）；其余非 2xx = TaskReviewRequestError。 */
export async function getTaskState(sessionId: string): Promise<TaskState> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/task`);
  return parseTaskResponse(res, '加载任务状态失败', (body) => (body?.task ?? null) as TaskState, true);
}

/** GET /api/sessions/{id}/evidence —— 证据投影 + 服务端新鲜度（只读）。
 *  404 = 未定义任务（TaskNotDefinedError）；无证据 = `{}`。 */
export async function getEvidenceState(sessionId: string): Promise<EvidenceByCriterion> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/evidence`);
  return parseTaskResponse(
    res,
    '加载证据失败',
    (body) => {
      const evidence = body?.evidence;
      return (typeof evidence === 'object' && evidence !== null ? evidence : {}) as EvidenceByCriterion;
    },
    true,
  );
}

/** POST /api/sessions/{id}/task/acceptance —— 用户裁决（CAS：expected_version 必填）。
 *  42x 原样上抛（调用方展示服务端 reason；409 由调用方对照最新投影判定语义）。 */
export async function acceptTask(
  sessionId: string,
  body: { decision: 'accepted' | 'accepted_with_gaps'; reason?: string; expected_version: number },
): Promise<TaskState> {
  const payload: Record<string, unknown> = {
    decision: body.decision,
    expected_version: body.expected_version,
  };
  if (body.reason !== undefined) payload.reason = body.reason;
  const res = await apiFetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/task/acceptance`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  return parseTaskResponse(res, '接受任务失败', (result) => (result?.task ?? null) as TaskState);
}

/** POST /api/sessions/{id}/task/acceptance/release —— 撤销裁决（同样 CAS）。 */
export async function releaseTaskAcceptance(
  sessionId: string,
  body: { reason?: string; expected_version: number },
): Promise<TaskState> {
  const payload: Record<string, unknown> = { expected_version: body.expected_version };
  if (body.reason !== undefined) payload.reason = body.reason;
  const res = await apiFetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/task/acceptance/release`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  return parseTaskResponse(res, '撤销接受失败', (result) => (result?.task ?? null) as TaskState);
}

/** POST /api/sessions/{id}/task/lease/release —— 释放**目录写租约**（幂等）。
 *  与"撤销接受"是两个不同事实（#353 §D）：`released:false` = 本会话非持有者，如实显示。 */
export async function releaseTaskLease(sessionId: string): Promise<LeaseReleaseResult> {
  const res = await apiFetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/task/lease/release`,
    { method: 'POST' },
  );
  if (!res.ok) throw await taskReviewError(res, '释放目录失败');
  const body = (await res.json().catch(() => null)) as
    | { released?: unknown; promoted_to?: unknown }
    | null;
  return {
    released: body?.released === true,
    promoted_to: typeof body?.promoted_to === 'string' ? body.promoted_to : null,
  };
}

function parseGitResult(raw: unknown): GitCommandResult {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  return {
    exit_code: typeof r.exit_code === 'number' ? r.exit_code : -1,
    stdout: typeof r.stdout === 'string' ? r.stdout : '',
    stderr: typeof r.stderr === 'string' ? r.stderr : '',
    artifact_ref: typeof r.artifact_ref === 'string' ? r.artifact_ref : null,
  };
}

/** GET /api/sessions/{id}/workspace/git/status —— `git status --porcelain`（只读）。
 *  `pathspec` 非空才带查询串；非 2xx（403 越界 / 422 形态）→ TaskReviewRequestError。 */
export async function getWorkspaceGitStatus(
  sessionId: string,
  pathspec?: string,
): Promise<GitCommandResult> {
  const query = pathspec ? `?pathspec=${encodeURIComponent(pathspec)}` : '';
  const res = await apiFetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/workspace/git/status${query}`,
  );
  if (!res.ok) throw await taskReviewError(res, '加载 git 状态失败');
  return parseGitResult(await res.json().catch(() => null));
}

/** GET /api/sessions/{id}/workspace/git/diff —— unified diff（只读，正文在 stdout）。
 *  单文件 diff 传 `path`；`staged` 为真时看暂存区。 */
export async function getWorkspaceGitDiff(
  sessionId: string,
  opts: { path?: string; staged?: boolean } = {},
): Promise<GitCommandResult> {
  const params = new URLSearchParams();
  if (opts.path) params.set('path', opts.path);
  if (opts.staged) params.set('staged', 'true');
  const query = params.toString() ? `?${params.toString()}` : '';
  const res = await apiFetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/workspace/git/diff${query}`,
  );
  if (!res.ok) throw await taskReviewError(res, '加载 git diff 失败');
  return parseGitResult(await res.json().catch(() => null));
}
