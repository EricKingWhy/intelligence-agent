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

/** 行顶与阅读位置比较时的亚像素容差（见 `rowAtOffset`）。 */
const ROW_EPSILON_PX = 0.5;

/** 「阅读带」占滚动容器高度的比例：V2 的 `IntersectionObserver` 用
 *  `rootMargin: "0px 0px -68% 0px"`，即只把容器**顶部 32%** 当作判定带
 *  （`center-v2.html:4478`）。这里照抄同一比例，保证换轮时机与视觉基线一致。 */
export const CURRENT_TURN_BAND_RATIO = 0.32;

/** 虚拟化窗口内的一行（只取判定当前轮需要的字段；与 `VirtualItem` 结构兼容）。 */
export interface TurnRowSpan {
  index: number;
  /** 行顶在列表坐标系里的偏移（与虚拟器的 `scrollOffset` 同一坐标系）。 */
  start: number;
  end: number;
}

/** 阅读位置（滚动容器顶边，列表坐标系）对应的行。窗口为空（SSR / 尚未测量）⇒ null。
 *
 *  口径 = V2 观察器：把「阅读带」`[offset, offset + band]` 里**行顶最靠上**的那一行
 *  当作当前行（V2 取 topmost intersecting `.turn-mark`）。带内没有行顶（正处在一个
 *  长轮中间）时退回「阅读线所在行」（V2 此时不触发回调、保留上一个值，等价于不换）。
 *
 *  `band` 的作用（不是装饰）：`scrollToIndex({ align: 'start' })` 到列表末尾时，
 *  目标轮顶边可能停在阅读线**下方**若干像素（容器已到底、滚不动），纯「顶边所在行」
 *  会把高亮留在上一轮；V2 的带判定此时仍把目标轮算作当前轮。
 *
 *  `ROW_EPSILON_PX` 吸收分数像素：跳转落点与实测行顶可能差零点几像素。 */
export function rowAtOffset(
  rows: readonly TurnRowSpan[],
  offset: number,
  band = 0,
): number | null {
  if (rows.length === 0) return null;
  // ① 带内行顶最靠上的那一行。
  for (const r of rows) {
    if (r.start > offset + band + ROW_EPSILON_PX) break;
    if (r.start >= offset - ROW_EPSILON_PX) return r.index;
  }
  // ② 带内没有行顶：阅读线所在行（最后一行 start <= offset）。
  let row = rows[0].index;
  for (const r of rows) {
    if (r.start > offset + ROW_EPSILON_PX) break;
    row = r.index;
  }
  return row;
}

/** 当前所在轮（V2 `.turn-nav button.is-on` 的判定）：从阅读行向前找最近的**可导航**
 *  轮次（`turn_index` 为正且未被取代）。
 *
 *  为什么不直接取阅读行的 `turn_index`：被取代的轮整段不渲染、没有药丸；旧版后端
 *  缺 `turn_index` 的续轮也没有药丸。这两类行都"属于"上一颗可导航药丸——沿用上一轮
 *  比清空高亮更接近用户在读的内容，也不会在滚动经过零高行时闪断。
 *  行下标先夹到 `turns` 范围内（窗口与 turns 短暂不同步时防御，不抛错）。
 *
 *  `atEnd`（滚动容器已到底）：**取最后一个可导航轮**，不按阅读带判。两处依据：
 *  ① 到底时容器滚不动，末轮顶边可能永远进不了阅读带（列表比一屏短时尤其明显）——
 *     药丸点「跳到第 N 轮」却高亮不到第 N 轮，与票面「跳转后高亮随之落到目标轮」
 *     直接冲突；② 读到底本来就是「在看最后一轮」。这是相对 V2 观察器几何的**有意
 *     偏差**（V2 到底时会停在带内最靠上的轮），登记在报告里。 */
export function currentTurnIndex(
  turns: readonly Turn[],
  rows: readonly TurnRowSpan[],
  offset: number,
  band = 0,
  atEnd = false,
): number | null {
  if (rows.length === 0) return null;
  const row = atEnd ? rows[rows.length - 1].index : rowAtOffset(rows, offset, band);
  if (row === null) return null;
  for (let i = Math.min(row, turns.length - 1); i >= 0; i -= 1) {
    const t = turns[i];
    if (t.superseded === true) continue;
    const n = t.turn_index;
    if (n !== null && n > 0) return n;
  }
  return null;
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
  // 首写胜：药丸「第 N 轮」必须跳到**这一轮的开头**。若同一 turn_index 意外落在多个
  // 渲染轮上（恢复续轮的继承曾如此），后写会让药丸跳到后半段（#1429 复审 P2）。
  for (const t of visible) {
    const idx = t.turn_index as number;
    if (!stepByIdx.has(idx)) stepByIdx.set(idx, t.step_id);
  }
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
