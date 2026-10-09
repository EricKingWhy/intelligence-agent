// @vitest-environment jsdom
/** #864（W-27.1）：窗口化折叠决策**纯函数**单测（AC8a）。
 *
 * 覆盖边界：空 / 1 / 6 / 7 项 / 全完成 / 无 in_progress / 锚点近尾 clamp /
 * 单组为空 / 黄金夹具（AC5）；文案四分支 + 「收起 N 项待处理」（AC3）。
 * 算法权威 = issue #864「目标算法」（ZCode 实测裁剪，close-source 仅行为对齐）。
 */
import { describe, expect, it } from 'vitest';
import {
  FOLD_THRESHOLD,
  WINDOW_SIZE,
  computePlanFold,
  foldRowLabel,
  type FoldRow,
} from './PlanList';
import type { PlanItem } from '../types';

const item = (id: string, status: string): PlanItem => ({
  id,
  content: `任务 ${id}`,
  activeForm: `正在执行 ${id}`,
  status,
  source: 'agent',
});

const many = (spec: Array<[string, number]>): PlanItem[] =>
  spec.flatMap(([status, count]) => Array.from({ length: count }, (_, i) => item(`${status}-${i}`, status)));

describe('#864（W-27.1）：computePlanFold 折叠决策', () => {
  it('常量 = 阈值 6 / 窗口 3（ZCode 实测；Codex DETAIL_PREVIEW_LINES 同值）', () => {
    expect(FOLD_THRESHOLD).toBe(6);
    expect(WINDOW_SIZE).toBe(3);
  });

  it('空清单：不折叠（组件层另外不渲染）', () => {
    expect(computePlanFold([])).toEqual({
      folded: false,
      windowStart: 0,
      windowEnd: 0,
      before: { count: 0, kind: 'mixed' },
      after: { count: 0, kind: 'mixed' },
    });
  });

  it('1 项：不折叠，窗口 = 全量', () => {
    const fold = computePlanFold([item('a', 'pending')]);
    expect(fold.folded).toBe(false);
    expect([fold.windowStart, fold.windowEnd]).toEqual([0, 1]);
    expect(fold.before.count).toBe(0);
    expect(fold.after.count).toBe(0);
  });

  it('6 项（= 阈值）：不折叠，全量展示', () => {
    const fold = computePlanFold(many([['completed', 6]]));
    expect(fold.folded).toBe(false);
    expect([fold.windowStart, fold.windowEnd]).toEqual([0, 6]);
  });

  it('7 项（= 阈值 + 1）：折叠；锚点在头部 → 前组为空、后组混合', () => {
    const items = many([['in_progress', 1], ['completed', 6]]);
    const fold = computePlanFold(items);
    expect(fold.folded).toBe(true);
    expect([fold.windowStart, fold.windowEnd]).toEqual([0, 3]);
    expect(fold.before.count).toBe(0);
    expect(fold.after.count).toBe(4);
    expect(fold.after.kind).toBe('mixed'); // 后组不是全 pending
  });

  it('全完成（无 in_progress、无未完成项）：锚点回退末尾 → clamp 到 n-3', () => {
    const fold = computePlanFold(many([['completed', 8]]));
    expect(fold.folded).toBe(true);
    expect([fold.windowStart, fold.windowEnd]).toEqual([5, 8]);
    expect(fold.before).toEqual({ count: 5, kind: 'completed' });
    expect(fold.after.count).toBe(0);
  });

  it('无 in_progress 但有未完成项：锚点 = 第一个非 completed', () => {
    const fold = computePlanFold(many([['completed', 3], ['pending', 5]]));
    expect([fold.windowStart, fold.windowEnd]).toEqual([3, 6]);
    expect(fold.before).toEqual({ count: 3, kind: 'completed' });
    expect(fold.after).toEqual({ count: 2, kind: 'pending' });
  });

  it('锚点近尾：clamp 到 n-3（窗口贴尾，后组为空）', () => {
    const fold = computePlanFold(many([['completed', 7], ['in_progress', 1]]));
    expect([fold.windowStart, fold.windowEnd]).toEqual([5, 8]);
    expect(fold.before).toEqual({ count: 5, kind: 'completed' });
    expect(fold.after.count).toBe(0);
  });

  it('单组为空：前组为空（锚点在 0）', () => {
    const fold = computePlanFold(many([['pending', 7]]));
    expect([fold.windowStart, fold.windowEnd]).toEqual([0, 3]);
    expect(fold.before.count).toBe(0);
    expect(fold.after).toEqual({ count: 4, kind: 'pending' });
  });

  it('AC5 黄金夹具：17 项（11×completed + 1×pending + 1×in_progress + 4×pending）', () => {
    const items = many([['completed', 11], ['pending', 1], ['in_progress', 1], ['pending', 4]]);
    expect(items).toHaveLength(17);
    const fold = computePlanFold(items);
    expect(fold.folded).toBe(true);
    // 锚点 = 首个 in_progress（index 12）→ clamp(12, 0, 14) = 12
    expect([fold.windowStart, fold.windowEnd]).toEqual([12, 15]);
    // 前组 = 11 completed + 1 pending（混合）→「前面 12 项」
    expect(fold.before).toEqual({ count: 12, kind: 'mixed' });
    // 后组 = 末尾 2 pending →「待处理 2 项」
    expect(fold.after).toEqual({ count: 2, kind: 'pending' });
    // 窗口 = in_progress + 2×pending
    expect(items.slice(12, 15).map((it) => it.status)).toEqual(['in_progress', 'pending', 'pending']);
  });
});

describe('#864（W-27.1）：foldRowLabel 四分支文案（AC3）', () => {
  const row = (count: number, kind: FoldRow['kind']): FoldRow => ({ count, kind });

  it('count = 0 → 空串（空组不渲染）', () => {
    expect(foldRowLabel('before', row(0, 'mixed'), false)).toBe('');
    expect(foldRowLabel('after', row(0, 'pending'), false)).toBe('');
  });

  it('前组全 completed →「已完成 N 项」', () => {
    expect(foldRowLabel('before', row(5, 'completed'), false)).toBe('已完成 5 项');
  });

  it('前组混合 →「前面 N 项」', () => {
    expect(foldRowLabel('before', row(12, 'mixed'), false)).toBe('前面 12 项');
  });

  it('后组全 pending：收起 →「待处理 N 项」，展开 →「收起 N 项待处理」', () => {
    expect(foldRowLabel('after', row(2, 'pending'), false)).toBe('待处理 2 项');
    expect(foldRowLabel('after', row(2, 'pending'), true)).toBe('收起 2 项待处理');
  });

  it('后组混合 →「后面 N 项」（展开与否同文案）', () => {
    expect(foldRowLabel('after', row(4, 'mixed'), false)).toBe('后面 4 项');
    expect(foldRowLabel('after', row(4, 'mixed'), true)).toBe('后面 4 项');
  });
});
