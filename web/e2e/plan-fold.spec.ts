/** #864（W-27.1）进度清单窗口化折叠 —— focused e2e（真实渲染下悬浮弹出 + 点击固定）。
 *
 * 车道归属：Playwright e2e（同 context-usage.spec.ts 约定；mock SSE via routeApi，
 * 不依赖真后端）。数据注入 = 一条 `task/plan_updated`（17 项黄金夹具，AC5），
 * 复现真实投影管线（不变量 #22）。
 *
 * 覆盖：折叠行文案（前面 12 项 / 待处理 2 项）+ 3 项窗口 + 悬浮延迟弹出 +
 * 点击固定（离开不收起）+ 键盘（Enter/Space 原生 button 语义）+ 互斥。
 */

import { expect, test } from '@playwright/test';
import { SID, T, routeApi, sessionRow, type FrameSpec } from './fixtures';

const RUN = 'e2e-plan-run';

/** AC5 黄金夹具：11×completed + 1×pending + 1×in_progress + 4×pending = 17 项。 */
function planItems(): Array<Record<string, unknown>> {
  const rows: Array<Record<string, unknown>> = [];
  for (let i = 0; i < 11; i++) {
    rows.push({ id: `c${i}`, content: `已完成 ${i}`, activeForm: `完成 ${i}`, status: 'completed', source: 'agent' });
  }
  rows.push({ id: 'p11', content: '待办 11', activeForm: '处理 11', status: 'pending', source: 'agent' });
  rows.push({ id: 'ip12', content: '当前任务', activeForm: '正在执行当前任务', status: 'in_progress', source: 'agent' });
  for (let i = 13; i < 17; i++) {
    rows.push({ id: `p${i}`, content: `待办 ${i}`, activeForm: `处理 ${i}`, status: 'pending', source: 'agent' });
  }
  return rows;
}

const events: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '折叠夹具' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'task/plan_updated', data: { items: planItems() }, seq: 4, session_id: SID, run_id: RUN, time: T },
  { type: 'run/completed', data: {}, seq: 5, session_id: SID, run_id: RUN, time: T },
];

test('#864：黄金夹具窗口化折叠 + 悬浮弹出 + 点击固定 + 互斥', async ({ page }) => {
  await routeApi(page, { sessions: [sessionRow(SID)], events });
  await page.goto('/');
  await page.locator('.session-item').first().click();

  const list = page.locator('.plan-list');
  await expect(list).toBeVisible();
  await expect(list).toContainText('进程 17 项 · 已完成 11 项');
  const panelToggle = page.locator('.plan-list-summary-toggle');
  await expect(panelToggle).toHaveAttribute('aria-expanded', 'false');
  await panelToggle.click();
  await expect(panelToggle).toHaveAttribute('aria-expanded', 'true');
  const completedText = list.locator('.plan-list-item-done .plan-list-text').first();
  await expect(completedText).toHaveCSS('text-decoration-line', 'none');

  const before = page.locator('.plan-list-fold-before .plan-list-fold-toggle');
  const after = page.locator('.plan-list-fold-after .plan-list-fold-toggle');

  // ── 收起态：两条折叠行 + 3 项窗口 ──
  await expect(before).toHaveText('前面 12 项');
  await expect(after).toHaveText('待处理 2 项');
  await expect(before).toHaveAttribute('aria-expanded', 'false');
  await expect(after).toHaveAttribute('aria-expanded', 'false');
  await expect(list.locator('.plan-list-item:not([hidden])')).toHaveCount(3);
  await expect(list.locator('.plan-list-item[data-plan-id="p15"]')).toHaveAttribute('hidden', '');

  // ── 悬浮延迟弹出：待处理 2 项 → 收起 2 项待处理，末尾两项可见 ──
  await after.hover();
  await expect(after).toHaveAttribute('aria-expanded', 'true');
  await expect(after).toHaveText('收起 2 项待处理');
  await expect(list.locator('.plan-list-item[data-plan-id="p15"]')).not.toHaveAttribute('hidden', '');

  // 离开 → 延迟关闭
  await page.mouse.move(4, 4);
  await expect(after).toHaveAttribute('aria-expanded', 'false');

  // ── 点击固定：离开不收起 ──
  await after.click();
  await expect(after).toHaveAttribute('aria-expanded', 'true');
  await page.mouse.move(4, 4);
  await page.waitForTimeout(200); // 超过 closeDelay；固定态不得被悬浮离开收起
  await expect(after).toHaveAttribute('aria-expanded', 'true');
  await expect(after).toHaveText('收起 2 项待处理');

  // ── 互斥：点击前组 → 后组自动收起 ──
  await before.click();
  await expect(before).toHaveAttribute('aria-expanded', 'true');
  await expect(after).toHaveAttribute('aria-expanded', 'false');
  await expect(list.locator('.plan-list-item[data-plan-id="c0"]')).not.toHaveAttribute('hidden', '');

  // ── 键盘：原生 button 语义（Enter / Space 触发切换）──
  await before.focus();
  await page.keyboard.press('Space');
  await expect(before).toHaveAttribute('aria-expanded', 'false');
  await page.keyboard.press('Enter');
  await expect(before).toHaveAttribute('aria-expanded', 'true');
});

test('#1184 progress panel stays in layout without covering messages or composer', async ({ page }) => {
  await routeApi(page, { sessions: [sessionRow(SID)], events });
  await page.goto('/');
  await page.locator('.session-item').first().click();

  const toggle = page.locator('.plan-list-summary-toggle');
  const details = page.locator('.plan-list-details');
  const scroll = page.locator('.conversation-scroll');
  const composer = page.locator('.composer');

  for (const viewport of [
    { width: 1280, height: 800 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(toggle).toContainText('✓ 本次运行结束 11/17');

    const stripBox = await toggle.boundingBox();
    const collapsedScroll = await scroll.boundingBox();
    const collapsedComposer = await composer.boundingBox();
    expect(stripBox).not.toBeNull();
    expect(stripBox!.height).toBeLessThanOrEqual(30);

    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const detailsBox = await details.boundingBox();
    const expandedScroll = await scroll.boundingBox();
    const expandedComposer = await composer.boundingBox();
    expect(detailsBox).not.toBeNull();
    expect(detailsBox!.height).toBeLessThanOrEqual(viewport.height * 0.32 + 1);
    expect(expandedScroll!.y).toBeGreaterThanOrEqual(detailsBox!.y + detailsBox!.height - 1);
    expect(expandedScroll!.y + expandedScroll!.height).toBeLessThanOrEqual(expandedComposer!.y + 1);
    expect(expandedScroll!.y).toBeGreaterThan(collapsedScroll!.y);
    expect(expandedScroll!.height).toBeLessThan(collapsedScroll!.height);
    expect(expandedComposer!.y).toBeCloseTo(collapsedComposer!.y, 0);

    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  }
});
