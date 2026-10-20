/** #1195 — Composer 内联 `/` 命令菜单 e2e（真实浏览器键盘/请求断言）。
 *
 * 车道归属：Playwright e2e（同 i-keyboard.spec.ts / context-usage.spec.ts 约定）。
 *
 * 断言纪律（同 i-keyboard §注释）：断言**发出去的请求及其载荷**，不断言 UI 文字
 * 回显——会话行渲染复用夹具 first_user_message，无从区分发没发。命令 → API 的
 * 映射（/plan → workflow-mode、/goal → goal、/compact → context/compact）
 * 是本票对后端契约的唯一集成断言面。
 *
 * mock 策略：routeApi 铺底（未知路径 404），slash 端点用独立 page.route **叠加**
 * （后注册者先匹配，未命中 route.fallback() 放行——mockTaskReviewApi 同范式）。
 * fixtures 对这三个端点本无 mock（routeApi 未知路径一律 404），叠加是唯一通路。
 */

import { expect, test, type Page, type Route } from '@playwright/test';
import { routeApi, sessionRow, type FrameSpec } from './fixtures';
import type { GoalCommandResult } from '../src/lib/api';

const SID = 'slash-session-1';

const IDLE_FRAMES: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: 'slash-run-1', time: '2026-10-16T00:00:00Z' },
  { type: 'run/started', seq: 2, session_id: SID, run_id: 'slash-run-1', time: '2026-10-16T00:00:00Z' },
  { type: 'user/message', data: { content: '斜杠命令' }, seq: 3, session_id: SID, run_id: 'slash-run-1', step_id: 1, time: '2026-10-16T00:00:00Z' },
  { type: 'model/completed', data: { content: '好。' }, seq: 4, session_id: SID, run_id: 'slash-run-1', step_id: 1, time: '2026-10-16T00:00:01Z' },
  { type: 'run/completed', data: {}, seq: 5, session_id: SID, run_id: 'slash-run-1', time: '2026-10-16T00:00:02Z' },
];

/** 记录命令端点收到的载荷（断言请求体用）。 */
interface SlashCalls {
  workflowMode: Array<Record<string, unknown>>;
  taskDefinition: Array<Record<string, unknown>>;
  compact: number;
}

/** 叠加 slash 三端点 mock（routeApi 之后注册；其余 fallback 给 routeApi）。 */
async function mockSlashEndpoints(page: Page, calls: SlashCalls, events: FrameSpec[] = IDLE_FRAMES): Promise<void> {
  const jsonFulfill = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, body: JSON.stringify(body), contentType: 'application/json' });
  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();
    if (path === `/api/sessions/${SID}/workflow-mode` && method === 'POST') {
      calls.workflowMode.push((req.postDataJSON() ?? {}) as Record<string, unknown>);
      // 回执形状 = 真后端 `{id, mode}`（tests/web/test_526_approvals_workflow_api.py）
      const mode = (calls.workflowMode[calls.workflowMode.length - 1] as { mode?: string }).mode ?? 'normal';
      return jsonFulfill(route, { id: SID, mode });
    }
    if (path === `/api/sessions/${SID}/task/definition` && method === 'POST') {
      calls.taskDefinition.push((req.postDataJSON() ?? {}) as Record<string, unknown>);
      return jsonFulfill(route, { status: 'applied' });
    }
    if (path === `/api/sessions/${SID}/context/compact` && method === 'POST') {
      calls.compact += 1;
      // 低水位零写入仍 200（黑盒契约：bracket_id=null / compacted_turn_count=0）
      return jsonFulfill(route, {
        bracket_id: null, source_seq_start: null, source_seq_end: null,
        tokens_before: 0, tokens_after: 0, compacted_turn_count: 0, summary_model: null,
      });
    }
    if (path === `/api/sessions/${SID}/task` && method === 'GET') {
      // 任务交付投影仍走 /task；#1359 的 /goal 使用独立的持久目标接口。
      return jsonFulfill(route, {
        task: {
          defined: false, task_text: null, read_write_intent: null, cwd: null,
          authorization: null, criteria: [], verification: {}, acceptance: null,
          version: 0, open_run_ids: [], product_state: '',
        },
      });
    }
    if (path === `/api/sessions/${SID}/events` && method === 'GET') {
      // /plan 的 mode 投影读事件流——routeApi 的 mock.events 在这里原样回
      return jsonFulfill(route, events);
    }
    return route.fallback();
  });
}

/** 前置：打开空闲会话（轨道行点击进入，run 已收尾 → composer 可用）。 */
async function openIdleSession(page: Page): Promise<void> {
  await page.goto('/');
  await page.locator('.session-item').first().click();
  await expect(page.getByLabel('Agent 任务')).toBeEnabled({ timeout: 5000 });
}

test('触发与渲染：`/` 出菜单 5 条齐全；全角不触发；未知命令给无匹配提示', async ({ page }) => {
  const calls: SlashCalls = { workflowMode: [], taskDefinition: [], compact: 0 };
  await routeApi(page, {
    sessions: [sessionRow(SID)],
    events: IDLE_FRAMES,
  });
  await mockSlashEndpoints(page, calls);
  await openIdleSession(page);

  // 行首 `/` → 菜单开，5 条命令齐全（分组头 添加/指令 由单测锁，这里锁总数与可见）
  await page.getByLabel('Agent 任务').fill('/');
  const menu = page.getByTestId('slash-menu');
  await expect(menu).toBeVisible();
  for (const name of ['goal', 'plan', 'compact', 'permission', 'model']) {
    await expect(menu.locator(`[data-slash-name="${name}"]`)).toBeVisible();
  }

  // A-P1-2 回归断言（jsdom 查不出，只有真浏览器算得出）：菜单 absolute bottom:100%
  // 必须以 .composer-wrap 为定位锚——wrap 缺 position:relative 时祖先
  // .workspace-panel（container-type: inline-size）接管 containing block，
  // 菜单几何位会跑到工作面板顶缘。
  expect(await page.locator('.composer-wrap').evaluate((el) => getComputedStyle(el).position)).toBe('relative');
  const menuBox = await menu.boundingBox();
  const composerBox = await page.getByLabel('Agent 任务').boundingBox();
  expect(menuBox).toBeTruthy();
  expect(composerBox).toBeTruthy();
  expect(menuBox!.y + menuBox!.height).toBeLessThanOrEqual(composerBox!.y + 1);

  // 全角 `／` 不触发（票面 §3.1：半角限定）
  await page.getByLabel('Agent 任务').fill('／');
  await expect(menu).toHaveCount(0);

  // 词中 `/` 不触发（正则要求行首或空格后）
  await page.getByLabel('Agent 任务').fill('看一看/目');
  await expect(menu).toHaveCount(0);

  // 无匹配 → 菜单仍开（可见提示，不静默消失）
  await page.getByLabel('Agent 任务').fill('/zzz');
  await expect(menu).toBeVisible();
  await expect(menu).toContainText('无匹配命令');
});

test('过滤与键盘：/pl 唯一命中回车执行；Esc 关菜单不清输入；↑↓ 移动高亮', async ({ page }) => {
  const calls: SlashCalls = { workflowMode: [], taskDefinition: [], compact: 0 };
  await routeApi(page, { sessions: [sessionRow(SID)], events: IDLE_FRAMES });
  await mockSlashEndpoints(page, calls);
  await openIdleSession(page);

  const composer = page.getByLabel('Agent 任务');
  const menu = page.getByTestId('slash-menu');

  // `/pl` fuzzy 过滤 → 唯一命中「计划」（中文 label 可搜——单测锁打分，这里锁端到端）
  await composer.fill('/pl');
  await expect(menu.locator('[data-slash-name]')).toHaveCount(1);
  await expect(menu.locator('[data-slash-name="plan"]')).toBeVisible();

  // ↑↓ 在唯一命中上循环移动（不报错即可；多选循环由单测锁）
  await composer.press('ArrowDown');
  await composer.press('ArrowUp');
  await expect(menu).toBeVisible();

  // Esc：菜单关、输入不清（票面 §4.1）
  await composer.press('Escape');
  await expect(menu).toHaveCount(0);
  await expect(composer).toHaveValue('/pl');
});

test('/plan：事件流无 mode 事件 → POST workflow-mode {mode:"plan"}', async ({ page }) => {
  const calls: SlashCalls = { workflowMode: [], taskDefinition: [], compact: 0 };
  await routeApi(page, { sessions: [sessionRow(SID)], events: IDLE_FRAMES });
  await mockSlashEndpoints(page, calls);
  await openIdleSession(page);

  // waitForResponse 而非 waitForRequest：request 事件先于 route handler 跑完，
  // `calls` 数组的 push 可能还没发生（实测竞态）——response 返回 ⇒ handler 已执行。
  const workflowPost = page.waitForResponse(
    (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/sessions/${SID}/workflow-mode`,
  );
  await page.getByLabel('Agent 任务').fill('/plan');
  await page.getByLabel('Agent 任务').press('Enter');
  // 没发出这条请求 ⇒ await 超时失败（本用例锁的就是命令→端点映射）
  const res = await workflowPost;
  expect(res.request().postDataJSON()).toEqual({ mode: 'plan' });
  expect(calls.workflowMode).toHaveLength(1);
});

test('/plan toggle：事件流已处 plan → POST {mode:"normal"}（last-wins 投影）', async ({ page }) => {
  // 预置 mode-changed(plan) 于事件流尾（last-wins）→ /plan 应回切 normal
  const withMode: FrameSpec[] = [
    ...IDLE_FRAMES,
    { type: 'workflow/mode-changed', data: { mode: 'plan' }, seq: 6, session_id: SID, run_id: null, time: '2026-10-16T00:00:03Z' },
  ];
  const calls: SlashCalls = { workflowMode: [], taskDefinition: [], compact: 0 };
  await routeApi(page, { sessions: [sessionRow(SID)], events: withMode });
  await mockSlashEndpoints(page, calls, withMode);
  await openIdleSession(page);

  const workflowPost = page.waitForResponse(
    (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/sessions/${SID}/workflow-mode`,
  );
  await page.getByLabel('Agent 任务').fill('/plan');
  await page.getByLabel('Agent 任务').press('Enter');
  const res = await workflowPost;
  // toggle 语义：前端不记布尔值，mode 从事件流投影派生（票面 §4.2 硬性规定）
  expect(res.request().postDataJSON()).toEqual({ mode: 'normal' });
});

test('/goal 带参：POST goal {operation, objective}，不当聊天发给 agent', async ({ page }) => {
  const calls: SlashCalls = { workflowMode: [], taskDefinition: [], compact: 0 };
  await routeApi(page, { sessions: [sessionRow(SID)], events: IDLE_FRAMES });
  await mockSlashEndpoints(page, calls);
  let goalPosts = 0;
  await page.route(`**/api/sessions/${SID}/goal`, async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    goalPosts += 1;
    const body = route.request().postDataJSON() as { objective: string };
    const response: GoalCommandResult = {
      status: 'applied',
      activation: 'armed',
      goal: {
        defined: true, goal_id: 'slash-goal-1', revision: 1,
        objective: body.objective, phase: 'active',
        blocked_reason: null, pause_reason: null,
        max_goal_rounds: 32, rounds_started: 0, needs_interview: true,
      },
    };
    return route.fulfill({
      status: 200, body: JSON.stringify(response), contentType: 'application/json',
    });
  });
  await openIdleSession(page);

  const definePost = page.waitForResponse(
    (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/sessions/${SID}/goal`,
  );
  // #1359：多词参数整体作为持久目标 objective，由 create 命令提交。
  await page.getByLabel('Agent 任务').fill('/goal 修复登录超时并补回归测试');
  await page.getByLabel('Agent 任务').press('Enter');
  const res = await definePost;
  expect(res.request().postDataJSON()).toEqual({
    operation: 'create', objective: '修复登录超时并补回归测试',
  });
  expect(goalPosts).toBe(1);
  expect(calls.taskDefinition).toHaveLength(0);
  // 拦截语义：这条输入绝不能误触其他命令端点（不当聊天也不串命令）
  expect(calls.workflowMode).toHaveLength(0);
  expect(calls.compact).toBe(0);
});

test('/compact：POST context/compact（复用既有压缩端点）；被拒走中性信息面（#1197 三态）', async ({ page }) => {
  const calls: SlashCalls = { workflowMode: [], taskDefinition: [], compact: 0 };
  await routeApi(page, { sessions: [sessionRow(SID)], events: IDLE_FRAMES });
  await mockSlashEndpoints(page, calls);
  await openIdleSession(page);

  const compactPost = page.waitForResponse(
    (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/sessions/${SID}/context/compact`,
  );
  await page.getByLabel('Agent 任务').fill('/compact');
  await page.getByLabel('Agent 任务').press('Enter');
  await compactPost;
  expect(calls.compact).toBe(1);
  expect(calls.workflowMode).toHaveLength(0);
  // mock 回执 compacted_turn_count=0（零写入被拒）⇒ #1197 三态契约的中性信息面，
  // 不是红色错误面（A-P1-3）。
  await expect(page.getByTestId('slash-info')).toHaveText('没有可压缩的早期内容');
  await expect(page.getByTestId('slash-error')).toHaveCount(0);
});
