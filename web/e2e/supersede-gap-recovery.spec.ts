/** #213（设计票）的**证伪点**：被丢掉的 `message/superseded` 帧靠什么自愈。
 *
 * **决议、机制与"本决议失效条件"见 `docs/adr/0030-…md` §12**（不引入增量重写计数器）——
 * 这里不重复机制叙述，只记**这段代码自己看不出来的操作性事实**（AGENTS.md §16.1：同一事实
 * 只在一处写全，其余处留指针；起因就是这份注释曾与 ADR 各写一遍，其中一遍被实测推翻）。
 *
 * 夹具做什么（两条缺一不可）：
 *  - 首轮 seq 1..7；编辑保存后的第二轮 seq 8..14 **连续**（与首轮末条 7 相接），其中
 *    **seq 13 的 `message/superseded` 在 live 流被丢掉**、只留 seq 14 的终态 ⇒ 客户端在
 *    `12 → 14` 撞跳号（与"尾部增长"同形）。第二轮若与首轮之间凭空留洞（旧版从 seq 9 起），
 *    客户端**第一帧**就判跳号、一条都不投影，走的其实是 give-up → 全量回读那条兜底。
 *  - durable 日志里该帧还在，且 WS 快照**按 `seq > after_seq` 真裁** ⇒ 重放窗口是**唯一**
 *    能把它送回来的通道。日志里没有 ⇒ 任何通道都收不到；快照不裁 ⇒ 游标越过丢帧处也照样
 *    收得到，"窗口必含该帧"这条不变量不受检（旧版就是这样，恒真）。
 *
 * 四条断言各挡一种拆法（缺任一条都会"为错误的理由变绿"，实测过一次）：
 *  ① 撞跳号那次重连的游标 = **丢帧前最后一条已应用事件**（12）⇒ 挡"跳号帧照常投影"（拆 T4）；
 *  ② 视图：旧轮问 + 答整段消失、改写后的问题与其回答各一次 ⇒ 挡"游标被抬过丢帧处"（拆水位线）；
 *  ③ Inspector 里能查到 `message/superseded` ⇒ 事实确实到达了客户端；
 *  ④ **只发生一次重连、且不得出现 give-up 条** ⇒ 挡"重试耗尽 → 全量回读"兜底：那条路在本
 *    夹具里也能把视图修对，所以没有 ④，①②③ 会退化成"最终视图对就行"。
 *    ⚠ **④ 必须排在视图断言之后**：兜底要先耗尽重试才把视图修对，"视图已对"这个时刻在坏形态里
 *    恰好意味着 3 次订阅已经发完；上移到视图断言之前就重新变成竞态（实测：那时把重放窗口置空
 *    **照样绿**）。
 *
 * 红证（2026-09-17 实测两态）：窗口置空 ⇒ `subscribes=[12,12,12]`、give-up 条出现、视图由两次
 * `/events` 回读兜对、④ 红（`Received +2`）；正常形态 `[12]`、give-up 0。
 *
 * ⚠ 夹具保真度（勿当成后端实证）：
 *  - 真后端写 `message/superseded` 的时机是**登记 carrier 之后立刻**（`service.py` §4.4 第 3 步），
 *    即落在新一轮流的**前段**；夹具把它挪到流尾，因为本用例要丢的**必须就是它**——这是**合成**的
 *    客户端最坏形态，不是后端产得出来的形态。
 *  - 后端真会丢帧的位置是 `runmanager.py` 的有界队列——满时丢**最旧**那条（ADR-0016），与夹具
 *    方向相反；服务端那半的重放语义由 `tests/web/test_web_ws_relay.py` 锁，本文件只锁客户端这半。
 *  - 编辑保存走 `mode` 缺省（`App.tsx::handleEditTurn` 只发 `supersedes_seq`）⇒ 取代帧的
 *    `carrier` 取 `"queue"`（ADR-0030 §4.1 的取值域）。
 *
 * 取代轮本体与编辑入口已由 `multiturn-queue.spec.ts` T11 锁，本文件只锁"丢帧后的自愈"。 */

import { expect, test } from '@playwright/test';
import { fulfillSse, routeApi, submitTask, type FrameSpec } from './fixtures';

const SID = 'sg-session-1';
const RUN1 = 'sg-run-1';
const RUN2 = 'sg-run-2';
const T = '2026-09-16T00:00:00Z';

/** 首轮 seq 1..7：问句 seq 3（待取代）+ 回答段（锁"整段消失"，见 T11 §4.5.1）。 */
const FIRST: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN1, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN1, time: T },
  { type: 'user/message', data: { content: '原始问题' }, seq: 3, session_id: SID, run_id: RUN1, step_id: 1, time: T },
  { type: 'model/started', seq: 4, session_id: SID, run_id: RUN1, step_id: 1, time: T },
  { type: 'model/delta', data: { delta: '旧回答' }, seq: 5, session_id: SID, run_id: RUN1, step_id: 1, time: T },
  { type: 'model/completed', data: { content: '旧回答' }, seq: 6, session_id: SID, run_id: RUN1, step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 7, session_id: SID, run_id: RUN1, time: T },
];

/** 编辑保存后的故事：seq 8..14 **连续**（首轮末条是 7），其中 seq 13 是被丢的那一帧。
 *
 *  连续性本身是本用例的前提：seq 8 与首轮末条相接，客户端才真的把 8..12 应用进水位线，
 *  于是丢掉 13 时撞出的跳号是 `12 → 14`（**取代帧之后**那一跳）——上一版把第二轮从
 *  seq 9 起编（= 在 7 与 9 之间凭空留了个洞），客户端在**第一帧**就判跳号、一条都没投影，
 *  走的其实是"重试耗尽 → 全量回读"那条路，本文件宣称的机制根本没被走到。 */
const SUPERSEDE_SEQ = 13;
const LATER: FrameSpec[] = [
  { type: 'run/started', seq: 8, session_id: SID, run_id: RUN2, time: T },
  { type: 'user/message', data: { content: '改写后的问题' }, seq: 9, session_id: SID, run_id: RUN2, step_id: 2, time: T },
  { type: 'model/started', seq: 10, session_id: SID, run_id: RUN2, step_id: 2, time: T },
  { type: 'model/delta', data: { delta: '新回答' }, seq: 11, session_id: SID, run_id: RUN2, step_id: 2, time: T },
  { type: 'model/completed', data: { content: '新回答' }, seq: 12, session_id: SID, run_id: RUN2, step_id: 2, time: T },
  { type: 'message/superseded', data: { superseded_seq: 3, carrier: 'queue' }, seq: SUPERSEDE_SEQ, session_id: SID, time: T },
  { type: 'run/completed', data: {}, seq: 14, session_id: SID, run_id: RUN2, time: T },
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
    // 重放窗口 = 服务端契约「只补 `seq > after_seq` 的 durable 事件」。夹具必须**真的
    // 按游标裁**，否则"窗口必含该帧"这条不变量不受检——不裁的话就算客户端游标越过
    // 丢帧处，全量倾倒也会把该帧送来，用例照样绿。
    // 夹具在调 `onWs` **之前**已经把这次订阅的游标记进 `wsSubscribes`，故这里读得到。
    onWs: () => ({
      events: durableLog.filter((f) => (f.seq ?? 0) > Number(subscribes.at(-1)?.after_seq ?? -1)),
    }),
    onMessagesPost: (route) => {
      durableLog.push(...LATER);
      // **丢帧注入**：live 流里不含 seq 13（被丢的取代帧），却含 seq 14 的终态——
      // durable log 里它还在，所以"重放窗口"是唯一能把它送回来的通道。
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

  // ② 承重不变量：跳号那次重连的游标 = **丢帧前最后一条已应用事件**（seq 12）。
  //    游标是客户端自己的水位线（"≤ 此值都已应用"），故重放窗口 `seq > 12` 必然
  //    包含 seq 13 这条被丢的取代帧 ⇒ 取代事实不可能被静默跳过。
  //    （写死 12 而不是"某个偏小的值"：偏小的游标只是多补几帧、由幂等门吸收，
  //    但**偏大**正是本用例要挡的失效——游标一过 13，那条帧就再也回不来了。）
  await expect.poll(() => subscribes.length, { timeout: 10_000 }).toBeGreaterThan(0);
  await expect
    .poll(() => subscribes.map((s) => Number(s.after_seq)), { timeout: 10_000 })
    .toContain(SUPERSEDE_SEQ - 1);

  // ③ 取代段重算：区间 [3, 9) 覆盖旧轮（问 + 答整段），改写后的问题与其回答在场且各一次。
  await expect(page.locator('.msg-bubble-user', { hasText: '改写后的问题' })).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('.msg-bubble-user', { hasText: '原始问题' })).toHaveCount(0);
  await expect(page.locator('.msg-model', { hasText: '旧回答' })).toHaveCount(0);
  await expect(page.locator('.msg-model', { hasText: '新回答' })).toHaveCount(1);
  // ④ 事实确实到达客户端（不是"视图恰好没渲染"）：被丢的那条 `message/superseded`
  //    经重放进了本地事件日志，Inspector 里能查到它。
  await expect(page.getByText('message/superseded', { exact: false }).first()).toBeVisible();

  // ⑤ **判定性断言**：自愈走的是"一次重连带游标重放"，不是"重试耗尽 → 全量回读"。
  //    后者在本夹具里也能把视图修对（durable log 一直都在，give-up 后 viewing 会重新
  //    拉 `/events`），所以前四条断言单独存在时，本用例会退化成"最终视图对就行"。
  //    实测两种形态（2026-09-17，两个 viewport 各跑一次）：
  //      - 正常（窗口按游标裁、含被丢帧）：`subscribes=[12]`、give-up 条 0、视图对；
  //      - 把窗口置空（等价"游标越过丢帧处"）：`subscribes=[12,12,12]`（重试 3 次耗尽）、
  //        give-up 条 1、两次 `/events` 回读**把视图兜对**。
  //    ⚠ 断言位置是承重的：**必须排在视图断言之后**。全量回读那条兜底要先耗尽重试
  //    才会把视图修对，所以"视图已对"这个时刻在坏形态里恰好意味着 3 次订阅已经发完
  //    ——把本断言上移到视图断言之前就会重新变成竞态（第一次订阅时它还是 1）。
  expect(subscribes.map((s) => Number(s.after_seq))).toEqual([SUPERSEDE_SEQ - 1]);
  await expect(page.getByText(/重试\s*\d+\s*次未成功/)).toHaveCount(0);
});
