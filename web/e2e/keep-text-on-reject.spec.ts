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
 *  `domain_errors` / `session/service.py::recover` 的 RecoveryConflict）。
 *
 *  ⚠ 本文件是 **5 条** `test()`（前三条：一并断言上面三件事 / 锁「裁决能落定」/
 *  对照组「受理成功才清空」；第四条是 #1148 的窗外迟到失败回填；第五条是 #1151 加的
 *  「读失败 ≠ 没事」）；Playwright 每例跑两个视口 ⇒ 报告里是 **10 个 run**。别把
 *  「10」读成 10 条用例（独立审查两次记录过这处口径混淆）。 */

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

/** #1151：权威读面（`GET /recovery`）必须与用例伪造的事件说**同一件事**，
 *  所以它得**有状态**：409 之前答空（那一刻后端**确实**没有待裁决事实），409 之后
 *  答**同一份** `PENDING`，裁决落定（`POST /recover` 200）之后再回到空。
 *
 *  为什么不能只把夹具缺省值改成非空常量：那是用新假象换掉旧假象——「对照组（受理
 *  成功才清空）」与「#1148 窗外迟到失败」两条已通过用例在那些时刻后端真的没有待裁决
 *  事实，恒回非空会让它们同时失真。读面必须跟着用例的事件顺序走。
 *
 *  修前形状（本票的红）：本文件只传 `onMessagesPost`（409 + 清单），`GET /recovery`
 *  走夹具缺省 `[]`，而 `useSession.ts:1553-1559` 为 P1 会把 mode 落回 `viewing(sid)`
 *  ⇒ 主动读面 effect（依赖 `mode`）立刻重跑，**空数组覆盖了刚被 409 点亮的非空清单**
 *  ⇒ `hasPendingDecisions`（`App.tsx:1145`）转假、面板根本不挂载。 */
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

/** 打开会话并停在「可恢复」态；返回 `state` 供用例里的 409 分支置位。 */
function scenario(): PendingState {
  return { conflicted: false, adjudicated: false };
}

async function openCrashed(page: Page, mock: ApiMock): Promise<void> {
  await routeApi(page, mock);
  await page.goto('/');
  await page.locator('.session-item').first().click();
  await expect(page.locator('.recover-btn')).toBeVisible();
}

test('待裁决 409：composer 文本仍在 + 人话横幅 + 裁决面板被唤起', async ({ page }) => {
  // #1151：权威读面与这次 409 说同一件事（见 pendingReadFace 注释）。
  const state = scenario();
  await openCrashed(page, {
    sessions: [ROW],
    events: crashedEvents(),
    onRecoveryGet: pendingReadFace(state),
    onMessagesPost: async (route) => {
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

test('待裁决 409 后提交裁决：面板能落定（不永久停在「提交中」）', async ({ page }) => {
  /* P1 的端到端盲区（独立审查记录）：此前只验到「面板渲染出来」，没验「能提交」。
     修复前 mode 留在 `live` ⇒ `recover` 的落地守护 `shouldApplyRecoverResult`（只认
     `viewing`）把 200 结果整段丢弃 ⇒ 面板永久「提交中…」、按钮禁用、用户点不动。 */
  const state = scenario();
  await openCrashed(page, {
    sessions: [ROW],
    events: crashedEvents(),
    onRecoveryGet: pendingReadFace(state),
    onMessagesPost: async (route) => {
      state.conflicted = true;
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: { message: PROTOCOL_MESSAGE, pending_decisions: PENDING } }),
      });
    },
    // 裁决 200 = 后端返回**已结清**的事件流（run 有终态、无悬空 tool_call）。
    onRecoverPost: async (route) => {
      state.adjudicated = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          ...crashedEvents(),
          { type: 'tool/result', data: { tool_call_id: 'call-1', content: 'ok' }, seq: 6, session_id: SID, step_id: 1, run_id: RUN, time: T },
          { type: 'run/completed', data: {}, seq: 7, session_id: SID, run_id: RUN, time: T },
        ]),
      });
    },
  });

  const composer = page.getByLabel('Agent 任务');
  await composer.fill(DRAFT);
  await page.getByLabel('发送').click();
  await expect(page.locator('.recovery-decision-panel')).toBeVisible();

  await page.locator('.recovery-submit-btn').click();

  // 裁决落定：面板退场（修复前 `submitting` 恒真 ⇒ 面板挂着、按钮永远禁用）
  await expect(page.locator('.recovery-decision-panel')).toHaveCount(0);
  // 结清后会话不再可恢复 ⇒ 恢复入口随之消失
  await expect(page.locator('.recover-btn')).toHaveCount(0);
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

test('#1151 读失败 ≠ 没事：权威读面 5xx 不得抹掉已知的待裁决清单', async ({ page }) => {
  /* 本票（#1151）的裁决要求第 2 条：夹具改成有状态之后，「读失败 ≠ 没事」这条意图
     仍必须**可测**。这是本类缺陷的真正防线（不变量 #22 在 UI 侧的表达）：读不到是
     「未知」，与后端明确说「没有待裁决」是两件事。

     消融对照（三臂，同一会话、同一 ROW/events）：
       A 臂 = 读面答非空     ⇒ 面板在场（`pending-adjudication.spec.ts` 已锁，不在本文件重复）
       B 臂 = 读面答空清单   ⇒ 面板不在场（本节）
       C 臂 = 读面 5xx       ⇒ **同样**不在场，且**不得**出现任何「已结清 / 已中断」式的
                              谎报——`fetchPendingDecisions` 读失败回 `null`（"未知"，
                               `useSession.ts:696-699`），该路径下 409 已经点亮的清单
                               不会被抹掉（`refreshPendingDecisions` 只在 `list !== null`
                               时写入）。
     本臂守住的是 C 与「409 点亮的清单」的关系：读面坏了，界面**保留**已知事实。 */
  await openCrashed(page, {
    sessions: [ROW],
    events: crashedEvents(),
    // 读面整体不可用（500）：前端必须保持"未知"，绝不回落空清单。
    onRecoveryGet: async (route) => {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'boom' }),
      });
    },
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

  // ① 数据不丢这条契约不因读面故障而变。
  await expect(composer).toHaveValue(DRAFT);
  // ② 读面坏了 ≠ 没事：409 已点亮的清单必须仍在（面板在场，不是空态）。
  await expect(page.locator('.recovery-decision-panel')).toBeVisible();
  // ③ 也不得反向谎报"已结清"——横幅仍如实说明需要人工裁决。
  await expect(page.locator('.app-error')).toContainText('待人工裁决');
});
