/** 场景 O（FE-01/#148）：模型流停顿时的等待提示。
 *
 * 这是**展示层观察态**：不新增任何 SSE / SessionEvent、不落库，刷新即消失
 * （不变量 #4 Event ≠ Diagnostic Log、#22 不造第二套会话真相）。
 *
 * 真机验证的难点是「等 30 秒」——这里用 `page.clock` 快进**虚拟时钟**：浏览器是真的、
 * 渲染是真的，只有时钟被替换。不这么做，本 spec 要真等 30s+，门禁不可接受。
 *
 * 本 spec 锁两件事（`runState.test.ts` 另锁阈值与相位门）：
 * 1. 空闲基准是**上一次新事件**而不是流龄——退化成流龄计时会让这里必红；
 * 2. run 收口后不再提示。
 *
 * **没锁什么（如实划界）**：「工具执行中不提示」这条相位门在纯函数
 * `shouldShowWaitHint` 的单测里锁。它在本车道要精确编排事件序才构造得出，
 * 收益不抵成本（相位本身的真相在纯函数那一层）。
 *
 * 车道能力：WS 接流 mock 能**保持连接**（`ending: 'keep'`）——于是「长连接 + 停摆 →
 * 重连补一帧 → 继续长连接」这个真机形状在本车道能被如实构造（旧 `route.fulfill`
 * 给不出长连接：帧一次性到齐、包体立刻结束）。
 */

import { expect, test } from '@playwright/test';
import { RUN, SID, T, routeApi, fulfillSse, submitTask, type FrameSpec } from './fixtures';

/** 未终态的流：run 一直 running、模式一直 live。 */
const LIVE_FRAMES: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '慢慢回答' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'text/delta', data: { delta: '开了个头…' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
];

/** 停摆重连时补进来的**一条新事件**（seq 5）：它把「空闲基准」重置一次，
 *  之后的幂等重放（同 seq）会被按 seq 去重，不再算进展——也正因此，重连额度
 *  不会耗尽（真进展会 `observeProgress` 复位额度），live 态得以保持。 */
const MID_FRAMES: FrameSpec[] = [
  { type: 'text/delta', data: { delta: '又吐了一点。' }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T },
];

/** 从脉冲文案里取流龄秒数（`思考中 · 21s`）。 */
function pulseSec(text: string): number {
  return Number(text.match(/· (\d+)s/)?.[1]);
}

test('停顿提示锚的是「空闲」而非「流龄」：阈值前不出现；出现后秒数必须小于流龄', async ({ page }) => {
  let wsSubscribes = 0;
  await page.clock.install();
  await routeApi(page, {
    onSessionPost: (route) => fulfillSse(route, LIVE_FRAMES),
    // 停摆重连补进 MID_FRAMES（一条 seq 5 新事件）后**连接保持**——真后端在
    // run 未收口时就是这样，界面必须一直维持生成态（否则脉冲秒数无从比较）。
    onWs: () => {
      wsSubscribes += 1;
      return { frames: MID_FRAMES, ending: 'keep' };
    },
    events: LIVE_FRAMES,
  });

  await page.goto('/');
  await submitTask(page, '慢慢回答');

  // 前置条件：生成态真的起来了（否则后面的「不出现」可能只是因为什么都没跑）
  const pulse = page.locator('.run-pulse');
  await expect(pulse).toContainText('思考中');
  await expect(page.locator('.stream-caret')).toBeVisible();
  await expect(page.locator('.wait-hint')).toHaveCount(0); // 阈值以下：一个节点都不多

  // 推进到虚拟 21s：停摆重连（心跳 10s / 阈值 10s / 退避 500ms 起）已在途中补进
  // seq 5。定时器由虚拟时钟触发，但 **WS 帧的到达是实时的**——所以用轮询等它落地；
  // 虚拟时钟停住不动，等多久都不影响任何秒数。
  await page.clock.fastForward(21_000);
  await expect
    .poll(() => wsSubscribes, { message: '停摆重连必须发生过——本用例要证明新事件会重置空闲基准' })
    .toBeGreaterThan(0);
  await expect(page.locator('.model-output').last()).toContainText('又吐了一点。');
  await expect(page.locator('.wait-hint')).toHaveCount(0);

  // 再推进 31s（虚拟 ~52s）：距最后一次新事件只过了 ~31s → 才刚过阈值
  await page.clock.fastForward(31_000);
  const hint = page.locator('.wait-hint');
  await expect(hint).toBeVisible();
  await expect(hint).toContainText('没有新进展');

  // 决定性断言：提示秒数（空闲）必须**严格小于**脉冲秒数（流龄）。
  // 实现若退化成「按流龄计时」，两个数字会相等 → 这里必红。
  const hintSec = Number((await hint.innerText()).match(/已 (\d+)s/)?.[1]);
  expect(hintSec).toBeGreaterThanOrEqual(30);
  expect(hintSec).toBeLessThan(pulseSec(await pulse.innerText()));
});

test('刷新后不残留：提示不在，会话内容仍在（本地状态不进持久化）', async ({ page }) => {
  // 提示是纯本地状态，所以「刷新后没有它」是构造性的；这条用例把刷新后的**会话内容**
  // 一起锁住，免得把「内容也没了」误当成本条通过。
  await page.clock.install();
  await routeApi(page, {
    sessions: [{ session_id: SID, event_count: LIVE_FRAMES.length, first_event_time: T, last_event_time: T, first_user_message: '慢慢回答', trace_id: null, trace_url: null }],
    onSessionPost: (route) => fulfillSse(route, LIVE_FRAMES),
    // 服务端仍在跑（`ending: 'keep'`）：POST 那条流是有限长度的（route.fulfill 给不出
    // 长连接），接流这条补上真机的形状——否则重连额度耗尽走 give-up、streaming
    // 变假，等待提示根本没有出现的条件（这条用例会因错误的原因时绿时红）。
    onWs: () => ({ frames: [], hasActiveRun: true, ending: 'keep' }),
    events: LIVE_FRAMES,
  });

  await page.goto('/');
  await submitTask(page, '慢慢回答');
  await page.clock.fastForward(31_000);
  // 前置条件：本次会话刷新前确实出现过提示（否则「刷新后没有」毫无信息量）
  await expect(page.locator('.wait-hint')).toBeVisible();

  await page.reload();
  await page.locator('.session-item').first().click();
  await expect(page.locator('.model-output').last()).toContainText('开了个头…');
  await expect(page.locator('.wait-hint')).toHaveCount(0);
});

test('run 收口后不出现等待提示（终态不是停顿）', async ({ page }) => {
  const done: FrameSpec[] = [
    ...LIVE_FRAMES,
    { type: 'model/completed', data: { content: '开了个头…又吐了一点。' }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'run/completed', data: {}, seq: 6, session_id: SID, run_id: RUN, time: T },
  ];
  await page.clock.install();
  await routeApi(page, { onSessionPost: (route) => fulfillSse(route, done), events: done });

  await page.goto('/');
  await submitTask(page, '慢慢回答');
  await expect(page.locator('.run-pulse')).toContainText('已完成');

  await page.clock.fastForward(31_000);
  await expect(page.locator('.wait-hint')).toHaveCount(0);
});
