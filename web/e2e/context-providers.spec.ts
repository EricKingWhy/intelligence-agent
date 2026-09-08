/** F3 e2e：Context Providers 多选控件交互。
 *
 * 验收项：
 *   - 目录非空 → ContextProviderPicker 渲染
 *   - 目录空 → 不渲染（return null）
 *   - 键盘 toggle 选中/取消
 *   - trigger 显示选中数量
 *   - POST body 的 context_providers 是 string[]
 *
 * 车道归属：Playwright e2e（同 control-row.spec.ts 约定）。 */

import { expect, test } from '@playwright/test';
import { fulfillSse, routeApi } from './fixtures';

const CONTEXT_PROVIDERS = [
  { id: 'memory', display_name: 'Memory', description: 'Inject relevant recalled memories scoped to the user into the model context.' },
  { id: 'skills', display_name: 'Skills', description: 'Inject the catalog of available skills (name + description) into the model context.' },
];

const PERMISSION_MODES = [
  { id: 'auto', display_name: 'Auto Approve', description: '自动批准工具调用' },
];

const frames = [
  { type: 'session/started', seq: 1, session_id: 'e2e-cp-session', run_id: 'e2e-cp-run', time: '2026-09-08T00:00:00Z' },
  { type: 'run/started', seq: 2, session_id: 'e2e-cp-session', run_id: 'e2e-cp-run', time: '2026-09-08T00:00:00Z' },
  { type: 'user/message', data: { content: 'context providers 测试' }, seq: 3, session_id: 'e2e-cp-session', run_id: 'e2e-cp-run', step_id: 1, time: '2026-09-08T00:00:00Z' },
  { type: 'run/completed', data: {}, seq: 4, session_id: 'e2e-cp-session', run_id: 'e2e-cp-run', time: '2026-09-08T00:00:00Z' },
];

test('ContextProviderPicker：目录非空 → 渲染；键盘 toggle + POST string[]', async ({ page }) => {
  let capturedBody: string | null = null;

  routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: PERMISSION_MODES,
    contextProviders: CONTEXT_PROVIDERS,
    onSessionPost: (route) => {
      const req = route.request();
      capturedBody = req.postData() ?? '';
      return fulfillSse(route, frames);
    },
  });

  await page.goto('/');

  // ContextProviderPicker 在场
  const ctxTrigger = page.locator('.composer-control[aria-label="Context Providers"]');
  await expect(ctxTrigger).toBeVisible();
  // 初始 placeholder——未选
  await expect(ctxTrigger).toContainText('Context');

  // 键盘打开浮层
  await ctxTrigger.focus();
  await page.keyboard.press('Enter');
  // cmdk 注入 combobox 角色
  await expect(page.locator('[role="combobox"]')).toBeVisible();
  // 两个 option 在场
  await expect(page.locator('[role="option"]')).toHaveCount(2);

  // Enter 选中第一项（memory）——多选模式不关闭 popover
  await page.keyboard.press('Enter');
  // trigger 显示选中数量
  await expect(ctxTrigger).toContainText('Context · 1');

  // ArrowDown 移到第二项（skills），Enter 选中
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(ctxTrigger).toContainText('Context · 2');

  // Esc 关闭浮层
  await page.keyboard.press('Escape');
  await expect(page.locator('[role="combobox"]')).toBeHidden();

  // 提交任务
  await page.getByLabel('Agent 任务').fill('cp 测试');
  await page.getByLabel('发送').click();

  // 等待 POST 被拦截
  await expect.poll(() => capturedBody).not.toBeNull();

  const body = JSON.parse(capturedBody!);
  // context_providers 是 string[]，包含 memory 和 skills
  expect(body.context_providers).toEqual(['memory', 'skills']);
});

test('ContextProviderPicker：目录空 → 不渲染', async ({ page }) => {
  routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: PERMISSION_MODES,
    // contextProviders 不传 → mock 返 [] → 空目录降级
    onSessionPost: (route) => fulfillSse(route, frames),
  });

  await page.goto('/');

  // ContextProviderPicker 不在场——空目录 return null
  const ctxTrigger = page.locator('.composer-control[aria-label="Context Providers"]');
  await expect(ctxTrigger).toHaveCount(0);
});
