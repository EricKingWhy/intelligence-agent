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
  parseGoalCommand,
  formatGoalSnapshot,
  slashTriggerMatch,
  slashSubmitMatch,
  filterSlashCommands,
  currentWorkflowMode,
  slashDisplayName,
  type SlashCommand,
  type SlashCommandContext,
  type SlashRegistryEntry,
} from './slashCommands';
import { postSessionCommand, SessionError, type GoalSnapshot } from './api';

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>();
  return {
    ...actual,
    postSessionCommand: vi.fn(),
    getGoalState: vi.fn(),
    postGoalCommand: vi.fn(),
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
    prefillInput: vi.fn(),
    runCompact: vi.fn(async () => {}),
    getSessionEvents: vi.fn(async () => []),
  });
  type CtxMock = ReturnType<typeof makeCtx>;
  const asCtx = (c: CtxMock): SlashCommandContext => c as unknown as SlashCommandContext;

  const snapshot = (over: Partial<GoalSnapshot['goal']> = {}, activation: GoalSnapshot['activation'] = 'armed'): GoalSnapshot => ({
    id: 's-1',
    activation,
    goal: {
      defined: true,
      goal_id: 'goal-1',
      revision: 3,
      objective: '## Objective\n投 20 份简历',
      phase: 'active',
      blocked_reason: null,
      pause_reason: null,
      max_goal_rounds: 32,
      rounds_started: 3,
      needs_interview: false,
      ...over,
    },
  });

  it('/goal 查看（无参）走 reportInfo 中性面，不 reportError（B-S-4/A-P3-2）', async () => {
    vi.mocked((await import('./api')).getGoalState).mockResolvedValue(snapshot());
    const c = makeCtx();
    await cmd('goal').run(undefined, asCtx(c));
    const msg = c.reportInfo.mock.calls[0][0] as string;
    expect(msg).toContain('投 20 份简历');
    expect(msg).toContain('状态：active');
    expect(msg).toContain('轮数：3/32');
    expect(msg).toContain('自动续跑：armed');
    expect(msg).toContain('/goal pause');
    expect(c.reportError).not.toHaveBeenCalled();
  });

  it('/goal 创建：带 objective 走 POST /goal create，回执仍走 reportInfo', async () => {
    const mod = await import('./api');
    vi.mocked(mod.postGoalCommand).mockResolvedValue(snapshot());
    const c = makeCtx();
    await cmd('goal').run('## Objective\n投 20 份简历', asCtx(c));
    expect(mod.postGoalCommand).toHaveBeenCalledWith('s-1', {
      operation: 'create',
      objective: '## Objective\n投 20 份简历',
    });
    expect(c.reportInfo).toHaveBeenCalledTimes(1);
  });

  it('控制词只在占满整个输入时识别（/goal pause after verification 是 objective）', () => {
    expect(parseGoalCommand('pause')).toEqual({ kind: 'pause' });
    expect(parseGoalCommand(' PAUSE ')).toEqual({ kind: 'pause' });
    expect(parseGoalCommand('pause after verification')).toEqual({
      kind: 'create',
      objective: 'pause after verification',
    });
    expect(parseGoalCommand('edit 新目标')).toEqual({ kind: 'edit', objective: '新目标' });
    expect(parseGoalCommand('edit')).toEqual({ kind: 'invalid-edit' });
    expect(parseGoalCommand('')).toEqual({ kind: 'show' });
    expect(parseGoalCommand(undefined)).toEqual({ kind: 'show' });
  });

  it('/goal edit 缺 objective → reportError 可操作提示，不发请求', async () => {
    const mod = await import('./api');
    const c = makeCtx();
    await cmd('goal').run('edit', asCtx(c));
    expect(mod.postGoalCommand).not.toHaveBeenCalled();
    expect(c.reportError.mock.calls[0][0]).toContain('/goal edit <objective>');
  });

  it('/goal 409（revision 冲突）→ reportError 带当前 revision 引导（AC3）', async () => {
    const mod = await import('./api');
    vi.mocked(mod.getGoalState).mockResolvedValue(snapshot());
    vi.mocked(mod.postGoalCommand).mockRejectedValue(
      new SessionError(409, 'revision 冲突：请求基于 r2，当前是 r3'),
    );
    const c = makeCtx();
    await cmd('goal').run('edit 新目标', asCtx(c));
    const msg = c.reportError.mock.calls[0][0] as string;
    expect(msg).toContain('当前是 r3');
    expect(msg).toContain('/goal 查看当前 revision');
  });

  it('/goal edit 先读当前 revision 再带 expected_revision（AC3 的 UI 面）', async () => {
    const mod = await import('./api');
    vi.mocked(mod.getGoalState).mockResolvedValue(snapshot({ revision: 7 }));
    vi.mocked(mod.postGoalCommand).mockResolvedValue(snapshot({ revision: 8 }));
    const c = makeCtx();
    await cmd('goal').run('edit 新目标', asCtx(c));
    expect(mod.postGoalCommand).toHaveBeenCalledWith('s-1', {
      operation: 'edit',
      objective: '新目标',
      expected_revision: 7,
    });
    expect(c.reportInfo.mock.calls[0][0]).toContain('版本：r8');
  });

  it('/goal 回执渲染 paused 的 pause_reason（AC9 可读原因）', async () => {
    const mod = await import('./api');
    vi.mocked(mod.getGoalState).mockResolvedValue(snapshot({
      phase: 'paused',
      pause_reason: { code: 'no-progress', message: '最近两轮没有工具活动' },
    }));
    const c = makeCtx();
    await cmd('goal').run(undefined, asCtx(c));
    expect(c.reportInfo.mock.calls[0][0]).toContain(
      '暂停原因：no-progress: 最近两轮没有工具活动',
    );
  });

  it('AC6：needs_interview 时把补全草稿写回输入框，只补缺失段', async () => {
    vi.mocked((await import('./api')).getGoalState).mockResolvedValue(
      snapshot({ objective: '## Objective\nA\n\n## Success criteria\nB', needs_interview: true }),
    );
    const c = makeCtx();
    await cmd('goal').run(undefined, asCtx(c));
    const draft = c.prefillInput.mock.calls[0][0] as string;
    expect(draft.startsWith('/goal edit ')).toBe(true);
    expect(draft).toContain('## Verification');
    // 已有的段不重复补：标题只出现一次（原文一次，不另加空壳）。
    expect(draft.split('## Success criteria').length - 1).toBe(1);
  });

  it('AC6：needs_interview=false 不写输入框（不打断用户）', async () => {
    vi.mocked((await import('./api')).getGoalState).mockResolvedValue(snapshot());
    const c = makeCtx();
    await cmd('goal').run(undefined, asCtx(c));
    expect(c.prefillInput).not.toHaveBeenCalled();
  });

  it('/goal 其他错误（422）原样上抛（Composer 统一呈现），不加引导', async () => {
    vi.mocked((await import('./api')).postGoalCommand).mockRejectedValue(
      new SessionError(422, 'objective 必须是非空字符串'),
    );
    const c = makeCtx();
    await expect(cmd('goal').run('目标文本', asCtx(c))).rejects.toThrow(
      'objective 必须是非空字符串',
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
    vi.mocked((await import('./api')).getGoalState).mockResolvedValue(
      snapshot({ defined: false, goal_id: null, objective: null, phase: null }, 'disarmed'),
    );
    const c = makeCtx();
    await cmd('goal').run(undefined, asCtx(c));
    expect(c.reportInfo).toHaveBeenCalledWith(
      '当前会话未定义目标。用 /goal <objective> 创建。',
    );
    expect(c.reportError).not.toHaveBeenCalled();
  });

  it('blocked 快照带 code/message；needs_interview 有提示（AC1/AC6）', () => {
    const text = formatGoalSnapshot(
      snapshot(
        {
          phase: 'blocked',
          blocked_reason: { code: 'round-limit', message: '轮次用尽' },
          needs_interview: true,
        },
        'disarmed',
      ),
    );
    expect(text).toContain('阻塞：round-limit: 轮次用尽');
    expect(text).toContain('## Success criteria');
    expect(text).toContain('/goal resume');
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
