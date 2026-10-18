/** #1322 数据层契约测试：`replaySession`（`GET /api/sessions/{id}/replay`）。
 *
 *  后端形状以 `src/agent_harness/web/lineage.py::replay_session` 为准：
 *  `{session_id, count, steps:[{seq,type,line}]}`。fetch 全局 mock（与 api.test.ts
 *  同款），不发真实请求。
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { replaySession, SessionError } from './api';

function captureFetch(status = 200, body: unknown = {}) {
  const calls: { url: string; method: string | undefined }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), method: init?.method });
      return new Response(JSON.stringify(body), { status });
    }),
  );
  return { calls };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('replaySession', () => {
  it('GET 端点并解析 steps（seq / type / line）', async () => {
    const { calls } = captureFetch(200, {
      session_id: 's1',
      count: 2,
      steps: [
        { seq: 2, type: 'user/message', line: '[user] 列出文件' },
        { seq: 5, type: 'model/completed', line: '[assistant] 有两个文件' },
      ],
    });

    const result = await replaySession('s1');

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain('/api/sessions/s1/replay');
    expect(calls[0].method).toBe('GET');
    expect(result.session_id).toBe('s1');
    expect(result.count).toBe(2);
    expect(result.steps).toHaveLength(2);
    expect(result.steps[0]).toEqual({ seq: 2, type: 'user/message', line: '[user] 列出文件' });
  });

  it('id 做 URL 编码（路径穿越防线的前端一半）', async () => {
    const { calls } = captureFetch(200, { session_id: 'x', count: 0, steps: [] });
    await replaySession('a b/c');
    expect(calls[0].url).toContain('/api/sessions/a%20b%2Fc/replay');
  });

  it('非 2xx → SessionError（message = 后端 detail）', async () => {
    captureFetch(404, { detail: "session 'ghost' not found" });
    await expect(replaySession('ghost')).rejects.toBeInstanceOf(SessionError);
    await expect(replaySession('ghost')).rejects.toThrow("session 'ghost' not found");
  });

  it('形状防御：缺 steps / 非对象 → 空列表，不抛 TypeError', async () => {
    captureFetch(200, null);
    const result = await replaySession('s1');
    expect(result).toEqual({ session_id: 's1', count: 0, steps: [] });
  });

  it('形状防御：steps 里的脏条目被过滤掉', async () => {
    captureFetch(200, {
      session_id: 's1',
      count: 3,
      steps: [
        { seq: 1, type: 'user/message', line: 'ok' },
        { seq: 2, type: 'model/completed' }, // 缺 line → 丢弃
        'garbage',
      ],
    });
    const result = await replaySession('s1');
    expect(result.steps).toEqual([{ seq: 1, type: 'user/message', line: 'ok' }]);
    // count 以后端给的为准（这里后端说 3，前端不替它改口）
    expect(result.count).toBe(3);
  });
});
