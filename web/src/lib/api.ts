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
  /** Phase 5 staged amend 字段（Ticket B1 清单端点对齐）：
   *  - permission_mode: GET /api/permission-modes 的 id；不传 = 后端默认。
   *  - agent_profile: GET /api/agent-profiles 的 id；不传 = 后端默认。
   *  - reasoning_effort: GET /api/reasoning-efforts 的 id；不传 = 后端默认。
   *  - context_providers: GET /api/context-providers 的 id 列表；不传 = 后端默认。
   *
   *  这些字段在 POST /api/sessions 是 staged 契约：API 边界验证通过（未知值 → 422），
   *  运行时记一条 INFO 日志后忽略（received but not yet consumed by runtime）。
   *  前端不应断言"已生效"——控件只提交偏好，运行时是否消费由后端决定。 */
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

/** POST a new session. Returns the raw Response — SSE stream is consumed by caller.
 *  401 throws UnauthorizedError (after broadcasting) — fail fast, no empty stream. */
export async function startSession(payload: StartSessionPayload): Promise<Response> {
  return apiFetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
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
}

export async function sendMessage(sessionId: string, payload: SendMessagePayload): Promise<Response> {
  return apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      content: payload.content,
      mode: payload.mode ?? 'queue',
      max_steps: payload.max_steps ?? 10,
    }),
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

export async function postApproval(sessionId: string, approved: boolean): Promise<unknown> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved }),
  });
  if (!res.ok) throw new Error(`approve ${res.status}`);
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
