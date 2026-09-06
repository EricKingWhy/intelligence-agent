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
