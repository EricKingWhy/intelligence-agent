/** lib/commands — Command Palette 数据层（PRD §15，ADR-0014）。
 *
 * 命令 = 静态动作（切换密度 / Inspector / 主题 / 复制）+ 动态事件项
 * （Search Runtime Events：最近事件直出为可选项，选中即定位）。
 * fuzzy 匹配是纯函数：子序列匹配 + 前缀/连续加分，null = 不命中。
 * 不引第三方 palette 库——Radix Dialog（已有依赖）承担浮层语义。
 */

export interface CommandItem {
  id: string;
  label: string;
  /** 右侧弱化提示：快捷键 / 事件类型 / 状态。 */
  hint?: string;
  group: 'actions' | 'density' | 'events';
  run: () => void;
}

/** 子序列 fuzzy 打分：query 每个字符按序出现在 text 中即命中。
 *  得分 = 命中基础上前缀 +40 / 连续命中每对 +8 / 越早命中 +（剩余长度）。
 *  不命中返回 null。大小写不敏感。 */
export function fuzzyScore(query: string, text: string): number | null {
  const q = query.trim().toLowerCase();
  if (!q) return 0; // 空 query = 全量（调用方决定展示上限）
  const t = text.toLowerCase();
  let score = 0;
  let ti = 0;
  let prevHit = -2;
  for (let qi = 0; qi < q.length; qi++) {
    const ch = q[qi];
    const idx = t.indexOf(ch, ti);
    if (idx === -1) return null;
    if (idx === 0) score += 40; // 前缀命中
    if (idx === prevHit + 1) score += 8; // 连续命中（子串）
    score += Math.max(0, 20 - idx); // 越早越好
    prevHit = idx;
    ti = idx + 1;
  }
  return score;
}

/** 过滤 + 排序：命中项按分数降序（稳定——同分保持原序，事件按时间新→旧自然可读）。
 *  空 query 原序返回。 */
export function filterCommands(items: readonly CommandItem[], query: string): CommandItem[] {
  if (!query.trim()) return [...items];
  return items
    .map((item) => ({ item, score: fuzzyScore(query, item.label) }))
    .filter((x): x is { item: CommandItem; score: number } => x.score !== null)
    .sort((a, b) => b.score - a.score)
    .map((x) => x.item);
}

/** 快捷键判定：Ctrl（Win/Linux）或 Cmd（macOS）+ K。 */
export function isPaletteShortcut(e: {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
}): boolean {
  return (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k';
}
