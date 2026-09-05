import { describe, expect, it } from 'vitest';
import { Bot, Brain, CircleX, Cpu, FilePen, ListChecks, Plug, Search, Sparkles, Terminal, Wrench } from 'lucide-react';
import { KIND_ICON, KIND_LABEL, hasIconRow, modelKind, toolKind, withError } from './eventKind';

describe('toolKind', () => {
  it('mcp 前缀最先识别（优先于内置语义名）', () => {
    expect(toolKind('mcp__github__list_issues')).toBe('mcp');
    // 名字含 bash/read 字样但带 mcp 前缀 → 仍归 mcp
    expect(toolKind('mcp__x__bash')).toBe('mcp');
  });

  it('内置语义组', () => {
    expect(toolKind('bash')).toBe('terminal');
    expect(toolKind('read')).toBe('search');
    expect(toolKind('grep')).toBe('search');
    expect(toolKind('glob')).toBe('search');
    expect(toolKind('edit')).toBe('write');
    expect(toolKind('write')).toBe('write');
    expect(toolKind('apply_patch')).toBe('write');
  });

  it('其它工具归 generic tool', () => {
    expect(toolKind('inspect_artifact')).toBe('tool');
    expect(toolKind('load_skill')).toBe('tool');
    expect(toolKind('unknown_future_tool')).toBe('tool');
  });

  it('推断器永不返回预留 kind（skill/subagent/todo 无后端事件）', () => {
    const names = ['bash', 'read', 'grep', 'glob', 'edit', 'write', 'apply_patch',
      'inspect_artifact', 'load_skill', 'retrieve_knowledge', 'ingest_document',
      'mcp__github__list_issues', 'mcp__chrome__take_screenshot'];
    for (const n of names) {
      expect(['skill', 'subagent', 'todo']).not.toContain(toolKind(n));
    }
  });
});

describe('modelKind', () => {
  it('streaming 一律 thinking', () => {
    expect(modelKind('streaming', false)).toBe('thinking');
    expect(modelKind('streaming', true)).toBe('thinking');
  });

  it('done 段按是否终态分 final-answer / model', () => {
    expect(modelKind('done', true)).toBe('final-answer');
    expect(modelKind('done', false)).toBe('model');
  });
});

describe('withError', () => {
  it('失败工具叠加 error 态', () => {
    expect(withError('terminal', true)).toBe('error');
    expect(withError('search', true)).toBe('error');
  });

  it('非失败原样返回；final-answer 不叠加', () => {
    expect(withError('terminal', false)).toBe('terminal');
    expect(withError('final-answer', true)).toBe('final-answer');
  });
});

describe('视觉映射完备性', () => {
  it('KIND_ICON / KIND_LABEL 覆盖全部 kind', () => {
    const allKinds = ['model', 'thinking', 'final-answer', 'terminal', 'search',
      'write', 'mcp', 'tool', 'error', 'skill', 'subagent', 'todo'] as const;
    for (const k of allKinds) {
      expect(KIND_ICON[k]).toBeDefined();
      expect(typeof KIND_LABEL[k]).toBe('string');
    }
  });

  it('图标按 PRD §10.2 语义对号', () => {
    expect(KIND_ICON.thinking).toBe(Brain);
    expect(KIND_ICON.search).toBe(Search);
    expect(KIND_ICON.terminal).toBe(Terminal);
    expect(KIND_ICON.write).toBe(FilePen);
    expect(KIND_ICON.tool).toBe(Wrench);
    expect(KIND_ICON.mcp).toBe(Plug);
    expect(KIND_ICON.model).toBe(Cpu);
    expect(KIND_ICON.error).toBe(CircleX);
    expect(KIND_ICON.skill).toBe(Sparkles);
    expect(KIND_ICON.subagent).toBe(Bot);
    expect(KIND_ICON.todo).toBe(ListChecks);
  });

  it('final-answer 无图标行；其余都有', () => {
    expect(hasIconRow('final-answer')).toBe(false);
    expect(hasIconRow('thinking')).toBe(true);
    expect(hasIconRow('terminal')).toBe(true);
    expect(hasIconRow('error')).toBe(true);
  });
});

// ── streamKeyFromEvent（PRD §9 联动定位 key）──
import { streamKeyFromEvent } from './eventKind';

describe('streamKeyFromEvent — Inspector → 中间主区定位 key', () => {
  it('工具域事件（带 tool_call_id）→ tool:{id}，与 L 级 key 同构', () => {
    expect(streamKeyFromEvent({ tool_call_id: 'abc' }, 3)).toBe('tool:abc');
    // tool/result / artifact/created 都带 tool_call_id
    expect(streamKeyFromEvent({ tool_call_id: 'x', ok: false }, null)).toBe('tool:x');
  });

  it('非工具域事件 → step:{step_id}（轮次级诚实粒度）', () => {
    expect(streamKeyFromEvent({}, 5)).toBe('step:5');
    expect(streamKeyFromEvent({ delta: '…' }, 2)).toBe('step:2');
  });

  it('无 tool_call_id 且无 step → null（session 级事件无定位目标）', () => {
    expect(streamKeyFromEvent({}, null)).toBeNull();
    expect(streamKeyFromEvent({ content: 'hi' }, null)).toBeNull();
  });

  it('tool_call_id 非字符串（畸形）→ 按 step 回退', () => {
    expect(streamKeyFromEvent({ tool_call_id: 42 }, 7)).toBe('step:7');
  });
});
