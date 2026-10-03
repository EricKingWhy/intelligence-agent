/** #561（UI-01）e2e：外部启动 run 的接管——t3/t4/t6b 在外部触发路径下全部 PASS。
 *
 * 外部启动 = run 不由页面发起（POST /messages / CLI / 另一标签页）：页面在 viewing
 * 态没有任何订阅者，靠常驻轻量轮询读既有 GET /events 发现新事实后接管（在途 →
 * resumeLiveStream；全收口 → 增量追赶）。
 *
 * mock 手法：routeApi 的 `/events`、WS 快照共用**同一份 events 数组引用**（fixtures
 * 的同源契约）——测试里 push 新帧即「durable log 长出外部 run」，与真后端
 * 「先落盘、再（若有订阅者）经流广播」的事件序一致。t3 的流式帧同样先补进 durable
 * log 再作为 WS frames 下发（fixtures onSessionPost 的同一条纪律：durable log =
 * 刚才流的那些帧，否则终态后的历史回读会清掉流式答案）。
 *
 * 轮询间隔是生产常量（EXTERNAL_RUN_POLL_INTERVAL_MS = 5s），断言超时给 15s。
 */

import { expect, test, type Page } from '@playwright/test';
import { RUN, SID, T, routeApi, type FrameSpec } from './fixtures';

/** 空闲会话的 durable 历史：一个已收口的首轮（游标 = 5）。 */
const IDLE: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '第一条' }, seq: 2, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'text/delta', data: { delta: '首轮回答。' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'model/completed', data: { content: '首轮回答。' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 5, session_id: SID, run_id: RUN, time: T },
];

const sessionRow = (count: number) => ({
  session_id: SID, event_count: count, first_event_time: T, last_event_time: T,
  first_user_message: '第一条', trace_id: null, trace_url: null,
});

/** 打开会话并确认首轮内容已物化（外部启动前的静止基线）。 */
async function openIdleSession(page: Page): Promise<number> {
  await page.goto('/');
  await page.locator('.session-item').first().click();
  await expect(page.locator('.model-output').last()).toContainText('首轮回答。');
  return await page.locator('.timeline-row').count();
}

test('t3 外部触发：流式渲染——外部启动的在途 run 被接管并逐帧渲染', async ({ page }) => {
  const events: FrameSpec[] = [...IDLE];
  const wsSubscribes: Array<{ session_id: string; after_seq?: unknown }> = [];
  await routeApi(page, {
    sessions: [sessionRow(IDLE.length)],
    events,
    wsSubscribes,
    // 第 1 次 subscribe = 轮询接管：快照（同源 events）+ 在途 run 的增量帧。
    // 增量帧同时补进 durable log——终态后的历史回读不清掉流式答案。
    onWs: ({ call }) => {
      if (call !== 1) return undefined;
      events.push(
        { type: 'text/delta', data: { delta: '外部运行的流式回答。' }, seq: 8, session_id: SID, run_id: 'r-ext-1', step_id: 2, time: T },
        { type: 'model/completed', data: { content: '外部运行的流式回答。' }, seq: 9, session_id: SID, run_id: 'r-ext-1', step_id: 2, time: T },
        { type: 'run/completed', data: {}, seq: 10, session_id: SID, run_id: 'r-ext-1', time: T },
      );
      return {
        frames: [
          { type: 'text/delta', data: { delta: '外部运行的流式回答。' }, seq: 8, session_id: SID, run_id: 'r-ext-1', step_id: 2, time: T },
          { type: 'model/completed', data: { content: '外部运行的流式回答。' }, seq: 9, session_id: SID, run_id: 'r-ext-1', step_id: 2, time: T },
          { type: 'run/completed', data: {}, seq: 10, session_id: SID, run_id: 'r-ext-1', time: T },
        ],
      };
    },
  });

  const rowsBefore = await openIdleSession(page);

  // ── 外部启动：durable log 长出在途 run（等价 POST /messages 的落盘序列）──
  events.push(
    { type: 'run/started', seq: 6, session_id: SID, run_id: 'r-ext-1', time: T },
    { type: 'user/message', data: { content: '外部启动的第二轮' }, seq: 7, session_id: SID, run_id: 'r-ext-1', step_id: 2, time: T },
  );

  // 接管 = 恰好一条 WS 订阅（订阅数 ≤1），游标带上本地最大 seq（#208 契约）
  await expect.poll(() => wsSubscribes.length, { timeout: 15_000 }).toBe(1);
  expect(wsSubscribes[0]).toMatchObject({ session_id: SID, after_seq: 7 });

  // 流式文本渲染（t3 的核心断言：对话区不再纹丝不动）
  await expect(page.locator('.model-output').last()).toContainText('外部运行的流式回答。', { timeout: 15_000 });
  // 终态收尾回到 viewing，不重复接流
  await expect(page.locator('.timeline-row')).toHaveCount(rowsBefore + 5, { timeout: 15_000 });
  await expect.poll(() => wsSubscribes.length).toBe(1);
});

test('t4 外部触发：审批弹窗——外部 run 的审批卡可达且可决策', async ({ page }) => {
  const events: FrameSpec[] = [...IDLE];
  const wsSubscribes: Array<{ session_id: string; after_seq?: unknown }> = [];
  const approves: Array<{ path: string; body: unknown }> = [];
  await routeApi(page, {
    sessions: [sessionRow(IDLE.length)],
    events,
    wsSubscribes,
    // 审批等待期：有在途 run、无新帧、连接保持（不 done）——真后端等待决策的形态
    onWs: ({ call }) => (call === 1 ? { hasActiveRun: true, ending: 'keep' } : undefined),
    onApprovePost: async (route) => {
      // 同 n-approval-card：记 URL 路径——只断请求体会漏掉「打到别的会话」
      approves.push({
        path: new URL(route.request().url()).pathname,
        body: route.request().postDataJSON(),
      });
      await route.fulfill({
        status: 200,
        body: JSON.stringify({ status: 'resolved', approval_id: 'ap-1', decision: 'approve_once' }),
        contentType: 'application/json',
      });
    },
  });

  await openIdleSession(page);

  // 外部 run 打到审批：approval-requested 落盘，run 在途等待
  events.push(
    { type: 'run/started', seq: 6, session_id: SID, run_id: 'r-ext-2', time: T },
    { type: 'user/message', data: { content: '外部启动需要写文件' }, seq: 7, session_id: SID, run_id: 'r-ext-2', step_id: 2, time: T },
    {
      type: 'tool/approval-requested',
      data: {
        approval_id: 'ap-1',
        tool_name: 'write',
        tool_call_id: 'tc-1',
        action_type: 'workspace-write',
        title: 'write (workspace-write)',
        description: '工具授权级别为 workspace-write，但当前策略为只读（read-only）。',
        arguments_preview: { path: 'demo.txt', content: 'HELLO' },
        permission: 'workspace-write',
        policy: 'read-only',
        reason: '工具授权级别为 workspace-write，但当前策略为只读（read-only）。',
        allowed_decisions: ['deny', 'approve_once'],
      },
      seq: 8, session_id: SID, run_id: 'r-ext-2', step_id: 2, time: T,
    },
  );

  // 接管（一条订阅）→ 审批卡出现（90s 无弹窗 / 300s 超时 fail-closed 的原缺陷不再发生）
  await expect.poll(() => wsSubscribes.length, { timeout: 15_000 }).toBe(1);
  const card = page.locator('.approval-card');
  await expect(card).toBeVisible({ timeout: 15_000 });
  await expect(card.locator('.approval-tool code')).toHaveText('write');

  // 决策经既有通道提交：POST /approve 打到**本会话**、决策体正确
  await card.locator('.approval-approve').click();
  await expect(card.locator('.approval-title')).toHaveText('决策已提交');
  expect(approves).toHaveLength(1);
  expect(approves[0].path).toBe(`/api/sessions/${SID}/approve`);
  expect(approves[0].body).toMatchObject({ approval_id: 'ap-1', decision: 'approve_once' });
});

test('t6b 外部触发：错误态——两次轮询之间跑完且失败的外部 run 出现在对话区', async ({ page }) => {
  const events: FrameSpec[] = [...IDLE];
  const wsSubscribes: Array<{ session_id: string; after_seq?: unknown }> = [];
  await routeApi(page, {
    sessions: [sessionRow(IDLE.length)],
    events,
    wsSubscribes,
  });

  const rowsBefore = await openIdleSession(page);

  // 外部 run 整个跑完且失败（started → failed，全收口——无可接的流）
  events.push(
    { type: 'run/started', seq: 6, session_id: SID, run_id: 'r-ext-3', time: T },
    { type: 'user/message', data: { content: '外部启动然后失败' }, seq: 7, session_id: SID, run_id: 'r-ext-3', step_id: 2, time: T },
    { type: 'run/failed', data: { reason: 'provider_5xx' }, seq: 8, session_id: SID, run_id: 'r-ext-3', time: T },
  );

  // 增量追赶：不接任何流（订阅数恒 0），但失败 run 的事实全部出现在界面
  await expect(page.locator('.timeline-row')).toHaveCount(rowsBefore + 3, { timeout: 15_000 });
  await expect(page.locator('.msg.msg-user').last()).toContainText('外部启动然后失败');
  // 失败归因在 Inspector 的 Overview tab（ChatTab 的 RUN 摘要区）
  await page.getByRole('tab', { name: 'Overview' }).click();
  await expect(page.locator('.run-failure-val')).toContainText('provider_5xx', { timeout: 15_000 });
  await expect.poll(() => wsSubscribes.length).toBe(0);
});
