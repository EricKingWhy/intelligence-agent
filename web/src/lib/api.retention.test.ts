/** #368 [W-24] 证据保留清理数据层契约测试（Batch D）。
 *
 * 覆盖 web/src/lib/api.ts 三个新端点：
 *   - getSessionUsage   GET  /api/sessions/{id}/usage
 *   - previewCleanup    POST /api/sessions/{id}/cleanup/preview
 *   - executeCleanup    POST /api/sessions/{id}/cleanup/execute
 *
 * 断言分两类：① 形状防御（缺字段 / 非对象 body 给安全默认值，不抛 TypeError）；
 * ② 非 2xx 透出 SessionError（含 409 snapshot_token 过期与 403 跨源，状态码不吞）。
 * fetch 全局 mock（与 api.test.ts 同款），不发真实请求。 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  executeCleanup,
  getSessionUsage,
  previewCleanup,
  SessionError,
} from './api';

/** 捕获 fetch（url + 已解析 body），返回可配置响应。 */
function captureFetch(status = 200, body: unknown = {}) {
  const calls: { url: string; init: RequestInit | undefined; body: unknown }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({
        url: String(url),
        init,
        body: typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined,
      });
      return new Response(JSON.stringify(body), { status });
    }),
  );
  return { calls };
}

/** 非 2xx：后端 FastAPI 错误体 `{detail}`。 */
function errorFetch(status: number, detail: string) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ detail }), { status })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

async function caught(p: Promise<unknown>): Promise<SessionError> {
  return (await p.then(
    () => {
      throw new Error('should have thrown');
    },
    (e: unknown) => e,
  )) as SessionError;
}

describe('#368 getSessionUsage — 形状防御', () => {
  it('200 合法 body：逐字段读出，请求打向 /usage', async () => {
    const { calls } = captureFetch(200, {
      events_bytes: 2048,
      artifacts_bytes: 4096,
      progress_bytes: 512,
      artifact_count: 3,
      reclaimable_bytes: 3072,
      computed_at: '2026-10-07T10:00:00Z',
    });
    const usage = await getSessionUsage('s 1');
    expect(calls[0].url).toBe('/api/sessions/s%201/usage');
    expect(usage).toEqual({
      events_bytes: 2048,
      artifacts_bytes: 4096,
      progress_bytes: 512,
      artifact_count: 3,
      reclaimable_bytes: 3072,
      computed_at: '2026-10-07T10:00:00Z',
    });
  });

  it('缺字段 / 类型不对 / 非对象 body → 安全默认值（数字 0、字符串 ""），不抛', async () => {
    captureFetch(200, { events_bytes: 'nope', artifact_count: null });
    const usage = await getSessionUsage('s1');
    expect(usage).toEqual({
      events_bytes: 0,
      artifacts_bytes: 0,
      progress_bytes: 0,
      artifact_count: 0,
      reclaimable_bytes: 0,
      computed_at: '',
    });
  });

  it('非 2xx → SessionError（status + 后端 detail 原样透出）', async () => {
    errorFetch(500, '读取存储占用失败：内部错误');
    const err = await caught(getSessionUsage('s1'));
    expect(err).toBeInstanceOf(SessionError);
    expect(err.status).toBe(500);
    expect(err.message).toBe('读取存储占用失败：内部错误');
  });
});

describe('#368 previewCleanup — 形状防御与请求体', () => {
  it('200：解析 affected / blocked / evidence_invalidated / reclaimable_bytes；默认 mode=unreferenced', async () => {
    const { calls } = captureFetch(200, {
      snapshot_token: 'tok-1',
      affected: [
        { artifact_ref: 'art_a', size: 1024, referenced_by: ['ev-1', 'ev-2'] },
      ],
      evidence_invalidated: ['ev-1'],
      reclaimable_bytes: 1024,
      blocked: [{ artifact_ref: 'art_b', reason: 'active_task' }],
    });
    const preview = await previewCleanup('s1');
    expect(calls[0].url).toBe('/api/sessions/s1/cleanup/preview');
    expect(calls[0].body).toEqual({ mode: 'unreferenced' });
    expect(preview.snapshot_token).toBe('tok-1');
    expect(preview.affected).toEqual([
      { artifact_ref: 'art_a', size: 1024, referenced_by: ['ev-1', 'ev-2'] },
    ]);
    expect(preview.evidence_invalidated).toEqual(['ev-1']);
    expect(preview.reclaimable_bytes).toBe(1024);
    expect(preview.blocked).toEqual([{ artifact_ref: 'art_b', reason: 'active_task' }]);
  });

  it('mode 可显式传入并进请求体', async () => {
    const { calls } = captureFetch(200, {});
    await previewCleanup('s1', 'unreferenced');
    expect(calls[0].body).toEqual({ mode: 'unreferenced' });
  });

  it('缺字段 / 非对象 body → 数组 []、字符串 ""、数字 0', async () => {
    captureFetch(200, { snapshot_token: 42, affected: 'bad', blocked: null });
    const preview = await previewCleanup('s1');
    expect(preview).toEqual({
      snapshot_token: '',
      affected: [],
      evidence_invalidated: [],
      reclaimable_bytes: 0,
      blocked: [],
    });
  });

  it('affected/blocked 条目字段缺失 → 逐条安全默认值', async () => {
    captureFetch(200, {
      affected: [{ size: 10, referenced_by: 'x' }],
      blocked: [{ reason: 7 }],
    });
    const preview = await previewCleanup('s1');
    expect(preview.affected).toEqual([{ artifact_ref: '', size: 10, referenced_by: [] }]);
    expect(preview.blocked).toEqual([{ artifact_ref: '', reason: '' }]);
  });

  it('409（snapshot_token 过期）→ SessionError.status=409，detail 透出给调用方', async () => {
    errorFetch(409, '清理预览已过期，请重新预览');
    const err = await caught(previewCleanup('s1'));
    expect(err).toBeInstanceOf(SessionError);
    expect(err.status).toBe(409);
    expect(err.message).toBe('清理预览已过期，请重新预览');
  });
});

describe('#368 executeCleanup — 形状防御与请求体', () => {
  it('200：解析 deleted / failed / not_deleted；请求体含 snapshot_token + artifact_refs', async () => {
    const { calls } = captureFetch(200, {
      deleted: ['art_a'],
      failed: ['art_c'],
      not_deleted: [{ artifact_ref: 'art_b', reason: 'referenced' }],
    });
    const result = await executeCleanup('s1', 'tok-1', ['art_a', 'art_b', 'art_c']);
    expect(calls[0].url).toBe('/api/sessions/s1/cleanup/execute');
    expect(calls[0].body).toEqual({
      snapshot_token: 'tok-1',
      artifact_refs: ['art_a', 'art_b', 'art_c'],
    });
    expect(result).toEqual({
      deleted: ['art_a'],
      failed: ['art_c'],
      not_deleted: [{ artifact_ref: 'art_b', reason: 'referenced' }],
    });
  });

  it('缺字段 / 非对象 body → 三个字段都给安全默认值', async () => {
    captureFetch(200, { deleted: 'nope', not_deleted: 5 });
    const result = await executeCleanup('s1', 'tok-1', []);
    expect(result).toEqual({ deleted: [], failed: [], not_deleted: [] });
  });

  it('403（跨源）→ SessionError.status=403，detail 透出', async () => {
    errorFetch(403, '非本机来源不可执行清理');
    const err = await caught(executeCleanup('s1', 'tok-1', ['art_a']));
    expect(err).toBeInstanceOf(SessionError);
    expect(err.status).toBe(403);
    expect(err.message).toBe('非本机来源不可执行清理');
  });
});
