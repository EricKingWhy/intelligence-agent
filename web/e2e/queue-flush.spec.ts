/** 「立即发送全部」（POST /queue/flush，ADR-0030 D10）的传输轴回归锁（#205）。
 *
 * ## 为什么单开一个文件
 *
 * 交付层（CloudStudio Gateway / EdgeOne）会把**整个 HTTP 响应**攒到流结束才下发，
 * 于是 `await flushSessionQueue(...)` 在 run 跑完前不 resolve——「立即发送全部」点下去
 * 没有任何反应，等整轮答案答完才一起出现。flush 的 launched 分支因此也必须走 WS
 * 接流（`wsStreamResponse`），而不是读 SSE 响应体。
 *
 * 这条路此前**没有任何 e2e 覆盖**（`multiturn-queue` 的「立即」考的是逐项 steer，
 * 走 POST /messages；「立即发送全部」走的是 /queue/flush）。所以本文件按真机形状
 * 构造：flush 的响应**故意延迟**（等价攒包），回答必须经 WS 提前到达。
 *
 * ## 锁的三件事
 *
 * 1. **launched 不卡**：响应还在路上（6s）时，WS 帧已经把回答渲染出来；
 * 2. **游标不退化**：快照会重放整段历史（含上一轮终态），游标错了就会把新 run 误判
 *    「已收口」——断流不再重连。用例用「断线后必须重连」把它钉住；
 * 3. **idle / 404 / 409 三种非 launched 回执的语义**（静默 / 明确报错 / 重试）。 */

import { expect, test } from '@playwright/test';
import { RUN, SID, T, fulfillSse, routeApi, submitTask, type FrameSpec } from './fixtures';

/** 第一轮（已收口）：run 终态在 seq 7——正是「会被快照重放的旧终态」。 */
const FIRST: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '第一版问题' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'model/started', seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'model/delta', data: { delta: '第一版回答' }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'model/completed', data: { content: '第一版回答' }, seq: 6, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 7, session_id: SID, run_id: RUN, time: T },
];

/** flush 交付队首之后的新 run（seq 从 8 起）。 */
const DELIVERED_1 = { type: 'user/message', data: { content: '排队的问题' }, seq: 8, session_id: SID, run_id: 'run-2', step_id: 2, time: T };
const DELIVERED_2 = { type: 'text/delta', data: { delta: '投递后的回答。' }, seq: 9, session_id: SID, run_id: 'run-2', step_id: 2, time: T };
const DELIVERED_DONE = { type: 'run/completed', data: {}, seq: 10, session_id: SID, run_id: 'run-2', time: T };

const QUEUED = {
  items: [{ queue_id: 'q-1', content: '排队的问题', created_at: T }],
  steers: [],
};

/** 空闲会话 + 一条排队项（「立即发送全部」按钮的渲染条件）。 */
async function openWithQueued(page: import('@playwright/test').Page): Promise<void> {
  await submitTask(page, '第一版问题');
  await expect(page.getByLabel('Agent 任务')).toBeEnabled({ timeout: 5_000 });
  await expect(page.locator('.queue-item', { hasText: '排队的问题' })).toBeVisible();
}

const FLUSH_BUTTON = { name: '立即发送全部' } as const;

test('launched：flush 响应被攒包时，回答经 WS 提前到达（不再卡到 run 结束）', async ({ page }) => {
  let flushCalls = 0;
  await routeApi(page, {
    sessions: [],
    events: FIRST,
    onSessionPost: (route) => fulfillSse(route, FIRST),
    onQueueGet: (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(QUEUED) }),
    // 攒包等价物：响应头要等 run 结束才到（实测 44.2s，这里压到 6s 免得拖慢门禁）
    onFlushPost: async (route) => {
      flushCalls += 1;
      await new Promise((r) => setTimeout(r, 6_000));
      return fulfillSse(route, [DELIVERED_1, DELIVERED_2, DELIVERED_DONE]);
    },
    // 交付后新 run 的增量经 WS 逐帧到达（同一 after_seq 语义由客户端游标承担）
    onWs: () => ({ frames: [DELIVERED_1, DELIVERED_2], hasActiveRun: true, ending: 'keep' }),
  });

  await page.goto('/');
  await openWithQueued(page);

  await page.getByRole('button', FLUSH_BUTTON).click();

  // 决定性断言：回答在 flush 响应（6s）之前就渲染出来——旧实现在这里必然全红
  // （它要 await 到响应体，等于等 6s）。
  await expect(page.locator('.model-output').last()).toContainText('投递后的回答。', {
    timeout: 3_500,
  });
  // 排队项被 queue/consumed 帧摘除（真后端语义；这里由 WS 帧驱动的是文本，
  // 条目摘除的真相仍是事件流——本用例不假装它已被摘除）
  expect(flushCalls).toBe(1);
});

test('游标：快照重放的旧终态不得把新 run 判成「已收口」——断流后必须重连', async ({ page }) => {
  const wsCalls: number[] = [];
  // 可变 durable log（fixtures 持有该数组本身）：投递后新事件**真的**落进日志——
  // 真后端就是这样，而前端 run 收尾/回读时按 #22 对账（日志里没有的会从视图消失）。
  const log: FrameSpec[] = [...FIRST];
  await routeApi(page, {
    sessions: [],
    events: log,
    onSessionPost: (route) => fulfillSse(route, FIRST),
    onQueueGet: (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(QUEUED) }),
    onFlushPost: async (route) => {
      await new Promise((r) => setTimeout(r, 3_000)); // 攒包等价物：窗外交付
      return fulfillSse(route, [DELIVERED_1, DELIVERED_2]);
    },
    // 第 1 次接流：新事件已到（并落库）后**异常断流**（没有 done）。第 2 次 = 重连。
    onWs: ({ call }) => {
      wsCalls.push(call);
      if (call === 1) {
        log.push(DELIVERED_1, DELIVERED_2);
        return { frames: [DELIVERED_1, DELIVERED_2], hasActiveRun: true, ending: 'drop' };
      }
      // 重连后 run 仍在跑（连接保持）——本用例只考「有没有重连」，终态不是它的对象
      return { frames: [], hasActiveRun: true, ending: 'keep' };
    },
  });

  await page.goto('/');
  await openWithQueued(page);
  await page.getByRole('button', FLUSH_BUTTON).click();

  // 重连必须发生：游标若丢（快照里的 run/completed(seq 7) 被投影），
  // terminalSeenRef 会置真 ⇒ 断流被当成「自然终结」⇒ 这里只会有 1 次接流。
  await expect.poll(() => wsCalls.length, { message: '旧终态被重放 ⇒ 误判已收口 ⇒ 不再重连' }).toBe(2);
  await expect(page.locator('.model-output').last()).toContainText('投递后的回答。');
  // 重放的历史不重复：快照带全量 durable 事件（1..7），游标必须把它们滤掉。
  // 按**整段对话**计数（不是最后一块）——重放重复会多出一个块，而不是同块里翻倍。
  const text = (await page.locator('.model-output').allInnerTexts()).join('\n');
  expect(text.match(/第一版回答/g)).toHaveLength(1);
});

test('idle：空队列 → 静默（不接流、不报错、界面不动）', async ({ page }) => {
  let wsCalls = 0;
  await routeApi(page, {
    sessions: [],
    events: FIRST,
    onSessionPost: (route) => fulfillSse(route, FIRST),
    onQueueGet: (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(QUEUED) }),
    onFlushPost: (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'idle' }) }),
    onWs: () => {
      wsCalls += 1;
      return undefined;
    },
  });

  await page.goto('/');
  await openWithQueued(page);
  await page.getByRole('button', FLUSH_BUTTON).click();

  await page.waitForTimeout(1_500); // 越过判别窗口（1200ms）再断言
  expect(wsCalls).toBe(0); // idle = 没有新 run，接流无从谈起
  await expect(page.locator('.app-error')).toHaveCount(0); // 无动作即无反馈，不是报错
  await expect(page.getByLabel('Agent 任务')).toBeEnabled();
});

test('409：在途 run 未收口 → 重试到回执；三次仍 409 → 如实报错（不假装已投递）', async ({ page }) => {
  let flushCalls = 0;
  await routeApi(page, {
    sessions: [],
    events: FIRST,
    onSessionPost: (route) => fulfillSse(route, FIRST),
    onQueueGet: (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(QUEUED) }),
    onFlushPost: (route) => {
      flushCalls += 1;
      return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: '仍有在途 run' }) });
    },
  });

  await page.goto('/');
  await openWithQueued(page);
  await page.getByRole('button', FLUSH_BUTTON).click();

  // 后端 detail 原样转述（409 有两个来源：在途 run 未收口 / 需人工裁决的遗留操作
  // ——只有后端说得清是哪种，前端不替它编一句）
  await expect(page.locator('.app-error')).toContainText('投递失败：仍有在途 run', {
    timeout: 10_000,
  });
  expect(flushCalls).toBe(3); // 3 次（1s 间隔）——在途 run 收口前不放弃得太早
});

test('窗外才落定的 409：不得被吞成无反馈（判别是推断，推断错了要如实说）', async ({ page }) => {
  let wsCalls = 0;
  await routeApi(page, {
    sessions: [],
    events: FIRST,
    onSessionPost: (route) => fulfillSse(route, FIRST),
    onQueueGet: (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(QUEUED) }),
    // 回执比判别窗口（1200ms）晚到：客户端会先当成 launched 去接流，
    // 随后必须用真实回执纠正自己——404/409/idle 都不能静默。
    onFlushPost: async (route) => {
      await new Promise((r) => setTimeout(r, 1_800));
      return route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: '存在需人工裁决的高风险操作' }),
      });
    },
    onWs: () => {
      wsCalls += 1;
      return { frames: [], hasActiveRun: true, ending: 'keep' };
    },
  });

  await page.goto('/');
  await openWithQueued(page);
  await page.getByRole('button', FLUSH_BUTTON).click();

  // 后端的 detail 原样转述（409 的两个来源——在途 run / 需人工裁决——只有它说得清）
  await expect(page.locator('.app-error')).toContainText('投递失败：存在需人工裁决的高风险操作');
  // 那条「以为是 launched」的接流被收掉，不留服务端订阅
  await expect(page.getByLabel('Agent 任务')).toBeEnabled();
  expect(wsCalls).toBe(1);
});

test('窗外才落定的 idle：静默收流（不弹假「连接中断」）', async ({ page }) => {
  await routeApi(page, {
    sessions: [],
    events: FIRST,
    onSessionPost: (route) => fulfillSse(route, FIRST),
    onQueueGet: (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(QUEUED) }),
    onFlushPost: async (route) => {
      await new Promise((r) => setTimeout(r, 1_800)); // 同样晚于判别窗口
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'idle' }) });
    },
    onWs: () => ({ frames: [], hasActiveRun: true, ending: 'keep' }),
  });

  await page.goto('/');
  await openWithQueued(page);
  await page.getByRole('button', FLUSH_BUTTON).click();

  // 迟到的 idle 若被误判成 launched，就会挂着一条空流 → 停摆 → 假「连接中断」。
  // 这里等足够久（越过程序化重连的 3 次退避）确认什么都没冒出来。
  await page.waitForTimeout(6_000);
  await expect(page.locator('.app-error')).toHaveCount(0);
  await expect(page.locator('.reconnect-banner')).toBeHidden();
  await expect(page.getByLabel('Agent 任务')).toBeEnabled();
});

test('404：会话已不存在 → 明确报错，不静默吞掉', async ({ page }) => {
  await routeApi(page, {
    sessions: [],
    events: FIRST,
    onSessionPost: (route) => fulfillSse(route, FIRST),
    onQueueGet: (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(QUEUED) }),
    onFlushPost: (route) =>
      route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'session not found' }) }),
  });

  await page.goto('/');
  await openWithQueued(page);
  await page.getByRole('button', FLUSH_BUTTON).click();

  await expect(page.locator('.app-error')).toContainText('投递失败：会话不存在');
});
