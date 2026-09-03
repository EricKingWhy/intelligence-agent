/** REST API client. All fetches go through here — single seam for base URL / auth headers.
 *
 * V1: same-origin (Vite proxy in dev, FastAPI static in prod). No auth header yet.
 * Multi-user seam: add AuthContext here later (Q2=A埋点).
 */

import type { AgentEvent, SessionSummary } from '../types';

const BASE = ''; // relative — Vite proxy handles /api → :8000

export async function getHealth(): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/api/health`);
  if (!res.ok) throw new Error(`health ${res.status}`);
  return res.json();
}

export async function listSessions(): Promise<SessionSummary[]> {
  const res = await fetch(`${BASE}/api/sessions`);
  if (!res.ok) throw new Error(`list sessions ${res.status}`);
  return res.json();
}

export async function getSessionEvents(sessionId: string): Promise<AgentEvent[]> {
  const res = await fetch(`${BASE}/api/sessions/${encodeURIComponent(sessionId)}/events`);
  if (!res.ok) throw new Error(`get events ${res.status}`);
  return res.json();
}

export interface StartSessionPayload {
  task: string;
  workspace?: string;
  max_steps?: number;
  auto_approve?: boolean;
}

/** POST a new session. Returns the raw Response — SSE stream is consumed by caller. */
export async function startSession(payload: StartSessionPayload): Promise<Response> {
  return fetch(`${BASE}/api/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function postApproval(sessionId: string, approved: boolean): Promise<unknown> {
  const res = await fetch(`${BASE}/api/sessions/${encodeURIComponent(sessionId)}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved }),
  });
  if (!res.ok) throw new Error(`approve ${res.status}`);
  return res.json();
}
