/** #1195 — Composer 内联 `/` 命令菜单：注册表与触发逻辑（纯函数层）。
 *
 * 判据逐条对票面 §四：
 *  - 注册表 5 条（goal/plan/compact/permission/model），`/export` `/feedback` `/file` 不做；
 *  - 触发正则 `/(^|\s)\/(\w*)$/`（行首或空格后半角 `/`，全角不触发——`\w` 不含 `／`）;
 *  - 带参提交拦截 `^\/(\w+)(?:\s+([\s\S]+))?$`；
 *  - fuzzy 复用 `fuzzyScore`（不另起一套，票面铁律）；
 *  - mode 投影 last-wins（与后端 `effective_workflow_mode` 同语义）。
 */

import { describe, expect, it, vi } from 'vitest';
import {
  BUILTIN_RUNS,
  slashTriggerMatch,
  slashSubmitMatch,
  filterSlashCommands,
  currentWorkflowMode,
  slashDisplayName,
  type SlashCommand,
  type SlashCommandContext,
  type SlashRegistryEntry,
} from './slashCommands';
import { postSessionCommand, SessionError, type TaskState } from './api';

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>();
  return {
    ...actual,
    postSessionCommand: vi.fn(),
    getTaskState: vi.fn(),
  };
});

describe('#1200 注册表 — 元数据在后端，前端只有 run 回调', () => {
  it('恰好 5 条 builtin 执行回调（名字与 #1195 定稿表一致）', () => {
    expect(Object.keys(BUILTIN_RUNS).sort()).toEqual([
      'compact',
      'goal',
      'model',
      'permission',
      'plan',
    ]);
  });

  it('前端不再维护 label / description / group 元数据（D2=A）', () => {
    for (const run of Object.values(BUILTIN_RUNS)) {
      expect(typeof run).toBe('function');
    }
  });

  it('export / feedback / file 不在执行表（王浩宇 10-16 裁决）', () => {
    const names = Object.keys(BUILTIN_RUNS);
    expect(names).not.toContain('export');
    expect(names).not.toContain('feedback');
    expect(names).not.toContain('file');
  });
});

describe('#1195 触发正则 slashTriggerMatch', () => {
  it('行首 / 触发（query 为其余部分；start=0）', () => {
    expect(slashTriggerMatch('/')).toEqual({ query: '', start: 0 });
    expect(slashTriggerMatch('/pl')).toEqual({ query: 'pl', start: 0 });
  });

  it('空格后 / 触发（start = 匹配段起点，含分隔空白）', () => {
    expect(slashTriggerMatch('hello /')).toEqual({ query: '', start: 5 });
    expect(slashTriggerMatch('  /co')).toEqual({ query: 'co', start: 1 });
  });

  it('全角 ／ 不触发（\\w 不含全角）', () => {
    expect(slashTriggerMatch('／')).toBeNull();
    expect(slashTriggerMatch('hello ／pl')).toBeNull();
  });

  it('词中间的 / 不触发', () => {
    expect(slashTriggerMatch('a/b')).toBeNull();
    expect(slashTriggerMatch('http://x')).toBeNull();
  });

  it('已带空格的参数不再是触发态（菜单关闭，走提交拦截）', () => {
    expect(slashTriggerMatch('/goal xxx')).toBeNull();
  });

  it('#1200 冒号进 query（插件 skill `/plugin:` 可继续 fuzzy）', () => {
    expect(slashTriggerMatch('/acme:')).toEqual({ query: 'acme:', start: 0 });
    expect(slashTriggerMatch('x /acme:co')).toEqual({ query: 'acme:co', start: 1 });
  });
});

describe('#1195 带参提交拦截 slashSubmitMatch', () => {
  it('纯命令名命中', () => {
    expect(slashSubmitMatch('/compact', ['goal', 'plan', 'compact', 'permission', 'model'])).toEqual({
      name: 'compact',
      args: undefined,
    });
  });

  it('带参数拆出 name + args（多行参数保留）', () => {
    expect(slashSubmitMatch('/goal 每天投20份简历', ['goal'])).toEqual({
      name: 'goal',
      args: '每天投20份简历',
    });
    expect(slashSubmitMatch('/goal 两行\n参数', ['goal'])).toEqual({ name: 'goal', args: '两行\n参数' });
  });

  it('命令名不在注册表 = 不拦截（当聊天发送）', () => {
    expect(slashSubmitMatch('/unknown foo', ['goal'])).toBeNull();
  });

  it('#1200 插件 skill 的 `/namespace:name` 也能被提交拦截（与触发正则同字符集）', () => {
    expect(slashSubmitMatch('/acme:code-review 改一下', ['acme:code-review'])).toEqual({
      name: 'acme:code-review',
      args: '改一下',
    });
  });

  it('仅斜杠不拦截', () => {
    expect(slashSubmitMatch('/', ['goal'])).toBeNull();
  });
});

describe('#1200 fuzzy 过滤 — 后端 registry 条目', () => {
  const entry = (
    name: string,
    description = '',
    extra: Partial<SlashRegistryEntry> = {},
  ): SlashRegistryEntry => ({ name, kind: 'skill', description, ...extra });
  const cmds = (): SlashRegistryEntry[] => [
    { name: 'goal', kind: 'builtin', description: '设置或查看长期任务目标' },
    { name: 'plan', kind: 'builtin', description: '进入或退出计划模式' },
    entry('deep-research', '深度调研', { argumentHint: '主题…' }),
    entry('code-review', '双轴代码审查', { namespace: 'acme' }),
  ];

  it('空 query 全量（保持后端顺序）', () => {
    expect(filterSlashCommands(cmds(), '').map((c) => c.name)).toEqual([
      'goal',
      'plan',
      'deep-research',
      'code-review',
    ]);
  });

  it('中文描述可过滤', () => {
    expect(filterSlashCommands(cmds(), '目标').map((c) => c.name)).toEqual(['goal']);
  });

  it('命令名前缀可过滤', () => {
    expect(filterSlashCommands(cmds(), 'pl').map((c) => c.name)).toEqual(['plan']);
  });

  it('argument-hint 进可搜面', () => {
    expect(filterSlashCommands(cmds(), '主题').map((c) => c.name)).toEqual(['deep-research']);
  });

  it('插件 skill 的 namespace 前缀可过滤（/acme:）', () => {
    expect(filterSlashCommands(cmds(), 'acme:').map((c) => c.name)).toEqual(['code-review']);
  });

  it('无命中 = 空数组（空态「无匹配命令」）', () => {
    expect(filterSlashCommands(cmds(), 'zzz')).toEqual([]);
  });

  it('不修改原数组', () => {
    const src = cmds();
    const snapshot = [...src];
    filterSlashCommands(src, 'pl');
    expect(src).toEqual(snapshot);
  });
});

describe('#1200 显示名 — 插件 skill 加 namespace 前缀', () => {
  it('无 namespace = 原名', () => {
    expect(slashDisplayName({ name: 'goal', kind: 'builtin', description: '' })).toBe('goal');
  });
  it('有 namespace = `plugin:skill`', () => {
    expect(
      slashDisplayName({ name: 'code-review', kind: 'skill', description: '', namespace: 'acme' }),
    ).toBe('acme:code-review');
  });
});

describe('#1195 命令执行层（run 语义，findings 修回）', () => {
  const cmd = (name: string): SlashCommand => ({ name, run: BUILTIN_RUNS[name]! });

  const makeCtx = () => ({
    sessionId: 's-1',
    openPermissionPicker: vi.fn(),
    openModelPicker: vi.fn(),
    reportError: vi.fn(),
    reportInfo: vi.fn(),
    runCompact: vi.fn(async () => {}),
    getSessionEvents: vi.fn(async () => []),
  });
  type CtxMock = ReturnType<typeof makeCtx>;
  const asCtx = (c: CtxMock): SlashCommandContext => c as unknown as SlashCommandContext;

  it('/goal 查看（无参）走 reportInfo 中性面，不 reportError（B-S-4/A-P3-2）', async () => {
    vi.mocked(await import('./api')).getTaskState.mockResolvedValue({
      defined: true,
      task_text: '每天投20份简历',
    } as TaskState);
    const c = makeCtx();
    await cmd('goal').run(undefined, asCtx(c));
    expect(c.reportInfo).toHaveBeenCalledWith('当前目标：每天投20份简历');
    expect(c.reportError).not.toHaveBeenCalled();
  });

  it('/goal 二次设置 409 → 后端 detail + 可操作引导（A-P2-2）', async () => {
    vi.mocked(await import('./api')).postSessionCommand.mockRejectedValue(
      new SessionError(409, '任务已定义（task/defined 全流至多一条）'),
    );
    const c = makeCtx();
    await cmd('goal').run('再设一个', asCtx(c));
    expect(c.reportError).toHaveBeenCalledTimes(1);
    const msg = c.reportError.mock.calls[0][0] as string;
    expect(msg).toContain('任务已定义');
    expect(msg).toContain('/goal（无参数）可查看当前目标');
  });

  it('/goal 其他错误（422）原样上抛（Composer 统一呈现），不加引导', async () => {
    vi.mocked(await import('./api')).postSessionCommand.mockRejectedValue(
      new SessionError(422, 'task_text 必须是非空字符串'),
    );
    const c = makeCtx();
    await expect(cmd('goal').run('目标文本', asCtx(c))).rejects.toThrow(
      'task_text 必须是非空字符串',
    );
    expect(c.reportError).not.toHaveBeenCalled();
  });

  it('/compact 只经 ctx.runCompact 触发三态宿主，不自持压缩状态（A-P1-3）', async () => {
    const c = makeCtx();
    await cmd('compact').run(undefined, asCtx(c));
    expect(c.runCompact).toHaveBeenCalledTimes(1);
    expect(postSessionCommand).not.toHaveBeenCalled();
  });

  it('/goal 查看未定义 → reportInfo「未定义」，不是错误', async () => {
    const mod = await import('./api');
    vi.mocked(mod.getTaskState).mockRejectedValue(new mod.TaskNotDefinedError('未定义任务'));
    const c = makeCtx();
    await cmd('goal').run(undefined, asCtx(c));
    expect(c.reportInfo).toHaveBeenCalledWith('当前会话未定义目标');
    expect(c.reportError).not.toHaveBeenCalled();
  });

  it('slashSubmitMatch 对 trim 后整条输入匹配（A-P2-1：尾随空格由调用方 trim 兜住）', () => {
    expect(slashSubmitMatch('/goal ', ['goal'])).toBeNull();
    expect(slashSubmitMatch('/goal', ['goal'])).toEqual({ name: 'goal', args: undefined });
  });
});

describe('#1195 mode 投影 last-wins（对齐 effective_workflow_mode）', () => {
  const ev = (mode: string) => ({
    type: 'workflow/mode-changed',
    seq: 0,
    ts: '',
    run_id: null,
    data: { mode },
  });
  const ctx = (events: unknown[]): SlashCommandContext =>
    ({ getSessionEvents: async () => events }) as unknown as SlashCommandContext;

  it('无事件 = normal', async () => {
    await expect(currentWorkflowMode(ctx([]))).resolves.toBe('normal');
  });

  it('最后一条 mode-changed 决定当前档', async () => {
    await expect(currentWorkflowMode(ctx([ev('plan'), ev('normal')]))).resolves.toBe('normal');
    await expect(currentWorkflowMode(ctx([ev('normal'), ev('plan')]))).resolves.toBe('plan');
  });

  it('坏值跳过继续向前找（后端 warning-continue 语义）', async () => {
    await expect(currentWorkflowMode(ctx([ev('plan'), ev('bogus')]))).resolves.toBe('plan');
  });

  it('非 mode-changed 事件被忽略', async () => {
    await expect(
      currentWorkflowMode(ctx([{ type: 'model/started', seq: 0, ts: '', run_id: null, data: {} }, ev('plan')])),
    ).resolves.toBe('plan');
  });
});
