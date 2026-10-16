/** #1195 — Composer 内联 `/` 命令菜单：注册表与触发逻辑（纯函数层）。
 *
 * 判据逐条对票面 §四：
 *  - 注册表 5 条（goal/plan/compact/permission/model），`/export` `/feedback` `/file` 不做；
 *  - 触发正则 `/(^|\s)\/(\w*)$/`（行首或空格后半角 `/`，全角不触发——`\w` 不含 `／`）;
 *  - 带参提交拦截 `^\/(\w+)(?:\s+([\s\S]+))?$`；
 *  - fuzzy 复用 `fuzzyScore`（不另起一套，票面铁律）；
 *  - mode 投影 last-wins（与后端 `effective_workflow_mode` 同语义）。
 */

import { describe, expect, it } from 'vitest';
import {
  SLASH_COMMANDS,
  slashTriggerMatch,
  slashSubmitMatch,
  filterSlashCommands,
  currentWorkflowMode,
  type SlashCommandContext,
} from './slashCommands';

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
  it('行首 / 触发（query 为其余部分）', () => {
    expect(slashTriggerMatch('/')).toEqual({ query: '' });
    expect(slashTriggerMatch('/pl')).toEqual({ query: 'pl' });
  });

  it('空格后 / 触发', () => {
    expect(slashTriggerMatch('hello /')).toEqual({ query: '' });
    expect(slashTriggerMatch('  /co')).toEqual({ query: 'co' });
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
