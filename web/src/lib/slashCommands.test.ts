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
  SLASH_COMMANDS,
  slashTriggerMatch,
  slashSubmitMatch,
  filterSlashCommands,
  currentWorkflowMode,
  type SlashCommand,
  type SlashCommandContext,
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

describe('#1195 注册表 — 5 条定稿命令', () => {
  it('恰好 5 条、名字与分组与票面一致', () => {
    expect(SLASH_COMMANDS.map((c) => c.name)).toEqual([
      'goal',
      'plan',
      'compact',
      'permission',
      'model',
    ]);
    expect(SLASH_COMMANDS.map((c) => c.group)).toEqual([
      'add',
      'add',
      'command',
      'command',
      'command',
    ]);
  });

  it('每条都有中文 label 与 description（效果图文案）', () => {
    for (const c of SLASH_COMMANDS) {
      expect(c.labelCN.length).toBeGreaterThan(0);
      expect(c.description.length).toBeGreaterThan(0);
    }
    const byName = new Map(SLASH_COMMANDS.map((c) => [c.name, c]));
    expect(byName.get('goal')!.labelCN).toBe('目标');
    expect(byName.get('plan')!.labelCN).toBe('计划');
    expect(byName.get('compact')!.labelCN).toBe('压缩');
    expect(byName.get('permission')!.labelCN).toBe('权限');
    expect(byName.get('model')!.labelCN).toBe('模型');
  });

  it('export / feedback / file 不在注册表（王浩宇 10-16 裁决）', () => {
    const names = SLASH_COMMANDS.map((c) => c.name);
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

  it('仅斜杠不拦截', () => {
    expect(slashSubmitMatch('/', ['goal'])).toBeNull();
  });
});

describe('#1195 fuzzy 过滤 — 复用 fuzzyScore', () => {
  const cmds = () => SLASH_COMMANDS;
  it('空 query 全量（按注册表序）', () => {
    expect(filterSlashCommands(cmds(), '').map((c) => c.name)).toEqual([
      'goal',
      'plan',
      'compact',
      'permission',
      'model',
    ]);
  });

  it('中文 label 可过滤', () => {
    expect(filterSlashCommands(cmds(), '目标').map((c) => c.name)).toEqual(['goal']);
  });

  it('英文名可过滤（前缀优先）', () => {
    expect(filterSlashCommands(cmds(), 'pl').map((c) => c.name)).toEqual(['plan']);
  });

  it('英文 keywords 命中（labelEN 可搜）', () => {
    const hits = filterSlashCommands(cmds(), 'compact');
    expect(hits.map((c) => c.name)).toContain('compact');
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

describe('#1195 命令执行层（run 语义，findings 修回）', () => {
  const cmd = (name: string): SlashCommand => SLASH_COMMANDS.find((c) => c.name === name)!;

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
