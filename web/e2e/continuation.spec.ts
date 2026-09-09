/** F1 续聊 e2e：验证续聊入口走 POST /api/sessions/{id}/messages 而非新会话。
 *
 * 流程：
 *   1. 发第一条消息 → 断言走 POST /api/sessions（新会话）
 *   2. 等 run/completed
 *   3. 发第二条消息 → 断言走 POST /api/sessions/{id}/messages（续聊）
 *
 * 车道归属：Playwright e2e（同 control-row.spec.ts 约定）。 */

import { expect, test } from '@playwright/test';
import { fulfillSse, routeApi } from './fixtures';

const FIRST_FRAMES = [
  { type: 'session/started', seq: 1, session_id: 'cont-session-1', run_id: 'cont-run-1', time: '2026-09-08T00:00:00Z' },
  { type: 'run/started', seq: 2, session_id: 'cont-session-1', run_id: 'cont-run-1', time: '2026-09-08T00:00:00Z' },
  { type: 'user/message', data: { content: '第一条消息' }, seq: 3, session_id: 'cont-session-1', run_id: 'cont-run-1', step_id: 1, time: '2026-09-08T00:00:00Z' },
  { type: 'run/completed', data: {}, seq: 4, session_id: 'cont-session-1', run_id: 'cont-run-1', time: '2026-09-08T00:00:00Z' },
];

const SECOND_FRAMES = [
  { type: 'run/started', seq: 5, session_id: 'cont-session-1', run_id: 'cont-run-2', time: '2026-09-08T00:00:01Z' },
  { type: 'user/message', data: { content: '第二条消息' }, seq: 6, session_id: 'cont-session-1', run_id: 'cont-run-2', step_id: 1, time: '2026-09-08T00:00:01Z' },
  { type: 'run/completed', data: {}, seq: 7, session_id: 'cont-session-1', run_id: 'cont-run-2', time: '2026-09-08T00:00:02Z' },
];

// 控制目录（续聊 amend 透传用）：与 control-row.spec.ts 同一形状。
const MODELS = [
  { name: 'deepseek-v4-flash-0731', provider: 'senseaudio', model: 'deepseek-v4-flash-0731', default: true },
  { name: 'qwen-max', provider: 'senseaudio', model: 'qwen3.8-max-0902', default: false },
];
const AGENT_PROFILES = [
  { id: 'main', display_name: 'Main', description: '通用编排代理（默认）' },
  { id: 'coding', display_name: 'Coding', description: '代码编辑、调试和构建任务专用' },
];
const REASONING_EFFORTS = [
  { id: 'minimal', display_name: 'Minimal', description: '最少推理开销。' },
  { id: 'standard', display_name: 'Standard', description: '平衡推理深度（默认）。' },
  { id: 'deep', display_name: 'Deep', description: '最多推理开销。' },
];

test('续聊：第二条消息走 /messages 端点而非新建会话', async ({ page }) => {
  let firstPostHit = false;
  let messagesPostHit = false;

  routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => {
      firstPostHit = true;
      return fulfillSse(route, FIRST_FRAMES);
    },
    onMessagesPost: (route) => {
      messagesPostHit = true;
      return fulfillSse(route, SECOND_FRAMES);
    },
  });

  await page.goto('/');

  // 第一条消息
  await page.getByLabel('Agent 任务').fill('第一条消息');
  await page.getByLabel('发送').click();

  // 等待第一条消息的 POST 被拦截
  await expect.poll(() => firstPostHit).toBe(true);

  // 等 run/completed 到达——textarea 重新可用
  await expect(page.getByLabel('Agent 任务')).toBeEnabled({ timeout: 5000 });

  // 第二条消息（续聊）
  await page.getByLabel('Agent 任务').fill('第二条消息');
  await page.getByLabel('发送').click();

  // 断言第二条走了 /messages 端点
  await expect.poll(() => messagesPostHit).toBe(true);
});

test('续聊 amend 透传：所选 model / agent_profile / reasoning_effort 进 /messages payload', async ({ page }) => {
  let messagesBody: string | null = null;

  routeApi(page, {
    sessions: [],
    events: [],
    models: MODELS,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
    // 目录非空但用户不选 → 断言 context_providers 不发键（有值才带）
    contextProviders: [
      { id: 'memory', display_name: 'Memory', description: '记忆检索' },
      { id: 'skills', display_name: 'Skills', description: '技能目录' },
    ],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onMessagesPost: (route) => {
      messagesBody = route.request().postData() ?? '';
      return fulfillSse(route, SECOND_FRAMES);
    },
  });

  await page.goto('/');

  // 第一条消息建会话，等终态
  await page.getByLabel('Agent 任务').fill('第一条消息');
  await page.getByLabel('发送').click();
  await expect(page.getByLabel('Agent 任务')).toBeEnabled({ timeout: 5000 });

  // 选模型：默认链之后第一个 = deepseek-v4-flash-0731
  const modelTrigger = page.locator('.composer-model[aria-label="模型选择"]');
  await modelTrigger.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('[role="combobox"]')).toBeVisible();
  await page.locator('[role="combobox"]').fill('');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(modelTrigger).toContainText('deepseek-v4-flash-0731');
  await page.keyboard.press('Escape');

  // 选 Agent Profile → coding（第二项）
  const agentTrigger = page.locator('.composer-control[aria-label="Agent Profile"]');
  await agentTrigger.focus();
  await page.keyboard.press('Enter');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(agentTrigger).toContainText('Coding');
  await page.keyboard.press('Escape');

  // 选 Reasoning Effort → deep（第三项）
  const effortTrigger = page.locator('.composer-control[aria-label="Reasoning Effort"]');
  await effortTrigger.focus();
  await page.keyboard.press('Enter');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(effortTrigger).toContainText('Deep');
  await page.keyboard.press('Escape');

  // 续聊发第二条
  await page.getByLabel('Agent 任务').fill('第二条消息');
  await page.getByLabel('发送').click();
  await expect.poll(() => messagesBody).not.toBeNull();

  const body = JSON.parse(messagesBody!);
  expect(body.content).toBe('第二条消息');
  expect(body.model).toBe('deepseek-v4-flash-0731');
  expect(body.agent_profile).toBe('coding');
  expect(body.reasoning_effort).toBe('deep');
  // 未选 context_providers → 不发键（有值才带，与 create 分支同模式）
  expect(body.context_providers).toBeUndefined();
  // /messages 的 amend 契约不含 permission_mode
  expect(body.permission_mode).toBeUndefined();
});
