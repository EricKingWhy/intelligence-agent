/** lib/memory.ts 纯函数测试（MEM-5 / #160）。
 *
 *  面板的乐观删除/回滚语义由 `e2e/s-memories.spec.ts` 走真实交互锁住；这里只锁
 *  取值与格式化规则（不需要 DOM）。 */

import { describe, expect, it } from 'vitest';
import {
  MEMORY_MAX_LIMIT,
  MEMORY_PAGE_SIZE,
  MEMORY_CONTENT_MAX_CHARS,
  formatMemoryTime,
  hasMoreAfter,
  memoryPayloadDraft,
  memoryPayloadFromDraft,
  refetchLimit,
  scopeLabel,
  validateMemoryEdit,
  withoutIds,
} from './memory';
import type { MemoryPayload } from './memoryV2Api';
import type { MemorySummary } from '../types';

const row = (id: string): MemorySummary => ({
  id,
  content: `记忆 ${id}`,
  scope: 'user',
  metadata: {},
  created_at: '2026-09-12T08:30:00Z',
});

describe('scopeLabel', () => {
  it('两种 scope 都给中文文案；session 不冒充 user', () => {
    expect(scopeLabel('user')).toBe('用户');
    expect(scopeLabel('session')).toBe('会话');
  });
});

describe('formatMemoryTime', () => {
  it('合法 ISO → 本地化字符串（不等于原串即已解析）', () => {
    const formatted = formatMemoryTime('2026-09-12T08:30:00Z');
    expect(formatted).not.toBe('2026-09-12T08:30:00Z');
    expect(formatted.length).toBeGreaterThan(0);
  });

  it('不可解析的串 → 原样返回（不伪造时间、不显示 Invalid Date）', () => {
    expect(formatMemoryTime('not-a-date')).toBe('not-a-date');
    expect(formatMemoryTime('')).toBe('');
  });
});

describe('hasMoreAfter', () => {
  it('取满请求量 → 可能还有；取得更少 → 到底', () => {
    expect(hasMoreAfter(MEMORY_PAGE_SIZE, MEMORY_PAGE_SIZE)).toBe(true);
    expect(hasMoreAfter(MEMORY_PAGE_SIZE - 1, MEMORY_PAGE_SIZE)).toBe(false);
    expect(hasMoreAfter(0, MEMORY_PAGE_SIZE)).toBe(false);
  });

  it('请求量为 0 视为到底（防止 0 >= 0 恒真导致死循环式翻页）', () => {
    expect(hasMoreAfter(0, 0)).toBe(false);
  });
});

describe('withoutIds', () => {
  it('空集合原样返回同一引用（避免无谓的重渲染）', () => {
    const rows = [row('a')];
    expect(withoutIds(rows, new Set())).toBe(rows);
  });

  it('按 id 过滤（乐观删除的行先不可见）', () => {
    expect(withoutIds([row('a'), row('b'), row('c')], new Set(['b'])).map((r) => r.id)).toEqual([
      'a',
      'c',
    ]);
  });
});

describe('refetchLimit', () => {
  it('少于/等于一页 → 至少一页（重拉不要退化成 0 条）', () => {
    expect(refetchLimit(0)).toBe(MEMORY_PAGE_SIZE);
    expect(refetchLimit(3)).toBe(MEMORY_PAGE_SIZE);
    expect(refetchLimit(MEMORY_PAGE_SIZE)).toBe(MEMORY_PAGE_SIZE);
  });

  it('已加载多页 → 保留已加载的条数（重拉不把用户已看过的内容缩回第一页）', () => {
    expect(refetchLimit(120)).toBe(120);
  });

  it('不越过后端硬上界 200（越界就是 422：回滚重拉与"重试"会永久失败）', () => {
    expect(refetchLimit(250)).toBe(MEMORY_MAX_LIMIT);
    expect(refetchLimit(10_000)).toBe(MEMORY_MAX_LIMIT);
  });
});

describe('Memory V2 editor contract', () => {
  it('builds only the payload fields required by each kind', () => {
    expect(memoryPayloadFromDraft('episodic', {
      situation: 'service outage', action: 'restarted worker', outcome: 'recovered', lesson: 'check queue',
    })).toEqual({
      kind: 'episodic',
      situation: 'service outage',
      action: 'restarted worker',
      outcome: 'recovered',
      lesson: 'check queue',
    });
    expect(memoryPayloadFromDraft('semantic', {
      subject: 'user', fact: 'likes tea', category: 'preference',
    })).toEqual({
      kind: 'semantic', subject: 'user', fact: 'likes tea', category: 'preference',
    });
    expect(memoryPayloadDraft({
      kind: 'procedural', trigger: 'deploy', procedure: 'run checks', success_condition: 'green',
    })).toEqual({
      kind: 'procedural', trigger: 'deploy', procedure: 'run checks', success_condition: 'green',
    });
    expect(memoryPayloadFromDraft('semantic', { subject: 'user', fact: 'likes tea' })).toBeNull();
  });

  it('uses code-point limits and rejects blank or incomplete edits before submission', () => {
    const record = { kind: 'semantic' as const, status: 'active' as const };
    const payload: MemoryPayload = {
      kind: 'semantic', subject: 'user', fact: 'likes tea', category: 'preference',
    };
    expect(validateMemoryEdit(record, '😀'.repeat(MEMORY_CONTENT_MAX_CHARS), payload)).toEqual([]);
    expect(validateMemoryEdit(record, '😀'.repeat(MEMORY_CONTENT_MAX_CHARS + 1), payload))
      .toContain(`记忆正文不能超过 ${MEMORY_CONTENT_MAX_CHARS} 个字符。`);
    expect(validateMemoryEdit(record, '  ', payload)).toContain('记忆正文不能为空。');
    expect(validateMemoryEdit(record, 'ok', { ...payload, fact: ' ' })).toContain('事实不能为空。');
    expect(validateMemoryEdit({ kind: 'semantic', status: 'superseded' }, 'ok', payload))
      .toContain('只有当前有效的 V2 记忆可以编辑。');
  });
});
