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
