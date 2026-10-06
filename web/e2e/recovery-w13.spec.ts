/** #357 W-13 UI 层端到端（真实浏览器 + 固定夹具，Playwright）。
 *
 *  覆盖修订 A §9.4 的四件事：
 *  ① 409 → 裁决卡片，每卡默认选中最安全项（= 后端 default_action）；
 *  ② 「全部按默认安全动作处理」一键回到各自默认项，提交载荷取默认 verdict；
 *  ③ 「当作已生效」采集的来源**逐字**进请求体；
 *  ④ 恢复列表四要素展示 + 缺失进度文件版本诚实标注「未知」+「先列后继续」
 *     （加载完成前「继续」disabled）。
 *
 *  夹具：`routeApi` 拦截 API（`GET /api/recovery/interrupted` 由 `recoveryInterrupted`
 *  驱动）；帧形状与后端 contract 一致。 */

import { expect, test, type Page, type Route } from '@playwright/test';
import { RUN, SID, T, routeApi, type ApiMock, type FrameSpec } from './fixtures';

const ROW = {
  session_id: SID,
  event_count: 5,
  first_event_time: T,
  last_event_time: T,
  first_user_message: '崩溃前的问题',
  trace_id: null,
  trace_url: null,
};

/** 崩溃会话：run 无终态 + 一条未配对 tool/call → isRecoverableRun 为真。 */
function crashedEvents(): FrameSpec[] {
  return [
    { type: 'session/started', seq: 1, session_id: SID, time: T },
    { type: 'user/message', data: { content: '崩溃前的问题' }, seq: 2, session_id: SID, time: T },
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

/** 修复后的事件流（带 decisions 的 recover 返回它）。 */
function repairedEvents(): FrameSpec[] {
  return [
    ...crashedEvents(),
    {
      type: 'tool/result',
      data: { tool_call_id: 'call-1', content: JSON.stringify({ ok: true, message: 'done', data: {} }) },
      seq: 6,
      session_id: SID,
      step_id: 1,
      run_id: RUN,
      time: T,
    },
    { type: 'run/interrupted', data: { reason: 'process_restart' }, seq: 7, session_id: SID, step_id: 1, run_id: RUN, time: T },
  ];
}

/** 后端 409 的结构化 `pending_decisions`（形状 = `session/service.py::_reconcile_pending`
 *  的富化输出；两条覆盖 RETRY-low 与 DEFER-high 两种默认）。 */
const PENDING = [
  {
    tool_call_id: 'call-1',
    tool_name: 'git_status',
    state: 'NEED_RECONCILE',
    default_action: 'RETRY',
    risk_level: 'low',
    probe: { verifiable: true, suggested_action: '重新运行 git status，核对工作区状态。' },
  },
  {
    tool_call_id: 'call-2',
    tool_name: 'bash',
    state: 'UNKNOWN',
    default_action: 'DEFER',
    risk_level: 'high',
    probe: { verifiable: false, suggested_action: null },
  },
];

/** 打开崩溃会话并触发一次 recover → 结构化 409 → 裁决卡片出现。 */
async function openDecisionPanel(
  page: Page,
  onSubmitted?: (body: unknown) => void,
): Promise<void> {
  const mock: ApiMock = {
    sessions: [ROW],
    events: crashedEvents(),
    onRecoverPost: async (route: Route) => {
      let body: { decisions?: unknown } | null = null;
      try {
        body = route.request().postDataJSON() as { decisions?: unknown } | null;
      } catch {
        body = null; // 首次 recover 无 body（api 在无 decisions 时不发 body）
      }
      if (body && body.decisions) {
        onSubmitted?.(body);
        await route.fulfill({ status: 200, body: JSON.stringify(repairedEvents()), contentType: 'application/json' });
        return;
      }
      await route.fulfill({
        status: 409,
        body: JSON.stringify({
          detail: { message: '存在 UNKNOWN 状态的高风险工具操作，需人工裁决', pending_decisions: PENDING },
        }),
        contentType: 'application/json',
      });
    },
  };
  await routeApi(page, mock);
  await page.goto('/');
  await page.locator('.session-item').first().click();
  await page.locator('.recover-btn').click();
  await expect(page.locator('.recovery-decision-panel')).toBeVisible();
}

test('409 裁决卡片：后果语言标题、每卡默认选中最安全项', async ({ page }) => {
  await openDecisionPanel(page);
  const title = page.locator('.recovery-decision-title');
  await expect(title).toContainText('结果不确定');
  await expect(title).not.toContainText('tool_call_id');

  const cards = page.locator('.recovery-card');
  await expect(cards).toHaveCount(2);
  // call-1：replay_safe 工具 → 默认「安全重做」
  await expect(cards.nth(0).getByRole('radio', { name: /当作没生效，安全重做/ })).toBeChecked();
  await expect(cards.nth(0).getByRole('radio', { name: /当作已生效/ })).not.toBeChecked();
  // call-2：unsafe → 默认「先跳过」
  await expect(cards.nth(1).getByRole('radio', { name: /先跳过，稍后再说/ })).toBeChecked();
  await expect(cards.nth(1).getByRole('radio', { name: /当作没生效/ })).not.toBeChecked();
});

test('批量「全部按默认安全动作处理」：各卡回到默认项，提交载荷取默认 verdict', async ({ page }) => {
  let submitted: { decisions?: { tool_call_id: string; verdict: string; source?: string }[] } | null = null;
  await openDecisionPanel(page, (body) => { submitted = body as typeof submitted; });

  // 先手动改一卡，再点批量 → 应回到默认
  const firstCard = page.locator('.recovery-card').nth(0);
  await firstCard.getByRole('radio', { name: /当作已生效/ }).check();
  await page.locator('.recovery-batch-btn').click();
  await expect(firstCard.getByRole('radio', { name: /当作没生效/ })).toBeChecked();

  await page.locator('.recovery-submit-btn').click();
  await expect(page.locator('.recovery-decision-panel')).toHaveCount(0);
  expect(submitted!.decisions).toEqual([
    { tool_call_id: 'call-1', verdict: 'RETRY' },
    { tool_call_id: 'call-2', verdict: 'DEFER' },
  ]);
});

test('「当作已生效」的来源逐字进请求体', async ({ page }) => {
  let submitted: { decisions?: { tool_call_id: string; verdict: string; source?: string }[] } | null = null;
  await openDecisionPanel(page, (body) => { submitted = body as typeof submitted; });

  const firstCard = page.locator('.recovery-card').nth(0);
  await firstCard.getByRole('radio', { name: /当作已生效/ }).check();
  const sourceBox = firstCard.locator('.recovery-source');
  await expect(sourceBox).toBeVisible();
  await sourceBox.locator('.recovery-source-choice').selectOption('我查了外部系统');

  await page.locator('.recovery-submit-btn').click();
  await expect(page.locator('.recovery-decision-panel')).toHaveCount(0);
  const first = submitted!.decisions!.find((d) => d.tool_call_id === 'call-1')!;
  expect(first.verdict).toBe('CONFIRM_SUCCESS');
  expect(first.source).toBe('我查了外部系统');
});

test('恢复列表：先列后继续（加载完成前「继续」disabled），完成后展示四要素', async ({ page }) => {
  await routeApi(page, {
    sessions: [ROW],
    events: crashedEvents(),
    recoveryInterruptedDelayMs: 1200,
    recoveryInterrupted: {
      snapshot_available: true,
      items: [
        {
          session_id: SID,
          recovery: 'needs_manual_reconcile',
          detail: '存在 UNKNOWN 操作',
          interrupted_runs: [{ run_id: 'run-7', interrupted_seq: 12, step_id: 3, agent_id: null }],
          resume_available: true,
          task: '修好登录页的对比度',
          workspace_root: '/home/me/proj',
          progress: { schema_version: '2', source_event_seq: 128 },
        },
        {
          session_id: 'sess-2',
          recovery: 'recovered',
          detail: null,
          interrupted_runs: [{ run_id: null, interrupted_seq: 4, step_id: null, agent_id: 'agent-x' }],
          resume_available: false,
          task: null,
          workspace_root: null,
          progress: { status: 'missing', reason: 'no progress file' },
        },
      ],
    },
  });
  await page.goto('/');
  await page.getByRole('button', { name: '恢复列表' }).click();

  // ① 加载窗口内：只有 disabled 的占位「继续」+ 原因
  const loadingButton = page.locator('.recovery-list-loading .recovery-resume-btn');
  await expect(loadingButton).toBeDisabled();
  await expect(page.locator('.recovery-list-loading .recovery-gate-reason')).toContainText('加载完成前');
  await expect(page.locator('.recovery-list-item')).toHaveCount(0);

  // ② 加载完成：四要素 + 可点「继续」
  const items = page.locator('.recovery-list-item');
  await expect(items).toHaveCount(2);
  await expect(items.nth(0)).toContainText('修好登录页的对比度');
  await expect(items.nth(0)).toContainText('run-7');
  await expect(items.nth(0)).toContainText('/home/me/proj');
  await expect(items.nth(0)).toContainText('128');
  await expect(items.nth(0).getByRole('button', { name: '继续' })).toBeEnabled();

  // ③ 缺失进度文件版本如实标「未知」，不写「最新」
  await expect(items.nth(1)).toContainText('未知');
  await expect(items.nth(1)).not.toContainText('最新');
  await expect(items.nth(1).getByRole('button', { name: '继续' })).toBeDisabled();
});

test('恢复列表：点「继续」打开该会话并关闭列表', async ({ page }) => {
  await routeApi(page, {
    sessions: [ROW],
    events: crashedEvents(),
    recoveryInterrupted: {
      snapshot_available: true,
      items: [
        {
          session_id: SID,
          recovery: 'needs_manual_reconcile',
          detail: null,
          interrupted_runs: [{ run_id: RUN, interrupted_seq: 3, step_id: null, agent_id: null }],
          resume_available: true,
          task: '崩溃前的问题',
          workspace_root: '/home/me/proj',
          progress: { schema_version: '1', source_event_seq: 3 },
        },
      ],
    },
  });
  await page.goto('/');
  await page.getByRole('button', { name: '恢复列表' }).click();
  await page.locator('.recovery-list-item').first().getByRole('button', { name: '继续' }).click();
  await expect(page.locator('.recovery-list-panel')).toHaveCount(0);
});
