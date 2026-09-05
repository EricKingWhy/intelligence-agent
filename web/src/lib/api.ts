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

/** Single request seam: auth header injection + 401 interception. */
async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (res.status === 401) {
    const detail = await res
      .json()
      .then((j) => (j && typeof j.detail === 'string' ? j.detail : ''))
      .catch(() => '');
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

export async function postApproval(sessionId: string, approved: boolean): Promise<unknown> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved }),
  });
  if (!res.ok) throw new Error(`approve ${res.status}`);
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
    const detail = await res
      .json()
      .then((j) => (j && typeof j.detail === 'string' ? j.detail : ''))
      .catch(() => '');
    throw new RecoverError(409, detail || '存在需要人工裁决的高风险操作');
  }
  if (!res.ok) throw new RecoverError(res.status, `恢复失败（${res.status}）`);
  return res.json();
}
