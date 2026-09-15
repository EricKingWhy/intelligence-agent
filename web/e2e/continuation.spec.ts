/** F1 续聊 e2e：验证续聊入口走 POST /api/sessions/{id}/messages 而非新会话。
 *
 * 流程：
 *   1. 发第一条消息 → 断言走 POST /api/sessions（新会话）
 *   2. 等 run/completed
 *   3. 发第二条消息 → 断言走 POST /api/sessions/{id}/messages（续聊）
 *
 * 车道归属：Playwright e2e（同 control-row.spec.ts 约定）。 */

import { expect, test, type Page } from '@playwright/test';
import {
  AGENT_PROFILES,
  MODELS,
  REASONING_EFFORTS,
  fulfillSse,
  pickControl,
  pickFirstModel,
  routeApi,
  submitTask,
} from './fixtures';

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

/** 发第一条消息建会话，等 run 终态（Composer 重新可用）——续聊前置。 */
async function openIdleSession(page: Page): Promise<void> {
  await submitTask(page, '第一条消息');
  await expect(page.getByLabel('Agent 任务')).toBeEnabled({ timeout: 5000 });
}

test('续聊：第二条消息走 /messages 端点而非新建会话', async ({ page }) => {
  let firstPostHit = false;
  let messagesPostHit = false;

  await routeApi(page, {
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

  // 第一条消息（新会话）
  await submitTask(page, '第一条消息');
  await expect.poll(() => firstPostHit).toBe(true);
  await expect(page.getByLabel('Agent 任务')).toBeEnabled({ timeout: 5000 });

  // 第二条消息（续聊）
  await submitTask(page, '第二条消息');
  await expect.poll(() => messagesPostHit).toBe(true);
});

test('续聊 amend 透传：所选 model / agent_profile / reasoning_effort 进 /messages payload', async ({ page }) => {
  let messagesBody: string | null = null;

  await routeApi(page, {
    sessions: [],
    events: [],
    models: MODELS,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
    // #201：UI 已无 context_providers 选择入口（多选控件删除）→ 断言该键不发
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onMessagesPost: (route) => {
      messagesBody = route.request().postData() ?? '';
      return fulfillSse(route, SECOND_FRAMES);
    },
  });

  await page.goto('/');
  await openIdleSession(page);

  // 选模型：目录第一行（catalog 里 default: true 的项）
  await pickFirstModel(page);
  // Agent 档位 → coding（第二项；下压 2 次，首项是「默认（未选）」）
  // 推理深度 → deep（第三项；下压 3 次）
  await pickControl(page, 'Agent Profile', 2, 'Coding');
  await pickControl(page, 'Reasoning Effort', 3, 'Deep');

  // 续聊发第二条
  await submitTask(page, '第二条消息');
  await expect.poll(() => messagesBody).not.toBeNull();

  const body = JSON.parse(messagesBody!);
  expect(body.content).toBe('第二条消息');
  expect(body.model).toBe(MODELS[0].name);
  expect(body.agent_profile).toBe('coding');
  expect(body.reasoning_effort).toBe('deep');
  // #201：UI 已无该键的入口 → 必然不发（后端默认全集）；契约面由 web/src/lib/api.test.ts 锁
  expect(body.context_providers).toBeUndefined();
  // /messages 的 amend 契约不含 permission_mode
  expect(body.permission_mode).toBeUndefined();
});

/* #201 删除：「所选 context_providers 进 /messages payload」这条 e2e 随多选控件一并下线。
 * 它唯一独有的覆盖是「UI 勾选 → App → useSession → api」这条链路，而该入口已不存在；
 * 契约面（`context_providers: string[]` 发键 / 空数组不发键）仍由 web/src/lib/api.test.ts
 * 的五条用例锁住，程序化调用路径未削弱。 */

test('续聊 queued：在途 run 的 JSON 确认不误报、不报错，amend 照发', async ({ page }) => {
  let messagesBody: string | null = null;

  await routeApi(page, {
    sessions: [],
    events: [],
    models: MODELS,
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onMessagesPost: (route) => {
      messagesBody = route.request().postData() ?? '';
      // 竞态：前端以为会话空闲、后端仍有在途 run → queued JSON 确认（非 SSE）。
      // 后端忽略 amend（runtime 已固定）；前端必须照发且不得报错/进入流式。
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'queued', mode: 'queue' }),
      });
    },
  });

  await page.goto('/');
  await openIdleSession(page);
  await pickFirstModel(page);

  await submitTask(page, '第二条消息');
  await expect.poll(() => messagesBody).not.toBeNull();

  expect(JSON.parse(messagesBody!).model).toBe(MODELS[0].name);
  // queued ≠ 新 run：不弹错误、不进入流式（Composer 保持可用）
  await expect(page.locator('.app-error')).toHaveCount(0);
  await expect(page.getByLabel('Agent 任务')).toBeEnabled();
});

test('续聊 422：提示「续聊参数无效」而非「未知模型」（handoff §5 P2）', async ({ page }) => {
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    // P1 修复后 /messages 的 422 可能是 session_id 非法 / 未知引用类 id /
    // 非法枚举取值——前端不做 detail 子串区分，统一提示刷新选项。
    onMessagesPost: (route) =>
      route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'unknown session id' }),
      }),
  });

  await page.goto('/');
  await openIdleSession(page);
  await submitTask(page, '第二条消息');

  const err = page.locator('.app-error');
  await expect(err).toContainText('续聊参数无效');
  await expect(err).not.toContainText('模型不可用');
});
