/** lib/turnNav — 轮次导航的**纯推断**（#1429 打磨点 6）。
 *
 * 导航药丸 1..N 与压缩置灰都只由投影事实推导，不持有第二套真相：
 *  - N / 可跳目标来自 `Turn.turn_index`（per-turn 事实，run/started 携带）；
 *  - 「已压缩」来自 `ConversationState.compactions[].compacted_turn_count`。
 *
 * 置灰规则（票面写死，只置灰 `1 .. 首个可见轮 turn_index − 1`）：
 *  - **主判据**是渲染序列里首个可见轮的 `turn_index`；它之前的轮次没有可滚动目标，
 *    一律置灰划线、不可点。
 *  - `compacted_turn_count` 决定**措辞**：覆盖到的轮次说「已压缩为摘要」（这是后端
 *    折叠进摘要的事实），没覆盖到的（视图起始晚于 1，但不是压缩造成）只说「不在当前
 *    视图中」——不把没发生过的压缩说成压缩。
 *  - 已经在视图里的轮次**永不**置灰：能滚到的轮次被标成"已压缩"会自相矛盾。
 *
 * 本票不做 hash 深链：`#` 锚点只复制 `#turn-N` 文本（`turnAnchor`）。
 */

import type { ContextCompaction, Turn } from '../types';

export interface TurnNavItem {
  /** 药丸文案（1-based 轮次号）。 */
  turnIndex: number;
  /** 滚动目标（`Turn.step_id`）；null = 置灰不可点。 */
  stepId: number | null;
  /** 置灰原因（null = 可跳）。 */
  offReason: 'compacted' | 'not-visible' | null;
  /** 无障碍/悬停标题。 */
  title: string;
}

/** 压缩已折叠到的最大轮次号（多条压缩记录取最大；无记录 = 0）。 */
export function compactedThrough(compactions: readonly ContextCompaction[]): number {
  let max = 0;
  for (const c of compactions) {
    const n = Number(c.compacted_turn_count);
    if (Number.isFinite(n) && n > max) max = n;
  }
  return max;
}

/** 轮次号 → 锚点文本（只复制这个，不承诺深链恢复）。 */
export function turnAnchor(turnIndex: number): string {
  return `#turn-${turnIndex}`;
}

/** 由渲染序列 + 压缩记录推导导航项（1..N 升序）。 */
export function deriveTurnNav(
  turns: readonly Turn[],
  compactions: readonly ContextCompaction[],
): TurnNavItem[] {
  // 只有正整数 turn_index 可导航（null = 旧版后端缺字段；≤0 = 防御性丢弃）。
  // 被取代的轮（ADR-0030 supersede）整段不渲染（`TurnView` 返回 null），既没有
  // 可滚动目标、也不该占一颗「不在当前视图中」的灰药丸——它们不是压缩掉的。
  const visible = turns.filter(
    (t) => t.turn_index !== null && t.turn_index > 0 && t.superseded !== true,
  );
  if (visible.length === 0) return [];

  const maxIndex = visible.reduce((m, t) => Math.max(m, t.turn_index as number), 0);
  const firstIndex = visible.reduce(
    (m, t) => Math.min(m, t.turn_index as number),
    Number.POSITIVE_INFINITY,
  );
  const stepByIdx = new Map<number, number>();
  for (const t of visible) stepByIdx.set(t.turn_index as number, t.step_id);
  const compacted = compactedThrough(compactions);

  const items: TurnNavItem[] = [];
  for (let i = 1; i <= maxIndex; i += 1) {
    const stepId = stepByIdx.get(i) ?? null;
    // 在视图里的轮次永不置灰（即使压缩计数更大——见文件头）。
    if (stepId !== null) {
      items.push({ turnIndex: i, stepId, offReason: null, title: `跳到第 ${i} 轮` });
      continue;
    }
    // 只置灰**视图起点之前**那段前缀（票面写死）：视图中间缺号（被取代 / 未投影）
    // 不是「滚不到」，给一颗灰药丸反而暗示那里有一轮可看。
    if (i > firstIndex) continue;
    const offReason = i <= compacted ? 'compacted' : 'not-visible';
    items.push({
      turnIndex: i,
      stepId: null,
      offReason,
      title:
        offReason === 'compacted'
          ? `第 ${i} 轮已压缩为摘要`
          : `第 ${i} 轮不在当前视图中`,
    });
  }
  return items;
}
