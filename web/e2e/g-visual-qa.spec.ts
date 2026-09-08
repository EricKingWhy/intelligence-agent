/** F1-Fix3: 6 档宽度 GUI QA 回归。
 *  验证 composer-controls 在不同宽度下不溢出、不破坏响应式布局。
 *  B1 spec 要求：6 档宽度（1440/1280/1024/820/768/浅色）回归。 */

import { expect, test } from '@playwright/test';
import { routeApi, fulfillSse } from './fixtures';

const PERMISSION_MODES = [
  { id: 'auto', display_name: 'Auto Approve', description: '自动批准工具调用' },
  { id: 'ask', display_name: 'Ask Each Time', description: '每次工具调用都询问' },
];

const AGENT_PROFILES = [
  { id: 'main', display_name: 'Main', description: '通用编排代理（默认）' },
  { id: 'coding', display_name: 'Coding', description: '代码编辑专用' },
];

const REASONING_EFFORTS = [
  { id: 'minimal', display_name: 'Minimal', description: '最少推理开销' },
  { id: 'standard', display_name: 'Standard', description: '平衡推理深度' },
];

const frames = [
  { type: 'session/started', seq: 1, session_id: 'e2e-vqa', run_id: 'e2e-vqa-run', time: '2026-09-08T00:00:00Z' },
  { type: 'run/started', seq: 2, session_id: 'e2e-vqa', run_id: 'e2e-vqa-run', time: '2026-09-08T00:00:00Z' },
  { type: 'user/message', data: { content: 'GUI QA' }, seq: 3, session_id: 'e2e-vqa', run_id: 'e2e-vqa-run', step_id: 1, time: '2026-09-08T00:00:00Z' },
  { type: 'run/completed', data: {}, seq: 4, session_id: 'e2e-vqa', run_id: 'e2e-vqa-run', time: '2026-09-08T00:00:00Z' },
];

const WIDTHS = [1440, 1280, 1024, 820, 768];

for (const width of WIDTHS) {
  test(`Composer control row 在 ${width}px 宽度下不溢出`, async ({ page }) => {
    routeApi(page, {
      sessions: [],
      events: [],
      permissionModes: PERMISSION_MODES,
      agentProfiles: AGENT_PROFILES,
      reasoningEfforts: REASONING_EFFORTS,
      onSessionPost: (route) => fulfillSse(route, frames),
    });

    await page.setViewportSize({ width, height: 800 });
    await page.goto('/');

    // 控件行在场
    const controls = page.locator('.composer-controls');
    await expect(controls).toBeVisible();

    // 控件行不超出 composer-dock 边界
    const dockBox = await page.locator('.composer-dock').boundingBox();
    const controlsBox = await controls.boundingBox();
    expect(controlsBox).not.toBeNull();
    expect(dockBox).not.toBeNull();
    expect(controlsBox!.x).toBeGreaterThanOrEqual(dockBox!.x);
    expect(controlsBox!.x + controlsBox!.width).toBeLessThanOrEqual(dockBox!.x + dockBox!.width + 1);

    // 四个控件 trigger 在场（ModelPicker 空 → 不渲染；三个 ControlPicker 在场）
    await expect(page.locator('.composer-control')).toHaveCount(3);
  });
}

test('Composer control row 在浅色模式下可见', async ({ page }) => {
  routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
    onSessionPost: (route) => fulfillSse(route, frames),
  });

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto('/');

  // 切换到浅色模式
  await page.evaluate(() => {
    document.documentElement.setAttribute('data-theme', 'light');
  });

  // 控件行仍然可见
  const controls = page.locator('.composer-controls');
  await expect(controls).toBeVisible();

  // trigger 文本可读（非透明）
  const trigger = page.locator('.composer-control').first();
  const color = await trigger.evaluate((el) => window.getComputedStyle(el).color);
  expect(color).not.toBe('rgba(0, 0, 0, 0)');
});
