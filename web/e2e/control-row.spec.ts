/** F1（Phase 2b）Composer control row e2e 交互测试。
 *
 * 验收项：
 *   - 四个控件 trigger 在场（ModelPicker + Permission/Agent/Reasoning）
 *   - 空目录隐藏入口（context-providers 返 [] → 控件不渲染）
 *   - 键盘打开浮层 + 方向键导航 + Enter 选档 → trigger 文本更新
 *   - Esc 关闭浮层（§19）
 *
 * 车道归属：Playwright e2e（同 model-picker.spec.ts 约定）。 */

import { expect, test } from '@playwright/test';
import {
  AGENT_PROFILES,
  PERMISSION_MODES,
  REASONING_EFFORTS,
  fulfillSse,
  pickControl,
  routeApi,
} from './fixtures';

test('Composer control row：四控件渲染 + 键盘选档 + Esc 关闭', async ({ page }) => {
  const frames = [
    { type: 'session/started', seq: 1, session_id: 'e2e-session-0001', run_id: 'e2e-run-0001', time: '2026-09-08T00:00:00Z' },
    { type: 'run/started', seq: 2, session_id: 'e2e-session-0001', run_id: 'e2e-run-0001', time: '2026-09-08T00:00:00Z' },
    { type: 'user/message', data: { content: '测试 control row' }, seq: 3, session_id: 'e2e-session-0001', run_id: 'e2e-run-0001', step_id: 1, time: '2026-09-08T00:00:00Z' },
    { type: 'run/completed', data: {}, seq: 4, session_id: 'e2e-session-0001', run_id: 'e2e-run-0001', time: '2026-09-08T00:00:00Z' },
  ];

  routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
    onSessionPost: (route) => fulfillSse(route, frames),
  });

  await page.goto('/');

  // 四个控件 trigger 在场
  const modelTrigger = page.locator('.composer-model[aria-label="模型选择"]');
  const permTrigger = page.locator('.composer-control[aria-label="权限模式"]');
  const agentTrigger = page.locator('.composer-control[aria-label="Agent Profile"]');
  const effortTrigger = page.locator('.composer-control[aria-label="Reasoning Effort"]');

  // ModelPicker 目录空时不渲染——这里没 mock models，所以 model-picker 不在场
  await expect(modelTrigger).toHaveCount(0);
  // 三个 ControlPicker 在场
  await expect(permTrigger).toBeVisible();
  await expect(agentTrigger).toBeVisible();
  await expect(effortTrigger).toBeVisible();

  // 键盘打开 Permission Mode 浮层
  await permTrigger.focus();
  await page.keyboard.press('Enter');
  // 浮层已开——listbox 恒可见（combobox 在短目录下会随搜索框隐藏，见 F-DEFER-1）
  await expect(page.locator('[role="listbox"]')).toBeVisible();
  // option 角色在场——至少 3 个（auto/ask/deny）
  await expect(page.locator('[role="option"]')).toHaveCount(3);

  // Esc 关闭浮层（§19）
  await page.keyboard.press('Escape');
  await expect(page.locator('[role="listbox"]')).toBeHidden();

  // 再次打开 + Enter 选第一个 option → trigger 文本更新
  await pickControl(page, '权限模式', 0, 'Auto Approve');
});

test('Composer control row：长目录搜索过滤 + 短目录隐藏搜索框', async ({ page }) => {
  // 权限模式 3 条 ≤ 5 → 搜索框隐藏；要测搜索需换长目录（F-DEFER-1）
  const LONG_MODES = Array.from({ length: 6 }, (_, i) => ({
    id: `mode-${i}`,
    display_name: i === 2 ? 'Ask Each Time' : `Mode ${i}`,
    description: `档位 ${i}`,
  }));

  routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: LONG_MODES,
  });

  await page.goto('/');

  const permTrigger = page.locator('.composer-control[aria-label="权限模式"]');
  await expect(permTrigger).toBeVisible();
  await permTrigger.focus();
  await page.keyboard.press('Enter');

  // 长目录（6 > 5）→ 搜索框可见、可交互
  const combo = page.getByRole('combobox', { name: '权限模式' });
  await expect(combo).toBeVisible();
  await expect(page.locator('[role="option"]')).toHaveCount(6);

  // 搜索过滤：键入「ask」只剩匹配项
  await page.keyboard.type('ask');
  await expect(page.locator('[role="option"]')).toHaveCount(1);
  await expect(page.locator('[role="option"]')).toContainText('Ask Each Time');
});

test('Composer control row：提交 payload 字段名对齐后端契约', async ({ page }) => {
  const frames = [
    { type: 'session/started', seq: 1, session_id: 'e2e-session-payload', run_id: 'e2e-run-payload', time: '2026-09-08T00:00:00Z' },
    { type: 'run/started', seq: 2, session_id: 'e2e-session-payload', run_id: 'e2e-run-payload', time: '2026-09-08T00:00:00Z' },
    { type: 'user/message', data: { content: 'payload 测试' }, seq: 3, session_id: 'e2e-session-payload', run_id: 'e2e-run-payload', step_id: 1, time: '2026-09-08T00:00:00Z' },
    { type: 'run/completed', data: {}, seq: 4, session_id: 'e2e-session-payload', run_id: 'e2e-run-payload', time: '2026-09-08T00:00:00Z' },
  ];

  let capturedBody: string | null = null;

  routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
    onSessionPost: (route) => {
      const req = route.request();
      capturedBody = req.postData() ?? '';
      return fulfillSse(route, frames);
    },
  });

  await page.goto('/');

  // 选 Permission Mode → auto / Agent Profile → coding / Reasoning Effort → deep
  await pickControl(page, '权限模式', 0, 'Auto Approve');
  await pickControl(page, 'Agent Profile', 1, 'Coding');
  await pickControl(page, 'Reasoning Effort', 2, 'Deep');

  // 提交任务
  await page.getByLabel('Agent 任务').fill('payload 测试');
  await page.getByLabel('发送').click();

  // 等待 POST 被拦截
  await expect.poll(() => capturedBody).not.toBeNull();

  const body = JSON.parse(capturedBody!);
  // 字段名对齐后端 B1 契约
  expect(body.permission_mode).toBe('auto');
  expect(body.agent_profile).toBe('coding');
  expect(body.reasoning_effort).toBe('deep');
  // 未选 context_providers → 不传该字段
  expect(body.context_providers).toBeUndefined();
});
