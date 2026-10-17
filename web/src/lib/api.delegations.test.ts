/** #1256 `getSessionDelegations` 契约测试。
 *
 *  与 `getSessionEvents` 同款错误分支：404 → `NotFoundError`（父会话不存在），
 *  其余非 2xx → 普通 Error（不透传 detail）。路径是 roster 的冷启动数据面，
 *  URL 必须 encodeURIComponent 拼接（session id 可含需转义字符）。
 *  fetch 全局 mock（与 api.test.ts 同款），不发真实请求。
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { getSessionDelegations, NotFoundError, type DelegationEntry } from './api';

function captureFetch(status = 200, body: unknown = {}) {
  const calls: { url: string }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      calls.push({ url: String(url) });
      return new Response(JSON.stringify(body), { status });
    }),
  );
  return { calls };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const SAMPLE: DelegationEntry = {
  child_session_id: 'c1',
  target: 'research_review',
  task: '审一下这个改动',
  status: 'completed',
  started_at: '2026-10-16T00:00:00.000Z',
  completed_at: '2026-10-16T00:12:18.000Z',
  summary: 'OK',
  used_tokens: 12842,
  usage_state: 'usage_only',
};

describe('#1256 getSessionDelegations', () => {
  it('GET /api/sessions/{id}/delegations，按后端契约形状原样返回', async () => {
    const { calls } = captureFetch(200, [SAMPLE]);
    const rows = await getSessionDelegations('s1');
    expect(calls[0].url).toBe('/api/sessions/s1/delegations');
    expect(rows).toEqual([SAMPLE]);
  });

  it('空 roster：原样返回空数组（不伪造行）', async () => {
    captureFetch(200, []);
    expect(await getSessionDelegations('s1')).toEqual([]);
  });

  it('session id 需转义字符走 encodeURIComponent', async () => {
    const { calls } = captureFetch(200, []);
    await getSessionDelegations('a/b c');
    expect(calls[0].url).toBe('/api/sessions/a%2Fb%20c/delegations');
  });

  it('404 → NotFoundError（父会话不存在，与 getSessionEvents 同款）', async () => {
    captureFetch(404, { detail: 'session not found' });
    await expect(getSessionDelegations('missing')).rejects.toBeInstanceOf(NotFoundError);
  });

  it('其它非 2xx → 普通 Error（不透传 detail）', async () => {
    captureFetch(500, { detail: 'boom' });
    await expect(getSessionDelegations('s1')).rejects.toThrow('get delegations 500');
  });
});
