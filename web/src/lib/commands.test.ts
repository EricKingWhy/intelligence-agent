import { describe, expect, it } from 'vitest';
import { filterCommands, fuzzyScore, isPaletteShortcut, type CommandItem } from './commands';

const item = (id: string, label: string): CommandItem => ({ id, label, group: 'actions', run: () => {} });

describe('fuzzyScore — 子序列匹配', () => {
  it('空 query = 全量命中（score 0）', () => {
    expect(fuzzyScore('', 'anything')).toBe(0);
    expect(fuzzyScore('   ', 'anything')).toBe(0);
  });

  it('子序列命中（不要求连续）', () => {
    expect(fuzzyScore('insp', 'Toggle Run Inspector')).not.toBeNull();
    expect(fuzzyScore('tthm', 'Toggle Theme')).not.toBeNull();
  });

  it('不命中返回 null', () => {
    expect(fuzzyScore('xyz', 'Toggle Run Inspector')).toBeNull();
    expect(fuzzyScore('ttti', 'Raw')).toBeNull();
  });

  it('大小写不敏感', () => {
    expect(fuzzyScore('RAW', 'Switch to Raw')).not.toBeNull();
    expect(fuzzyScore('raw', 'SWITCH TO RAW')).not.toBeNull();
  });

  it('前缀命中 > 中间命中', () => {
    const prefix = fuzzyScore('ins', 'Inspector 面板');
    const middle = fuzzyScore('ins', 'Toggle Run Inspector');
    expect(prefix).not.toBeNull();
    expect(middle).not.toBeNull();
    expect(prefix! - middle!).toBeGreaterThanOrEqual(40 - 8); // 前缀加分减去中间可能拿到的连续分
  });

  it('连续命中加分（子串 > 散点）', () => {
    expect(fuzzyScore('insp', 'insp 连续')).toBeGreaterThan(fuzzyScore('insp', 'i n s p 散点')!);
  });
});

describe('filterCommands — 过滤排序', () => {
  const items = [item('1', 'Switch to Compact'), item('2', 'Switch to Balanced'), item('3', 'Toggle Run Inspector'), item('4', 'Switch to Raw')];

  it('空 query 保持原序', () => {
    expect(filterCommands(items, '').map((x) => x.id)).toEqual(['1', '2', '3', '4']);
  });

  it('query 过滤不命中项', () => {
    expect(filterCommands(items, 'insp').map((x) => x.id)).toEqual(['3']);
  });

  it('"switch" 命中全部 switch 系，且不命中 toggle', () => {
    const got = filterCommands(items, 'switch');
    expect(got.map((x) => x.id)).toEqual(['1', '2', '4']);
  });

  it('同分稳定（原序）', () => {
    const twins = [item('a', 'alpha one'), item('b', 'alpha two')];
    expect(filterCommands(twins, 'alpha').map((x) => x.id)).toEqual(['a', 'b']);
  });
});

describe('isPaletteShortcut — Ctrl/Cmd + K', () => {
  it('Ctrl+K（Win/Linux）与 Meta+K（macOS）命中', () => {
    expect(isPaletteShortcut({ key: 'k', ctrlKey: true, metaKey: false })).toBe(true);
    expect(isPaletteShortcut({ key: 'k', ctrlKey: false, metaKey: true })).toBe(true);
    expect(isPaletteShortcut({ key: 'K', ctrlKey: true, metaKey: false })).toBe(true);
  });

  it('无修饰键 / 其他键不命中', () => {
    expect(isPaletteShortcut({ key: 'k', ctrlKey: false, metaKey: false })).toBe(false);
    expect(isPaletteShortcut({ key: 'a', ctrlKey: true, metaKey: false })).toBe(false);
  });
});
