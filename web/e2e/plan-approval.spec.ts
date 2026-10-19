/** #1356 方案 3：计划审批卡全链（纯 UI，mock 只按真后端契约应答）。
 *
 *  夹具走**真实渲染路径**：耐久事件里 `user/input-requested(kind=plan_approval)`
 *  与 `run/paused(reason=user_input, input_request_id=…)` 对齐 ⇒ 卡片落屏；三个按钮
 *  各点一次 ⇒ App 发出 `POST /api/sessions/{id}/resume`，断言打在**请求体**上
 *  （`resume_basis: 'user_input'` + `input_request.decision`）。
 *
 *  与单测的分工：`PlanApprovalCard.test.tsx` 锁卡片自身的渲染与三裁决回调，
 *  `projection.test.ts` 锁投影分流与折叠；这里锁**接线**——App 真的把卡片回调交给
 *  resume 通道、composer 真的被锁、计划审批不污染 `PlanList`（方案 4 的行为级边界）。 */

import { expect, test, type Route } from '@playwright/test';
import { RUN, SID, T, fulfillSse, routeApi, sessionRow, type FrameSpec } from './fixtures';

const PLAN_REQ = 'plan-approval-req-1';
const PLAN_MARKDOWN = '# 计划\n\n## 目标\n把 A 改成 B。\n\n## 分步改动\n1. 改 `src/a.py`\n2. 跑测试';
const TOUCHED = ['src/a.py', 'tests/test_a.py'];

/** 暂停在计划审批的帧剧本（末帧 `run/paused` 非终态 ⇒ 客户端转 WS 接流保持在场）。 */
function planPausedFrames(): FrameSpec[] {
  return [
    { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
    { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
    { type: 'user/message', data: { content: '先给个计划' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
    {
      type: 'user/input-requested',
      seq: 4,
      session_id: SID,
      run_id: RUN,
      step_id: 1,
      time: T,
      data: {
        request_id: PLAN_REQ,
        kind: 'plan_approval',
        run_id: RUN,
        plan_markdown: PLAN_MARKDOWN,
        touched_files: TOUCHED,
        artifact_id: 'art-plan-1',
        plan_sha256: 'deadbeef',
      },
    },
    {
      type: 'run/paused',
      seq: 5,
      session_id: SID,
      run_id: RUN,
      step_id: 1,
      time: T,
      data: { reason: 'user_input', input_request_id: PLAN_REQ, budget_version: 1 },
    },
  ];
}

/** 装好页面并停到「计划卡已落屏」这一步。返回 resume 请求体的取值器。 */
async function openPlanCard(page: import('@playwright/test').Page) {
  const frames = planPausedFrames();
  const history: FrameSpec[] = [...frames];
  let resumeBody: Record<string, unknown> | null = null;

  await routeApi(page, {
    sessions: [sessionRow(SID)],
    events: history,
    // 暂停 run 的传输语义同 budget-entry：POST 流在 run/paused 后干净收尾、重连快照
    // `has_active_run: false` ⇒ 客户端自行收流后退回 viewing，卡片才落屏。
    onWs: () => ({ events: history, hasActiveRun: false }),
  });
  // 后注册先匹配 ⇒ 盖过 routeApi 的 catch-all。响应 = SSE 帧（与真后端同形）。
  await page.route(new RegExp(`/api/sessions/${SID}/resume$`), async (route: Route) => {
    resumeBody = route.request().postDataJSON() as Record<string, unknown>;
    const resumeFrames: FrameSpec[] = [
      { type: 'run/resumed', data: { resume_basis: 'user_input' }, seq: 6, session_id: SID, run_id: RUN, time: T },
      { type: 'text/delta', data: { delta: '收到。' }, seq: 7, session_id: SID, run_id: RUN, step_id: 2, time: T },
      { type: 'run/completed', data: {}, seq: 8, session_id: SID, run_id: RUN, time: T },
    ];
    history.push(...resumeFrames);
    await fulfillSse(route, resumeFrames);
  });

  await page.goto('/');
  await page.locator('.session-item').first().click();
  await expect(page.locator('.plan-approval-card')).toBeVisible();
  return () => resumeBody;
}

test('#1356 AC9：计划卡渲染正文 + 触碰文件 + 三按钮', async ({ page }) => {
  await openPlanCard(page);

  await expect(page.locator('.plan-approval-status')).toContainText('等待批准');
  // 正文走 markdown 渲染：标题成元素，不显示裸 `#`。
  const plan = page.locator('[data-testid="plan-approval-markdown"]');
  await expect(plan).toContainText('把 A 改成 B。');
  await expect(plan).not.toContainText('# 计划');

  await expect(page.locator('.plan-approval-file')).toHaveText(TOUCHED);
  await expect(page.locator('.plan-approval-btn')).toHaveCount(3);
});

test('#1356 AC9/AC10：批准 → resume 体含 user_input + decision=approve，卡片随 run/resumed 收起', async ({ page }) => {
  const resumeBody = await openPlanCard(page);

  await page.locator('.plan-approval-approve').click();

  // 卡片卸载由 resumePausedRun 发起时的乐观 setMode('live') 驱动，先于拦截层捕获
  // 请求体（#509 竞态②）⇒ 用 poll 等请求体落定，而不是断言卡片消失即可。
  await expect.poll(() => resumeBody()).toEqual({
    run_id: RUN,
    resume_basis: 'user_input',
    // `user_input` 恢复仍走同一个 resume 契约：budget 是必填壳（expected_version +
    // 空的 run 覆盖），不是"计划审批要改预算"。
    budget: { expected_version: 1, run: {} },
    input_request: { request_id: PLAN_REQ, decision: 'approve' },
  });
  await expect(page.locator('.plan-approval-card')).toHaveCount(0);
});

test('#1356 AC9：要求修改 → decision=revise，反馈随裁决发出', async ({ page }) => {
  const resumeBody = await openPlanCard(page);

  await page.locator('.plan-approval-feedback').fill('先补测试再改');
  await page.locator('.plan-approval-revise').click();

  await expect.poll(() => resumeBody()).toEqual({
    run_id: RUN,
    resume_basis: 'user_input',
    budget: { expected_version: 1, run: {} },
    input_request: { request_id: PLAN_REQ, decision: 'revise', feedback: '先补测试再改' },
  });
});

test('#1356 AC9：拒绝 → decision=reject，反馈随裁决发出', async ({ page }) => {
  const resumeBody = await openPlanCard(page);

  await page.locator('.plan-approval-feedback').fill('方向不对');
  await page.locator('.plan-approval-reject').click();

  await expect.poll(() => resumeBody()).toEqual({
    run_id: RUN,
    resume_basis: 'user_input',
    budget: { expected_version: 1, run: {} },
    input_request: { request_id: PLAN_REQ, decision: 'reject', feedback: '方向不对' },
  });
});

/** 计划卡 + 一条真实 `task/plan_updated` 同时在场的帧剧本（AC11 的"同屏"面）。 */
const PLAN_ITEMS = [
  { id: 'p1', content: '改 a.py', activeForm: '改 a.py', status: 'in_progress', source: 'agent' },
  { id: 'p2', content: '跑测试', activeForm: '跑测试', status: 'pending', source: 'agent' },
];

test('#1356 AC11：计划卡与 PlanList 同屏互不干扰（批准后清单不变）', async ({ page }) => {
  const frames = planPausedFrames();
  // 在 `user/input-requested` 之前插一条清单帧：两者都由**独立**投影驱动，
  // 同屏是正常状态，不是互斥关系（submit_plan 刻意不发 `task/plan_updated`）。
  frames.splice(3, 0, {
    type: 'task/plan_updated', data: { items: PLAN_ITEMS },
    seq: 3.5, session_id: SID, run_id: RUN, time: T,
  });
  const history: FrameSpec[] = [...frames];
  await routeApi(page, {
    sessions: [sessionRow(SID)],
    events: history,
    onWs: () => ({ events: history, hasActiveRun: false }),
  });
  await page.route(new RegExp(`/api/sessions/${SID}/resume$`), async (route: Route) => {
    const resumeFrames: FrameSpec[] = [
      { type: 'run/resumed', data: { resume_basis: 'user_input' }, seq: 6, session_id: SID, run_id: RUN, time: T },
      { type: 'run/completed', data: {}, seq: 7, session_id: SID, run_id: RUN, time: T },
    ];
    history.push(...resumeFrames);
    await fulfillSse(route, resumeFrames);
  });

  await page.goto('/');
  await page.locator('.session-item').first().click();
  await expect(page.locator('.plan-approval-card')).toBeVisible();
  // 同屏：清单与计划卡同时在场，清单内容与帧里的一致（审批卡没有"吃掉"清单）。
  const list = page.locator('.plan-list');
  await expect(list).toBeVisible();
  await expect(list).toContainText('2 项');
  const itemsBefore = await page.locator('.plan-list-item').allInnerTexts();

  await page.locator('.plan-approval-approve').click();
  await expect(page.locator('.plan-approval-card')).toHaveCount(0);
  // 批准不产生 `task/plan_updated`（`submit_plan` 与 `update_plan` 正交）⇒ 清单原样。
  await expect(page.locator('.plan-list')).toBeVisible();
  expect(await page.locator('.plan-list-item').allInnerTexts()).toEqual(itemsBefore);
});

test('#1356 AC11：计划审批锁住 composer，且不污染进度清单', async ({ page }) => {
  await openPlanCard(page);

  await expect(page.locator('.composer-locked-hint')).toHaveText('请先决定当前计划怎么处理，再发送新消息');
  await expect(page.locator('.composer-dock textarea')).toBeDisabled();
  // 方案 4 的行为级边界：计划正文不进 `task/plan_updated` ⇒ 进度清单不存在。
  await expect(page.locator('.plan-list')).toHaveCount(0);
});
