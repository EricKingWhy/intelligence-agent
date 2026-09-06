/** api.ts 契约测试——getModels 窄化解析（T10 #103，契约 C6）。
 *  fetch 全局 mock；auth.getToken 在 node 下走 try/catch 兜底（无 localStorage）。 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { getModels } from './api';

function mockFetchOnce(status: number, body: unknown): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(body), { status })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('getModels — 模型目录窄化解析（#103，零伪造）', () => {
  it('200 合法 body：提取条目，name 为选择键', async () => {
    mockFetchOnce(200, {
      models: [
        { name: 'deepseek-v4-flash-0731', provider: 'senseaudio', model: 'deepseek-v4-flash-0731', default: true },
        { name: 'qwen-max', provider: 'senseaudio', model: 'qwen3.8-max-0902', default: false },
      ],
    });
    const models = await getModels();
    expect(models).toHaveLength(2);
    expect(models[0]).toEqual({ name: 'deepseek-v4-flash-0731', provider: 'senseaudio', model: 'deepseek-v4-flash-0731', default: true });
    expect(models[1].model).toBe('qwen3.8-max-0902');
  });

  it('畸形条目剔除（name 缺失/非对象），零伪造；可选字段缺失记 null', async () => {
    mockFetchOnce(200, {
      models: [
        { name: 'ok-model' },
        { provider: 'x', model: 'y', default: true }, // 无 name → 剔除
        'garbage', // 非对象 → 剔除
        null,
      ],
    });
    const models = await getModels();
    expect(models).toEqual([{ name: 'ok-model', provider: null, model: null, default: false }]);
  });

  it('models 数组缺失/形状不符 → 空数组（入口降级隐藏，不伪造列表）', async () => {
    mockFetchOnce(200, {});
    expect(await getModels()).toEqual([]);
    mockFetchOnce(200, { models: 'not-an-array' });
    expect(await getModels()).toEqual([]);
  });

  it('非 2xx → 抛错（调用方降级隐藏入口）', async () => {
    mockFetchOnce(500, {});
    await expect(getModels()).rejects.toThrow('models 500');
  });
});
