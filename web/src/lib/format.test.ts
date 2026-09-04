/** format.ts 时间/数值格式化测试。 */

import { describe, expect, it } from 'vitest';
import { formatDuration } from './format';

describe('formatDuration', () => {
  it('缺 started/completed 任一（running 中）返回 null', () => {
    expect(formatDuration(undefined, undefined)).toBeNull();
    expect(formatDuration('2026-09-04T00:00:00Z', undefined)).toBeNull();
    expect(formatDuration(undefined, '2026-09-04T00:00:00Z')).toBeNull();
  });

  it('小于 1s 用 ms 表达，最小 1ms', () => {
    const t0 = '2026-09-04T00:00:00.000Z';
    expect(formatDuration(t0, '2026-09-04T00:00:00.082Z')).toBe('82ms');
    expect(formatDuration(t0, '2026-09-04T00:00:00.000Z')).toBe('1ms');
  });

  it('大于等于 1s 用一位小数秒', () => {
    const t0 = '2026-09-04T00:00:00.000Z';
    expect(formatDuration(t0, '2026-09-04T00:00:01.400Z')).toBe('1.4s');
    expect(formatDuration(t0, '2026-09-04T00:01:00.000Z')).toBe('60.0s');
  });
});
