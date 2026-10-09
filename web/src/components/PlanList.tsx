/** #381（W-27）进度清单渲染 + #864（W-27.1）长清单窗口化折叠（ZCode 对齐）。
 *
 * 数据源：仅 `ConversationState.plan`（`task/plan_updated` 整表覆盖投影，W-26
 * 契约）——组件不自取数、不维护第二套清单真相（不变量 #22）。零项不渲染
 * （无清单会话不留空壳，票面）。桌面同构：Electron 直接承载本组件（#344 冻结），
 * 无独立实现。
 *
 * 渲染契约（PRD 长任务 §7.5 四件套 + #864 窗口化折叠）：
 *   ①计数「进程 M 项 · 已完成 N 项」（票面逐字，**不做** ZCode 的 M/N 形式改动）；
 *   ②当前项高亮（activeForm 非 content + → 前缀，调研 §zcode 实测同构）；
 *   ③完成项划线 + ✓；④**窗口化折叠**——清单 ≤6 项全量展示（完成项也划线展示），
 *   >6 项取 3 项窗口（锚点 = 首个 in_progress，否则首个非 completed，否则末尾；
 *   窗口起点 clamp(锚点, 0, n-3)），窗口前/后各一条可展开折叠行。
 *   折叠行文案由被折叠组的构成决定（前组全 completed → 「已完成 N 项」否则
 *   「前面 N 项」；后组全 pending → 「待处理 N 项」否则「后面 N 项」；「待处理」
 *   展开时变「收起 N 项待处理」）。#864 改变了 #381 的「完成项恒收起」行为。
 *
 * 折叠只改 `hidden` 属性 / 内联 `display`，不卸载、不拆列表 ⇒ 整表更新时行 DOM
 * 节点身份跨更新存续（`key={item.id}`，票面 reconcile 判据）。
 *
 * 渲染端容错（票面）：服务端已硬校验状态机（PRD §7.2），防御的只是手写/旧数据
 * ——双 in_progress 只高亮第一个；未知 status 按未完成渲染，不崩。
 *
 * 折叠算法（`computePlanFold`）与文案（`foldRowLabel`）抽为**纯函数**并单测
 * （AC8a）——组件只做呈现与交互。交互（AC4）：折叠行是可聚焦 button +
 * `aria-expanded`；click / Enter / Space 切换并**固定**；悬浮延迟开合（ZCode
 * openDelay≈120ms / closeDelay≈80ms）；触摸设备（hover:none）点按；同一时刻
 * 至多一组展开（打开一组自动收起另一组）。
 */
import { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, ChevronRight } from 'lucide-react';
import type { PlanItem } from '../types';

/** 折叠阈值：清单 ≤ 此值不折叠（ZCode 实测 = 6）。 */
export const FOLD_THRESHOLD = 6;
/** 折叠窗口大小：> 阈值时恒取连续 3 项（ZCode 实测 = 3；Codex `DETAIL_PREVIEW_LINES` 同值）。 */
export const WINDOW_SIZE = 3;
/** 悬浮打开延迟（ZCode 实测 ≈120ms）。 */
export const HOVER_OPEN_DELAY_MS = 120;
/** 悬浮关闭延迟（ZCode 实测 ≈80ms）。 */
export const HOVER_CLOSE_DELAY_MS = 80;

export type FoldSide = 'before' | 'after';
/** 被折叠组的构成：全 completed / 全 pending / 混合。 */
export type FoldKind = 'completed' | 'pending' | 'mixed';

export interface FoldRow {
  /** 该组被折叠的项数（0 = 无折叠行）。 */
  count: number;
  /** 组构成，决定文案分支。 */
  kind: FoldKind;
}

export interface PlanFold {
  /** 是否应用窗口化折叠（项数 > FOLD_THRESHOLD）。false ⇒ 全量展示、无折叠行。 */
  folded: boolean;
  /** 窗口在 items 中的范围 [windowStart, windowEnd)。未折叠时 = [0, n)。 */
  windowStart: number;
  windowEnd: number;
  before: FoldRow;
  after: FoldRow;
}

/** 折叠决策（纯函数，AC8a）：阈值、锚点、窗口起点 clamp、组构成。 */
export function computePlanFold(items: PlanItem[]): PlanFold {
  const n = items.length;
  const none: FoldRow = { count: 0, kind: 'mixed' };
  if (n <= FOLD_THRESHOLD) {
    return { folded: false, windowStart: 0, windowEnd: n, before: none, after: none };
  }
  // 锚点 = 第一个 in_progress；若无 → 第一个非 completed；再无（全完成）→ 末尾。
  let anchor = items.findIndex((it) => it.status === 'in_progress');
  if (anchor === -1) anchor = items.findIndex((it) => it.status !== 'completed');
  if (anchor === -1) anchor = n - 1;
  // 窗口起点 = clamp(锚点, 0, n-3)（n > 阈值 ≥ 3 ⇒ n-3 ≥ 1）。
  const start = Math.min(Math.max(anchor, 0), n - WINDOW_SIZE);
  const end = start + WINDOW_SIZE;
  const beforeItems = items.slice(0, start);
  const afterItems = items.slice(end);
  return {
    folded: true,
    windowStart: start,
    windowEnd: end,
    before: {
      count: beforeItems.length,
      kind:
        beforeItems.length > 0 && beforeItems.every((it) => it.status === 'completed')
          ? 'completed'
          : 'mixed',
    },
    after: {
      count: afterItems.length,
      kind:
        afterItems.length > 0 && afterItems.every((it) => it.status === 'pending')
          ? 'pending'
          : 'mixed',
    },
  };
}

/** 折叠行文案（纯函数，AC3）：前组全 completed →「已完成 N 项」否则「前面 N 项」；
 *  后组全 pending →「待处理 N 项」否则「后面 N 项」；「待处理」展开时 →「收起 N 项待处理」。 */
export function foldRowLabel(side: FoldSide, row: FoldRow, open: boolean): string {
  if (row.count === 0) return '';
  if (side === 'before') {
    return row.kind === 'completed' ? `已完成 ${row.count} 项` : `前面 ${row.count} 项`;
  }
  if (row.kind === 'pending') {
    return open ? `收起 ${row.count} 项待处理` : `待处理 ${row.count} 项`;
  }
  return `后面 ${row.count} 项`;
}

/** 触摸设备（无悬浮）：悬浮触发层直接退出，点按由 click 承担（AC4）。 */
function hoverNone(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(hover: none)').matches
  );
}

function PlanRow({ item, current, collapsed }: { item: PlanItem; current: boolean; collapsed: boolean }) {
  const done = item.status === 'completed';
  // 当前项用 activeForm（进行时文案，Claude Code TaskCreate 同款字段语义）；
  // activeForm 缺失时投影已回落 content，这里不再兜底第二次。
  const text = current ? item.activeForm : item.content;
  return (
    <li
      className={
        'plan-list-item' +
        (done ? ' plan-list-item-done' : '') +
        (current ? ' plan-list-item-current' : '')
      }
      data-plan-id={item.id}
      data-plan-status={item.status}
      // 折叠用 hidden 属性而非卸载/拆列表：节点留在同一父 <ul> 里，
      // key 对齐的 reconcile 跨状态翻转与折叠切换都不断（票面「不重新挂载」）。
      hidden={collapsed}
      // 内联 display:none 是折叠的**执行层**：hidden 的 UA 规则会被作者样式
      // `.plan-list-item { display: flex }` 盖过（workspace-panel / detail-peek
      // 注释守卫各漏过一次后，2026-10-02 真机第三次踩到）——inline style 胜过
      // 任何作者样式表，且 jsdom 断言得到，测试不再是盲区。
      style={collapsed ? { display: 'none' } : undefined}
    >
      <span className="plan-list-marker">
        {done ? <Check size={13} strokeWidth={2.5} /> : current ? '→' : '○'}
      </span>
      <span className="plan-list-text">{text}</span>
    </li>
  );
}

export function PlanList({ items }: { items: PlanItem[] }) {
  // 交互状态：openGroup = 当前展开组（悬浮或已固定）；pinnedRef = 由 click 固定的组。
  // 悬浮是**短暂**的：延迟打开、延迟关闭；click / 键盘固定后不再被悬浮关闭接管。
  const [openGroup, setOpenGroup] = useState<FoldSide | null>(null);
  const pinnedRef = useRef<FoldSide | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    },
    [],
  );

  if (items.length === 0) return null;

  const clearTimer = () => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const fold = computePlanFold(items);
  const completed = items.filter((it) => it.status === 'completed');
  const currentId = items.find((it) => it.status === 'in_progress')?.id ?? null;

  const beforeOpen = fold.folded && openGroup === 'before';
  const afterOpen = fold.folded && openGroup === 'after';

  // click / Enter / Space（原生 button 语义）：切换并**固定**；再点同一组则收起并取消固定。
  // 「打开一组自动收起另一组」由单一 openGroup 状态自然成立。
  const activate = (side: FoldSide) => {
    clearTimer();
    if (pinnedRef.current === side) {
      pinnedRef.current = null;
      setOpenGroup(null);
    } else {
      pinnedRef.current = side;
      setOpenGroup(side);
    }
  };
  const hoverEnter = (side: FoldSide) => {
    if (hoverNone()) return;
    clearTimer();
    timerRef.current = setTimeout(() => {
      pinnedRef.current = null; // 悬浮接管：上一组的固定被解除（互斥）
      setOpenGroup(side);
      timerRef.current = null;
    }, HOVER_OPEN_DELAY_MS);
  };
  const hoverLeave = (side: FoldSide) => {
    if (pinnedRef.current === side) return; // 已固定：悬浮离开不收起
    clearTimer();
    timerRef.current = setTimeout(() => {
      setOpenGroup((cur) => (cur === side ? null : cur));
      timerRef.current = null;
    }, HOVER_CLOSE_DELAY_MS);
  };

  /** 某行是否隐藏：折叠时，窗口外且所在组未展开 ⇒ 隐藏；未折叠 ⇒ 全显示。 */
  const hiddenFor = (index: number): boolean => {
    if (!fold.folded) return false;
    if (index < fold.windowStart) return !beforeOpen;
    if (index >= fold.windowEnd) return !afterOpen;
    return false;
  };

  const foldRow = (side: FoldSide, row: FoldRow, open: boolean) => (
    <li key={`fold-${side}`} className={`plan-list-fold plan-list-fold-${side}`}>
      <button
        type="button"
        // 复用既有 `.plan-list-toggle` 视觉（inline-flex / muted / hover 底），
        // 不新增 CSS（`app.css` 属三 clone 共享面，本票 Scope Lock 在组件内）。
        className="plan-list-toggle plan-list-fold-toggle"
        aria-expanded={open}
        onClick={() => activate(side)}
        onMouseEnter={() => hoverEnter(side)}
        onMouseLeave={() => hoverLeave(side)}
      >
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        {foldRowLabel(side, row, open)}
      </button>
    </li>
  );

  return (
    <section className="plan-list" aria-label="进度清单">
      <div className="plan-list-head">
        <span className="plan-list-counter">
          进程 {items.length} 项 · 已完成 {completed.length} 项
        </span>
      </div>
      <ul className="plan-list-items">
        {fold.folded && fold.before.count > 0 && foldRow('before', fold.before, beforeOpen)}
        {items.map((it, index) => (
          <PlanRow key={it.id} item={it} current={it.id === currentId} collapsed={hiddenFor(index)} />
        ))}
        {fold.folded && fold.after.count > 0 && foldRow('after', fold.after, afterOpen)}
      </ul>
    </section>
  );
}
