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
import { useEffect, useRef, useState, type ReactNode } from 'react';
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

/** 一个被折叠组的构成：切片非空且全为该状态 → 该状态，否则 mixed。
 *  （空组 count=0、kind 无意义——折叠行不渲染，文案取空串。） */
function groupRow(slice: PlanItem[], matchStatus: 'completed' | 'pending'): FoldRow {
  return {
    count: slice.length,
    kind: slice.length > 0 && slice.every((it) => it.status === matchStatus) ? matchStatus : 'mixed',
  };
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
  return {
    folded: true,
    windowStart: start,
    windowEnd: end,
    before: groupRow(items.slice(0, start), 'completed'),
    after: groupRow(items.slice(end), 'pending'),
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
  // 交互：pinnedGroup = click/键盘**固定**展开的组；hoverGroup = 悬浮**临时**展开的组。
  // 生效组 = hoverGroup ?? pinnedGroup（悬浮临时接管、离开即回落到固定组）。
  // 单一生效组天然满足互斥（打开一组自动收起另一组）；固定态不会被悬浮静默解除。
  const [pinnedGroup, setPinnedGroup] = useState<FoldSide | null>(null);
  const [hoverGroup, setHoverGroup] = useState<FoldSide | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 指针当前所在的组（悬浮判定用）：快速穿到别组时，上一组的「离开」不再排定关闭，
  // 避免其关闭被新组的 enter 取消后留下永久展开的悬停孤儿。
  const hoverSideRef = useRef<FoldSide | null>(null);

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
  /** 排定一次延迟动作（悬浮开/合共用同一形状）：先取消在途定时器，动作后清引用。 */
  const schedule = (action: () => void, delay: number) => {
    clearTimer();
    timerRef.current = setTimeout(() => {
      action();
      timerRef.current = null;
    }, delay);
  };

  const fold = computePlanFold(items);
  const completed = items.filter((it) => it.status === 'completed');
  const currentId = items.find((it) => it.status === 'in_progress')?.id ?? null;

  const openGroup: FoldSide | null = hoverGroup ?? pinnedGroup;
  const beforeOpen = fold.folded && openGroup === 'before';
  const afterOpen = fold.folded && openGroup === 'after';

  // click / Enter / Space（原生 button 语义，AC4「切换」）：按**可见展开态**切换——已展开
  // （无论由悬浮还是固定而来）则收起，否则展开并**固定**。收起时区分来源：若当前可见组是
  // **悬浮**撑开的，只清悬浮态、回落到 pinnedGroup（可能是另一组）——否则会把用户此前固定的
  // 另一组一并清掉（AC4 互斥是「开一组收起另一组」，不等于「清掉另一组的固定」）。
  const activate = (side: FoldSide) => {
    clearTimer();
    if (openGroup !== side) {
      setPinnedGroup(side);
      setHoverGroup(null);
    } else if (hoverGroup === side) {
      setHoverGroup(null);
    } else {
      setPinnedGroup(null);
    }
  };
  const hoverEnter = (side: FoldSide) => {
    if (hoverNone()) return;
    hoverSideRef.current = side;
    schedule(() => setHoverGroup(side), HOVER_OPEN_DELAY_MS);
  };
  const hoverLeave = (side: FoldSide) => {
    // 只解除**临时**展开；固定组由 pinnedGroup 承担 ⇒ 悬浮离开后自动回落到固定组。
    // 指针已不在本组（快速穿到别组）时不排关闭 —— 否则会留下悬停孤儿：A 的关闭被
    // B 的 enter 取消、B 又未到打开延迟即离开 ⇒ 无任何组排定关闭、A 永久展开。
    if (hoverSideRef.current !== side) return;
    hoverSideRef.current = null;
    schedule(() => setHoverGroup(null), HOVER_CLOSE_DELAY_MS);
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
        // `.plan-list-fold-toggle`（及下面的 -before/-after）是测试/查询钩子，无独立 CSS 规则。
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

  // 后组折叠行必须排在**它展开后新增的行之上**：否则展开时新增行插在折叠行前面、折叠行被
  // 下移，指针落到别处 ⇒ 悬浮态立刻 mouseleave 收起（e2e 实测 chromium-1280 悬停展开后
  // p15 仍 hidden）。故把后组折叠行插入「窗口末尾」位置（后组各项之前）。
  // 关键：所有行仍在**同一个数组**里（forEach + push）——若拆成两段 `.map()`，锚点变化使
  // windowEnd 移动时 item 会跨数组搬迁，触发 remount，破坏 AC6 的行节点身份。
  const rows: ReactNode[] = [];
  if (fold.folded && fold.before.count > 0) rows.push(foldRow('before', fold.before, beforeOpen));
  items.forEach((it, index) => {
    if (fold.folded && fold.after.count > 0 && index === fold.windowEnd) {
      rows.push(foldRow('after', fold.after, afterOpen));
    }
    rows.push(<PlanRow key={it.id} item={it} current={it.id === currentId} collapsed={hiddenFor(index)} />);
  });

  return (
    <section className="plan-list" aria-label="进度清单">
      <div className="plan-list-head">
        <span className="plan-list-counter">
          进程 {items.length} 项 · 已完成 {completed.length} 项
        </span>
      </div>
      <ul className="plan-list-items">{rows}</ul>
    </section>
  );
}
