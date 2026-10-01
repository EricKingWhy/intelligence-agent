/** #426：预算入口纯 UI 全链——设置预算 → 启动 → run/paused(budget) → PausedPanel → UI 恢复。
 *
 *  票面验收「纯 UI 路径：设置预算 → 发多 turn 任务 → 触发暂停 → PausedPanel 恢复，
 *  全链零 API 手工调用」的 e2e 落点：所有请求都由界面发出，mock 只按真后端契约应答
 *  （创建=SSE 帧、恢复=SSE 帧、接流=WS 快照+keep）；断言打在**请求体**上——
 *  budget.run 三维逐字对齐 `RunBudgetRequest`（extra="forbid"，app.py:179-206）；
 *  不设置预算 ⇒ 载荷不含 budget 键（默认行为不变）。
 *
 *  与单测的分工：amend/api 单测锁映射与 wire 形状（金钉），这里锁**接线**——
 *  App 真的把三个输入的草稿交给了 toCreateBudget、PausedPanel 真的在 run/paused
 *  后出现、恢复按钮真的发出同 run CAS 请求并在 run/resumed 后收起。 */

import { expect, test, type Route } from '@playwright/test';
import { RUN, SID, T, fulfillSse, routeApi, submitTask, type FrameSpec } from './fixtures';

/** 启动即到 tokens 维预算顶的帧剧本（末帧 run/paused 非终态 ⇒ 客户端转 WS 接流保持在场）。 */
function pausedFrames(): FrameSpec[] {
  return [
    { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
    { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
    { type: 'user/message', data: { content: '跑一个长任务' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'model/started', data: { model: 'e2e-model' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
    {
      type: 'run/paused',
      seq: 5,
      session_id: SID,
      run_id: RUN,
      time: T,
      data: {
        reason: 'budget_exhausted',
        trigger_dimension: 'run.max_total_tokens',
        budget_version: 1,
        consumed: { agent_turns: 2, total_tokens: 1234 },
        limits: { run: { max_agent_turns_total: 3, max_total_tokens: 1234 } },
        resume_requirements: ['抬高 run.max_total_tokens 的绝对 ceiling 后同 run 恢复'],
      },
    },
  ];
}

test('#426 纯 UI 全链：预算三项随启动请求提交 → 预算暂停 → PausedPanel 恢复抬高同维', async ({ page }) => {
  const frames = pausedFrames();
  // durable log = /events 与 WS 快照的同源真相（fixtures 纪律）。恢复帧在恢复请求
  // 应答时补进这份 log——真后端里 run/resumed 及其后的事件都是 durable 的，恢复后的
  // 历史重载必须能看到它们，否则投影会退回"暂停中"。
  const history: FrameSpec[] = [...frames];
  let createBody: Record<string, unknown> | null = null;
  let resumeBody: Record<string, unknown> | null = null;

  await routeApi(page, {
    onSessionPost: async (route: Route) => {
      createBody = route.request().postDataJSON() as Record<string, unknown>;
      await fulfillSse(route, frames);
    },
    events: history,
    // 暂停 run 的传输语义（runmanager.py:342 `_drive` finally → finish() → DONE）：
    // POST 流在 run/paused 后**干净收尾**（非终态 ⇒ 客户端视为"流异常收尾"）；
    // 重连订阅时 run task 已结束 ⇒ `get_active()` 为 None ⇒ 快照 `has_active_run:
    // false`（websocket.py:240），客户端据此自行收流（wsStream.ts:259）。
    // 客户端在重连额度耗尽后退回 viewing 并重读历史——面板（App.tsx:818 要求
    // `!streaming`）正是在那一步出现的。
    onWs: () => ({ events: history, hasActiveRun: false }),
  });
  // fixtures 的 routeApi 不认识 /resume（真实端点 POST /api/sessions/{id}/resume，
  // api.ts:1274）。Playwright 后注册的路由先匹配 ⇒ 这里盖过 catch-all；响应 = SSE 帧
  // （与真后端 `_run_stream_response` 同形），seq 接着暂停帧单调续。
  await page.route(new RegExp(`/api/sessions/${SID}/resume$`), async (route: Route) => {
    resumeBody = route.request().postDataJSON() as Record<string, unknown>;
    const resumeFrames: FrameSpec[] = [
      { type: 'run/resumed', data: {}, seq: 6, session_id: SID, run_id: RUN, time: T },
      { type: 'text/delta', data: { delta: '预算已抬高，继续。' }, seq: 7, session_id: SID, run_id: RUN, step_id: 2, time: T },
      { type: 'run/completed', data: {}, seq: 8, session_id: SID, run_id: RUN, time: T },
    ];
    history.push(...resumeFrames);
    await fulfillSse(route, resumeFrames);
  });

  await page.goto('/');

  // ① 纯 UI 设置预算三维（新建会话态的 Composer 预算行）
  await page.getByLabel('预算上限（Agent turns，留空为默认）').fill('3');
  await page.getByLabel('预算上限（总 tokens，留空为默认）').fill('1234');
  const deadlineLocal = '2026-10-01T12:30';
  await page.getByLabel('预算截止时间（留空为不设）').fill(deadlineLocal);
  await submitTask(page, '跑一个长任务');

  // ② 创建请求体：budget.run 三维逐字对齐 RunBudgetRequest（deadline = UTC 瞬时文本）
  expect(createBody).not.toBeNull();
  expect((createBody as Record<string, unknown>).budget).toEqual({
    run: {
      max_agent_turns_total: 3,
      max_total_tokens: 1234,
      deadline_at: new Date(deadlineLocal).toISOString(),
    },
  });

  // ③ 预算暂停 → PausedPanel 在场（恢复前置条件逐字来自 run/paused 载荷）。
  //    面板出现在客户端退回 viewing 并重读历史之后（见上面 onWs 的传输语义注释），
  //    重连退避（500ms×2³）走完需要几秒 ⇒ 放宽这一条 expect 的超时。
  const panel = page.locator('.pause-panel');
  await expect(panel).toBeVisible({ timeout: 15_000 });
  await expect(panel).toContainText('已在预算到顶处暂停');
  await expect(panel).toContainText('抬高 run.max_total_tokens 的绝对 ceiling 后同 run 恢复');

  // ④ UI 抬高卡住的那一维 → 恢复同一 run
  await panel.getByLabel(/恢复用的绝对 ceiling/).fill('9999');
  await panel.getByRole('button', { name: '恢复同一 run' }).click();

  // ⑤ 面板收起（run/resumed 清 run_paused）之后，恢复请求体必已发出：同 run CAS
  //    + budget_increase + 抬高的绝对值（字段名由 resumeTarget 从触发维度导出）。
  await expect(panel).toHaveCount(0);
  expect(resumeBody).toEqual({
    run_id: RUN,
    resume_basis: 'budget_increase',
    budget: { expected_version: 1, run: { max_total_tokens: 9999 } },
  });

  // ⑥ 全链终态：恢复后的回答呈现（零手工 API——请求全部由界面发出）
  await expect(page.locator('.model-output').last()).toContainText('预算已抬高，继续。');
});

test('#426 不设置预算：创建载荷不含 budget 键（默认行为不变）', async ({ page }) => {
  const frames: FrameSpec[] = [
    { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
    { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
    { type: 'user/message', data: { content: '普通任务' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'model/started', data: { model: 'e2e-model' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'text/delta', data: { delta: '完成。' }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'model/completed', data: { model: 'e2e-model' }, seq: 6, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'run/completed', data: {}, seq: 7, session_id: SID, run_id: RUN, time: T },
  ];
  let createBody: Record<string, unknown> | null = null;
  await routeApi(page, {
    onSessionPost: async (route: Route) => {
      createBody = route.request().postDataJSON() as Record<string, unknown>;
      await fulfillSse(route, frames);
    },
    events: frames,
  });

  await page.goto('/');
  // 预算行在场（#426 的入口本身），但全部留空 = 后端默认
  await expect(page.getByLabel('预算上限（Agent turns，留空为默认）')).toBeVisible();
  await submitTask(page, '普通任务');

  expect(createBody).not.toBeNull();
  expect(createBody).not.toHaveProperty('budget');
  await expect(page.locator('.model-output').last()).toContainText('完成。');
});
