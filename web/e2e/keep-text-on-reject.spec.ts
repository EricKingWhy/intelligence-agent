/** #1029 端到端：发送失败后 composer 文本的处理 + 待裁决被动通知。
 *
 *  `#1327` 起：发送不再 409（未结清的行由后端按操作类型自动裁决，消息被受理），
 *  `RecoveryDecisionPanel` 已删。本文件原先的"待裁决 409"三条用例（文本保留 +
 *  面板唤起 / 面板落定 / 读面 5xx 保清单）随旧 409 流一并删除或重写。
 *
 *  为什么必须是 e2e（不是只有单测）：本票的**接缝在 App 层**——`handleSubmit` 把
 *  `sendMessage` 的 `Promise<boolean>` 透传给 `Composer`，`Composer` 据此决定清不清空。
 *  单测（`Composer.keepText` / `useSession.followUpRecovery`）各自把对面 mock 掉，
 *  唯独「这条线接没接上」在 jsdom 里照不出来：App 若把返回值丢掉（`void sendMessage(...)`，
 *  这正是修复前的形态），两边单测**都还是绿的**。所以这条断言只能由真实浏览器跑。
 *
 *  锁三件事：
 *  ① 受理成功（JSON 收据）后 composer 才清空（对照组）；
 *  ② #1148：窗外迟到的失败 ⇒ 先按已受理清空，迟到失败后原文回到输入框；
 *  ③ #1151（新口径）：权威读面 5xx 不得抹掉已知的待裁决横幅（不变量 #22）。
 *
 *  夹具形状与 `recovery-w13.spec.ts` 同源（`pending_decisions` 逐字段对齐后端
 *  `domain_errors` / `session/service.py::recover` 的 RecoveryConflict）。 */

import { expect, test, type Page, type Route } from '@playwright/test';
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

/** #1151：权威读面（`GET /recovery`）必须与用例伪造的事件说**同一件事** ⇒ 它得**有状态**：
 *  409 前答空、409 后答**同一份** `PENDING`、裁决 200 后再回空。
 *
 *  刻意**不**只把夹具缺省值改成非空常量：那是用新假象换旧假象——「对照：受理成功才
 *  清空」与「#1148 窗外迟到失败」两条用例在那些时刻后端真的没有待裁决事实。
 *
 *  修前形状（本票的红）：本文件只传 `onMessagesPost`（409 + 清单），读面走夹具缺省
 *  `[]`，而 `useSession.ts:1553-1560` 为 P1 会把 mode 落回 `viewing(sid)` ⇒ 依赖 `mode`
 *  的主动读面 effect（`:718-730`）立刻重跑，**空数组覆盖了刚被 409 点亮的非空清单**
 *  ⇒ `hasPendingDecisions`（`App.tsx:1145`）转假、面板不挂载。 */
type PendingState = {
  /** 本用例伪造的那次 409 是否已经落定。 */
  conflicted: boolean;
  /** 裁决（`POST /recover` 200）是否已经落定——后端结清后读面自然回空。 */
  adjudicated: boolean;
};

function pendingReadFace(state: PendingState): (route: Route) => Promise<void> {
  return async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ pending_decisions: state.conflicted && !state.adjudicated ? PENDING : [] }),
    });
  };
}

/** 造一份「本次 409 尚未发生、尚未裁决」的读面状态（打开会话由 `openCrashed` 负责）。 */
function scenario(): PendingState {
  return { conflicted: false, adjudicated: false };
}

async function openCrashed(page: Page, mock: ApiMock): Promise<void> {
  await routeApi(page, mock);
  await page.goto('/');
  await page.locator('.session-item').first().click();
  await expect(page.locator('.recover-btn')).toBeVisible();
}

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

test('#1148 窗外落定的 409：先按已受理清空，迟到失败后原文回到输入框', async ({ page }) => {
  /* 判定窗口（1200ms）量的是"响应头到没到"，而待裁决 409 在吐响应头之前要先在后端把
     pending 裁决查出来（`resume_and_launch` → `recover`）——冷启动/慢盘越过 1200ms
     完全正常。一旦越窗，hook 已按"已受理"返回 ⇒ composer 乐观清空；此时若没有回填
     通道，用户看到的就是「横幅说失败、输入框却是空的」。
     这条 e2e 锁的就是这条接缝：迟到失败必须经 `failedDraft` 把原文还回输入框。 */
  // #1151：这条也伪造了 409（同样带清单），权威读面照样必须与它自洽。
  const state = scenario();
  await openCrashed(page, {
    sessions: [ROW],
    events: crashedEvents(),
    onRecoveryGet: pendingReadFace(state),
    onMessagesPost: async (route) => {
      // 越过判定窗口（1200ms）才吐响应头——真机慢网络/慢盘的同形时序。
      await new Promise((resolve) => setTimeout(resolve, 1800));
      state.conflicted = true;
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

  // ① 窗外 = 无从等待结局 ⇒ 先按已受理清空（乐观）。
  await expect(composer).toHaveValue('');
  // ② 迟到的 409 落定 ⇒ 原文回到输入框（修复前：只剩一条横幅，文本永久丢失）。
  await expect(composer).toHaveValue(DRAFT, { timeout: 10_000 });
  // ③ 失败呈现照旧（人话横幅，不透传协议原文）——回填是**附加**通道，不取代横幅。
  const banner = page.locator('.app-error');
  await expect(banner).toContainText('待人工裁决');
  await expect(banner).not.toContainText('tool_call_id');
});

test('#1151（新口径）读失败 ≠ 没事：重读 5xx 不得抹掉已知的待裁决横幅', async ({ page }) => {
  /* 本票（#1151）裁决的第 2 条在新呈现下的形态：`#1327` 起裁决面板已删，
     "已知的待裁决清单"以后端驱动的被动通知横幅呈现。不变量 #22 在 UI 侧的
     表达不变：读不到是「未知」，与后端明确说「没有待裁决」是两件事。

     触发重读的路径是"恢复会话" 200 后的 `refreshPendingDecisions`
    （`useSession.ts`：200 只证明请求被接受，清单一律重读后端事实；重读失败
     时保留上一个已知值，不回落空清单——正是本条要锁的）。
     注意不用"发送"触发：发送成功（ack）会把恢复态整体置回 IDLE（陈旧态退场），
     那是另一条正确的语义；也不用"换会话"触发：换会话照旧整表重置。 */
  let flipped = false;
  let recoveryReads = 0;
  await openCrashed(page, {
    sessions: [ROW],
    events: crashedEvents(),
    onRecoveryGet: async (route) => {
      recoveryReads += 1;
      await route.fulfill({
        status: flipped ? 500 : 200,
        contentType: 'application/json',
        body: JSON.stringify(flipped ? { detail: 'boom' } : { pending_decisions: PENDING }),
      });
    },
    // 恢复 200 但没修好任何东西（幂等 no-op）：只为触发重读，不为修好。
    onRecoverPost: async (route) => {
      await route.fulfill({
        status: 200,
        body: JSON.stringify(crashedEvents()),
        contentType: 'application/json',
      });
    },
  });

  // ① 打开会话：被动通知横幅在场（role=status），无裁决面板。
  const banner = page.locator('.adjudication-banner');
  await expect(banner).toBeVisible();
  await expect(banner).toHaveAttribute('role', 'status');
  await expect(page.locator('.recovery-decision-panel')).toHaveCount(0);
  const readsBefore = recoveryReads;

  // ② 翻转读面为 500，再点"恢复会话"触发重读。
  flipped = true;
  await page.locator('.recover-btn').click();

  // ③ 重读失败 ≠ 没事：横幅保留上一个已知值，不被抹掉。
  await expect.poll(() => recoveryReads).toBeGreaterThan(readsBefore);
  await expect(banner).toBeVisible();
  await expect(banner.locator('.adjudication-headline')).toContainText('自动处理');
});
