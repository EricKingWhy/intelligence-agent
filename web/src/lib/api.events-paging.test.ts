/** #1526 数据层契约测试：`getSessionEvents` 有界分页循环。
 *
 *  签名不变（所有重建点自动获得有界传输），循环语义：
 *  - 满 1000 页 → 以页尾 seq 续页，直到短页；
 *  - 页长 > limit（不识别参数的旧后端整包）→ 按完结处理，不发下一页；
 *  - 任何一页失败 / 帧缺 seq / 页内非严格递增 → 抛错，**不把不完整内容当
 *    完整历史**（已累积的页随异常一起丢弃）。
 *  fetch 全局 mock（与 api.replay.test.ts 同款），不发真实请求。
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { getSessionEvents, NotFoundError } from './api';

const PAGE_LIMIT = 1000;

type Frame = { seq: number; type: string };

function frame(seq: number): Frame {
  return { seq, type: 'user/message' };
}

function frames(from: number, to: number): Frame[] {
  const out: Frame[] = [];
  for (let s = from; s <= to; s += 1) out.push(frame(s));
  return out;
}

function stubPages(pages: { status?: number; body: unknown }[]) {
  const urls: string[] = [];
  let call = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      urls.push(String(url));
      const page = pages[Math.min(call, pages.length - 1)];
      call += 1;
      return new Response(JSON.stringify(page.body), { status: page.status ?? 200 });
    }),
  );
  return { urls, calls: () => call };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('getSessionEvents（#1526 有界分页）', () => {
  it('满页续页直到短页：拼接完整、after_seq 逐页前进', async () => {
    const { urls } = stubPages([
      { body: frames(0, PAGE_LIMIT - 1) },
      { body: frames(PAGE_LIMIT, PAGE_LIMIT + 7) },
    ]);

    const events = await getSessionEvents('s1');

    expect(events).toHaveLength(PAGE_LIMIT + 8);
    expect(events.map((e) => e.seq)).toEqual(frames(0, PAGE_LIMIT + 7).map((f) => f.seq));
    expect(urls).toHaveLength(2);
    expect(urls[0]).toContain('after_seq=-1');
    expect(urls[1]).toContain(`after_seq=${PAGE_LIMIT - 1}`);
    expect(urls[0]).toContain(`limit=${PAGE_LIMIT}`);
  });

  it('历史恰为整数倍页：再发一次拿空页才算完结', async () => {
    const { urls } = stubPages([
      { body: frames(0, PAGE_LIMIT - 1) },
      { body: [] },
    ]);

    const events = await getSessionEvents('s1');

    expect(events).toHaveLength(PAGE_LIMIT);
    expect(urls).toHaveLength(2);
    expect(urls[1]).toContain(`after_seq=${PAGE_LIMIT - 1}`);
  });

  it('旧后端整包回退（页长 > limit，参数被忽略）：按完结处理，单请求返回', async () => {
    const { urls } = stubPages([{ body: frames(0, 1500) }]);

    const events = await getSessionEvents('s1');

    expect(events).toHaveLength(1501);
    expect(urls).toHaveLength(1);
  });

  it('中途页失败：整体抛错，不返回半截历史', async () => {
    stubPages([
      { body: frames(0, PAGE_LIMIT - 1) },
      { status: 500, body: { detail: 'boom' } },
    ]);

    await expect(getSessionEvents('s1')).rejects.toThrow('get events 500');
  });

  it('页内 seq 非严格递增：拒绝（不把乱序内容当完整）', async () => {
    stubPages([{ body: [frame(3), frame(3), frame(4)] }]);

    await expect(getSessionEvents('s1')).rejects.toThrow('非严格递增');
  });

  it('页接缝 seq 未越过游标（后端窗口错切/重叠）：拒绝，不把重复帧拼进历史', async () => {
    const { calls } = stubPages([
      { body: frames(0, PAGE_LIMIT - 1) },
      // 后端把 after_seq 误读成含端点之类：第二页从 999 重新开始（与游标重叠）。
      { body: frames(PAGE_LIMIT - 1, PAGE_LIMIT + 5) },
    ]);

    await expect(getSessionEvents('s1')).rejects.toThrow('未越过游标');
    expect(calls()).toBe(2);
  });

  it('帧缺 seq：拒绝', async () => {
    stubPages([{ body: [{ type: 'user/message' }] }]);

    await expect(getSessionEvents('s1')).rejects.toThrow('缺 seq');
  });

  it('404 仍映射 NotFoundError（空会话语义不因分页漂移）', async () => {
    stubPages([{ status: 404, body: { detail: 'nope' } }]);

    await expect(getSessionEvents('s1')).rejects.toBeInstanceOf(NotFoundError);
  });
});
