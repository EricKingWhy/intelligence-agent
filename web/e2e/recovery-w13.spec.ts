/** #357 W-13 UI 层端到端（真实浏览器 + 固定夹具，Playwright）。
 *
 *  `#1327` 起裁决面板已删：本文件原先覆盖的"409 → 裁决卡片 / 批量默认动作 /
 *  来源逐字进请求体"三条用例随 `RecoveryDecisionPanel` 一并删除（裁决改由后端
 *  按操作类型自动做）。保留的两条是仍存在的 `RecoveryListPanel`：
 *  恢复列表四要素展示 + 缺失进度文件版本诚实标注「未知」+「先列后继续」
 *  （加载完成前「继续」disabled）。
 *
 *  夹具：`routeApi` 拦截 API（`GET /api/recovery/interrupted` 由 `recoveryInterrupted`
 *  驱动）；帧形状与后端 contract 一致。 */

import { expect, test, type Page } from '@playwright/test';
import { RUN, SID, T, clickTopBarMenuItem, routeApi, type ApiMock, type FrameSpec } from './fixtures';

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
  // #1396：恢复列表入口已收进顶栏「更多工具」菜单（`menuitem`，不是常驻 `button`）。
  await clickTopBarMenuItem(page, '恢复列表');

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
  // #1396：恢复列表入口已收进顶栏「更多工具」菜单（`menuitem`，不是常驻 `button`）。
  await clickTopBarMenuItem(page, '恢复列表');
  await page.locator('.recovery-list-item').first().getByRole('button', { name: '继续' }).click();
  await expect(page.locator('.recovery-list-panel')).toHaveCount(0);
});
