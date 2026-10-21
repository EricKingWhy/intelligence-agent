/** 分组投影（#1429 打磨点 1）——**表驱动**锁票面写死的每一条规则。
 *
 * 规则表（票面「同类调用分组折叠」逐条）：
 *  - 类别键复用 toolKind()；只有 search/write/terminal/mcp 成组；
 *  - 断组：model / reasoning / delegation、kind 变化、链结束；
 *  - ≥2 成员才成组；
 *  - 失败 / 运行中 / stopped 正常入组；
 *  - 组头状态聚合 failed > running > stopped > success；
 *  - 耗时 = span（首成员 started_at → 末成员 completed_at；末成员运行中则缺省）。
 */

import { describe, expect, it } from 'vitest';
import type { ChainNode } from './projection';
import type { Delegation, ToolCall } from '../types';
import {
  GROUP_LABEL, groupChain, groupKindOf, groupSpan, groupStatus, type ChainEntry,
} from './toolGroups';

const tool = (id: string, name: string, over: Partial<ToolCall> = {}): ToolCall => ({
  tool_call_id: id,
  name,
  args: {},
  status: 'success',
  started_at: '2026-10-20T10:00:00.000Z',
  completed_at: '2026-10-20T10:00:01.000Z',
  ...over,
});

const t = (id: string, name: string, over: Partial<ToolCall> = {}): ChainNode => ({
  kind: 'tool',
  tool: tool(id, name, over),
});
const model = (): ChainNode => ({
  kind: 'model',
  segment: { text: 'hi', status: 'done' },
});
const reasoning = (id: string): ChainNode => ({
  kind: 'reasoning',
  block: { blockId: id, source: 'model', text: 'thinking', status: 'completed' },
});
const delegation = (id: string): ChainNode => {
  const d: Delegation = {
    child_session_id: id,
    target: 'researcher',
    task: 'survey',
    status: 'completed',
    started_at: '2026-10-20T10:00:00.000Z',
    completed_at: '2026-10-20T10:00:05.000Z',
  };
  return { kind: 'delegation', delegation: d };
};

/** 渲染序列的紧凑形状：`group:search(3)[grep,read,read]` / `tool:bash` / `model`。 */
function shape(entries: ChainEntry[]): string[] {
  return entries.map((e) =>
    e.type === 'group'
      ? `group:${e.group.kind}(${e.group.members.length})[${e.group.members.map((m) => m.tool.name).join(',')}]`
      : e.node.kind === 'tool'
        ? `tool:${e.node.tool.name}`
        : e.node.kind,
  );
}

describe('groupKindOf — 类别键复用 toolKind()', () => {
  it.each([
    ['read', 'search'],
    ['grep', 'search'],
    ['glob', 'search'],
    ['edit', 'write'],
    ['write', 'write'],
    ['apply_patch', 'write'],
    ['bash', 'terminal'],
    ['pytest', null], // pytest 是 bash 通道里的命令名，不是工具名——不入组（见下方 bash 用例）
    ['mcp__github__list_issues', 'mcp'],
    ['delegate', null], // subagent 无票面组头文案，不猜
    ['inspect_artifact', null],
  ])('%s → %s', (name, expected) => {
    expect(groupKindOf(tool('x', name))).toBe(expected);
  });

  it('组头文案 = 票面指定的人话', () => {
    expect(GROUP_LABEL).toEqual({
      search: '读取',
      write: '编辑',
      terminal: '命令',
      mcp: 'MCP 调用',
    });
  });
});

describe('groupChain — 断组规则（表驱动）', () => {
  const cases: { name: string; chain: ChainNode[]; want: string[] }[] = [
    {
      name: '连续同类 ≥2 成组',
      chain: [t('1', 'grep'), t('2', 'read'), t('3', 'read')],
      want: ['group:search(3)[grep,read,read]'],
    },
    {
      name: '单个调用不分组',
      chain: [t('1', 'read')],
      want: ['tool:read'],
    },
    {
      name: 'kind 变化断组（两个单成员 → 各自单节点）',
      chain: [t('1', 'read'), t('2', 'bash')],
      want: ['tool:read', 'tool:bash'],
    },
    {
      name: 'kind 变化断组（1 个 + 2 个）',
      chain: [t('1', 'read'), t('2', 'bash'), t('3', 'bash')],
      want: ['tool:read', 'group:terminal(2)[bash,bash]'],
    },
    {
      name: 'model 节点断组',
      chain: [t('1', 'read'), t('2', 'read'), model(), t('3', 'read'), t('4', 'read')],
      want: ['group:search(2)[read,read]', 'model', 'group:search(2)[read,read]'],
    },
    {
      name: 'reasoning 节点断组',
      chain: [t('1', 'bash'), t('2', 'bash'), reasoning('r1'), t('3', 'bash'), t('4', 'bash')],
      want: ['group:terminal(2)[bash,bash]', 'reasoning', 'group:terminal(2)[bash,bash]'],
    },
    {
      name: 'delegation 节点断组',
      chain: [t('1', 'read'), t('2', 'read'), delegation('c1'), t('3', 'read')],
      want: ['group:search(2)[read,read]', 'delegation', 'tool:read'],
    },
    {
      name: '不成组的 kind 也打断相邻组',
      chain: [t('1', 'read'), t('2', 'inspect_artifact'), t('3', 'read'), t('4', 'read')],
      want: ['tool:read', 'tool:inspect_artifact', 'group:search(2)[read,read]'],
    },
    {
      name: 'mcp 组独立于内置组',
      chain: [t('1', 'mcp__a__x'), t('2', 'mcp__a__y'), t('3', 'read'), t('4', 'read')],
      want: ['group:mcp(2)[mcp__a__x,mcp__a__y]', 'group:search(2)[read,read]'],
    },
    {
      name: '空链',
      chain: [],
      want: [],
    },
  ];

  it.each(cases)('$name', ({ chain, want }) => {
    expect(shape(groupChain(chain))).toEqual(want);
  });

  it('组 key = 首成员 tool_call_id（开合状态的稳定键）', () => {
    const [entry] = groupChain([t('a1', 'read'), t('a2', 'grep')]);
    expect(entry.type).toBe('group');
    if (entry.type === 'group') expect(entry.group.key).toBe('a1');
  });

  it('不改入参：chain 顺序与成员引用原样', () => {
    const chain = [t('1', 'read'), t('2', 'read')];
    const snapshot = [...chain];
    groupChain(chain);
    expect(chain).toEqual(snapshot);
  });

  it('保留原 chain 下标（isFinalModel 之类链级判定仍用原下标）', () => {
    const entries = groupChain([model(), t('1', 'read'), t('2', 'read'), model()]);
    expect(entries.map((e) => e.index)).toEqual([0, 1, 3]);
  });
});

describe('groupStatus — 组头状态聚合（failed > running > stopped > success）', () => {
  const members = (statuses: ToolCall['status'][]) =>
    statuses.map((s, i) => ({ tool: tool(`x${i}`, 'read', { status: s }), index: i }));

  it.each([
    [['success', 'success'], 'success'],
    [['success', 'failed'], 'failed'],
    [['failed', 'running'], 'failed'],
    [['running', 'success'], 'running'],
    [['stopped', 'success'], 'stopped'],
    [['stopped', 'running'], 'running'],
    [['success', 'stopped', 'success'], 'stopped'],
  ] as [ToolCall['status'][], string][])('%s → %s', (statuses, want) => {
    expect(groupStatus(members(statuses))).toBe(want);
  });

  it('失败 / 运行中 / stopped 成员正常入组（不被剔除）', () => {
    const entries = groupChain([
      t('1', 'read', { status: 'failed' }),
      t('2', 'read', { status: 'running' }),
      t('3', 'read', { status: 'stopped' }),
    ]);
    expect(entries).toHaveLength(1);
    const [e] = entries;
    if (e.type === 'group') {
      expect(e.group.members).toHaveLength(3);
      expect(e.group.status).toBe('failed');
    }
  });
});

describe('groupSpan — 耗时 = span 口径', () => {
  it('首成员 started_at → 末成员 completed_at（不是各成员耗时之和）', () => {
    const entries = groupChain([
      t('1', 'read', {
        started_at: '2026-10-20T10:00:00.000Z',
        completed_at: '2026-10-20T10:00:05.000Z',
      }),
      t('2', 'read', {
        started_at: '2026-10-20T10:00:03.000Z',
        completed_at: '2026-10-20T10:00:09.000Z',
      }),
    ]);
    const [e] = entries;
    if (e.type !== 'group') throw new Error('expected group');
    expect(e.group.startedAt).toBe('2026-10-20T10:00:00.000Z');
    expect(e.group.completedAt).toBe('2026-10-20T10:00:09.000Z');
  });

  it('末成员运行中 → 终点缺省（渲染层按当前时刻续算，本层不读时钟）', () => {
    const span = groupSpan([
      { tool: tool('1', 'bash', { completed_at: '2026-10-20T10:00:05.000Z' }), index: 0 },
      { tool: tool('2', 'bash', { status: 'running', completed_at: undefined }), index: 1 },
    ]);
    expect(span.startedAt).toBe('2026-10-20T10:00:00.000Z');
    expect(span.completedAt).toBeUndefined();
  });

  it('成员缺 started_at → 起点缺省（不编造时间）', () => {
    const span = groupSpan([{ tool: tool('1', 'read', { started_at: undefined }), index: 0 }]);
    expect(span.startedAt).toBeUndefined();
  });
});
