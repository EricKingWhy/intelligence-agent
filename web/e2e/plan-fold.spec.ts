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
