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
 * 车道能力：WS 接流 mock 能**保持连接**（`ending: 'keep'`），且 `frameGate` 让用例
 * 掌握「新事件」到达的**虚拟时刻**——于是「长连接 + 用例扣住一帧 → 放行 → 继续长连接」
 * 这个形状能在本车道确定性地构造（旧 `route.fulfill` 给不出长连接：帧一次性到齐、
 * 包体立刻结束）。**不构造停摆重连**：虚拟时钟一跳与真实定时器的到点无法编排先后，
 * 依赖重连的编排是不确定的（#1214 实测）。
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

/** 闸门放行后补进来的**一条新事件**（seq 5）：它把「空闲基准」重置一次。
 *  （此前注释称它由「停摆重连」补进——**不属实**：它随首次接流到达，只是被
 *  `frameGate` 扣住；虚拟时钟下无法编排重连时序，见文件头。） */
const MID_FRAMES: FrameSpec[] = [
  { type: 'text/delta', data: { delta: '又吐了一点。' }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T },
];

/** 从脉冲文案里取流龄秒数（`思考中 · 21s`）。 */
function pulseSec(text: string): number {
  return Number(text.match(/· (\d+)s/)?.[1]);
}

test('停顿提示锚的是「空闲」而非「流龄」：阈值前不出现；出现后秒数必须小于流龄', async ({ page }) => {
  // 「新事件」（seq 5）的**放行闸门**（#1214）：握在用例手里。
  // 详见下方注释——本用例的红/绿过去取决于「seq 5 何时到达」，而那件事在
  // 虚拟时钟下不可靠，闸门把它变成确定值。
  let releaseMid!: () => void;
  const midGate = new Promise<void>((resolve) => {
    releaseMid = resolve;
  });
  await page.clock.install();
  await routeApi(page, {
    onSessionPost: (route) => fulfillSse(route, LIVE_FRAMES),
    // 连接保持（`ending: 'keep'`）：真后端在 run 未收口时就是这样，界面必须一直
    // 维持生成态（否则脉冲秒数无从比较）。
    //
    // ⚠ `frameGate` 是本用例（#1214）的关键：**seq 5 只在用例放行时到达**。
    // 改前 seq 5 随首次接流在**挂载当口**就到了，于是它与「流龄起点」（`startedAt`）
    // 写在同一瞬间：`hintSec`（距新事件的空闲）与 `pulseSec`（流龄）只差 0~1s，
    // 决定性断言 `hintSec < pulseSec` 的余量被压成 1 秒 → 负载下随机变红
    // （实测 `Expected: < 52, Received: 52`）。注意这与「产品退化成按流龄计时」
    // **读数同形但成因不同**：那种红是夹具时序造的假象，产品侧锚点是正确重置的。
    onWs: () => ({ frames: MID_FRAMES, hasActiveRun: true, ending: 'keep', frameGate: midGate }),
    events: LIVE_FRAMES,
  });

  await page.goto('/');
  await submitTask(page, '慢慢回答');

  // 前置条件：生成态真的起来了（否则后面的「不出现」可能只是因为什么都没跑）
  const pulse = page.locator('.run-pulse');
  const hint = page.locator('.wait-hint');
  await expect(pulse).toContainText('思考中');
  await expect(page.locator('.stream-caret')).toBeVisible();
  await expect(hint).toHaveCount(0); // 阈值以下：一个节点都不多

  // 推到阈值之后（虚拟 ~32s）：seq 5 仍被闸门扣着 ⇒ 空闲锚点还是流起点，
  // 提示必须出现。这一步同时锁住「阈值门」本身。
  await page.clock.fastForward(32_000);
  await expect(hint).toBeVisible();
  await expect(hint).toContainText('没有新进展');

  // **放行 seq 5**：这是一条新事件，空闲锚点必须随之重置 ⇒ 提示必须消失
  // （`TopBar` 的进度 effect 会把 idleSec 归零，文案随之为空、节点卸载）。
  // 这一步是确定性的：观察到「提示消失」就是「锚点确实重置过」的直接证据，
  // 不用靠跳时钟去碰运气——正是它把断言余量从 1 秒拉回到 ~32 秒。
  releaseMid();
  await expect(page.locator('.model-output').last()).toContainText('又吐了一点。');
  await expect(hint).toHaveCount(0);

  // 再推 32s：距 seq 5 累计空闲 ~32s（刚过阈值），距流起点 ~64s。
  await page.clock.fastForward(32_000);
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
  // 确定性锚（#442 族4，与上一条用例 fastForward 前的「思考中」锚同款）：等生成态
  // 真的起来（POST 流的帧已被消费、空闲基准已在实时侧复位）再快进——否则虚拟时钟
  // burst 与实时帧消费交错，空闲秒数永远到不了 30，下面的前置条件必然超时。
  await expect(page.locator('.run-pulse')).toContainText('思考中');
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
