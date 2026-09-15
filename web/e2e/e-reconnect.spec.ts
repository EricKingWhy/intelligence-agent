/** 场景 E（spec 01 §22 + 契约 §3）：断连重连——流异常收尾（无终态）→
 *  断线状态条 → 重新订阅 WS 并补齐缺口（快照按本地游标去重）→ 终态收尾；
 *  显式 cancel 不触发重连（T5）。
 *
 *  **锁什么 / 不锁什么**：这里锁「断线 → 条 → 重新订阅 → 零重复 → 清条」。
 *  游标本身在 WS 上没有可断言的载体（订阅只带 session_id，过滤在客户端做），
 *  而「重放被 seenSeqs 幂等门吸收」意味着游标丢了这里也照样绿——那条的可观测
 *  后果是「快照里的**旧终态**把新 run 判成已收口 ⇒ 断流不再重连」，由
 *  `queue-flush.spec.ts` 的「游标」用例锁（它构造了快照内含旧终态的形态）。 */

import { expect, test } from '@playwright/test';
import { RUN, SID, T, routeApi, fulfillSse, submitTask, type FrameSpec } from './fixtures';

test('断连重连：断线条在场 → 重新订阅补齐缺口 → 文本无重复 → 收尾清条', async ({ page }) => {
  const wsSessions: string[] = [];
  await routeApi(page, {
    // 初始流：部分帧后异常收尾（不伪造终态——重连信号）
    onSessionPost: (route) => fulfillSse(route, [
          { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
          { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
          { type: 'user/message', data: { content: '讲个故事' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
          { type: 'text/delta', data: { delta: '从前' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
        ]),
    // 终态后 viewing 迁移重读历史（#22 对账）——fixture 提供同一真相
    events: [
      { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
      { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
      { type: 'user/message', data: { content: '讲个故事' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
      { type: 'text/delta', data: { delta: '从前' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
      { type: 'text/delta', data: { delta: '有座山' }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T },
      { type: 'run/completed', data: {}, seq: 6, session_id: SID, run_id: RUN, time: T },
    ],
    // 重连：延迟 > 800ms 断线条显示阈值才考得到「条在场」；快照按缺省剧本给全量
    // durable 事件（1..6），其中 ≤ 本地游标(4) 的由客户端自己滤掉 → 零重叠。
    onWs: ({ sessionId }) => {
      wsSessions.push(sessionId);
      return { delayMs: 1500 };
    },
  });

  await page.goto('/');
  await submitTask(page, '讲个故事');

  // 断线条出现（800ms 延迟阈值后）
  await expect(page.locator('.reconnect-banner')).toBeVisible({ timeout: 5_000 });
  // 续传后终态：条消失，文本完整且 '从前' 只出现一次（重放无重复块）
  await expect(page.locator('.reconnect-banner')).toBeHidden({ timeout: 10_000 });
  const text = await page.locator('.model-output').last().innerText();
  expect(text).toContain('从前');
  expect(text).toContain('有座山');
  expect(text.match(/从前/g)).toHaveLength(1);
  // 重连契约：重新订阅**同一个会话**，且只订阅一次（单飞守卫）
  expect(wsSessions).toEqual([SID]);
});
