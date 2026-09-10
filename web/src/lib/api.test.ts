/** api.ts 契约测试——getModels 窄化解析（T10 #103，契约 C6）+ 续聊 amend 透传（Q2）。
 *  fetch 全局 mock；auth.getToken 在 node 下走 try/catch 兜底（无 localStorage）。 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { getModels, getSessionEvents, NotFoundError, UnauthorizedError, sendMessage, startSession } from './api';
import { onUnauthorized } from './auth';

/** 捕获 fetch 调用（url + 已解析 body）并返回可配置响应——请求体契约断言用。 */
function captureFetch(
  status = 200,
  body: unknown = {},
): {
  calls: { url: string; body: Record<string, unknown> }[];
} {
  const calls: { url: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({
        url: String(url),
        body:
          typeof init?.body === 'string'
            ? (JSON.parse(init.body) as Record<string, unknown>)
            : {},
      });
      return new Response(JSON.stringify(body), { status });
    }),
  );
  return { calls };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('getModels — 模型目录窄化解析（#103，零伪造）', () => {
  it('200 合法 body：提取条目，name 为选择键', async () => {
    captureFetch(200, {
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
    captureFetch(200, {
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
    captureFetch(200, {});
    expect(await getModels()).toEqual([]);
    captureFetch(200, { models: 'not-an-array' });
    expect(await getModels()).toEqual([]);
  });

  it('非 2xx → 抛错（调用方降级隐藏入口）', async () => {
    captureFetch(500, {});
    await expect(getModels()).rejects.toThrow('models 500');
  });
});

describe('sendMessage — 续聊 amend 透传（Q2：有值才带键）', () => {
  it('四项 amend 全有值 → payload 带全部键（端点路径不变）', async () => {
    const cap = captureFetch();
    await sendMessage('s1', {
      content: '继续',
      max_steps: 10,
      model: 'glm-4.5',
      agent_profile: 'coding',
      reasoning_effort: 'deep',
      context_providers: ['memory', 'skills'],
    });
    expect(cap.calls).toHaveLength(1);
    expect(cap.calls[0].url).toBe('/api/sessions/s1/messages');
    expect(cap.calls[0].body).toEqual({
      content: '继续',
      mode: 'queue',
      max_steps: 10,
      model: 'glm-4.5',
      agent_profile: 'coding',
      reasoning_effort: 'deep',
      context_providers: ['memory', 'skills'],
    });
  });

  it('全空 → payload 不含任何 amend 键（缺省不传键，保持干净）', async () => {
    const cap = captureFetch();
    await sendMessage('s1', { content: '继续' });
    const body = cap.calls[0].body;
    expect(body).toEqual({ content: '继续', mode: 'queue', max_steps: 10 });
    for (const key of ['model', 'agent_profile', 'reasoning_effort', 'context_providers']) {
      expect(body).not.toHaveProperty(key);
    }
  });

  it('context_providers 空数组 → 不发键（与 create 分支同模式：空 = 后端默认全集）', async () => {
    const cap = captureFetch();
    await sendMessage('s1', { content: '继续', context_providers: [] });
    expect(cap.calls[0].body).not.toHaveProperty('context_providers');
  });

  it('部分有值 → 只带该键，其余不发', async () => {
    const cap = captureFetch();
    await sendMessage('s1', { content: '继续', model: 'glm-4.5' });
    const body = cap.calls[0].body;
    expect(body.model).toBe('glm-4.5');
    expect(body).not.toHaveProperty('agent_profile');
    expect(body).not.toHaveProperty('reasoning_effort');
    expect(body).not.toHaveProperty('context_providers');
  });

  it('显式 mode / max_steps 覆盖缺省值', async () => {
    const cap = captureFetch();
    await sendMessage('s1', { content: '继续', mode: 'steer', max_steps: 3 });
    expect(cap.calls[0].body).toMatchObject({ mode: 'steer', max_steps: 3 });
  });
});

describe('startSession — create 路径的有值才带（归一化单一执行点）', () => {
  it('全空控制字段 → payload 只有 task + 显式传入的 max_steps/auto_approve', async () => {
    const cap = captureFetch();
    await startSession({ task: '干活', max_steps: 10, auto_approve: true });
    expect(cap.calls[0].url).toBe('/api/sessions');
    expect(cap.calls[0].body).toEqual({ task: '干活', max_steps: 10, auto_approve: true });
    for (const key of [
      'workspace',
      'model',
      'permission_mode',
      'agent_profile',
      'reasoning_effort',
      'context_providers',
    ]) {
      expect(cap.calls[0].body).not.toHaveProperty(key);
    }
  });

  it('context_providers 空数组 → 不发键（空 = 后端默认全集，不是显式零）', async () => {
    const cap = captureFetch();
    await startSession({ task: 't', context_providers: [] });
    expect(cap.calls[0].body).not.toHaveProperty('context_providers');
  });

  it('五项控制字段全有值 → 全部带键（含 permission_mode，create 路径独有）', async () => {
    const cap = captureFetch();
    await startSession({
      task: 't',
      model: 'glm-4.5',
      permission_mode: 'auto',
      agent_profile: 'coding',
      reasoning_effort: 'deep',
      context_providers: ['memory'],
    });
    expect(cap.calls[0].body).toMatchObject({
      model: 'glm-4.5',
      permission_mode: 'auto',
      agent_profile: 'coding',
      reasoning_effort: 'deep',
      context_providers: ['memory'],
    });
  });
});

describe('getSessionEvents — 404 归类为 NotFoundError（BUG-005 陈旧会话自愈）', () => {
  it('404 → 抛 NotFoundError（调用方据此清持久化键 + 静默回空态，不弹错误）', async () => {
    captureFetch(404, { detail: 'session not found' });
    await expect(getSessionEvents('gone')).rejects.toBeInstanceOf(NotFoundError);
  });

  it('500 → 抛普通 Error（真正的故障仍要显示错误横幅，不能被当成「会话已删除」吞掉）', async () => {
    captureFetch(500, { detail: 'boom' });
    const err = await getSessionEvents('x').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(NotFoundError);
  });

  it('200 → 原样返回事件数组', async () => {
    const events = [{ seq: 1, type: 'run/started' }];
    captureFetch(200, events);
    await expect(getSessionEvents('ok')).resolves.toEqual(events);
  });
});

/** auth_seam 的前端对侧（`lib/auth.ts` 文档：配了 JWT_SECRET 的部署匿名 → 401）。
 *  401 必须**同时**做两件事：分类成 UnauthorizedError（调用方据此走鉴权引导，
 *  而不是当成普通故障），并广播 detail（App 横幅据此显示）。两者此前都无单测；
 *  本组补齐——`e2e/l-auth-banner.spec.ts` 只覆盖广播之后的 UI，不覆盖这一层。 */
describe('401 → UnauthorizedError + onUnauthorized 广播', () => {
  it('401 → 抛 UnauthorizedError（既不是 NotFoundError，也不是普通 Error）', async () => {
    captureFetch(401, { detail: 'Missing identity token' });
    const err = await getSessionEvents('s').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(UnauthorizedError);
    expect(err).not.toBeInstanceOf(NotFoundError);
  });

  it('401 → 广播后端 detail（App 订阅后据此显示引导横幅）', async () => {
    const seen: string[] = [];
    const off = onUnauthorized((d) => seen.push(d));
    captureFetch(401, { detail: 'Missing identity token' });
    await expect(getSessionEvents('s')).rejects.toBeInstanceOf(UnauthorizedError);
    off();
    expect(seen).toEqual(['Missing identity token']);
  });

  it('401 且 body 不是 JSON → 广播回退文案（readErrorDetail 失败不得吞掉广播）', async () => {
    const seen: string[] = [];
    const off = onUnauthorized((d) => seen.push(d));
    vi.stubGlobal('fetch', vi.fn(async () => new Response('not json', { status: 401 })));
    await expect(getSessionEvents('s')).rejects.toBeInstanceOf(UnauthorizedError);
    off();
    expect(seen).toEqual(['Missing identity token']);
  });
});
