/** #1029 端到端：待裁决会话发送被拒后，composer 文本必须还在，且裁决面板被唤起。
 *
 *  为什么必须是 e2e（不是只有单测）：本票的**接缝在 App 层**——`handleSubmit` 把
 *  `sendMessage` 的 `Promise<boolean>` 透传给 `Composer`，`Composer` 据此决定清不清空。
 *  单测（`Composer.keepText` / `useSession.followUpRecovery`）各自把对面 mock 掉，
 *  唯独「这条线接没接上」在 jsdom 里照不出来：App 若把返回值丢掉（`void sendMessage(...)`，
 *  这正是修复前的形态），两边单测**都还是绿的**。所以这条断言只能由真实浏览器跑。
 *
 *  锁三件事：
 *  ① 待裁决 409 后，输入框里那段文本还在（数据不丢）；
 *  ② 横幅是**人话**（不透传后端协议原文的 `tool_call_id` / `POST /recover` 词表）；
 *  ③ 裁决面板被这条发送路径唤起（此前只由恢复路径 409 触发，发送路径永不可达）。
 *
 *  夹具形状与 `recovery-w13.spec.ts` 同源（`pending_decisions` 逐字段对齐后端
 *  `domain_errors` / `session/service.py::recover` 的 RecoveryConflict）。 */

import { expect, test, type Page } from '@playwright/test';
import { RUN, SID, T, routeApi, type ApiMock, type FrameSpec } from './fixtures';

const ROW = {
  session_id: SID,
  event_count: 5,
  first_event_time: T,
  last_event_time: T,
  first_user_message: '待裁决会话',
  trace_id: null,
  trace_url: null,
};

/** 待裁决会话：run 无终态 + 未配对 tool/call（与 recovery-w13 同形）。 */
function crashedEvents(): FrameSpec[] {
  return [
    { type: 'session/started', seq: 1, session_id: SID, time: T },
    { type: 'user/message', data: { content: '待裁决会话' }, seq: 2, session_id: SID, time: T },
    { type: 'run/started', data: { turn_index: 1 }, seq: 3, session_id: SID, run_id: RUN, time: T },
    { type: 'model/completed', data: { content: '开始执行。' }, seq: 4, session_id: SID, step_id: 1, run_id: RUN, time: T },
    {
      type: 'tool/call',
      data: { tool_call_id: 'call-1', tool_name: 'bash', args: { command: 'ls' } },
      seq: 5,
      session_id: SID,
      step_id: 1,
      run_id: RUN,
      time: T,
    },
  ];
}

const PENDING = [
  {
    tool_call_id: 'call-1',
    tool_name: 'bash',
    state: 'UNKNOWN',
    default_action: 'DEFER',
    risk_level: 'high',
    probe: { verifiable: false, suggested_action: null },
  },
];

/** 后端 RecoveryConflict 的协议原文（含用户看不懂的 `tool_call_id` / `POST /recover`）。 */
const PROTOCOL_MESSAGE =
  '存在需要人工裁决的 UNKNOWN Operation（bash(tool_call_id=call-1)）：先 POST ' +
  '/api/sessions/{id}/recover 携带 decisions=[{tool_call_id, verdict}] 结清';

const DRAFT = '这段文本不能丢';

async function openCrashed(page: Page, mock: ApiMock): Promise<void> {
  await routeApi(page, mock);
  await page.goto('/');
  await page.locator('.session-item').first().click();
  await expect(page.locator('.recover-btn')).toBeVisible();
}

test('待裁决 409：composer 文本仍在 + 人话横幅 + 裁决面板被唤起', async ({ page }) => {
  await openCrashed(page, {
    sessions: [ROW],
    events: crashedEvents(),
    onMessagesPost: async (route) => {
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: { message: PROTOCOL_MESSAGE, pending_decisions: PENDING } }),
      });
    },
  });

  const composer = page.getByLabel('Agent 任务');
  await composer.fill(DRAFT);
  await page.getByLabel('发送').click();

  // ① 数据不丢：被拒后文本原样留在输入框（修复前：提交瞬间同步清空 ⇒ 此处为空）
  await expect(composer).toHaveValue(DRAFT);
  // ③ 裁决面板由**发送路径**唤起（修复前：发送路径从不写 recoverState.pendingDecisions）
  await expect(page.locator('.recovery-decision-panel')).toBeVisible();
  // ② 横幅是人话，不透传协议原文（用户照 `tool_call_id` / `POST /recover` 行动不了）
  const banner = page.locator('.app-error');
  await expect(banner).toContainText('待人工裁决');
  await expect(banner).not.toContainText('tool_call_id');
  await expect(banner).not.toContainText('POST /api/sessions');
});

test('对照：受理成功（JSON 收据）后 composer 才清空', async ({ page }) => {
  await openCrashed(page, {
    sessions: [ROW],
    events: crashedEvents(),
    onMessagesPost: async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'queued', queue_id: 'q-1' }),
      });
    },
  });

  const composer = page.getByLabel('Agent 任务');
  await composer.fill(DRAFT);
  await page.getByLabel('发送').click();

  await expect(composer).toHaveValue('');
  await expect(page.locator('.recovery-decision-panel')).toHaveCount(0);
});
