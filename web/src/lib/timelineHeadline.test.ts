import { describe, expect, it } from 'vitest';
import { formatCount, timelineHeadline } from './timelineHeadline';
import { EventType } from '../types';
import type { AgentEvent, EventTypeValue, ToolCall } from '../types';

function ev(type: EventTypeValue, data: Record<string, unknown> = {}): AgentEvent {
  return { type, data, seq: 1, run_id: 'r', step_id: 1, session_id: 's', time: '2026-10-20T10:00:00.000Z' } as AgentEvent;
}

function call(name: string, args: Record<string, unknown> = {}): ToolCall {
  return { tool_call_id: 'c1', name, args, status: 'success' };
}

describe('timelineHeadline（T-8 人话标题）', () => {
  it('read：文件 basename + 行范围（offset/limit → 118–204）', () => {
    expect(timelineHeadline(ev(EventType.TOOL_CALL, {
      tool_name: 'read', args: { path: 'src/api/sessions.py', offset: 118, limit: 87 },
    })).title).toBe('读 sessions.py 的 118–204 行');
  });

  it('read：无 limit 时只给起始行；无 offset 时只给文件名', () => {
    expect(timelineHeadline(ev(EventType.TOOL_CALL, {
      tool_name: 'read_file', args: { path: 'a/b/c.py', offset: 12 },
    })).title).toBe('读 c.py 从第 12 行');
    expect(timelineHeadline(ev(EventType.TOOL_CALL, {
      tool_name: 'read', args: { path: 'a/b/c.py' },
    })).title).toBe('读 c.py');
  });

  it('grep：目录 + 模式；缺目录只给模式', () => {
    expect(timelineHeadline(ev(EventType.TOOL_CALL, {
      tool_name: 'grep', args: { path: 'src/api', pattern: 'list_sessions' },
    })).title).toBe('在 api 里找 list_sessions');
    expect(timelineHeadline(ev(EventType.TOOL_CALL, {
      tool_name: 'search', args: { query: 'TODO' },
    })).title).toBe('找 TODO');
  });

  it('bash：跑 + 命令首行（过长截断）', () => {
    expect(timelineHeadline(ev(EventType.TOOL_CALL, {
      tool_name: 'bash', args: { command: 'pytest tests/api -x' },
    })).title).toBe('跑 pytest tests/api -x');
    const long = timelineHeadline(ev(EventType.TOOL_CALL, {
      tool_name: 'bash', args: { command: 'x'.repeat(120) },
    })).title;
    expect(long.length).toBeLessThanOrEqual(50);
    expect(long.endsWith('…')).toBe(true);
  });

  it('edit：文件 + 首个新增行要旨；无 new_string 只给文件', () => {
    expect(timelineHeadline(ev(EventType.TOOL_CALL, {
      tool_name: 'edit', args: { path: 'src/serializers.py', new_string: '\n  payload = payload or {}' },
    })).title).toBe('改 serializers.py：payload = payload or {}');
    expect(timelineHeadline(ev(EventType.TOOL_CALL, {
      tool_name: 'edit', args: { path: 'src/serializers.py' },
    })).title).toBe('改 serializers.py');
  });

  it('write：文件 + 行数', () => {
    expect(timelineHeadline(ev(EventType.TOOL_CALL, {
      tool_name: 'write', args: { path: 'x/new.ts', content: 'a\nb\nc' },
    })).title).toBe('写 new.ts，3 行');
  });

  it('delegate：派子会话 + 任务首行', () => {
    expect(timelineHeadline(ev(EventType.TOOL_CALL, {
      tool_name: 'delegate', args: { task: '调研 MinIO 生命周期策略' },
    })).title).toBe('派子会话：调研 MinIO 生命周期策略');
  });

  it('TOOL_RESULT：标题取对应 call 的模板 + 完成/失败要旨（不再是裸 ok）', () => {
    const ok = timelineHeadline(
      ev(EventType.TOOL_RESULT, { tool_call_id: 'c1', content: '{"ok":true}' }),
      call('read', { path: 'a/b.py', offset: 1, limit: 3 }),
    );
    expect(ok.title).toBe('读 b.py 的 1–3 行 · 完成');
    const failed = timelineHeadline(
      ev(EventType.TOOL_RESULT, { tool_call_id: 'c1', content: '{"ok":false,"error_code":"TIMEOUT","message":"exceeded 30s"}' }),
      { ...call('bash', { command: 'pytest' }), status: 'failed' },
    );
    expect(failed.title).toBe('跑 pytest · 失败：TIMEOUT · exceeded 30s');
  });

  it('TOOL_RESULT：无配对 call 时退回「工具返回」，仍表达结果', () => {
    expect(timelineHeadline(ev(EventType.TOOL_RESULT, { content: '{"ok":true}' })).title)
      .toBe('工具返回 · 完成');
  });

  it('model/completed：标题 + 真实 token 副行；无 usage 时 note 为 null（不占位）', () => {
    const withUsage = timelineHeadline(ev(EventType.MODEL_COMPLETED, {
      model: 'deepseek-v4', usage: { prompt_tokens: 8420, completion_tokens: 1420 },
    }));
    expect(withUsage.title).toBe('模型生成完成 · deepseek-v4');
    expect(withUsage.note).toBe('8,420 in / 1,420 out');
    expect(timelineHeadline(ev(EventType.MODEL_COMPLETED, { model: 'm' })).note).toBeNull();
  });

  it('未覆盖的事件类型返回空标题（调用方回退投影摘要）', () => {
    expect(timelineHeadline(ev(EventType.ARTIFACT_CREATED, { artifact_id: 'x' })).title).toBe('');
  });

  it('formatCount：千分位不依赖 locale', () => {
    expect(formatCount(0)).toBe('0');
    expect(formatCount(8420)).toBe('8,420');
    expect(formatCount(1234567)).toBe('1,234,567');
  });
});
