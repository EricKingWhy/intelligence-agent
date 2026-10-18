/** #1030（Web 呈现）：状态投影的「待裁决」一等态（AC1–AC4）。
 *
 *  `#1327` 起本文件的裁决面板/入口用例已按新呈现重写：`RecoveryDecisionPanel`
 *  与「去裁决」按钮已删，待裁决只以后端驱动的被动通知横幅（`role="status"`，
 *  无需交互）呈现；裁决改由后端按操作类型自动做（只读 + replay_safe ⇒ 重跑，
 *  其余 ⇒ 结清不重跑），前端没有任何可点的裁决入口。
 *
 *  票面现场（会话 `b9aca235-…`）：一轮 run 的 bash 调用（find，60.4s 超时被杀）结果
 *  UNKNOWN，会话进入待人工裁决态。此时：
 *    ① 顶栏徽标显示「思考中 · 176,512 tok」，而该 run 早已死亡（run 无终态）；
 *    ② 唯一能解封会话的动作（裁决）要先点「恢复会话」、再撞一次特定 409 才出现；
 *    ③ 横幅透传后端 HTTP 协议原文，最终用户照着做不了；
 *    ④ 后端重启后 `run/interrupted` 落盘 → 徽标变「已中断」、横幅消失，看似痊愈，
 *       但 UNKNOWN 裁决仍挂着、发消息仍 409——误导性最强。
 *
 *  本文件用**主动读面**（`GET /api/sessions/{id}/recovery`，#1049）驱动全部用例；
 *  `#1327` 起不再有 409/裁决提交路径，被动通知横幅的唯一事实来源就是这份清单。
 */

import { expect, test, type Page } from '@playwright/test';
import { RUN, SID, T, routeApi, type ApiMock, type FrameSpec } from './fixtures';

const ROW = {
  session_id: SID,
  event_count: 5,
  first_event_time: T,
  last_event_time: T,
  first_user_message: '崩溃前的问题',
  trace_id: null, trace_url: null,
};

/** 后端 `_reconcile_pending` 的行（与 409 detail 的 `pending_decisions` 逐字段同源）。 */
const PENDING = [
  {
    tool_call_id: 'call-82ade',
    tool_name: 'bash',
    state: 'UNKNOWN',
    default_action: 'RETRY',
    risk_level: 'high',
    probe: { verifiable: false, suggested_action: null },
  },
];

/** 坏死 run：`run/started` 之后**没有任何终态事件**（票面现场的形状）。 */
function zombieEvents(): FrameSpec[] {
  return [
    { type: 'session/started', seq: 1, session_id: SID, time: T },
    { type: 'user/message', data: { content: '崩溃前的问题' }, seq: 2, session_id: SID, time: T },
    { type: 'run/started', data: { turn_index: 1 }, seq: 3, session_id: SID, run_id: RUN, time: T },
    {
      type: 'tool/call',
      data: { tool_call_id: 'call-82ade', tool_name: 'bash', args: { command: 'find /' } },
      seq: 4,
      session_id: SID,
      step_id: 1,
      run_id: RUN,
      time: T,
    },
  ];
}

/** AC4 现场：僵尸 run 被启动扫描补上 `run/interrupted`（后端重启两次之后）。 */
function restartedEvents(): FrameSpec[] {
  return [
    ...zombieEvents(),
    {
      type: 'run/interrupted',
      data: { interrupted_seq: 4, reason: 'process_restart' },
      seq: 5,
      session_id: SID,
      step_id: 1,
      run_id: RUN,
      time: T,
    },
  ];
}

/** 崩溃会话：run 无终态 + 一条未配对 tool/call ⇒ `isRecoverableRun` 为真
 *  （`canRecover` 才亮出「恢复会话」按钮，才能走到 409 那条路）。
 *  与 d-recover.spec.ts 的夹具同形——本文件要用它触发**带 message 的** 409。 */
function crashedEvents(): FrameSpec[] {
  return [
    { type: 'session/started', seq: 1, session_id: SID, time: T },
    { type: 'user/message', data: { content: '崩溃前的问题' }, seq: 2, session_id: SID, time: T },
    { type: 'run/started', data: { turn_index: 1 }, seq: 3, session_id: SID, run_id: RUN, time: T },
    { type: 'model/completed', data: { content: '开始执行。' }, seq: 4, session_id: SID, step_id: 1, run_id: RUN, time: T },
    {
      type: 'tool/call',
      data: { tool_call_id: 'call-1', tool_name: 'bash', args: { command: 'ls' } },
      seq: 5, session_id: SID, step_id: 1, run_id: RUN, time: T,
    },
  ];
}

/** 打开会话：只点击列表项，**不点**「恢复会话」。 */
async function openSession(page: Page, mock: ApiMock): Promise<void> {
  await routeApi(page, mock);
  await page.goto('/');
  await page.locator('.session-item').first().click();
}

test.describe('#1030 待裁决呈现', () => {
  test('AC1：坏死 run + 待裁决 ⇒ 徽标是「待裁决」，不是「思考中」', async ({ page }) => {
    await openSession(page, { sessions: [ROW], events: zombieEvents(), pendingDecisions: PENDING });

    const pulse = page.locator('.run-pulse');
    await expect(pulse).toContainText('待裁决');
    await expect(pulse).not.toContainText('思考中');
    await expect(pulse).toHaveClass(/pulse-awaiting-decision/);
  });

  test('AC1（对照臂）：同一会话后端说没有待裁决 ⇒ 徽标仍是「思考中」', async ({ page }) => {
    // 差异只由"后端有无待裁决"这个事实驱动。若这一臂也变「待裁决」，
    // 上一条就证明不了任何事（消融实验的对照组）。
    await openSession(page, { sessions: [ROW], events: zombieEvents() });
    await expect(page.locator('.run-pulse')).toContainText('思考中');
  });

  test('AC3（新口径）：待裁决清单非空 ⇒ 被动通知横幅在场，无任何裁决入口', async ({ page }) => {
    // `#1327`：裁决改由后端按操作类型自动做。前端只如实呈现"有欠账"——
    // 被动通知横幅（`role="status"`，无需交互），没有任何可点的裁决入口。
    await openSession(page, { sessions: [ROW], events: zombieEvents(), pendingDecisions: PENDING });

    const banner = page.locator('.adjudication-banner');
    await expect(banner).toBeVisible();
    await expect(banner).toHaveAttribute('role', 'status');
    await expect(banner.locator('.adjudication-headline')).toContainText('自动处理');
    // 没有任何裁决面板 / 弹窗 / 可点入口（「去裁决」按钮已删）。
    await expect(page.locator('.recovery-decision-panel')).toHaveCount(0);
    await expect(banner.locator('button')).toHaveCount(0);
    await expect(page.locator('body')).not.toContainText('去裁决');
  });

  test('AC2（新口径）：横幅第一层是人话（声明自动处理），不含协议词汇', async ({ page }) => {
    await openSession(page, { sessions: [ROW], events: zombieEvents(), pendingDecisions: PENDING });

    const headline = page.locator('.adjudication-banner .adjudication-headline');
    await expect(headline).toContainText('自动处理');
    await expect(headline).not.toContainText('裁决');
    for (const token of ['POST /api/sessions', 'tool_call_id', 'verdict ∈']) {
      await expect(headline).not.toContainText(token);
    }
  });

  test('AC4（新口径）：重启后待裁决 ⇒ 徽标「待裁决」+ 被动横幅如实呈现', async ({ page }) => {
    await openSession(page, { sessions: [ROW], events: restartedEvents(), pendingDecisions: PENDING });

    // 票面现场的旧误导（徽标「已中断」+ 看似痊愈）不再出现：徽标如实说「待裁决」，
    // 横幅是被动通知（无裁决入口）——后端下次恢复时按操作类型自动结清。
    const pulse = page.locator('.run-pulse');
    await expect(pulse).toContainText('待裁决');
    await expect(pulse).not.toContainText('已中断');
    await expect(page.locator('.adjudication-banner')).toBeVisible();
    await expect(page.locator('.recovery-decision-panel')).toHaveCount(0);
  });

  test('AC4（对照臂）：同一重启会话、后端说没有待裁决 ⇒ 徽标是「已中断」', async ({ page }) => {
    await openSession(page, { sessions: [ROW], events: restartedEvents() });
    await expect(page.locator('.run-pulse')).toContainText('已中断');
  });

  test('读不到清单 ≠ 没事：端点 500 时不冒称「待裁决」，也不谎报已结清', async ({ page }) => {
    // 「读不到」必须保持"未知"：既不点亮「待裁决」（没有事实），也不写任何
    // "已结清/正常"的承诺。这里锁的是不虚构。
    await openSession(page, {
      sessions: [ROW],
      events: zombieEvents(),
      onRecoveryGet: async (route) => {
        await route.fulfill({ status: 500, body: JSON.stringify({ detail: 'boom' }), contentType: 'application/json' });
      },
    });

    await expect(page.locator('.recovery-decision-panel')).toHaveCount(0);
    await expect(page.locator('.adjudication-banner')).toHaveCount(0);
  });

  test('长协议原文不撑破布局（折叠层展开后仍无横向滚动）', async ({ page }) => {
    // 后端 detail 是**一整段长单行**（内嵌端点 + verdict 枚举）。两层化之后
    // `.recover-conflict` 从"单行 + ellipsis"变成"多行 + 可展开"；若不允许折行，
    // 它会把 .workspace-toolbar 顶出横向滚动条——那正是"修好文案、弄坏布局"。
    const longDetail =
      '存在需要人工裁决的 UNKNOWN Operation（bash(tool_call_id=call_82ade-9f1c-4e77-b0aa-2f6d1c8e5a31)）：'
      + '先 POST /api/sessions/{id}/recover 携带 decisions=[{tool_call_id, verdict}] 结清'
      + '（verdict ∈ CONFIRM_SUCCESS / CONFIRM_FAILURE / RETRY / ABANDON / DEFER）；'
      + '裁决前请先核查该调用的外部事实（例如读回文件、查外部系统），不要凭猜测选"当作已生效"。';
    await openSession(page, {
      sessions: [ROW],
      events: crashedEvents(),
      onRecoverPost: async (route) => {
        await route.fulfill({
          status: 409,
          body: JSON.stringify({ detail: longDetail }),
          contentType: 'application/json',
        });
      },
    });
    await page.locator('.recover-btn').click();
    await expect(page.locator('.recover-conflict')).toBeVisible();

    // 展开技术详情（最坏情况：长原文可见）。
    await page.locator('.recover-conflict details.adjudication-tech > summary').click();
    await expect(page.locator('.recover-conflict .adjudication-tech-body')).toBeVisible();

    // 判据必须是**文字实际排布出来的宽度**，不能是 scrollWidth：
    // `overflow: visible` 的元素即使内容横着长出去，scrollWidth 仍等于 clientWidth
    // （实测 nowrap 与折行两种写法读数都是 0——那样的断言恒真、什么都锁不住）。
    // Range.getBoundingClientRect() 量的是文字真正占据的盒子，nowrap 下会显著超出容器。
    const overflow = await page.evaluate(() => {
      const el = document.querySelector('.recover-conflict') as HTMLElement | null;
      if (!el) return null;
      const range = document.createRange();
      range.selectNodeContents(el);
      const textWidth = range.getBoundingClientRect().width;
      const boxWidth = el.getBoundingClientRect().width;
      const tb = document.querySelector('.workspace-toolbar') as HTMLElement | null;
      return {
        docScroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        textWidth,
        boxWidth,
        toolbarWidth: tb ? tb.getBoundingClientRect().width : 0,
        toolbarScroll: tb ? tb.scrollWidth - tb.clientWidth : 0,
      };
    });
    expect(overflow).not.toBeNull();
    expect(overflow!.docScroll, '页面不得出现横向滚动').toBeLessThanOrEqual(1);
    expect(overflow!.toolbarScroll, '工具栏不得出现横向滚动').toBeLessThanOrEqual(1);
    // 文字不得比自己的盒子宽（超了就是没折行、横着溢出去了）。
    expect(
      overflow!.textWidth,
      `协议原文必须折行：文字宽 ${overflow!.textWidth} 不得超出容器宽 ${overflow!.boxWidth}`,
    ).toBeLessThanOrEqual(overflow!.boxWidth + 1);
    // 且不得把工具栏顶宽（盒子被 flex 撑开的另一种表现）。
    expect(overflow!.boxWidth).toBeLessThanOrEqual(overflow!.toolbarWidth + 1);
  });
});
