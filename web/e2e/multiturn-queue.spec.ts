/** #195 多轮投递 e2e（ADR-0030 §5）：T11 编辑后旧段消失 / T12 队列条可见可操作。
 *
 * T11（§5.3 supersede）：编辑最新一条用户消息 → POST /messages 带
 * supersedes_seq → message/superseded 事件 → 该轮整段从 DOM 移除、新问句可见。
 * T12（§5.2 队列条）：排队项显示 + 徽标；「取消」移除条目（queue/cancelled 帧）；
 * 空队列不渲染。
 * T12e（§5.1 发送键语义）：在途 run 时提交必须打到 /messages（mode=queue），
 * 不得另造会话——真机实测回归锁，见 docs/LIVE_BROWSER_TEST_20260916.md F4。
 * T12g/T12h/T12i（#219 的 Ctrl+Enter 丢消息修复）：成对锁「steer 只在后端说没有
 * 在途 run（409）时才回退成 queue」——T12g 锁不该回退，T12h 锁该回退，
 * T12i 锁带 queue_id 那一支的回退必须丢掉 queue_id。
 * 见 docs/LIVE_BROWSER_TEST_20260917.md §2.2（该缺陷的真机记录）。
 *
 * T12k-T12q（#221，机制全文见 ADR-0030 §13）：窗外落定的响应必须与窗内同等处置——
 * 四类非 2xx 各一条 + 迟到 2xx 收据（判错要纠正，且不得留下假「连接中断」）+
 * 迟到事件流（判对则不许动）+ **纠正之后的回退重投**（T12q：它失败时必须照样报错；
 * 纠正会推进「本次投递的代际」，报错守卫若沿用入口代际，就会把这条重投的失败当成
 * 「过期请求」静默丢弃，回到"消息没了、界面不说"的原症状）。
 *
 * 车道归属：Playwright e2e（同 continuation.spec.ts 约定）。 */

import { expect, test, type Page, type Route } from '@playwright/test';
import { fulfillSse, routeApi, submitTask, type FrameSpec } from './fixtures';

const FIRST_FRAMES = [
  { type: 'session/started', seq: 1, session_id: 'mt-session-1', run_id: 'mt-run-1', time: '2026-09-15T00:00:00Z' },
  { type: 'run/started', seq: 2, session_id: 'mt-session-1', run_id: 'mt-run-1', time: '2026-09-15T00:00:00Z' },
  { type: 'user/message', data: { content: '第一版问题' }, seq: 3, session_id: 'mt-session-1', run_id: 'mt-run-1', step_id: 1, time: '2026-09-15T00:00:00Z' },
  { type: 'model/started', seq: 4, session_id: 'mt-session-1', run_id: 'mt-run-1', step_id: 1, time: '2026-09-15T00:00:00Z' },
  { type: 'model/delta', data: { delta: '第一版回答' }, seq: 5, session_id: 'mt-session-1', run_id: 'mt-run-1', step_id: 1, time: '2026-09-15T00:00:00Z' },
  { type: 'model/completed', data: { content: '第一版回答' }, seq: 6, session_id: 'mt-session-1', run_id: 'mt-run-1', step_id: 1, time: '2026-09-15T00:00:01Z' },
  { type: 'run/completed', data: {}, seq: 7, session_id: 'mt-session-1', run_id: 'mt-run-1', time: '2026-09-15T00:00:01Z' },
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
    { type: 'message/superseded', data: { superseded_seq: 3 }, seq: 8, session_id: 'mt-session-1', time: '2026-09-15T00:00:02Z' },
    { type: 'run/started', seq: 9, session_id: 'mt-session-1', run_id: 'mt-run-2', time: '2026-09-15T00:00:02Z' },
    { type: 'user/message', data: { content: '第二版问题', steer_id: 'st-1' }, seq: 10, session_id: 'mt-session-1', run_id: 'mt-run-2', step_id: 2, time: '2026-09-15T00:00:02Z' },
    { type: 'steer/applied', data: { steer_id: 'st-1', applied_seq: 10, run_id: 'mt-run-2' }, seq: 11, session_id: 'mt-session-1', run_id: 'mt-run-2', time: '2026-09-15T00:00:02Z' },
    { type: 'run/completed', data: {}, seq: 12, session_id: 'mt-session-1', run_id: 'mt-run-2', time: '2026-09-15T00:00:03Z' },
  ];
  // 历史装载重读事件流（不变量 #22）。**可变引用**：fixtures 持有该数组本身，
  // POST 时补入 run 2 的故事（真后端 append 发生在投递时）——收尾回读会拿到
  // 全量故事，重放得出「旧轮消失」的同一视图。
  const durableLog = [...FIRST_FRAMES];

  await routeApi(page, {
    sessions: [],
    events: durableLog,
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    // 编辑保存 → /messages（supersede 分支）：launched SSE（新 run 回答新问句）；
    // 随后 durable log 追加全量故事（GET /events 回读会拿到它）。
    onMessagesPost: (route) => {
      messagesBody = route.request().postData() ?? '';
      durableLog.push(...LATER_FRAMES);
      return fulfillSse(route, LATER_FRAMES);
    },
  });

  await page.goto('/');
  await openIdleSession(page);

  // 旧问句在场；回答段（.msg-model）也在场——为锁 §4.5.1「问与答整段删除」，
  // FIRST_FRAMES 带 model/delta 输出（见下）。
  await expect(page.locator('.msg-bubble-user', { hasText: '第一版问题' })).toBeVisible();
  await expect(page.locator('.msg-model').first()).toBeVisible();

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

  // §5.4 / §4.5.1：旧轮**问与答整段**从视图移除（不含"已改写"标记）；新问句
  // 可见。Timeline 摘要行照旧在场——§4.5.1：事件照旧在 events 日志。
  await expect(page.locator('.msg-bubble-user', { hasText: '第一版问题' })).toHaveCount(0);
  await expect(page.locator('.msg-model', { hasText: '第一版回答' })).toHaveCount(0);
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

  await routeApi(page, {
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
  await routeApi(page, {
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
  await routeApi(page, {
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

/* ── T12c：「立即」必须带 queue_id（ADR-0030 §5.2 升级为 steer，先取消原项）── */

test('T12c：「立即」→ POST /messages 带 mode=steer 且带 queue_id（不重复投递）', async ({ page }) => {
  const bodies: Record<string, unknown>[] = [];
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onQueueGet: (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{ queue_id: 'q-1', content: '排队的问题', created_at: '2026-09-15T00:00:05Z' }],
          steers: [],
        }),
      }),
    onMessagesPost: (route) => {
      bodies.push(JSON.parse(route.request().postData() ?? '{}'));
      // steer 带 queue_id → 后端先取消原项、再注册 steer；steered JSON 确认。
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'steered', mode: 'steer' }),
      });
    },
  });

  await page.goto('/');
  await openIdleSession(page);

  const item = page.locator('.queue-item', { hasText: '排队的问题' });
  await item.getByRole('button', { name: '立即发送' }).click();

  await expect.poll(() => bodies.length).toBe(1);
  // 关键回归：queue_id 必须随请求带上——不带的话原排队项仍在队列里，
  // 终态驱动会把同一条内容再投递一次（同句被处理两遍）。
  expect(bodies[0]).toMatchObject({ mode: 'steer', queue_id: 'q-1', content: '排队的问题' });
});

/* ── T12f：带 queue_id 的 404 说的是「排队项没了」，不是「会话没了」── */

test('T12f：排队项 404 不得被说成「会话已不存在」（404 不唯一）', async ({ page }) => {
  /* 独立复审 P1-1：`/messages` 的 404 有两个来源——会话不存在，以及带 queue_id
   * 时目标排队项不存在（后端 `QueueItemNotFound`，ADR-0030 §5.2「queue_id 不存在
   * → 404」，错误面见后端 `web/domain_errors.py` 的 audit 表）。
   * 上一版修复把两者合成一句「会话已不存在（可能已被删除），请从左侧另选一个会话」，
   * 于是「另一个标签页刚把这条排队项消费掉」这种**会话完全正常**的情形，用户会被告知
   * 会话已被删除并被劝去离开它——事实错、下一步也错。本用例锁两者的区分。 */
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onQueueGet: (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{ queue_id: 'q-1', content: '排队的问题', created_at: '2026-09-15T00:00:05Z' }],
          steers: [],
        }),
      }),
    // 后端实况：该项已被消费/取消 → 404 QueueItemNotFound。
    onMessagesPost: (route) =>
      route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: "queue item 'q-1' not found, already consumed, or cancelled" }),
      }),
  });

  await page.goto('/');
  await openIdleSession(page);
  await page.locator('.queue-item', { hasText: '排队的问题' }).getByRole('button', { name: '立即发送' }).click();

  const err = page.locator('.app-error');
  await expect(err).toContainText('排队项已不存在');
  // 关键：不得把"排队项过期"谎报成"会话没了"（会话此时是好的）。
  await expect(err).not.toContainText('会话已不存在');
  await expect(err).not.toContainText('Send failed');
});

/* ── T12d：「编辑」是就地编辑，提交走 {content, queue_id}（ADR-0030 §5.2）── */

test('T12d：「编辑」就地改内容 → POST /messages 带 queue_id + 新内容', async ({ page }) => {
  const bodies: Record<string, unknown>[] = [];
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onQueueGet: (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{ queue_id: 'q-1', content: '排队的问题', created_at: '2026-09-15T00:00:05Z' }],
          steers: [],
        }),
      }),
    onMessagesPost: (route) => {
      bodies.push(JSON.parse(route.request().postData() ?? '{}'));
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'queued', mode: 'queue' }),
      });
    },
  });

  await page.goto('/');
  await openIdleSession(page);

  const item = page.locator('.queue-item', { hasText: '排队的问题' });
  await item.getByRole('button', { name: '编辑排队消息' }).click();
  // 就地编辑：该行原地变成输入框（不回填主 composer）。
  const editBox = page.getByLabel('编辑排队消息内容');
  await expect(editBox).toBeVisible();
  await expect(editBox).toHaveValue('排队的问题');
  await editBox.fill('改过的问题');
  await page.getByRole('button', { name: '保存排队消息' }).click();

  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0]).toMatchObject({ queue_id: 'q-1', content: '改过的问题' });
  // 主输入框没有被回填（就地编辑的语义边界）。
  await expect(page.getByLabel('Agent 任务')).toHaveValue('');
});

/* ── T12e：在途 run 时提交必须**排队**，不得另造会话（ADR-0030 §5.1）── */

/** 未终态的流：run 一直 running ⇒ `streaming` 恒真（与 composer-stream-actions
 *  spec 同一手法：mock 只给有限帧，模式停在 live）。 */
const LIVE_FRAMES: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: 'mt-live-1', run_id: 'mt-run-live', time: '2026-09-16T00:00:00Z' },
  { type: 'run/started', seq: 2, session_id: 'mt-live-1', run_id: 'mt-run-live', time: '2026-09-16T00:00:00Z' },
  { type: 'user/message', data: { content: '长任务' }, seq: 3, session_id: 'mt-live-1', run_id: 'mt-run-live', step_id: 1, time: '2026-09-16T00:00:00Z' },
];

test('T12e：在途 run 提交 → POST /messages(mode=queue)，且不产生第二个会话', async ({ page }) => {
  // 回归锁（真机实测，见 docs/LIVE_BROWSER_TEST_20260916.md F4）：修复前
  // `handleSubmit` 的 `selectedId && !streaming` 把「已有会话 + 在途 run」
  // 分流给了 submitTask（= **创建新会话**）。界面后果：用户对进行中任务的追问
  // 被拆成一个没有上下文的新会话，而发送按钮的 title 明写「Enter 排队」，
  // ADR-0030 §5.1 也规定 Enter = queue；队列条与 /queue 系列接口因此永不产生条目。
  // 本测试同时锁「打到哪个端点」与「mode 取值」两条，端点是关键——只锁模式的话，
  // 一旦有人又把分流写回去，mode 依然会是 queue 却发去了错的地方。
  let sessionPosts = 0;
  const messageBodies: Array<Record<string, unknown>> = [];
  const messageUrls: string[] = [];

  await routeApi(page, {
    sessions: [],
    events: LIVE_FRAMES,
    onSessionPost: (route) => {
      sessionPosts += 1;
      return fulfillSse(route, LIVE_FRAMES);
    },
    onMessagesPost: (route) => {
      messageUrls.push(route.request().url());
      messageBodies.push((route.request().postDataJSON() ?? {}) as Record<string, unknown>);
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'queued', mode: 'queue' }),
      });
    },
    // 服务端仍在跑：快照即全量、连接保持——这正是「流式中」的真相。
    onWs: () => ({ frames: [], hasActiveRun: true, ending: 'keep' }),
  });

  await page.goto('/');
  await submitTask(page, '长任务');

  // 在途的界面真相：停止按钮在场（发送按钮同时在场，ADR-0030 D10）。
  await expect(page.locator('.composer-stop')).toBeVisible({ timeout: 5000 });
  // 基线而不是硬编码 1：建立这一步本身就允许出现一次 POST /api/sessions
  // （**不**断言它必须等于 1——那会把"首条消息走哪条通道"也锁进来，
  // 而那由 selectedId 决定，与本次回归无关）。要锁的是**增量**。
  const sessionPostsBeforeFollowUp = sessionPosts;

  // 流式中提交（Enter 无修饰键 = queue）
  await expect(page.locator('.composer-stop')).toBeVisible();
  const box = page.getByLabel('Agent 任务');
  await box.fill('追问一句');
  await box.press('Enter');

  await expect.poll(() => messageBodies.length).toBe(1);
  // 三层一起锁：**(a) 发去哪个会话**、(b) 模式、(c) 内容。(a) 走 URL 路径而不是
  // body——`/messages` 的会话身份在 path 上，body 里根本没有 session_id 字段，
  // 只断言 body 会漏掉"发给另一个会话"这类回归。
  expect(messageUrls[0]).toMatch(/\/api\/sessions\/mt-live-1\/messages$/);
  expect(messageBodies[0]).toMatchObject({ content: '追问一句', mode: 'queue' });
  // 关键回归：**没有**第二个会话被创建（增量，不是绝对值）。
  expect(sessionPosts).toBe(sessionPostsBeforeFollowUp);
  // 追问的确切条数：多出一条 = 按钮/键位被绑了两次（重复提交）。
  expect(messageBodies).toHaveLength(1);
  await expect(page.locator('.app-error')).toHaveCount(0);
});

/* ── T12g：在途 steer 不得被回退逻辑误伤（与 T12h 成对）── */

test('T12g：在途 run + Ctrl+Enter → POST /messages(mode=steer)（未被降级成 queue）', async ({ page }) => {
  /* #219 引入的回退只在**后端回 409** 时触发（T12h），本用例锁它的反面：
     真有在途 run 时，steer 必须原样发出去。回退条件写宽了（例如拿本页的
     `streaming` 当服务端在途判据）就会把真正该打断的 steer 静默降级为排队——
     语义不同且不报错，只有这条断言能发现。见
     docs/LIVE_BROWSER_TEST_20260917.md §2.2。 */
  const messageBodies: Array<Record<string, unknown>> = [];
  const messageUrls: string[] = [];

  await routeApi(page, {
    sessions: [],
    events: LIVE_FRAMES,
    onSessionPost: (route) => fulfillSse(route, LIVE_FRAMES),
    onMessagesPost: (route) => {
      const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      messageUrls.push(route.request().url());
      messageBodies.push(body);
      // 回执按请求的 mode 原样回应：mock 自己编一个 mode 会把"前端发了什么"
      // 与"它拿到什么"搅在一起。
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'steered', mode: body.mode }),
      });
    },
    onWs: () => ({ frames: [], hasActiveRun: true, ending: 'keep' }),
  });

  await page.goto('/');
  await submitTask(page, '长任务');
  await expect(page.locator('.composer-stop')).toBeVisible({ timeout: 5000 });

  const box = page.getByLabel('Agent 任务');
  await box.fill('打断一下');
  await box.press('Control+Enter');

  await expect.poll(() => messageBodies.length).toBe(1);
  expect(messageUrls[0]).toMatch(/\/api\/sessions\/mt-live-1\/messages$/);
  expect(messageBodies[0]).toMatchObject({ content: '打断一下', mode: 'steer' });
  await expect(page.locator('.app-error')).toHaveCount(0);
});

/* ── T12h：idle 会话上 Ctrl+Enter（steer）→ 后端 409 → 改投 queue，消息不丢 ── */

test('T12h：idle 会话 Ctrl+Enter 被后端 409 拒 → 自动改投 queue（不报「人工裁决」错，不丢消息）', async ({ page }) => {
  /* `/messages` 的 409 **同码不同因**：「此刻没有可打断的 run」（后端要求改用 queue）
     与 T8 #138 的「存在需人工裁决的 UNKNOWN 操作」。只认后者时，idle 上按 Ctrl+Enter
     会消息被拒 + 输入框已清空 + 弹一句"人工裁决"的错话（真机实测，见
     docs/LIVE_BROWSER_TEST_20260917.md §2.2）。本用例锁回退：**两次请求、模式依次
     steer→queue**，且不出现错误条（错误条 = 又把它说成人工裁决）。 */
  const bodies: Record<string, unknown>[] = [];
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onMessagesPost: (route) => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      bodies.push(body);
      // 第一次（steer）→ 后端 detail 逐字取自 service.py::SteerTargetNotFound
      if (body.mode === 'steer') {
        return route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ detail: "steer requires an active run; use mode='queue' to enqueue" }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'launched', mode: 'queue' }),
      });
    },
  });

  await page.goto('/');
  await openIdleSession(page); // 会话已选中且 run 已终结 = 没有可打断的在途 run

  const box = page.getByLabel('Agent 任务');
  await box.fill('空闲态打断一下');
  await box.press('Control+Enter');

  await expect.poll(() => bodies.length).toBe(2);
  expect(bodies[0]).toMatchObject({ content: '空闲态打断一下', mode: 'steer' });
  // 回退必须改的是**模式**，内容一字不差（这是"消息没丢"的机械证据）
  expect(bodies[1]).toMatchObject({ content: '空闲态打断一下', mode: 'queue' });
  await expect(page.locator('.app-error')).toHaveCount(0);
});

/* ── T12i：带 queue_id 的 steer 撞 409 → 重投必须**丢掉 queue_id** ── */

test('T12i：队列条「立即」在 idle 上被 409 拒 → 重投去掉 queue_id（否则撞 404，消息真丢）', async ({ page }) => {
  /* service.py 的顺序是**先 cancel_queue 再判在途 run**（:779-788）：所以带 queue_id 的
     steer 走到 `SteerTargetNotFound` 时，那条排队项**已经被取消了**。重投要是照抄
     queue_id，后端找不到该项 → 404 QueueItemNotFound → 前端报"排队项已不存在，请刷新
     重试"——而用户的消息其实从没投出去过，且输入框早被清空。去掉 queue_id 重投，
     语义正好是"这条排队项立即作为新消息投递"。 */
  const bodies: Record<string, unknown>[] = [];
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onQueueGet: (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{ queue_id: 'q-9', content: '立即发这条', created_at: '2026-09-15T00:00:05Z' }],
          steers: [],
        }),
      }),
    onMessagesPost: (route) => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      bodies.push(body);
      if (body.mode === 'steer') {
        return route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ detail: "steer requires an active run; use mode='queue' to enqueue" }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'launched', mode: 'queue' }),
      });
    },
  });

  await page.goto('/');
  await openIdleSession(page);
  await page.locator('.queue-item', { hasText: '立即发这条' })
    .getByRole('button', { name: '立即发送' }).click();

  await expect.poll(() => bodies.length).toBe(2);
  expect(bodies[0]).toMatchObject({ content: '立即发这条', mode: 'steer', queue_id: 'q-9' });
  expect(bodies[1]).toMatchObject({ content: '立即发这条', mode: 'queue' });
  expect(bodies[1]).not.toHaveProperty('queue_id'); // ← 本用例的关键断言
  await expect(page.locator('.app-error')).toHaveCount(0);
});

/* ── T12j：与 steer 无关的 409 → 必须仍然原样显示后端 detail ── */

test('T12j：steer 撞上**无关**的 409（人工裁决）→ 原样显示后端 detail，不被"是否 steer 409"的判断吃掉 body', async ({ page }) => {
  /* 409 的 detail 现在只读一次、由判别与呈现共用（`readErrorDetail` → `settle`）。
     若把它当成"只给判别用"，人工裁决这一支就会退化成一句泛化文案——
     而 detail（工具名 / call id）是这条路径唯一的线索来源。 */
  const DECIDABLE_DETAIL = '存在需人工裁决的高峰操作：tool=bash call_id=call_abc123';
  await routeApi(page, {
    sessions: [],
    events: LIVE_FRAMES,
    onSessionPost: (route) => fulfillSse(route, LIVE_FRAMES),
    onMessagesPost: (route) =>
      route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: DECIDABLE_DETAIL }),
      }),
    onWs: () => ({ frames: [], hasActiveRun: true, ending: 'keep' }),
  });

  await page.goto('/');
  await submitTask(page, '长任务');
  await expect(page.locator('.composer-stop')).toBeVisible({ timeout: 5000 });

  const box = page.getByLabel('Agent 任务');
  await box.fill('打断一下');
  await box.press('Control+Enter');

  await expect(page.locator('.app-error')).toContainText('call_id=call_abc123');
});

/* ── #221：窗外落定的响应必须与窗内同等处置（机制见 ADR-0030 §13）──
 *
 * 五个用例：四类非 2xx 各一条（409 steer 打空 / 409 人工裁决 / 404 / 422），
 * 外加迟到 2xx JSON 收据一条（锁「纠正接错流要推进代际」，否则会弹假「连接中断」）。
 * 一律用**延迟响应**构造窗外，而不是改窗口常量——窗口值是产品决策（1200ms 由
 * `src/hooks/useSession.test.ts` 钉住），测试不该改它，只要保证 `LATE_MS` 大于它。 */

/** 延迟到窗外才落定（> EARLY_RESPONSE_WINDOW_MS）。 */
const LATE_MS = 1600;
const answerLate = async (route: Route, status: number, body: unknown) => {
  await new Promise((r) => setTimeout(r, LATE_MS));
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
};

test('T12k：迟到的 409（steer 打空）→ 与窗内一致改投 queue，消息不丢', async ({ page }) => {
  const bodies: Record<string, unknown>[] = [];
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onMessagesPost: async (route) => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      bodies.push(body);
      if (body.mode === 'steer') {
        // 窗外：响应先落进"判为 launched"，再由迟到分支纠正
        return answerLate(route, 409, { detail: "steer requires an active run; use mode='queue' to enqueue" });
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'launched', mode: 'queue' }),
      });
    },
  });

  await page.goto('/');
  await openIdleSession(page);

  const box = page.getByLabel('Agent 任务');
  await box.fill('慢链路下打断一下');
  await box.press('Control+Enter');

  await expect.poll(() => bodies.length, { timeout: 8000 }).toBe(2);
  expect(bodies[0]).toMatchObject({ content: '慢链路下打断一下', mode: 'steer' });
  // 迟到的回退同样只改模式、内容一字不差（"消息没丢"的机械证据）
  expect(bodies[1]).toMatchObject({ content: '慢链路下打断一下', mode: 'queue' });
  await expect(page.locator('.app-error')).toHaveCount(0);
});

test('T12l：迟到的 409（人工裁决）→ 原样显示后端 detail（不被吞成无反馈）', async ({ page }) => {
  const DECIDABLE = '存在需人工裁决的高风险操作：tool=bash call_id=call_late_9';
  await routeApi(page, {
    sessions: [],
    events: LIVE_FRAMES,
    onSessionPost: (route) => fulfillSse(route, LIVE_FRAMES),
    onMessagesPost: (route) => answerLate(route, 409, { detail: DECIDABLE }),
    onWs: () => ({ frames: [], hasActiveRun: true, ending: 'keep' }),
  });

  await page.goto('/');
  await submitTask(page, '长任务');
  await expect(page.locator('.composer-stop')).toBeVisible({ timeout: 5000 });

  const box = page.getByLabel('Agent 任务');
  await box.fill('打断一下');
  await box.press('Control+Enter');

  await expect(page.locator('.app-error')).toContainText('call_id=call_late_9');
});

test('T12m：迟到的 404 → 说出「会话已不存在」（不静默丢消息）', async ({ page }) => {
  const bodies: Record<string, unknown>[] = [];
  let settled = false;
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onMessagesPost: async (route) => {
      bodies.push(JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>);
      await answerLate(route, 404, { detail: 'session not found' });
      settled = true;
    },
  });

  await page.goto('/');
  await openIdleSession(page);

  const box = page.getByLabel('Agent 任务');
  await box.fill('发往已删会话的一句');
  await box.press('Enter');

  // 确定性锚（#442 族3，同 T12k 的 bodies + poll 写法）：先等 POST 真的发出、且
  // **在窗外落定**回给浏览器（负载下 LATE_MS 的兑现本身可能被拖慢），再断言错误条
  // ——断言的 5s 预算全部花在前端消费上，不再和迟到落定抢同一个窗口。文案期望一字不改。
  await expect.poll(() => bodies.length, { timeout: 8000 }).toBeGreaterThanOrEqual(1);
  expect(bodies[0]).toMatchObject({ content: '发往已删会话的一句' });
  await expect.poll(() => settled, { timeout: 8000 }).toBe(true);
  await expect(page.locator('.app-error')).toContainText('会话已不存在');
});

test('T12n：迟到的 422 → 说出「续聊参数无效」（与窗内同一条文案）', async ({ page }) => {
  const bodies: Record<string, unknown>[] = [];
  let settled = false;
  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onMessagesPost: async (route) => {
      bodies.push(JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>);
      await answerLate(route, 422, { detail: 'bad params' });
      settled = true;
    },
  });

  await page.goto('/');
  await openIdleSession(page);

  const box = page.getByLabel('Agent 任务');
  await box.fill('参数不对的一句');
  await box.press('Enter');

  // 确定性锚（#442 族3）：同 T12m——先锚「POST 已发出且窗外落定」，再断言文案。
  await expect.poll(() => bodies.length, { timeout: 8000 }).toBeGreaterThanOrEqual(1);
  expect(bodies[0]).toMatchObject({ content: '参数不对的一句' });
  await expect.poll(() => settled, { timeout: 8000 }).toBe(true);
  await expect(page.locator('.app-error')).toContainText('续聊参数无效');
});


/* ── T12o：迟到的 2xx JSON 收据 → 必须没有假「连接中断」 ──
 *
 * 窗外已按「launched」接管了一条流，迟到落定却证明判错了（这是 queued 收据，不是事件流）。
 * 只 `cancel()` 那条流是不够的：它已经排定的重连定时器仍会跑完 500/1000/2000ms 三次退避，
 * 最后弹一条「连接中断（stream ended unexpectedly）：重试 3 次未成功」，把"消息其实已
 * 受理"这件事盖成一次假故障。所以纠正时**必须推进代际**让那条链整体失效（ADR-0030 §13
 * 第 4 条）。断言要活过那三轮退避（5s+）才看得见差别——这正是本条存在的理由。
 *
 * 纠正之后的落点（T12r 的修复）：收据说"消息已受理"，而 queued 只在一个 run 正在跑时
 * 才可能出现（ADR-0030 §2 术语表：queue 的投递边界 = 当前 run 的**终态之后**）⇒ 判错要
 * 纠正的是"接错了哪条流"，不是"接流这件事本身"。
 * 因此纠正会**换一条**流（游标接上本地实况），而不是把用户留在一个死画面上。 */

test('T12o：迟到的 2xx 收据（queued）→ 无假「连接中断」、无错误条', async ({ page }) => {
  const calls: Record<string, unknown>[] = [];
  const subs: Array<{ session_id: string; after_seq?: unknown }> = [];
  await routeApi(page, {
    sessions: [],
    events: [],
    wsSubscribes: subs,
    onSessionPost: (route) => fulfillSse(route, FIRST_FRAMES),
    onMessagesPost: (route) => {
      calls.push(JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>);
      // 窗外落定的**收据**（极短 JSON 被延迟到窗外，慢链路上的真实形态）
      return answerLate(route, 200, { status: 'queued', mode: 'queue' });
    },
    // 心跳 opt-in：本用例等待 6s 验证无假「连接中断」，真后端 2s 一次
    // server_ping 计入停摆看门狗活性（#420 AC1）；mock 缺省静默，高负载下
    // 累计静默超 10s 会被误判断流。只给本用例开，不碰全局默认。
    onWs: () => ({ ending: 'keep', pingIntervalMs: 2000 }),
  });

  await page.goto('/');
  await openIdleSession(page);

  const box = page.getByLabel('Agent 任务');
  await box.fill('慢链路上排个队');
  await box.press('Enter');

  await expect.poll(() => calls.length, { timeout: 8000 }).toBe(1);
  // 活过三轮退避（500 + 1000 + 2000）＋余量：假「连接中断」正是在那之后才弹出来
  await page.waitForTimeout(6000);
  await expect(page.locator('.app-error')).toHaveCount(0);
  await expect(page.locator('.reconnect-banner')).toBeHidden();
  /* 订阅恰好两次：① 窗外按 launched 接的那条（已被纠正，代际推进后它的整条重连链
     失效）；② 纠正后由 ack 分支**换**上的那条（本地游标续接）。要锁的是"接错的流
     没有被重连复活"——那种复活会在上面留下假「连接中断」，也会把这里推成 3 次以上
     （每轮退避各一条）。 */
  expect(subs).toHaveLength(2);
});

/* ── T12p：迟到的**事件流**响应 → 当初判 launched 是对的，WS 继续收（不必也不许再接一条）──
 *
 * 窗外那条支路并非只在判错时才走到：交付层攒包时，正常流式的**响应头本身**就会
 * 晚于窗口到达，于是"迟到 + 事件流"是主路径而非例外。它必须保持原样——把迟到的
 * 事件流也当成"判错了"去 cancel，会直接把正在看的 run 掐掉（用户看到流停在半路，
 * 且没有任何错误提示）。本用例同时是 T12o 的反面：一个说"判错了要纠正"，一个说
 * "判对了别动它"。 */

test('T12p：迟到的事件流响应 → WS 继续收该 run 的输出（不掐流、不重订阅）', async ({ page }) => {
  const subs: Array<{ session_id: string; after_seq?: unknown }> = [];
  const DELTA: FrameSpec = {
    type: 'model/delta', data: { delta: '延迟启动的回答' }, seq: 4,
    session_id: 'mt-live-1', run_id: 'mt-run-live', step_id: 1, time: '2026-09-16T00:00:01Z',
  };
  await routeApi(page, {
    sessions: [],
    events: LIVE_FRAMES,
    wsSubscribes: subs,
    onSessionPost: (route) => fulfillSse(route, LIVE_FRAMES),
    // 响应头晚于窗口才到（交付层攒包的真实形态）：这是事件流，不是失败
    onMessagesPost: async (route) => {
      await new Promise((r) => setTimeout(r, LATE_MS));
      await fulfillSse(route, [DELTA]);
    },
    // 帧**必须晚于迟到的响应头**才到：否则"文本已上屏"这件事在 cancel 之前就发生了，
    // 断言对「掐流」这个变异毫无判别力（实测踩过——第一版 delayMs 没设，变异照样绿）。
    onWs: () => ({
      events: LIVE_FRAMES, frames: [DELTA], hasActiveRun: true, ending: 'keep',
      delayMs: LATE_MS + 1000, pingIntervalMs: 2000,
    }),
  });

  await page.goto('/');
  await submitTask(page, '长任务');
  await expect(page.locator('.composer-stop')).toBeVisible({ timeout: 5000 });

  /* mock 的一次性 SSE（fulfillSse：3 帧后包体即终）没有终态帧 ⇒ 客户端
   * onStreamEnd 必然走「stream ended unexpectedly」重连（500ms 退避）。真后端的
   * POST SSE 在 run 在途时不会提前收尾——这条重连是 mock 给不出长连接的副产品，
   * 也是**产品正确行为**（真机这一幕=代理掐流，客户端就该重连）。它构成一条
   * 「幻影订阅」。本用例曾断言 subs 恰好 1 条，等于假设 fill+Enter 永远赢下
   * 500ms 竞速——负载下必输（双 worker 实测 3/3 红，订阅 [3,3]）。等幻影落定
   * 把竞速变确定态：此后 Enter 作废旧代际，幻影链不可能再冒出新订阅。 */
  await expect.poll(() => subs.length, { timeout: 10_000 }).toBeGreaterThanOrEqual(1);

  const box = page.getByLabel('Agent 任务');
  await box.fill('慢链路下追问');
  await box.press('Enter');

  await expect(page.locator('.model-output').last()).toContainText('延迟启动的回答', { timeout: 12_000 });
  await expect(page.locator('.app-error')).toHaveCount(0);
  /* 恰好两条：① 幻影重连（上述 mock 副产品，Enter 之前已落定）；② 窗口到期时
   * launched 分支接的那条。**锁的是「Enter 之后只允许这一条」**：迟到的响应头
   * 落定（settle）若被当成「判错了」去 cancel + 重接，这里就会多出第三条——
   * 那是本用例真正要灭的变异（掐掉正在收的 run）。幻影的快照/帧挂在 delayMs
   * 上：正常时序（fill+Enter 远快于 delayMs）落在 Enter 之后、被代际守卫丢弃，
   * 文本来自 ②；fill+Enter 慢于 delayMs 的边缘时序下，幻影会在 Enter 前先送出
   * DELTA（旧代际当时仍是当前代际，帧会被应用），文本便来自 ①。两种时序下
   * 计数都是 2——本用例锁的是订阅数，不锚定上屏文本的来源流。 */
  expect(subs).toHaveLength(2);
});

/* ── T12q：纠正之后的**回退重投**失败时照样要报错 ──
 *
 * 纠正（ADR-0030 §13 第 4 条）会推进「本次投递的代际」，好让那条接错的流整体失效。
 * 而报错守卫若读的是 `sendFollowUp` 入口那个代际，就等于把自己刚推进的代际当成
 * 「用户换了会话」——回退重投的失败会被静默丢弃：消息已被后端拒掉、输入框早已清空、
 * 界面上一个字都没有。那正是本票要消灭的形状，所以这一条必须单独锁住
 * （T12k 只覆盖"回退成功"那一半）。 */
test('T12q：纠正后回退重投仍失败（409 人工裁决）→ 必须把原因说出来，不许静默', async ({ page }) => {
  const bodies: Record<string, unknown>[] = [];
  const DECIDABLE = '存在需人工裁决的高风险操作：tool=bash call_id=call_after_fallback';
  await routeApi(page, {
    sessions: [],
    events: LIVE_FRAMES,
    onSessionPost: (route) => fulfillSse(route, LIVE_FRAMES),
    onMessagesPost: (route) => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      bodies.push(body);
      if (body.mode === 'steer') {
        // 窗外：先判 launched，再由迟到分支纠正成 queue 重投（同 T12k）
        return answerLate(route, 409, { detail: "steer requires an active run; use mode='queue' to enqueue" });
      }
      // 回退重投**窗内**就落定，且这次是另一支 409（人工裁决）——它必须报出来
      return route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: DECIDABLE }),
      });
    },
    onWs: () => ({ frames: [], hasActiveRun: true, ending: 'keep' }),
  });

  await page.goto('/');
  await submitTask(page, '长任务');
  await expect(page.locator('.composer-stop')).toBeVisible({ timeout: 5000 });

  const box = page.getByLabel('Agent 任务');
  await box.fill('打断一下（回退也会被拒）');
  await box.press('Control+Enter');

  // 前置证据：确实走了「steer → 迟到 409 → 改投 queue」这条路（否则下面的断言无对象）
  await expect.poll(() => bodies.length, { timeout: 8000 }).toBe(2);
  expect(bodies[0]).toMatchObject({ content: '打断一下（回退也会被拒）', mode: 'steer' });
  expect(bodies[1]).toMatchObject({ content: '打断一下（回退也会被拒）', mode: 'queue' });

  await expect(page.locator('.app-error')).toContainText('call_id=call_after_fallback');
});

/* ── T12r：排队（ack）之后必须把 live 流接回来 ──
 *
 * 真机实测（`docs/LIVE_BROWSER_TEST_20260917.md` §9.4 F14）：在途 run 里按 Enter
 * 排队一条消息后，正在生成的回答**停止更新**——同一次运行内对照，排队前正文长度
 * 18→67 逐帧增长，排队后**停在 73 整整 11s 不动**；同一原因使队列项的摘除事件
 * （`queue/cancelled`）不再应用，「取消排队消息」点了界面毫无变化（服务端已取消）。
 *
 * 机制：`sendFollowUp` 入口就推进了代际（`streamGenRef.current += 1`），`onEvent`
 * 的 gen 守卫会把原流的后续帧全部丢弃；而 queued 的 ack 分支当时只
 * `setMode({kind:'viewing'})`（注释写「本次不接流」）——原流已作废、又不接新流，
 * 于是整场直播被一条排队项换掉。修复 = ack 分支按 launched 分支同一套写法重接流。
 *
 * 判别力从「本场只剩一次订阅机会」来：历史端点空日志（viewing 兜底失效）+ POST
 * 走窗内 SSE（不产生 WS），于是**唯一那次 WS 订阅只可能由 ack 分支发出**——修复前
 * 它一次都不发，那段文本永远不上屏（红证明：超时失败）。 */
test('T12r：在途 run 排队一条消息后，live 流必须继续收到并应用后续帧', async ({ page }) => {
  const subs: Array<{ session_id: string; after_seq?: unknown }> = [];
  // 序号接在 LIVE_FRAMES 的 max seq（3）之后：游标对齐后不构成 seq gap
  const AFTER_QUEUE: FrameSpec = {
    type: 'model/delta', data: { delta: '排队之后仍在写' }, seq: 4,
    session_id: 'mt-live-1', run_id: 'mt-run-live', step_id: 1, time: '2026-09-16T00:00:02Z',
  };
  await routeApi(page, {
    sessions: [],
    /* 历史端点返回**空日志**：让「viewing 迁移 → 历史装载 → 自动接流」这条
       兜底路径失效。否则本用例对修复毫无判别力——修复前 ack 分支置 viewing，
       那次迁移会顺带自动重接一次流，帧照样上屏（实测：第一版本用例在修复前
       也绿，属于假绿）。空日志 = 没有未收口的 run = 兜底不启动，于是
       ack 分支接的那条订阅是**唯一可能把新帧送进视图**的通道（初始 POST 的
       一次性 SSE 还会触发一条幻影重连订阅，但其帧被代际守卫丢弃——见尾部）。 */
    events: [],
    wsSubscribes: subs,
    onSessionPost: (route) => fulfillSse(route, LIVE_FRAMES),
    onMessagesPost: (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'queued', mode: 'queue' }),
      }),
    // 帧**必须晚于 Enter** 才到（delayMs）：初始 POST 的一次性 SSE 无终态收尾会
    // 触发一条 Enter 之前的幻影重连订阅（见用例尾部说明）——帧若即时到达，会在
    // 幻影流（旧代际）里被应用并推进本地游标，ack 分支的游标就成了时序彩票。
    // 挂到 delayMs 之后，幻影的帧落在 Enter 之后、被代际守卫整帧丢弃，游标才
    // 确定；正常时序下真正上屏的那一截只能来自 ack 分支接的流——判别力不变。
    // delayMs 取 LATE_MS+4000（≈5.6s）是给 fill+Enter 留富余量：poll 只等到幻影
    // **订阅落定**（≈T0+500ms），其帧要到 ≈T0+500+delayMs 才来——若 Enter 慢于
    // 这个差值（此前 2600ms 时余量仅 ~2.1s），幻影帧会在 Enter 前被应用、游标
    // 推到 4，ack 分支就会带 after_seq:4（负载下假红）。上屏断言的 8000ms 仍
    // 覆盖 ack 流的 delayMs 等待（5600 < 8000）。
    onWs: () => ({
      events: LIVE_FRAMES, frames: [AFTER_QUEUE], hasActiveRun: true, ending: 'keep',
      delayMs: LATE_MS + 4000, pingIntervalMs: 2000,
    }),
  });

  await page.goto('/');
  await submitTask(page, '长任务');
  await expect(page.locator('.composer-stop')).toBeVisible({ timeout: 5000 });

  // 等幻影重连落定（同 T12p 的说明：mock 一次性 SSE 无终态 ⇒ 500ms 重连是
  // 产品正确行为）。否则「Enter 赢 / 输 500ms 竞速」两种时序会给出演播
  // [1 条] 与 [2 条] 两种结局，下面的计数断言在负载下必炸（双 worker 实测）。
  await expect.poll(() => subs.length, { timeout: 10_000 }).toBeGreaterThanOrEqual(1);

  const box = page.getByLabel('Agent 任务');
  await box.fill('排队的一句');
  await box.press('Enter');

  // 关键断言：排队之后 run 的输出仍然上屏
  await expect(page.locator('.model-output').last()).toContainText('排队之后仍在写', { timeout: 8000 });
  await expect(page.locator('.app-error')).toHaveCount(0);
  // 恰好两条：① 幻影重连（mock 副产品，Enter 之前已落定，其帧被代际守卫丢弃）；
  // ② ack 分支接的那条。**锁的是「Enter 之后只允许这一条」**——修复前 ack 分支
  // 只置 viewing、一次都不接，那段文本永远不上屏；重连风暴（每轮退避各一条）
  // 也会把这里推过 2。
  expect(subs).toHaveLength(2);
  // 且 ② 必须带**本地游标**：不带就会从 -1 重发整段快照，旧终态会被重新投影，
  // 足以把「流还在跑」判成「已收尾」（#208 游标契约）。幻影帧被代际丢弃后
  // 本地游标停在 3，ack 分支接流必须原样带上它。
  expect(subs[1]).toMatchObject({ session_id: 'mt-live-1', after_seq: 3 });
});
