/** lib/memory.ts 纯函数测试（MEM-5 / #160）。
 *
 *  面板的乐观删除/回滚语义由 `e2e/s-memories.spec.ts` 走真实交互锁住；这里只锁
 *  取值与格式化规则（不需要 DOM）。 */

import { describe, expect, it } from 'vitest';
import { MEMORY_PAGE_SIZE, formatMemoryTime, hasMoreAfter, scopeLabel, withoutIds } from './memory';
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
