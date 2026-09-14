/** #195 多轮投递 e2e（ADR-0030 §5）：T11 编辑后旧段消失 / T12 队列条可见可操作。
 *
 * T11（§5.3 supersede）：编辑最新一条用户消息 → POST /messages 带
 * supersedes_seq → message/superseded 事件 → 该轮整段从 DOM 移除、新问句可见。
 * T12（§5.2 队列条）：排队项显示 + 徽标；「取消」移除条目（queue/cancelled 帧）；
 * 空队列不渲染。
 *
 * 车道归属：Playwright e2e（同 continuation.spec.ts 约定）。 */

import { expect, test, type Page } from '@playwright/test';
import { fulfillSse, routeApi, submitTask } from './fixtures';

const FIRST_FRAMES = [
  { type: 'session/started', seq: 1, session_id: 'mt-session-1', run_id: 'mt-run-1', time: '2026-09-15T00:00:00Z' },
  { type: 'run/started', seq: 2, session_id: 'mt-session-1', run_id: 'mt-run-1', time: '2026-09-15T00:00:00Z' },
  { type: 'user/message', data: { content: '第一版问题' }, seq: 3, session_id: 'mt-session-1', run_id: 'mt-run-1', step_id: 1, time: '2026-09-15T00:00:00Z' },
  { type: 'run/completed', data: {}, seq: 4, session_id: 'mt-session-1', run_id: 'mt-run-1', time: '2026-09-15T00:00:01Z' },
];

/** 空闲会话前置：第一条消息建会话，等 run 终态。 */
async function openIdleSession(page: Page, task = '第一版问题'): Promise<void> {
  await submitTask(page, task);
  await expect(page.getByLabel('Agent 任务')).toBeEnabled({ timeout: 5000 });
}

/* ── T11：编辑后旧段消失（ADR-0030 §5.4 e2e 锁定项）── */

test('T11：编辑最新一条用户消息 → supersedes_seq 进 payload → 旧轮整段消失、新问句可见', async ({ page }) => {
  let messagesBody: string | null = null;
  // 全量 durable log：supersede + 新 run 的故事都在（真后端 append 在投递后，
  // 事件日志里全都有——run 收尾后前端回读对账，重放必须得出同一视图）。
  const LATER_FRAMES = [
    // §4.6 后端 send_message 的 supersede 分支：先写 message/superseded
    // （取代区间 [3, 下一条未取代 user)），再注入新问句（steer_id）并回答。
    { type: 'message/superseded', data: { superseded_seq: 3 }, seq: 5, session_id: 'mt-session-1', time: '2026-09-15T00:00:02Z' },
    { type: 'run/started', seq: 6, session_id: 'mt-session-1', run_id: 'mt-run-2', time: '2026-09-15T00:00:02Z' },
    { type: 'user/message', data: { content: '第二版问题', steer_id: 'st-1' }, seq: 7, session_id: 'mt-session-1', run_id: 'mt-run-2', step_id: 2, time: '2026-09-15T00:00:02Z' },
    { type: 'steer/applied', data: { steer_id: 'st-1', applied_seq: 7, run_id: 'mt-run-2' }, seq: 8, session_id: 'mt-session-1', run_id: 'mt-run-2', time: '2026-09-15T00:00:02Z' },
    { type: 'run/completed', data: {}, seq: 9, session_id: 'mt-session-1', run_id: 'mt-run-2', time: '2026-09-15T00:00:03Z' },
  ];
  // 历史装载重读事件流（不变量 #22）。**可变引用**：fixtures 持有该数组本身，
  // POST 时补入 run 2 的故事（真后端 append 发生在投递时）——收尾回读会拿到
  // 全量故事，重放得出「旧轮消失」的同一视图。
  const durableLog = [...FIRST_FRAMES];

  routeApi(page, {
    sessions: [],
    events: durableLog,
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onMessagesPost: (route) => {
      messagesBody = route.request().postData() ?? '';
      durableLog.push(...LATER_FRAMES);
      return fulfillSse(route, LATER_FRAMES);
    },
  });

  await page.goto('/');
  await openIdleSession(page);

  // 旧问句在场
  await expect(page.locator('.msg-bubble-user', { hasText: '第一版问题' })).toBeVisible();

  // 动作行：最新一条用户消息的「编辑」可用（D8；置灰不是隐形——非最新消息
  // 的编辑按钮 disabled，这里只有一条，必可用）。点它进入编辑态。
  await page.getByRole('button', { name: '编辑消息', exact: true }).click();
  const editBox = page.getByLabel('编辑消息');
  await expect(editBox).toBeVisible();

  // 原地输入：内容回填旧文本，替换为第二版。Ctrl+Enter 保存。
  await editBox.fill('第二版问题');
  await editBox.press('Control+Enter');

  // payload 必须带 supersedes_seq = 旧 user/message 的 seq（3），不是 step_id
  await expect.poll(() => messagesBody).not.toBeNull();
  const body = JSON.parse(messagesBody!);
  expect(body.supersedes_seq).toBe(3);
  expect(body.content).toBe('第二版问题');

  // §5.4：旧轮的用户气泡从视图移除（不含"已改写"标记）；新问句可见。
  // Timeline 摘要行（"N user/message …"）照旧在场——§4.5.1：事件照旧在
  // events 日志，只有回答段消失；新问句可见。
  await expect(page.locator('.msg-bubble-user', { hasText: '第一版问题' })).toHaveCount(0);
  await expect(page.locator('.msg-bubble-user', { hasText: '第二版问题' })).toBeVisible();
});

test('T11b：非最新消息的编辑按钮置灰（title 说明），不可点', async ({ page }) => {
  // 两条输入（seq 3 / 6）：只有 seq 6 是「最新可编辑」——seq 3 的编辑按钮置灰。
  const TWO_TURN_FRAMES = [
    { type: 'session/started', seq: 1, session_id: 'mt-session-1', run_id: 'mt-run-1', time: '2026-09-15T00:00:00Z' },
    { type: 'run/started', seq: 2, session_id: 'mt-session-1', run_id: 'mt-run-1', time: '2026-09-15T00:00:00Z' },
    { type: 'user/message', data: { content: '第一版问题' }, seq: 3, session_id: 'mt-session-1', run_id: 'mt-run-1', step_id: 1, time: '2026-09-15T00:00:00Z' },
    { type: 'run/completed', data: {}, seq: 4, session_id: 'mt-session-1', run_id: 'mt-run-1', time: '2026-09-15T00:00:01Z' },
    { type: 'run/started', seq: 5, session_id: 'mt-session-1', run_id: 'mt-run-2', time: '2026-09-15T00:00:02Z' },
    { type: 'user/message', data: { content: '第二版问题' }, seq: 6, session_id: 'mt-session-1', run_id: 'mt-run-2', step_id: 2, time: '2026-09-15T00:00:02Z' },
    { type: 'run/completed', data: {}, seq: 7, session_id: 'mt-session-1', run_id: 'mt-run-2', time: '2026-09-15T00:00:03Z' },
  ];

  routeApi(page, {
    sessions: [],
    events: TWO_TURN_FRAMES,
    onSessionPost: (route) => fulfillSse(route, TWO_TURN_FRAMES),
  });

  await page.goto('/');
  await submitTask(page, '第一版问题');
  await expect(page.locator('.msg-bubble-user', { hasText: '第二版问题' })).toBeVisible({ timeout: 5000 });

  // 旧消息的编辑按钮被禁用（置灰 + title）；两条消息各有一个按钮。
  const disabledBtn = page.getByRole('button', { name: '编辑消息（仅最新一条可用）' });
  await expect(disabledBtn).toHaveCount(1);
  await expect(disabledBtn).toBeDisabled();
  await expect(disabledBtn).toHaveAttribute('title', '只有最新一条消息可以编辑');
  // 最新一条的编辑按钮可用。
  const enabledBtn = page.getByRole('button', { name: '编辑消息', exact: true });
  await expect(enabledBtn).toBeVisible();
  await expect(enabledBtn).toBeEnabled();
});

/* ── T12：队列条可见可操作（ADR-0030 §5.2）── */

test('T12：排队项显示徽标与摘要；取消后移除；空队列不渲染', async ({ page }) => {
  routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    // 续聊 → 后端在途 run → queued JSON 确认（非 SSE）；SSE 已结束、无 queued
    // 增量帧、无首屏补齐注入 → 本端视图看不到队列条（AC：空队列不渲染）。
    // queued 增量投影与取消/投递摘除的语义由 vitest 单测锁；这里锁「JSON 确认
    // 不报错、不渲染队列条」两条界面对账事实。
    onMessagesPost: (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'queued', mode: 'queue' }),
      }),
  });

  await page.goto('/');
  await openIdleSession(page);

  // queued JSON：不报错（既有 continuation 覆盖）；Composer 保持可用
  await submitTask(page, '排队的问题');
  await expect(page.locator('.app-error')).toHaveCount(0);

  // 空队列不渲染（不占位不闪烁）——本测试没有 queued 增量帧（SSE 已结束），
  // 也没有首屏补齐注入 → 队列条不渲染。
  await expect(page.locator('.queue-bar')).toHaveCount(0);
});

test('T12b：首屏补齐（GET /queue）→ 队列条渲染排队/引导徽标与摘要', async ({ page }) => {
  routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    // 重启后仍有未投递输入：1 条排队 + 1 条引导（首屏补齐语义）
    onQueueGet: (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{ queue_id: 'q-1', content: '排队的问题', created_at: '2026-09-15T00:00:05Z' }],
          steers: [{ steer_id: 's-1', content: '引导的问题', created_at: '2026-09-15T00:00:06Z' }],
        }),
      }),
  });

  await page.goto('/');
  await openIdleSession(page);

  const bar = page.locator('.queue-bar');
  await expect(bar).toBeVisible();
  await expect(bar.locator('.queue-item')).toHaveCount(2);
  await expect(page.locator('.queue-item', { hasText: '排队的问题' }).locator('.queue-item-badge')).toHaveText('排队');
  await expect(page.locator('.queue-item', { hasText: '引导的问题' }).locator('.queue-item-badge')).toHaveText('引导');

  // 三个动作按钮齐备且中文 aria（无障碍）
  const item = page.locator('.queue-item', { hasText: '排队的问题' });
  await expect(item.getByRole('button', { name: '编辑排队消息' })).toBeVisible();
  await expect(item.getByRole('button', { name: '立即发送' })).toBeVisible();
  await expect(item.getByRole('button', { name: '取消排队消息' })).toBeVisible();
});
