/** api.ts 契约测试——getModels 窄化解析（T10 #103，契约 C6）+ 续聊 amend 透传（Q2）。
 *  fetch 全局 mock；auth.getToken 在 node 下走 try/catch 兜底（无 localStorage）。 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  attachSessionToProject,
  createProject,
  deleteProject,
  detachSessionFromProject,
  getModels,
  getSessionEvents,
  listProjects,
  listSessions,
  NotFoundError,
  ProjectError,
  UnauthorizedError,
  AlreadyResolvedError,
  postApproval,
  renameProject,
  reorderProjectSession,
  sendMessage,
  startSession,
} from './api';
import { onUnauthorized } from './auth';
import type { Project, SessionSummary } from '../types';

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

describe('listSessions — SessionSummary 契约（ARCH-4b：trace_url / WS-3 #153：workspace）', () => {
  /** 后端 `GET /api/sessions` 一行的 canonical 形状，与
   *  `src/agent_harness/web/app.py::SessionSummary` 逐字段对齐。
   *
   *  两件事由**类型注解**在编译期锁住（无需运行时断言）：
   *  ① 类型新增必填字段 → 本 fixture 缺键 → `tsc -b` 红；
   *  ② fixture 多出类型没声明的键 → 对象字面量多余属性检查 → 红。
   *  后端侧权威锁（断言**值**，能抓住「键在但值是 null」的漏映射）：
   *  `tests/test_web_api.py::test_list_sessions_carries_terminal_trace_url`
   *  与 `tests/web/test_session_list_workspace.py::
   *  test_rows_carry_real_workspace_and_ungrouped_is_null`。
   */
  const CANONICAL_ROW: SessionSummary = {
    session_id: 's1',
    event_count: 6,
    first_event_time: '2026-09-07T00:00:00Z',
    last_event_time: '2026-09-07T00:00:00Z',
    first_user_message: '标题',
    trace_id: 'tr-1',
    trace_url: 'https://lf.example/trace/tr-1',
    workspace: { id: 'w1', title: '项目甲' },
  };

  it('原样保留 trace_url（fetch 层不重排/不丢键/不重命名）', async () => {
    captureFetch(200, [CANONICAL_ROW]);
    const rows = await listSessions();
    expect(rows[0].trace_url).toBe('https://lf.example/trace/tr-1');
  });

  it('未追踪会话：trace_url 保持 null（不伪造占位串，不变量 #21）', async () => {
    captureFetch(200, [{ ...CANONICAL_ROW, trace_id: null, trace_url: null }]);
    const rows = await listSessions();
    expect(rows[0].trace_id).toBeNull();
    expect(rows[0].trace_url).toBeNull();
  });

  it('原样保留 workspace（嵌套对象不被 fetch 层拍平/改名）', async () => {
    captureFetch(200, [CANONICAL_ROW]);
    const rows = await listSessions();
    expect(rows[0].workspace).toEqual({ id: 'w1', title: '项目甲' });
  });

  it('未分组会话：workspace 保持 null（绝不伪造项目，不变量 #21）', async () => {
    captureFetch(200, [{ ...CANONICAL_ROW, workspace: null }]);
    const rows = await listSessions();
    expect(rows[0].workspace).toBeNull();
  });
});

describe('startSession — create 路径的有值才带（归一化单一执行点）', () => {  it('全空控制字段 → payload 只有 task + 显式传入的 max_steps/auto_approve', async () => {
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

/** OBS-015：postApproval 必须区分幂等已决（409）与真失败（5xx/网络）。
 *  幂等 → AlreadyResolvedError（调用方翻卡片）；真失败 → 普通 Error（保持 pending）。 */
describe('postApproval — 409 幂等 vs 500 真失败（OBS-015）', () => {
  it('200 → 返回 resolved 结果', async () => {
    captureFetch(200, { status: 'resolved', approval_id: 'ap-1', decision: 'approve_once' });
    const result = await postApproval('s1', 'ap-1', true);
    expect(result).toEqual({ status: 'resolved', approval_id: 'ap-1', decision: 'approve_once' });
  });

  it('409 → 抛 AlreadyResolvedError（调用方据此翻卡片为幂等成功）', async () => {
    captureFetch(409, { detail: 'Approval already resolved' });
    const err = await postApproval('s1', 'ap-1', true).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(AlreadyResolvedError);
  });

  it('500 → 抛普通 Error（不是 AlreadyResolvedError）', async () => {
    captureFetch(500, { detail: 'Internal Server Error' });
    const err = await postApproval('s1', 'ap-1', true).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(AlreadyResolvedError);
  });

  it('422 → 抛普通 Error（无效决策，不是幂等成功）', async () => {
    captureFetch(422, { detail: 'Invalid decision' });
    const err = await postApproval('s1', 'ap-1', true).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(AlreadyResolvedError);
  });
});


// ── Projects（WS-4 / #154 契约，WS-5 / #155 前端消费）──

/** 项目端点用：记录 method + url + body，并可回一个带 detail 的错误体。
 *  （与顶部 captureFetch 的区别：那个不带 method，而本组要断言动词与子路径。） */
function captureProjectFetch(
  status = 200,
  body: unknown = {},
): { calls: { method: string; url: string; body: Record<string, unknown> }[] } {
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
  return { calls };
}

describe('projects — WS-4 端点契约（#154；窄化解析 + 状态码归类）', () => {
  /** 后端 `web/projects.py::Project` 的 canonical 形状。
   *  类型注解在编译期锁一致性（与 SessionSummary 同一手法）：后端加必填字段、
   *  或此处多出类型未声明的键 → `tsc -b` 红。 */
  const CANONICAL_PROJECT: Project = {
    id: 'p1',
    path: 'D:/repos/alpha',
    title: 'alpha',
    status: 'ok',
    session_ids: ['s1', 's2'],
    created_at: '2026-09-12T00:00:00Z',
    updated_at: '2026-09-12T00:00:00Z',
  };

  it('listProjects：解析全部字段，账本序原样保留（前端不重排）', async () => {
    captureProjectFetch(200, [
      CANONICAL_PROJECT,
      { ...CANONICAL_PROJECT, id: 'p2', session_ids: [] },
    ]);
    const projects = await listProjects();
    expect(projects.map((p) => p.id)).toEqual(['p1', 'p2']);
    expect(projects[0].session_ids).toEqual(['s1', 's2']);
    expect(projects[0].path).toBe('D:/repos/alpha');
  });

  it('listProjects：畸形条目剔除、其余保留（一条坏数据不得让整列表消失）', async () => {
    captureProjectFetch(200, [
      { id: 'ok', path: 'D:/x', title: 'x', session_ids: ['s1'], status: 'ok' },
      { path: 'D:/no-id', title: 'no id' }, // 缺 id → 剔除
      { id: 'no-path', title: 'no path' }, // 缺 path → 剔除
      { id: 'no-title', path: 'D:/y' }, // 缺 title → 剔除
      'garbage',
      null,
    ]);
    const projects = await listProjects();
    expect(projects.map((p) => p.id)).toEqual(['ok']);
  });

  it('listProjects：session_ids 非字符串元素过滤；缺失 → 空账本', async () => {
    captureProjectFetch(200, [
      { id: 'p1', path: 'D:/x', title: 'x', session_ids: ['a', 7, null, '', 'b'] },
      { id: 'p2', path: 'D:/y', title: 'y' },
    ]);
    const projects = await listProjects();
    expect(projects[0].session_ids).toEqual(['a', 'b']);
    expect(projects[1].session_ids).toEqual([]);
  });

  it('listProjects：status 只认 missing-dir，未知值按 ok（告警标志不得整条丢项目）', async () => {
    captureProjectFetch(200, [
      { ...CANONICAL_PROJECT, id: 'miss', status: 'missing-dir' },
      { ...CANONICAL_PROJECT, id: 'weird', status: 'something-else' },
      { ...CANONICAL_PROJECT, id: 'absent' },
    ]);
    const projects = await listProjects();
    expect(projects.map((p) => p.status)).toEqual(['missing-dir', 'ok', 'ok']);
  });

  it('listProjects：顶层不是数组 → 空数组（降级为"没有项目"，会话仍全部可见）', async () => {
    captureProjectFetch(200, { projects: [] });
    expect(await listProjects()).toEqual([]);
  });

  it('createProject：只带 path（无标题）→ body 不含 title 键', async () => {
    const { calls } = captureProjectFetch(200, CANONICAL_PROJECT);
    await createProject('D:/repos/alpha');
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toBe('/api/projects');
    expect(calls[0].body).toEqual({ path: 'D:/repos/alpha' });
  });

  it('createProject：带标题 → body 含 title；返回实体原样解析', async () => {
    const { calls } = captureProjectFetch(200, { ...CANONICAL_PROJECT, title: '自定义' });
    const p = await createProject('D:/repos/alpha', '自定义');
    expect(calls[0].body).toEqual({ path: 'D:/repos/alpha', title: '自定义' });
    expect(p.title).toBe('自定义');
  });

  it('createProject：路径不存在（404）→ ProjectError(404) 且带后端 detail', async () => {
    captureProjectFetch(404, { detail: '目录不存在' });
    const err = await createProject('D:/nope').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ProjectError);
    expect((err as ProjectError).status).toBe(404);
    expect((err as ProjectError).message).toBe('目录不存在');
  });

  it('createProject：非绝对路径（422）→ ProjectError(422)，不被当成"路径不存在"', async () => {
    captureProjectFetch(422, { detail: 'path must be an absolute path' });
    const err = await createProject('relative/dir').catch((e: unknown) => e);
    expect((err as ProjectError).status).toBe(422);
    expect((err as ProjectError).message).toBe('path must be an absolute path');
  });

  it('createProject：Pydantic 422（detail 是数组）→ 取出 msg 并剥掉 "Value error, " 前缀', async () => {
    // 真后端对非绝对路径走的是**请求体校验器**，FastAPI 回的 detail 不是字符串而是
    // 数组。只认字符串会把最该看懂的一条提示降级成"注册项目失败（422）"。
    captureProjectFetch(422, {
      detail: [
        {
          type: 'value_error',
          loc: ['body', 'path'],
          msg: "Value error, path must be an absolute path: 'relative/dir'",
        },
      ],
    });
    const err = await createProject('relative/dir').catch((e: unknown) => e);
    expect((err as ProjectError).status).toBe(422);
    expect((err as ProjectError).message).toBe(
      "path must be an absolute path: 'relative/dir'",
    );
  });

  it('deleteProject：200 但 body 是 null → 不抛 TypeError，按"没给回执"处理', async () => {
    captureProjectFetch(200, null);
    const result = await deleteProject('p1');
    expect(result.id).toBe('p1');
    expect(result.deleted).toBe(false);
    expect(result.sessions_detached).toBe(0);
    expect(result.detail).toBe('');
  });

  it('renameProject：PATCH /api/projects/{id} + body.title', async () => {
    const { calls } = captureProjectFetch(200, { ...CANONICAL_PROJECT, title: '改名' });
    const p = await renameProject('p1', '改名');
    expect(calls[0].method).toBe('PATCH');
    expect(calls[0].url).toBe('/api/projects/p1');
    expect(calls[0].body).toEqual({ title: '改名' });
    expect(p.title).toBe('改名');
  });

  it('deleteProject：DELETE + 逐字保留后端 detail（AC5 的那句"会话没被删"）', async () => {
    const detail = '项目「甲」已从注册表移除，2 个会话回到未分组。目录、用户文件与会话日志均未删除（软删除，可重新注册同一目录）。';
    const { calls } = captureProjectFetch(200, {
      id: 'p1',
      deleted: true,
      sessions_detached: 2,
      detail,
    });
    const result = await deleteProject('p1');
    expect(calls[0].method).toBe('DELETE');
    expect(calls[0].url).toBe('/api/projects/p1');
    expect(result.sessions_detached).toBe(2);
    expect(result.detail).toBe(detail);
  });

  it('deleteProject：detail 缺失 → 空串（调用方据此跳过提示，不编文案）', async () => {
    captureProjectFetch(200, { id: 'p1', deleted: true });
    const result = await deleteProject('p1');
    expect(result.detail).toBe('');
    expect(result.sessions_detached).toBe(0);
  });

  it('attach：POST /api/projects/{id}/sessions，body.session_id', async () => {
    const { calls } = captureProjectFetch(200, CANONICAL_PROJECT);
    await attachSessionToProject('p1', 's9');
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toBe('/api/projects/p1/sessions');
    expect(calls[0].body).toEqual({ session_id: 's9' });
  });

  it('attach：cwd 不一致（409）→ ProjectError(409)，与"不存在"（404）分开', async () => {
    captureProjectFetch(409, { detail: '会话 cwd 与项目路径不一致' });
    const err = await attachSessionToProject('p1', 's9').catch((e: unknown) => e);
    expect((err as ProjectError).status).toBe(409);
    expect((err as ProjectError).message).toBe('会话 cwd 与项目路径不一致');
  });

  it('detach：DELETE 子路径（id / session_id 都编码）', async () => {
    const { calls } = captureProjectFetch(200, CANONICAL_PROJECT);
    await detachSessionFromProject('p 1', 's/9');
    expect(calls[0].method).toBe('DELETE');
    expect(calls[0].url).toBe('/api/projects/p%201/sessions/s%2F9');
  });

  it('reorder：POST .../order，before=null（追加队尾）与具体锚点都原样透传', async () => {
    const { calls } = captureProjectFetch(200, CANONICAL_PROJECT);
    await reorderProjectSession('p1', 's2', null);
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toBe('/api/projects/p1/sessions/s2/order');
    expect(calls[0].body).toEqual({ before: null });

    const second = captureProjectFetch(200, CANONICAL_PROJECT);
    await reorderProjectSession('p1', 's2', 's1');
    expect(second.calls[0].body).toEqual({ before: 's1' });
  });

  it('错误体不是 JSON / 无 detail → 兜底文案带状态码（错误路径自身不再抛错）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('<html>502</html>', { status: 502 })),
    );
    const err = await listProjects().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ProjectError);
    expect((err as ProjectError).status).toBe(502);
    expect((err as ProjectError).message).toContain('502');
  });
});
