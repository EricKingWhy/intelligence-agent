/** REST API client. All fetches go through here — single seam for base URL / auth headers.
 *
 * Auth seam (backend auth_seam fail-closed, df4f7d8): when the user has configured
 * a Bearer token (lib/auth), apiFetch injects `Authorization` on every request —
 * including the SSE POST. 401 responses are broadcast (auth.onUnauthorized) and
 * thrown as UnauthorizedError so callers surface the guidance path.
 */

import type { AgentEvent, SessionSummary } from '../types';
import { emitUnauthorized, getToken } from './auth';

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

/** FastAPI 错误体 {detail} 读取：形状不符或 JSON 解析失败返回 ''——
 *  错误处理路径自身不再产生新错误（两处 401/409 消费共享的单一实现）。 */
async function readErrorDetail(res: Response): Promise<string> {
  try {
    const j = await res.json();
    return j && typeof j.detail === 'string' ? j.detail : '';
  } catch {
    return '';
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

export async function listSessions(): Promise<SessionSummary[]> {
  const res = await apiFetch('/api/sessions');
  if (!res.ok) throw new Error(`list sessions ${res.status}`);
  return res.json();
}

export async function getSessionEvents(sessionId: string): Promise<AgentEvent[]> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/events`);
  if (res.status === 404) throw new NotFoundError(`会话不存在（${sessionId}）`);
  if (!res.ok) throw new Error(`get events ${res.status}`);
  return res.json();
}

export interface StartSessionPayload {
  task: string;
  workspace?: string;
  max_steps?: number;
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
}

/** B1 契约通用清单条目——{id, display_name, description}。
 *  四个清单端点（permission-modes / agent-profiles / reasoning-efforts /
 *  context-providers）共用此结构，与 /api/models 富化模式对齐。 */
export interface CatalogEntry {
  id: string;
  display_name: string;
  description: string;
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
 *  当前诚实返空数组（runtime 尚未装配任何 provider）。 */
export async function getContextProviders(): Promise<CatalogEntry[]> {
  const res = await apiFetch('/api/context-providers');
  if (!res.ok) throw new Error(`context-providers ${res.status}`);
  return parseCatalogEntries(await res.json(), 'providers');
}

/** 窄化解析清单端点响应——仅 id/display_name/description 非空字符串的条目入选。
 *  顶层 key 用复数短名（modes/profiles/efforts/providers），调用方传入对应 key。 */
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
    return [
      {
        id: r.id,
        display_name: r.display_name,
        description: typeof r.description === 'string' ? r.description : '',
      },
    ];
  });
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
  workspace: (p) => (p.workspace ? ['workspace', p.workspace] : null),
  max_steps: (p) => (p.max_steps !== undefined ? ['max_steps', p.max_steps] : null),
  auto_approve: (p) => (p.auto_approve !== undefined ? ['auto_approve', p.auto_approve] : null),
  model: (p) => (p.model ? ['model', p.model] : null),
  permission_mode: (p) => (p.permission_mode ? ['permission_mode', p.permission_mode] : null),
  agent_profile: (p) => (p.agent_profile ? ['agent_profile', p.agent_profile] : null),
  reasoning_effort: (p) => (p.reasoning_effort ? ['reasoning_effort', p.reasoning_effort] : null),
  context_providers: (p) =>
    p.context_providers && p.context_providers.length > 0 ? ['context_providers', p.context_providers] : null,
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

/** POST /api/sessions/{id}/messages（PRD §5.3 续聊入口）。
 *  后端两种响应：
 *    - launched → SSE 流（与 POST /api/sessions 同形），返回原始 Response 供 consumeSSE 消费；
 *    - queued / steered → JSON 确认（Content-Type: application/json），调用方需检查
 *      res.headers.get('content-type') 区分。
 *  空闲会话（无在途 run）→ launched 直驱新 run，返回 SSE。
 *  在途会话 → queued 入队（JSON），消息在下个 run 自然消费。 */
export interface SendMessagePayload {
  content: string;
  mode?: 'queue' | 'steer';
  max_steps?: number;
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
}

/** 续聊路径字段表——amend 四项与 START_SESSION_FIELDS 同词汇；mode / max_steps
 *  有后端默认值，故缺省在此补齐（与 create 路径「缺省即不发键」不同）。 */
const SEND_MESSAGE_FIELDS: BodyFields<SendMessagePayload> = {
  content: (p) => ['content', p.content],
  mode: (p) => ['mode', p.mode ?? 'queue'],
  max_steps: (p) => ['max_steps', p.max_steps ?? 10],
  model: (p) => (p.model ? ['model', p.model] : null),
  agent_profile: (p) => (p.agent_profile ? ['agent_profile', p.agent_profile] : null),
  reasoning_effort: (p) => (p.reasoning_effort ? ['reasoning_effort', p.reasoning_effort] : null),
  context_providers: (p) =>
    p.context_providers && p.context_providers.length > 0 ? ['context_providers', p.context_providers] : null,
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
 *  "decided" on a network error is a dangerous false positive for security. */
export async function postApproval(
  sessionId: string,
  approvalId: string,
  approved: boolean,
): Promise<{ status: string; approval_id: string; decision: string }> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      approval_id: approvalId,
      approved,
      decision: approved ? 'approve_once' : 'deny',
    }),
  });
  if (res.status === 409) throw new AlreadyResolvedError('审批已决（幂等）');
  if (!res.ok) throw new Error(`审批失败（${res.status}）`);
  return res.json();
}

// ── Models（后端契约回执 §3，T10 #103：多模型不写死，grill Q2 拍板）──

/** GET /api/models 目录条目。零密钥字段；name 是 POST /api/sessions 的选择键；
 *  思考能力不进元数据（显示侧由 reasoning 事件族驱动，有则显示无则不显示）。 */
export interface ModelCatalogEntry {
  name: string;
  provider: string | null;
  model: string | null;
  default: boolean;
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
      },
    ];
  });
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

// ── Recover（后端新端点，df4f7d8 §1.1）──

/** 恢复失败的可区分错误：status 404 = 会话不存在；409 = 存在需人工裁决的高风险
 *  操作（detail 说明原因，本期只展示，不做裁决交互——不变量 #14 不盲跑）。 */
export class RecoverError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** POST /api/sessions/{id}/recover — 幂等。200 返回该 session 全量事件数组
 *  （与 GET events 同构），调用方直接走既有 projectHistory 重建管线（不变量 #22：
 *  不引入第二套会话真相）。 */
export async function recoverSession(sessionId: string): Promise<AgentEvent[]> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/recover`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  if (res.status === 404) throw new RecoverError(404, '会话不存在');
  if (res.status === 409) {
    const detail = await readErrorDetail(res);
    throw new RecoverError(409, detail || '存在需要人工裁决的高风险操作');
  }
  if (!res.ok) throw new RecoverError(res.status, `恢复失败（${res.status}）`);
  return res.json();
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

