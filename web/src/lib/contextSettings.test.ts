/** #1197 项 5B：`context-settings` 客户端契约（GET 读 / PATCH 写）。
 *
 *  形状与状态码语义照后端 `web/app.py:3524/3545` 的 docstring：
 *  200 → 生效后的完整设置（幂等）；404 → 没有这个会话；422 → 挡位非法（零写入）。
 *  错误一律走 `SessionError`（带 status），调用方据此分开讲——同
 *  `changeSessionPermission` 的既有口径。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { getContextSettings, setContextWindow, SessionError } from './api';

function capture(status = 200, body: unknown = {}) {
  const calls: { method: string; url: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({
        method: init?.method ?? 'GET',
        url: String(url),
        body:
          typeof init?.body === 'string'
            ? (JSON.parse(init.body) as Record<string, unknown>)
            : {},
      });
      return new Response(JSON.stringify(body), { status });
    }),
  );
  return calls;
}

const SETTINGS = {
  session_id: 's1',
  context_window: 200_000,
  configured_default: 200_000,
  tiers: [100_000, 200_000, 256_000, 300_000, 500_000],
  thresholds: { auto_compact: 140_000, hard_guard: 170_000 },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('getContextSettings', () => {
  it('GET /api/sessions/{id}/context-settings，原样带回 tiers / configured_default', async () => {
    const calls = capture(200, SETTINGS);
    const s = await getContextSettings('s1');
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url).toBe('/api/sessions/s1/context-settings');
    expect(s.context_window).toBe(200_000);
    expect(s.configured_default).toBe(200_000);
    expect(s.tiers).toEqual([100_000, 200_000, 256_000, 300_000, 500_000]);
    expect(s.thresholds).toEqual({ auto_compact: 140_000, hard_guard: 170_000 });
  });

  it('404 → SessionError 且带 status（调用方要能区分"会话没了"与"挡位非法"）', async () => {
    capture(404, { detail: 'session not found' });
    await expect(getContextSettings('gone')).rejects.toBeInstanceOf(SessionError);
    capture(404, { detail: 'session not found' });
    await expect(getContextSettings('gone')).rejects.toMatchObject({ status: 404 });
  });
});

describe('setContextWindow', () => {
  it('PATCH + body.context_window，回执是生效后的完整设置', async () => {
    const calls = capture(200, { ...SETTINGS, context_window: 500_000 });
    const s = await setContextWindow('s1', 500_000);
    expect(calls[0].method).toBe('PATCH');
    expect(calls[0].url).toBe('/api/sessions/s1/context-settings');
    expect(calls[0].body).toEqual({ context_window: 500_000 });
    expect(s.context_window).toBe(500_000);
  });

  it('422（挡位非法）→ SessionError 带 status 与后端 detail', async () => {
    capture(422, { detail: 'context_window 必须是 [100000, 200000] 之一，得到 12345' });
    await expect(setContextWindow('s1', 12345)).rejects.toMatchObject({
      status: 422,
    });
    capture(422, { detail: 'context_window 必须是 [100000, 200000] 之一，得到 12345' });
    await expect(setContextWindow('s1', 12345)).rejects.toThrow(/得到 12345/);
  });
});
