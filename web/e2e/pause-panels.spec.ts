/** #1346：暂停面板三处 e2e 缺口（continuation / stuck 路由限制 / 恢复 409）。
 *
 * ① continuation：`run/paused.data.continuation` 的四键（completed / remaining /
 *    blockers / next_safe_action）必须逐字上屏。单测（`PausedPanel.test.tsx` /
 *    `StuckPausedPanel.test.tsx`）只锁静态渲染；这里锁「事件载荷 → 投影 → 面板」
 *    的接线——两个面板共用 `.pause-panel-continuation`，两处各锁一条。
 * ② stuck 路由限制（`11 §6.1` MUST NOT 一键重试）：`reason=stuck` 走
 *    `StuckPausedPanel`，面板无 ceiling 输入框、无「恢复同一 run」按钮，且**不发**
 *    `POST /api/sessions/{id}/resume`（恢复需要变更依据）。
 * ③ 预算恢复 409：`POST /api/sessions/{id}/resume` 的请求体逐字（同 run CAS +
 *    budget_increase + 绝对 ceiling）；409 后界面显示后端 detail（`恢复被拒绝：…`），
 *    面板与用户草稿都保留，并重读 durable log 对齐权威状态（不变量 #22）。
 *
 * 与既有 spec 的分工：`budget-entry.spec.ts` 锁「纯 UI 恢复成功」全链；
 * `d-recover.spec.ts` 锁的是 `POST /recover` 的 409（崩溃遗留），不是本文件的
 * `POST /resume`（暂停 run）。 */

import { expect, test, type Page, type Route } from '@playwright/test';
import { RUN, SID, T, routeApi, type FrameSpec } from './fixtures';

const ROW = {
  session_id: SID,
  event_count: 5,
  first_event_time: T,
  last_event_time: T,
  first_user_message: '跑一个长任务',
  trace_id: null,
  trace_url: null,
};

/** 预算暂停的 durable log（`run/paused` 非终态 ⇒ 选中会话时不会自动接流，
 *  面板在历史装载后直接在场，无需重连退避）。 */
function pausedFrames(version = 1): FrameSpec[] {
  return [
    { type: 'session/started', seq: 1, session_id: SID, time: T },
    { type: 'run/started', data: { turn_index: 1 }, seq: 2, session_id: SID, run_id: RUN, time: T },
    { type: 'user/message', data: { content: '跑一个长任务' }, seq: 3, session_id: SID, time: T },
    { type: 'model/completed', data: { content: '先做一步。' }, seq: 4, session_id: SID, step_id: 1, run_id: RUN, time: T },
    {
      type: 'run/paused',
      seq: 5,
      session_id: SID,
      run_id: RUN,
      time: T,
      data: {
        reason: 'budget_exhausted',
        trigger_dimension: 'run.max_agent_turns_total',
        budget_version: version,
        consumed: { agent_turns: 3 },
        limits: {
          run: { max_agent_turns_total: 8 },
          local: { max_agent_turns: 500, source: 'deployment' },
        },
        closeout_source: 'model',
        continuation: {
          completed: ['本逻辑 run 已消耗 3 个 agent turn', '第二条已完成项'],
          remaining: ['暂停发生在下一轮模型决策之前'],
          blockers: ['run.max_agent_turns_total 到顶：consumed=3, ceiling=8'],
          next_safe_action: '提高绝对 ceiling 后以同一 run_id 恢复',
        },
        resume_requirements: ['抬高 run.max_agent_turns_total 的绝对 ceiling 后同 run 恢复'],
        trace_id: 'trace-pause-1346',
      },
    },
  ];
}

/** stuck 暂停的 durable log（`#968` / `#317`）：与预算暂停同形，但恢复依据是
 *  steer / 环境 / 策略变更，不是抬 ceiling。 */
function stuckFrames(): FrameSpec[] {
  const frames = pausedFrames(5);
  const paused = frames[frames.length - 1];
  paused.data = {
    reason: 'stuck',
    trigger_dimension: 'stuck.same_name_same_args',
    budget_version: 5,
    consumed: { agent_turns: 7 },
    limits: { run: { max_agent_turns_total: 20 } },
    closeout_source: 'deterministic',
    continuation: {
      completed: ['检测器连续 3 次观测到同工具同参数调用'],
      remaining: ['等待用户给出变更依据'],
      blockers: ['重复模式在 replan 后仍持续'],
      next_safe_action: '提供相关 steer / 环境变更 / 策略变更后恢复',
    },
    resume_requirements: ['需要相关 steer，或已记录的环境 / policy 变更'],
    trace_id: 'trace-stuck-1346',
  };
  return frames;
}

/** 选中唯一会话（无在途 run ⇒ 不接流，面板直接在场）。 */
async function openPausedSession(page: Page): Promise<void> {
  await page.goto('/');
  await page.locator('.session-item').first().click();
}

test('① continuation 四键逐字上屏（PausedPanel）', async ({ page }) => {
  await routeApi(page, { sessions: [ROW], events: pausedFrames() });
  await openPausedSession(page);

  const panel = page.locator('.pause-panel');
  await expect(panel).toBeVisible({ timeout: 15_000 });
  await expect(panel).toContainText('已在预算到顶处暂停');
  // 前三键按 CONTINUATION_SECTIONS 顺序渲染，第四键"下一步"为其后独立硬编码 li；文案逐字来自 run/paused 载荷。
  await expect(panel.locator('.pause-panel-continuation li')).toHaveText([
    '已完成：本逻辑 run 已消耗 3 个 agent turn；第二条已完成项',
    '剩余：暂停发生在下一轮模型决策之前',
    '阻塞：run.max_agent_turns_total 到顶：consumed=3, ceiling=8',
    '下一步：提高绝对 ceiling 后以同一 run_id 恢复',
  ]);
});

test('② stuck 暂停：引导面板不提供一键恢复，且不发 POST /resume', async ({ page }) => {
  let resumeCalls = 0;
  await routeApi(page, { sessions: [ROW], events: stuckFrames() });
  // 后注册的路由先匹配：若界面真的发了恢复请求，这里会记到（并如实回 409）。
  await page.route(new RegExp(`/api/sessions/${SID}/resume$`), async (route: Route) => {
    resumeCalls += 1;
    await route.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'stuck 暂停不接受 budget_increase' }),
    });
  });
  await openPausedSession(page);

  const panel = page.locator('.pause-panel');
  await expect(panel).toBeVisible({ timeout: 15_000 });
  await expect(panel).toContainText('已因疑似卡住暂停');
  // 引导面：给的是 steer / 变更依据，并明说本面板不提供一键恢复。
  await expect(panel).toContainText('提供新指示（steer）');
  await expect(panel).toContainText('不提供一键恢复');
  await expect(panel).toContainText('恢复前置条件：需要相关 steer，或已记录的环境 / policy 变更');
  await expect(panel.locator('.pause-panel-continuation li')).toHaveText([
    '已完成：检测器连续 3 次观测到同工具同参数调用',
    '剩余：等待用户给出变更依据',
    '阻塞：重复模式在 replan 后仍持续',
    '下一步：提供相关 steer / 环境变更 / 策略变更后恢复',
  ]);

  // 恢复控件必须一个都不在（无 ceiling 输入、无恢复按钮）。
  await expect(page.locator('#pause-resume-ceiling')).toHaveCount(0);
  await expect(panel.getByRole('button', { name: '恢复同一 run' })).toHaveCount(0);
  // Composer 不被禁用/遮挡：steer 通道保持可用。
  await expect(page.getByLabel('Agent 任务')).toBeEnabled();
  // wire 级：整个过程零 POST /resume。
  expect(resumeCalls).toBe(0);
});

test('③ 预算恢复 409：请求体逐字、后端 detail 上屏、草稿与面板保留、重读日志', async ({ page }) => {
  const frames = pausedFrames();
  let eventsCalls = 0;
  let resumeBody: Record<string, unknown> | null = null;
  let resumeUrl = '';

  await routeApi(page, { sessions: [ROW] });
  // 重读 durable log 的观测点：409 后 useSession 必须 `selectSession` 同一会话，
  // 让历史装载 effect 重跑（不变量 #22：屏幕上的暂停事实以服务器那份为准）。
  await page.route(new RegExp(`/api/sessions/${SID}/events$`), async (route: Route) => {
    eventsCalls += 1;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(frames) });
  });
  await page.route(new RegExp(`/api/sessions/${SID}/resume$`), async (route: Route) => {
    resumeBody = route.request().postDataJSON() as Record<string, unknown>;
    resumeUrl = route.request().url();
    // detail 逐字取自后端 `validate_resume` 的 CAS 失败分支（409 零副作用）。
    await route.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({
        detail: 'budget version 过期：expected_version=1，当前 version=2（CAS 比较失败，未启动任何工作）',
      }),
    });
  });

  await openPausedSession(page);
  const panel = page.locator('.pause-panel');
  await expect(panel).toBeVisible({ timeout: 15_000 });
  await expect.poll(() => eventsCalls).toBeGreaterThanOrEqual(1);

  await panel.getByLabel(/恢复用的绝对 ceiling/).fill('9999');
  await panel.getByRole('button', { name: '恢复同一 run' }).click();

  // wire 级：同 run CAS + budget_increase + 抬高的绝对 ceiling（字段名由触发维度导出）。
  await expect.poll(() => resumeBody).toEqual({
    run_id: RUN,
    resume_basis: 'budget_increase',
    budget: { expected_version: 1, run: { max_agent_turns_total: 9999 } },
  });
  expect(resumeUrl).toMatch(new RegExp(`/api/sessions/${SID}/resume$`));

  // 拒绝面：后端 detail 逐字上屏（前缀标明"被拒绝"而非"失败"）。
  await expect(page.locator('.app-error').first()).toHaveText(
    '恢复被拒绝：budget version 过期：expected_version=1，当前 version=2（CAS 比较失败，未启动任何工作）',
  );
  // 409 零副作用：面板仍在、用户草稿不丢（改大再来一次即可）。
  await expect(panel).toBeVisible();
  await expect(panel.getByLabel(/恢复用的绝对 ceiling/)).toHaveValue('9999');
  // 权威状态已重读（首屏 1 次 + 被拒后 1 次）。
  await expect.poll(() => eventsCalls).toBeGreaterThanOrEqual(2);
});
