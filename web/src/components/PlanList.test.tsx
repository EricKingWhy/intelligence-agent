// @vitest-environment jsdom
/** #381（W-27）：PlanList 四件套渲染（PRD 长任务 §7.5）+ 脏数据容错 + id 对齐 reconcile。
 *
 * 真投影 + createRoot（jumpPulse 同款）：「已完成项不重新挂载」必须 pin 到 DOM
 * 节点身份（票面 reconcile 判据），renderToString 给不了。完成组折叠用 `hidden`
 * 属性——保留 DOM 节点，切换只改属性不改结构。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { PlanList } from './PlanList';
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

describe('#381（W-27）：PlanList 渲染', () => {
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

  it('四件套③④完成项划线+勾、未完成空心圆；完成组默认收起（zcode 同构）', () => {
    act(() => root.render(createElement(PlanList, { items: threeState })));
    const done = container.querySelector('[data-plan-id="a"]');
    expect(done?.hasAttribute('hidden')).toBe(true);
    // 折叠的执行层断言：hidden 的 UA 规则会被 `.plan-list-item{display:flex}`
    // 盖过（真机第三次踩到，2026-10-02）——内联 display:none 才是跨样式表生效的
    // 保证，且 jsdom 断言得到（workspace-panel/detail-peek 两次只有属性断言，
    // CSS 侧回归全盲）。
    expect(done?.getAttribute('style')).toContain('display: none');
    expect(container.querySelector('[data-plan-id="c"]')?.hasAttribute('hidden')).toBe(false);
    expect(container.querySelector('[data-plan-id="c"]')?.getAttribute('style') ?? '').not.toContain('display: none');
    expect(container.querySelector('[data-plan-id="c"]')?.textContent).toContain('○');
    const toggle = container.querySelector<HTMLButtonElement>('.plan-list-toggle');
    expect(toggle?.getAttribute('aria-expanded')).toBe('false');
    act(() => {
      toggle?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    expect(toggle?.getAttribute('aria-expanded')).toBe('true');
    expect(done?.hasAttribute('hidden')).toBe(false);
    // 展开后 style 属性被 React 清成空串（不一定是删除）——断言语义「不再隐藏」。
    expect(done?.getAttribute('style') ?? '').not.toContain('display: none');
    expect(done?.className).toContain('plan-list-item-done');
    expect(done?.querySelector('svg')).not.toBeNull();
    expect(done?.textContent).toContain('任务 a');
  });

  it('脏数据容错：双 in_progress 只高亮第一个，不崩', () => {
    act(() =>
      root.render(createElement(PlanList, { items: [item('a', 'in_progress'), item('b', 'in_progress')] })),
    );
    const currents = container.querySelectorAll('.plan-list-item-current');
    expect(currents.length).toBe(1);
    expect(currents[0].getAttribute('data-plan-id')).toBe('a');
  });

  it('脏数据容错：未知 status 按未完成渲染（○），不崩', () => {
    act(() => root.render(createElement(PlanList, { items: [item('x', 'cancelled' as unknown as string)] })));
    const row = container.querySelector('[data-plan-id="x"]');
    expect(row?.className).not.toContain('plan-list-item-done');
    expect(row?.textContent).toContain('○');
  });

  it('reconcile：状态翻转整表替换后，全部行 DOM 节点身份不变（已完成项不重新挂载）', () => {
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
});
