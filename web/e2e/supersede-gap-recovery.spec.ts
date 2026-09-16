/** #213（设计票）：被丢掉的 `message/superseded` 帧靠什么自愈——**这是"是否需要
 *  增量重写计数器（DSH `replaceGeneration`）"的决定性检查**。
 *
 * 票面担心的事：「尾部增长」与「中间被改写」在客户端看到的是同一类帧，取代轮只能
 * 靠序号与内容推断。本用例把那个最坏形态造出来——**取代事实帧本身丢了**，客户端
 * 只看到一条跳号的尾帧——然后断言它靠什么回到正确视图。
 *
 * 判定链路（三件事叠加，缺一不可）：
 *  ① 契约 T4：跳号帧不投影，直接丢弃并重连（`useSession.ts` `isSeqGap` 分支）；
 *  ② 续传游标是客户端**自己的水位线**——"≤ 此值都已应用"。run 启动那次取本地对话的
 *     max seq（实测 7，`useSession.ts` launch 分支取自 `conv.events`），重试复用该值。
 *     水位线的性质决定了：**客户端缺哪条事件，那条的 seq 就一定 > 游标**，于是
 *     `seq > after_seq` 的重放窗口必然包含它；被丢的取代帧一定会再来一次——不是
 *     "等下次刷新才补"。用例断言的是这个**不变量**（游标不得越过丢帧处），不是某个
 *     具体数值：从更早处重放（游标偏小）只是多补几帧、由幂等门吸收，永远安全。
 *  ③ 前端取代区间由**全量事件日志**重算（`projection.ts` `supersedeRanges`），
 *     故取代帧一到，被取代的整轮立刻从视图移除。
 *
 * 结论（本用例通过即成立）：重写事实在本项目里**已经是一条带自身 seq 的 append-only
 * 事件**，丢帧由 ①②③ 自愈；再加一个服务端单调计数器只会是同一事实的第二套序号
 * 语义（票面 ② 明令禁止），且没有可观察的失效面支撑它。因此 #213 **不新增契约字段**。
 *
 * 反面保护：若将来有人拆掉 ①（跳号帧照样投影）或 ②（续传游标越过未应用的事件），
 * 被丢的取代帧就可能永久丢失——那正是"需要计数器"的前提。本用例是这个前提的证伪点。
 *
 * 时序与真后端同序（`service.py` §4.6：**先投递 carrier B，后写
 * `message/superseded`**），故取代帧落在流尾——这也正是它被丢掉时的最坏位置。
 * 取代轮本体与编辑入口已由 `multiturn-queue.spec.ts` T11 锁，本文件只锁"丢帧后
 * 的自愈"，不重复 T11 的断言。 */

import { expect, test } from '@playwright/test';
import { fulfillSse, routeApi, submitTask, type FrameSpec } from './fixtures';

const SID = 'sg-session-1';
const RUN1 = 'sg-run-1';
const RUN2 = 'sg-run-2';
const T = '2026-09-16T00:00:00Z';

/** 首轮：问句 seq 3（待取代）+ 回答段（锁"整段消失"，见 T11 §4.5.1）。 */
const FIRST: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN1, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN1, time: T },
  { type: 'user/message', data: { content: '原始问题' }, seq: 3, session_id: SID, run_id: RUN1, step_id: 1, time: T },
  { type: 'model/started', seq: 4, session_id: SID, run_id: RUN1, step_id: 1, time: T },
  { type: 'model/delta', data: { delta: '旧回答' }, seq: 5, session_id: SID, run_id: RUN1, step_id: 1, time: T },
  { type: 'model/completed', data: { content: '旧回答' }, seq: 6, session_id: SID, run_id: RUN1, step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 7, session_id: SID, run_id: RUN1, time: T },
];

/** 编辑保存后的故事：carrier（seq 10）→ 回答 → 取代事件（seq 15）→ 终态（seq 16）。
 *  seq 15 是**被丢掉的那一帧**；seq 16 是客户端实际看到的那条跳号尾帧。 */
const SUPERSEDE_SEQ = 15;
const LATER: FrameSpec[] = [
  { type: 'run/started', seq: 9, session_id: SID, run_id: RUN2, time: T },
  { type: 'user/message', data: { content: '改写后的问题', steer_id: 'st-1' }, seq: 10, session_id: SID, run_id: RUN2, step_id: 2, time: T },
  { type: 'steer/applied', data: { steer_id: 'st-1', applied_seq: 10, run_id: RUN2 }, seq: 11, session_id: SID, run_id: RUN2, time: T },
  { type: 'model/started', seq: 12, session_id: SID, run_id: RUN2, step_id: 2, time: T },
  { type: 'model/delta', data: { delta: '新回答' }, seq: 13, session_id: SID, run_id: RUN2, step_id: 2, time: T },
  { type: 'model/completed', data: { content: '新回答' }, seq: 14, session_id: SID, run_id: RUN2, step_id: 2, time: T },
  { type: 'message/superseded', data: { superseded_seq: 3, carrier: 10 }, seq: SUPERSEDE_SEQ, session_id: SID, time: T },
  { type: 'run/completed', data: {}, seq: 16, session_id: SID, run_id: RUN2, time: T },
];

test('#213：被丢掉的 message/superseded 帧 → 重连带游标重放 → 取代区间重算、旧轮不回来', async ({ page }) => {
  // 可变引用：POST /messages 时才把新故事 append 进 durable log（真后端 append 发生在
  // 投递时），收尾回读才拿得到全量。
  const durableLog: FrameSpec[] = [...FIRST];
  // 上行原文观测点（#208 起的既有夹具能力）：订阅游标只能在这里读到——客户端若
  // 从更靠后的位置续传（跳过了丢帧），界面上完全看不出来，只有这个数组能证。
  const subscribes: Array<{ session_id: string; after_seq?: unknown }> = [];

  await routeApi(page, {
    sessions: [],
    events: durableLog,
    wsSubscribes: subscribes,
    onSessionPost: (route) => fulfillSse(route, FIRST),
    onMessagesPost: (route) => {
      durableLog.push(...LATER);
      // **丢帧注入**：ss 流里不含 seq 15（被丢的取代帧），却含 seq 16 的终态——
      // 客户端看到的是 14 → 16 的跳号，与"尾部增长"完全同形。
      // 红证（本文件定稿前实测过一次）：若把该帧同时从 durable log 剔除（= 任何通道
      // 都收不到取代事实），旧轮留在视图（`原始问题` 计数 1）⇒ 下面的断言有判别力，
      // 不是因为"视图恰好这么渲染"。
      return fulfillSse(
        route,
        LATER.filter((f) => f.seq !== SUPERSEDE_SEQ),
      );
    },
  });

  await page.goto('/');
  await submitTask(page, '原始问题');
  await expect(page.locator('.msg-bubble-user', { hasText: '原始问题' })).toBeVisible();

  // 走真实产出者：编辑最新一条 → supersedes_seq=3 进 payload（T11 已锁 payload，这里只要故事）。
  await page.getByRole('button', { name: '编辑消息', exact: true }).click();
  const editBox = page.getByLabel('编辑消息');
  await expect(editBox).toBeVisible();
  await editBox.fill('改写后的问题');
  await editBox.press('Control+Enter');

  // ② 承重不变量：**任何一次订阅的游标都不得越过丢帧处（≤ 14）**。
  //    第一条注释里的旧说法「重连必须带 14」**已被实测推翻**（实发 7），故判据从
  //    "某个具体数值"改成下面这条不变量。
  //    游标是客户端自己的水位线（"≤ 此值都已应用"），故重放窗口 `seq > after_seq`
  //    必然包含 seq 15 这条被丢的取代帧 ⇒ 取代事实不可能被静默跳过。
  //    这条一旦被破坏（重连从更靠后的游标续、或跳号帧被照常投影），取代帧就可能
  //    永久丢失——那才是"需要服务端单调计数器"的前提；本用例正是这个前提的证伪点。
  //    注：首条订阅是 run 启动时那次，游标 7 = 本地对话的 max seq（`useSession.ts`
  //    launch 分支取自 conv.events，不是 lastAppliedSeqRef）；重试复用同一游标。
  await expect.poll(() => subscribes.length, { timeout: 10_000 }).toBeGreaterThan(0);
  for (const s of subscribes) expect(Number(s.after_seq)).toBeLessThanOrEqual(SUPERSEDE_SEQ - 1);

  // ③ 取代段重算：区间 [3, 10) 覆盖旧轮（问 + 答整段），carrier 与它的回答在场且各一次。
  await expect(page.locator('.msg-bubble-user', { hasText: '改写后的问题' })).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('.msg-bubble-user', { hasText: '原始问题' })).toHaveCount(0);
  await expect(page.locator('.msg-model', { hasText: '旧回答' })).toHaveCount(0);
  await expect(page.locator('.msg-model', { hasText: '新回答' })).toHaveCount(1);
  // ④ 事实确实到达客户端（不是"视图恰好没渲染"）：被丢的那条 `message/superseded`
  //    经重放进了本地事件日志，Inspector 里能查到它。
  await expect(page.getByText('message/superseded', { exact: false }).first()).toBeVisible();
});
