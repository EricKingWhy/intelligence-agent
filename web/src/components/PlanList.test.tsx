// @vitest-environment jsdom
/** #381（W-27）四件套渲染 + #864（W-27.1）窗口化折叠（ZCode 对齐）。
 *
 * 真投影 + createRoot（jumpPulse 同款）：「已完成项不重新挂载」必须 pin 到 DOM
 * 节点身份（票面 reconcile 判据），renderToString 给不了。折叠用 `hidden`
 * 属性——保留 DOM 节点，切换只改属性不改结构。
 *
 * #864 改变 #381 的「完成项恒收起」行为：≤6 项全量展示（含完成项划线），>6 项
 * 改 ZCode 式窗口化折叠。既有 #381 断言按 AC6 保留（零项不渲染 / activeForm
 * 高亮 + → / 双 in_progress 只高亮首个 / 未知 status 按未完成 / id 对齐 reconcile）。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createElement } from 'react';
import { HOVER_CLOSE_DELAY_MS, HOVER_OPEN_DELAY_MS, PlanList } from './PlanList';
import type { PlanItem } from '../types';

// act 环境旗标（ApprovalModal.test 同款）：压掉 jsdom 下的 act 警告噪音。
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const item = (id: string, status: string): PlanItem => ({
  id,
  content: `任务 ${id}`,
  activeForm: `正在执行 ${id}`,
  status,
  source: 'agent',
});

const threeState: PlanItem[] = [item('a', 'completed'), item('b', 'in_progress'), item('c', 'pending')];

/** AC5 黄金夹具：17 项（11×completed, 1×pending, 1×in_progress, 4×pending）。 */
const golden17 = (): PlanItem[] => [
  ...Array.from({ length: 11 }, (_, i) => item(`c${i}`, 'completed')),
  item('p11', 'pending'),
  item('ip12', 'in_progress'),
  ...Array.from({ length: 4 }, (_, i) => item(`p${13 + i}`, 'pending')),
];

/** React 的 onMouseEnter/Leave 由 mouseover/mouseout 合成：必须带 relatedTarget
 *  且可冒泡（原生 mouseenter 不冒泡，React 收不到）。 */
const fireEnter = (el: Element) =>
  el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, relatedTarget: document.body }));
const fireLeave = (el: Element) =>
  el.dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: document.body }));
const fireClick = (el: Element) => el.dispatchEvent(new MouseEvent('click', { bubbles: true }));

describe('#381（W-27）+ #864（W-27.1）：PlanList 渲染', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it('空清单不渲染（无清单会话不留空壳）', () => {
    act(() => root.render(createElement(PlanList, { items: [] })));
    expect(container.innerHTML).toBe('');
  });

  it('四件套①计数：标题区「进程 M 项 · 已完成 N 项」（票面逐字）', () => {
    act(() => root.render(createElement(PlanList, { items: threeState })));
    expect(container.textContent).toContain('进程 3 项 · 已完成 1 项');
  });

  it('四件套②当前项高亮：in_progress 显示 activeForm（非 content）+ → 标记', () => {
    act(() => root.render(createElement(PlanList, { items: threeState })));
    const current = container.querySelector('[data-plan-id="b"]');
    expect(current?.className).toContain('plan-list-item-current');
    expect(current?.textContent).toContain('正在执行 b');
    expect(current?.textContent).not.toContain('任务 b');
    expect(current?.textContent).toContain('→');
  });

  it('AC1 阈值：≤6 项全量展示（完成项划线 + ✓，不再恒收起），无折叠行', () => {
    act(() => root.render(createElement(PlanList, { items: threeState })));
    const done = container.querySelector('[data-plan-id="a"]');
    // #864 改变 #381 行为：≤6 项时完成项照常展示（hidden=false、无内联 display:none）。
    expect(done?.hasAttribute('hidden')).toBe(false);
    expect(done?.getAttribute('style') ?? '').not.toContain('display: none');
    expect(done?.className).toContain('plan-list-item-done');
    expect(done?.querySelector('svg')).not.toBeNull();
    expect(done?.textContent).toContain('任务 a');
    // 未完成项空心圆
    expect(container.querySelector('[data-plan-id="c"]')?.textContent).toContain('○');
    // ≤6 项不渲染任何折叠行
    expect(container.querySelectorAll('.plan-list-fold')).toHaveLength(0);
    expect(container.querySelectorAll('.plan-list-item:not([hidden])')).toHaveLength(3);
  });

  it('AC2/AC5 黄金夹具：>6 项 → 前面 12 项 + 3 项窗口（in_progress + 2×pending）+ 待处理 2 项', () => {
    act(() => root.render(createElement(PlanList, { items: golden17() })));
    expect(container.textContent).toContain('进程 17 项 · 已完成 11 项');
    const toggles = container.querySelectorAll<HTMLButtonElement>('.plan-list-fold-toggle');
    expect(toggles).toHaveLength(2);
    expect(toggles[0].textContent).toBe('前面 12 项');
    expect(toggles[1].textContent).toBe('待处理 2 项');
    // 折叠行是可聚焦 button + aria-expanded 收起
    expect(toggles[0].tagName).toBe('BUTTON');
    expect(toggles[0].getAttribute('aria-expanded')).toBe('false');
    expect(toggles[1].getAttribute('aria-expanded')).toBe('false');
    // 窗口 3 项可见，其余 hidden
    expect(container.querySelectorAll('.plan-list-item:not([hidden])')).toHaveLength(3);
    for (const id of ['ip12', 'p13', 'p14']) {
      expect(container.querySelector(`[data-plan-id="${id}"]`)?.hasAttribute('hidden')).toBe(false);
    }
    for (const id of ['c0', 'p11', 'p15', 'p16']) {
      expect(container.querySelector(`[data-plan-id="${id}"]`)?.hasAttribute('hidden')).toBe(true);
    }
  });

  it('AC4 点击切换 + 固定：click → 展开并被悬浮离开/再点收起，aria-expanded 同步', () => {
    act(() => root.render(createElement(PlanList, { items: golden17() })));
    const after = container.querySelector<HTMLButtonElement>('.plan-list-fold-after .plan-list-fold-toggle')!;
    act(() => fireClick(after));
    expect(after.getAttribute('aria-expanded')).toBe('true');
    // 「待处理」展开时文案变「收起 N 项待处理」
    expect(after.textContent).toBe('收起 2 项待处理');
    expect(container.querySelector('[data-plan-id="p15"]')?.hasAttribute('hidden')).toBe(false);
    expect(container.querySelector('[data-plan-id="p16"]')?.hasAttribute('hidden')).toBe(false);
    // 已固定：悬浮离开不收起（真实定时器下 hoverLeave 因 pinned 直接返回，不排关闭）
    act(() => fireLeave(after));
    expect(after.getAttribute('aria-expanded')).toBe('true');
    // 再点收起
    act(() => fireClick(after));
    expect(after.getAttribute('aria-expanded')).toBe('false');
    expect(after.textContent).toBe('待处理 2 项');
    expect(container.querySelector('[data-plan-id="p15"]')?.hasAttribute('hidden')).toBe(true);
  });

  it('AC4 悬浮延迟开合（openDelay≈120ms / closeDelay≈80ms）', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      act(() => root.render(createElement(PlanList, { items: golden17() })));
      const after = container.querySelector<HTMLButtonElement>(
        '.plan-list-fold-after .plan-list-fold-toggle',
      )!;
      act(() => fireEnter(after));
      // 未到延迟：仍收起
      expect(after.getAttribute('aria-expanded')).toBe('false');
      act(() => {
        vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS);
      });
      expect(after.getAttribute('aria-expanded')).toBe('true');
      expect(container.querySelector('[data-plan-id="p15"]')?.hasAttribute('hidden')).toBe(false);
      // 离开 → 延迟关闭
      act(() => fireLeave(after));
      expect(after.getAttribute('aria-expanded')).toBe('true');
      act(() => {
        vi.advanceTimersByTime(HOVER_CLOSE_DELAY_MS);
      });
      expect(after.getAttribute('aria-expanded')).toBe('false');
      expect(container.querySelector('[data-plan-id="p15"]')?.hasAttribute('hidden')).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('AC4 互斥：打开一组自动收起另一组', () => {
    act(() => root.render(createElement(PlanList, { items: golden17() })));
    const before = container.querySelector<HTMLButtonElement>('.plan-list-fold-before .plan-list-fold-toggle')!;
    const after = container.querySelector<HTMLButtonElement>('.plan-list-fold-after .plan-list-fold-toggle')!;
    act(() => fireClick(before));
    expect(before.getAttribute('aria-expanded')).toBe('true');
    expect(after.getAttribute('aria-expanded')).toBe('false');
    act(() => fireClick(after));
    expect(after.getAttribute('aria-expanded')).toBe('true');
    expect(before.getAttribute('aria-expanded')).toBe('false');
    // 展开前组时前组 12 项可见
    act(() => fireClick(before));
    expect(before.getAttribute('aria-expanded')).toBe('true');
    expect(container.querySelector('[data-plan-id="c0"]')?.hasAttribute('hidden')).toBe(false);
    expect(container.querySelector('[data-plan-id="p11"]')?.hasAttribute('hidden')).toBe(false);
  });

  it('AC6 脏数据容错：双 in_progress 只高亮第一个，不崩', () => {
    act(() =>
      root.render(createElement(PlanList, { items: [item('a', 'in_progress'), item('b', 'in_progress')] })),
    );
    const currents = container.querySelectorAll('.plan-list-item-current');
    expect(currents.length).toBe(1);
    expect(currents[0].getAttribute('data-plan-id')).toBe('a');
  });

  it('AC6 脏数据容错：未知 status 按未完成渲染（○），不崩', () => {
    act(() => root.render(createElement(PlanList, { items: [item('x', 'cancelled' as unknown as string)] })));
    const row = container.querySelector('[data-plan-id="x"]');
    expect(row?.className).not.toContain('plan-list-item-done');
    expect(row?.textContent).toContain('○');
  });

  it('AC6 reconcile：状态翻转整表替换后，全部行 DOM 节点身份不变（已完成项不重新挂载）', () => {
    const next: PlanItem[] = [item('a', 'completed'), item('b', 'completed'), item('c', 'in_progress')];
    act(() => root.render(createElement(PlanList, { items: threeState })));
    const before = new Map(
      ['a', 'b', 'c'].map((id) => [id, container.querySelector(`[data-plan-id="${id}"]`)]),
    );
    act(() => root.render(createElement(PlanList, { items: next })));
    for (const id of ['a', 'b', 'c']) {
      expect(container.querySelector(`[data-plan-id="${id}"]`)).toBe(before.get(id));
    }
  });

  it('AC6 reconcile：>6 项折叠态下整表替换，行节点身份仍存续（key={item.id} 保结构）', () => {
    const first = golden17();
    act(() => root.render(createElement(PlanList, { items: first })));
    const nodes = new Map(
      first.map((it) => [it.id, container.querySelector(`[data-plan-id="${it.id}"]`)]),
    );
    const next = first.map((it) => ({ ...it, status: it.id === 'ip12' ? 'completed' : it.status }));
    act(() => root.render(createElement(PlanList, { items: next })));
    for (const it of first) {
      expect(container.querySelector(`[data-plan-id="${it.id}"]`)).toBe(nodes.get(it.id));
    }
  });
});
