/** `lib/lineDiff`（#1419 C-3）：行级 unified diff 的判据。
 *  钉的是**可观察输出**：行号、增删计数、段头只在省略上下文时出现、规模闸降级。 */
import { describe, expect, it } from 'vitest';
import { LINE_DIFF_CONTEXT, LINE_DIFF_MAX_CELLS, lineDiff } from './lineDiff';

const kinds = (before: string, after: string) => lineDiff(before, after).rows.map((r) => r.kind);

describe('lineDiff — 行级 unified diff', () => {
  it('完全相同 → 全是上下文行，计数为 0，没有段头', () => {
    const r = lineDiff('a\nb\nc', 'a\nb\nc');
    expect(r.mode).toBe('unified');
    expect(r.added).toBe(0);
    expect(r.removed).toBe(0);
    expect(r.rows.map((x) => x.kind)).toEqual(['ctx', 'ctx', 'ctx']);
    expect(r.rows.map((x) => x.text)).toEqual(['a', 'b', 'c']);
  });

  it('行号：上下文/删除带左号，上下文/新增带右号，新增行没有左号', () => {
    const r = lineDiff('a\nb\nc', 'a\nB\nc');
    expect(r.added).toBe(1);
    expect(r.removed).toBe(1);
    expect(r.rows).toEqual([
      { kind: 'ctx', left: 1, right: 1, text: 'a' },
      { kind: 'del', left: 2, right: null, text: 'b' },
      { kind: 'add', left: null, right: 2, text: 'B' },
      { kind: 'ctx', left: 3, right: 3, text: 'c' },
    ]);
  });

  it('纯新增：删行计数为 0，右行号连续', () => {
    const r = lineDiff('a\nc', 'a\nb\nc');
    expect(r.added).toBe(1);
    expect(r.removed).toBe(0);
    expect(r.rows.filter((x) => x.kind === 'add')).toEqual([
      { kind: 'add', left: null, right: 2, text: 'b' },
    ]);
  });

  it('空 before（新建文件）→ 每一行都是新增，左号全空', () => {
    const r = lineDiff('', 'x\ny');
    expect(r.added).toBe(2);
    expect(r.removed).toBe(0);
    expect(r.rows.map((x) => x.left)).toEqual([null, null]);
    expect(r.rows.map((x) => x.right)).toEqual([1, 2]);
  });

  it('结尾换行不产生一条幽灵空行', () => {
    expect(lineDiff('a\n', 'a\n').rows).toHaveLength(1);
    expect(lineDiff('a', 'a\n').rows).toHaveLength(1);
  });

  it('远处的两处改动被省略的上下文分开 → 中间补一条段头，且段头只在省略处出现', () => {
    const before = Array.from({ length: 30 }, (_, i) => `l${i + 1}`).join('\n');
    const after = before.replace('l2\n', 'L2\n').replace('l29', 'L29');
    const r = lineDiff(before, after);
    const heads = r.rows.filter((x) => x.kind === 'hunk');
    expect(heads).toHaveLength(1);
    // 段头形如 `@@ -a,b +c,d @@`，起点必须是数字
    expect(heads[0].text).toMatch(/^@@ -\d+,\d+ \+\d+,\d+ @@$/);
    // 上下文不超过 2*CONTEXT 行围绕每处改动
    const ctx = r.rows.filter((x) => x.kind === 'ctx').length;
    expect(ctx).toBeLessThanOrEqual((LINE_DIFF_CONTEXT * 2 + 1) * 2);
    // 改动本身必须都在
    expect(r.rows.filter((x) => x.kind === 'del').map((x) => x.text)).toEqual(['l2', 'l29']);
    expect(r.rows.filter((x) => x.kind === 'add').map((x) => x.text)).toEqual(['L2', 'L29']);
  });

  it('紧邻的改动不产生段头（全在一个上下文窗口内）', () => {
    const r = lineDiff('a\nb\nc', 'a\nB\nC');
    expect(r.rows.some((x) => x.kind === 'hunk')).toBe(false);
  });

  it('超出规模闸 → mode=whole，行表为空，计数为 null（算不出来就不报数）', () => {
    // 1001 × 1001 > LINE_DIFF_MAX_CELLS
    const n = Math.floor(Math.sqrt(LINE_DIFF_MAX_CELLS)) + 1;
    const big = Array.from({ length: n }, (_, i) => `x${i}`).join('\n');
    const r = lineDiff(big, `${big}\ntail`);
    expect(r.mode).toBe('whole');
    expect(r.rows).toEqual([]);
    expect(r.added).toBeNull();
    expect(r.removed).toBeNull();
  });

  it('规模闸内的整文件替换仍给准确计数', () => {
    const r = lineDiff('a\nb', 'c\nd');
    expect(r.mode).toBe('unified');
    expect(kinds('a\nb', 'c\nd')).toEqual(['del', 'del', 'add', 'add']);
    expect(r.added).toBe(2);
    expect(r.removed).toBe(2);
  });
});
