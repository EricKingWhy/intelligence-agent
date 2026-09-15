/** WS 不可用时的行为（#205 AC「必须显式，不得静默卡住」）与 `stream/truncated` 重建路径。
 *
 * ## 为什么走降级通道才能考这两件事
 *
 * 实时流的主通道已经是 WS。`stream/truncated` 控制帧只由 SSE
 * `GET /stream` 发（`web/app.py`：`latest_seq - after_seq > STREAM_REPLAY_MAX_EVENTS`
 * 时单帧下发）——WS 快照没有上限、也不发控制帧（#208 记的后端落差）。所以这条路径
 * 在 e2e 里只能经**降级**触发：`onWs → closeNow`（WS 被拒）+ `onStreamGet` 喂控制帧。
 *
 * 顺带就把降级本身考了：整条链路（WS 零服务帧 → 同一 after_seq 改走 GET /stream）
 * 在真机上完全可能出现（前置代理拒 Upgrade），此前只有 `wsStream.test.ts` 的单测覆盖。
 *
 * 注意本车道**不**声称降级「好用」：攒包交付层下降级流同样要到 run 结束才有帧，
 * 停摆检查（10s）会先把它判成停摆并如实报错——那是「明确失败」，不是「静默卡住」。
 * 真机上的攒包行为本机造不出来，如实划界。 */

import { expect, test, type Page } from '@playwright/test';
import { RUN, SID, T, fulfillSse, routeApi, type FrameSpec } from './fixtures';

const KEY = 'ahi.selectedSession';

const ROW = {
  session_id: SID,
  event_count: 4,
  first_event_time: T,
  last_event_time: T,
  first_user_message: '刷新前就发出的任务',
  trace_id: null,
  trace_url: null,
};

/** 未收口的 run（末尾无终态）→ 进站装载后 `hasUnterminatedRun` 为真 → 触发接流。 */
const IN_FLIGHT: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '刷新前就发出的任务' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'text/delta', data: { delta: '刷新前已有的内容。' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
];

/** 进站即选中该会话（等价刷新后的那条路径）。 */
async function openStoredSession(page: Page): Promise<void> {
  await page.addInitScript(([k, v]) => localStorage.setItem(k, v), [KEY, SID]);
  await page.goto('/');
  await expect(page.locator('.model-output').last()).toContainText('刷新前已有的内容。');
}

test('WS 被拒（零服务帧）→ 自动降级 GET /stream，接流照样建立（不静默卡住）', async ({ page }) => {
  let sseCalls = 0;
  await routeApi(page, {
    sessions: [ROW],
    events: IN_FLIGHT,
    onWs: () => ({ closeNow: true }), // 代理拒掉 Upgrade：一个服务帧都没有
    onStreamGet: (route) => {
      sseCalls += 1;
      return fulfillSse(route, [
        { type: 'text/delta', data: { delta: '降级通道补进来的。' }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T },
        { type: 'run/completed', data: {}, seq: 6, session_id: SID, run_id: RUN, time: T },
      ]);
    },
  });

  await openStoredSession(page);

  // 降级真的接上了：这一帧只可能来自 GET /stream
  await expect(page.locator('.model-output').last()).toContainText('降级通道补进来的。');
  expect(sseCalls).toBe(1);
  // 降级是兜底，不是「断线重连」的可见状态：终态一到就干净收尾
  await expect(page.locator('.reconnect-banner')).toBeHidden();
});

test('降级流收到 stream/truncated → 先 GET /events 全量重建，再以真实 max seq 续传', async ({ page }) => {
  let sseCalls = 0;
  // 可变 durable log：下面 push 的这条**不在**页面已装载的那份里（历史装载发生在
  // 接流之前），所以它出现在视图里只可能来自重建时那次 GET /events 重读——
  // 「重建真的发生了」是可观测的，不是靠"看起来没变"推断。
  const log: FrameSpec[] = [...IN_FLIGHT];
  await routeApi(page, {
    sessions: [ROW],
    events: log,
    onWs: () => ({ closeNow: true }), // 两次接流都降级
    onStreamGet: (route) => {
      sseCalls += 1;
      if (sseCalls === 1) {
        // backlog 超阈值：契约 §3 单帧控制帧，客户端据此走全量重建。
        // latest_seq 只是回显（游标以重建后本地真实 max seq 为准），但要合法。
        log.push({ type: 'text/delta', data: { delta: '全量重建补回的。' }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T });
        return fulfillSse(route, [{ type: 'stream/truncated', data: { after_seq: 4, latest_seq: 5 }, seq: null, session_id: SID, time: T }]);
      }
      // 续传：游标必须是重建后的真实 max seq（5），所以这里从 6 起
      return fulfillSse(route, [
        { type: 'text/delta', data: { delta: '续传补上的。' }, seq: 6, session_id: SID, run_id: RUN, step_id: 1, time: T },
        { type: 'run/completed', data: {}, seq: 7, session_id: SID, run_id: RUN, time: T },
      ]);
    },
  });

  await openStoredSession(page);

  // 只有走完「重建 → 续传」两跳，这两段文本才可能同时在场
  await expect(page.locator('.model-output').last()).toContainText('全量重建补回的。');
  await expect(page.locator('.model-output').last()).toContainText('续传补上的。');
  expect(sseCalls).toBe(2);
  // 重建不是叠加：历史照旧只一份（重复投影会把这一段变成两段）
  const text = (await page.locator('.model-output').allInnerTexts()).join('\n');
  expect(text.match(/刷新前已有的内容。/g)).toHaveLength(1);
});
