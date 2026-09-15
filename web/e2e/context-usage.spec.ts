/** #200 上下文容量看板 e2e（设计稿 §5/§7 T6）：入口可见、六桶渲染、空态/未采集
 *  文案正确（不出现 0%）、阈值标记在场、Esc 可关闭。
 *
 * 车道归属：Playwright e2e（同 multiturn-queue.spec.ts 约定）。 */

import { expect, test, type Page } from '@playwright/test';
import { fulfillSse, routeApi, submitTask } from './fixtures';

const FIRST_FRAMES = [
  { type: 'session/started', seq: 1, session_id: 'ctx-session-1', run_id: 'ctx-run-1', time: '2026-09-15T00:00:00Z' },
  { type: 'run/started', seq: 2, session_id: 'ctx-session-1', run_id: 'ctx-run-1', time: '2026-09-15T00:00:00Z' },
  { type: 'user/message', data: { content: '看板问题' }, seq: 3, session_id: 'ctx-session-1', run_id: 'ctx-run-1', step_id: 1, time: '2026-09-15T00:00:00Z' },
  { type: 'run/completed', data: {}, seq: 4, session_id: 'ctx-session-1', run_id: 'ctx-run-1', time: '2026-09-15T00:00:01Z' },
];

/** 空闲会话前置。 */
async function openIdleSession(page: Page): Promise<void> {
  await submitTask(page, '看板问题');
  await expect(page.getByLabel('Agent 任务')).toBeEnabled({ timeout: 5000 });
}

test('T6a：空数据 → 「暂无用量数据」，不出现 0% 假话', async ({ page }) => {
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    // 缺省 mock = state no_data（fixtures 默认分支）
  });

  await page.goto('/');
  await openIdleSession(page);

  // TopBar Gauge 入口
  await page.getByRole('button', { name: '上下文容量' }).click();
  const dialog = page.getByRole('dialog', { name: '上下文容量' });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('暂无用量数据');
  // 不出现 0% 假话（诚实约束）
  await expect(dialog).not.toContainText('0.0%');
  // 「估算值」副标题在场
  await expect(dialog).toContainText('估算值');
});

test('T6b：有数据 → 六桶图例 + 分段条 + 阈值标记 + 缓存命中率', async ({ page }) => {
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onContextUsageGet: (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          estimated: true,
          window_tokens: 200000,
          used_tokens: 48123,
          thresholds: { auto_compact: 0.7, hard_guard: 0.85 },
          breakdown: {
            messages: 21000,
            system_prompt: 8000,
            skills: 1500,
            other: 623,
            tools: { system: 16000, mcp: 1000 },
          },
          cache: { state: 'ok', reported_calls: 12, total_calls: 12, avg_hit_rate: 0.996 },
          state: 'ok',
        }),
      }),
  });

  await page.goto('/');
  await openIdleSession(page);

  await page.getByRole('button', { name: '上下文容量' }).click();
  const dialog = page.getByRole('dialog', { name: '上下文容量' });
  await expect(dialog).toBeVisible();

  // 总量行（图 4 形态：18.3万/35万（52.2%）语言的同构）
  await expect(dialog).toContainText('48,123 / 200,000');
  // 六桶图例全渲染（不隐藏数据）
  for (const label of ['消息', '系统提示词', '技能', '系统工具', 'MCP 工具', '其他']) {
    await expect(dialog).toContainText(label);
  }
  // 分段条在场且有分段（used>0）
  await expect(dialog.locator('.ctx-usage-bar .ctx-usage-seg').first()).toBeVisible();
  // 阈值标记两根（70%/85%）
  await expect(dialog.locator('.ctx-usage-mark-compact')).toHaveCount(1);
  await expect(dialog.locator('.ctx-usage-mark-hard')).toHaveCount(1);
  // 缓存命中率（大字百分比 + 口径）
  await expect(dialog).toContainText('平均缓存命中率');
  await expect(dialog).toContainText('99.6%');
});

test('T6c：未采集 → 「未采集（提供商未返回缓存明细）」，不显示 0%', async ({ page }) => {
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onContextUsageGet: (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          estimated: true,
          window_tokens: 200000,
          used_tokens: 10000,
          thresholds: { auto_compact: 0.7, hard_guard: 0.85 },
          breakdown: { messages: 8000, system_prompt: 2000, skills: 0, other: 0, tools: { system: 0, mcp: 0 } },
          cache: { state: 'not_collected', reported_calls: 0, total_calls: 3, avg_hit_rate: null },
          state: 'ok',
        }),
      }),
  });

  await page.goto('/');
  await openIdleSession(page);

  await page.getByRole('button', { name: '上下文容量' }).click();
  const dialog = page.getByRole('dialog', { name: '上下文容量' });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('未采集');
  await expect(dialog).toContainText('提供商未返回缓存明细');
  // 不出现 0.0% 假命中率
  await expect(dialog).not.toContainText('平均缓存命中率 0.0%');
});

test('T6d：Esc 关闭看板（与既有 picker 一致）', async ({ page }) => {
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
  });

  await page.goto('/');
  await openIdleSession(page);

  await page.getByRole('button', { name: '上下文容量' }).click();
  await expect(page.getByRole('dialog', { name: '上下文容量' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: '上下文容量' })).toHaveCount(0);
});
