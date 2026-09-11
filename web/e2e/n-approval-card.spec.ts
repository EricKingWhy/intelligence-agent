/** 场景 N：交互式审批卡（#37, PRD §2.2）——「批准」「拒绝」两键的确定性点击回归锁。
 *
 * 可达性（此前登记为「产品不可达」，**已证伪**）：审批卡由 `tool/approval-requested`
 * 事件驱动（`projection.ts:514` → `pending_approvals` → `Conversation.tsx:348`），
 * 与 `auto_approve` 无关。真后端路径：显式选一个权限档位且非 danger-full-access
 * 时 `interactive=True`（`session/service.py:348`），policy=read-only 下任何
 * workspace-write 工具都会走 `_check_approval` 发该事件（`tooling/executor.py:659`）。
 * 真机证据见 `docs/FRONTEND_ISSUES_LOG.md` OBS-006 订正条（含 JSONL seq 与 decision）。
 *
 * 本 spec 用同一形状的 fixture 锁**前端契约**（事件 → 卡片 → POST /approve 请求体
 * → 决后 UI），与网络真实与否解耦，故可进标准门禁。
 *
 * OBS-015 回归锁：POST 500 → 卡片保持 pending + 按钮仍可用 + 出现错误提示；
 * POST 409 → 视为幂等成功，翻「已批准」。 */

import { expect, test } from '@playwright/test';
import { RUN, SID, T, routeApi, fulfillSse, submitTask, type FrameSpec } from './fixtures';

/** 一条与后端 `session/approval.py:56-72` 同形状的审批请求帧。 */
function approvalRequestedFrame(approvalId: string, seq: number): FrameSpec {
  return {
    type: 'tool/approval-requested',
    data: {
      approval_id: approvalId,
      tool_name: 'write',
      tool_call_id: 'tc-1',
      action_type: 'workspace-write',
      title: 'write (workspace-write)',
      description: '工具授权级别为 workspace-write，但当前策略为只读（read-only）。',
      arguments_preview: { path: 'demo.txt', content: 'HELLO' },
      permission: 'workspace-write',
      policy: 'read-only',
      reason: '工具授权级别为 workspace-write，但当前策略为只读（read-only）。',
      allowed_decisions: ['deny', 'approve_once'],
    },
    seq,
    session_id: SID,
    run_id: RUN,
    step_id: 1,
    time: T,
  };
}

const HEAD: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '写个文件' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
];

async function openCard(page: import('@playwright/test').Page, frames: FrameSpec[], sent: unknown[]) {
  routeApi(page, {
    onSessionPost: (route) => fulfillSse(route, frames),
    onApprovePost: async (route) => {
      // 同时记 URL 路径：只断请求体会漏掉「打到别的会话」的回归（本 spec 的 route 正则
      // 是 `[^/]+`，任何 id 都匹配，故必须显式断路径）。
      sent.push({
        path: new URL(route.request().url()).pathname,
        body: route.request().postDataJSON(),
      });
      await route.fulfill({
        status: 200,
        body: JSON.stringify({ status: 'resolved', approval_id: 'ap-1', decision: 'approve_once' }),
        contentType: 'application/json',
      });
    },
    events: frames,
  });
  await page.goto('/');
  await submitTask(page, '写个文件');
}

test('审批请求 → 卡片渲染工具名与参数预览', async ({ page }) => {
  const sent: unknown[] = [];
  await openCard(page, [...HEAD, approvalRequestedFrame('ap-1', 4)], sent);

  const card = page.locator('.approval-card');
  await expect(card).toBeVisible();
  await expect(card.locator('.approval-title')).toHaveText('需要审批');
  await expect(card.locator('code')).toHaveText('write'); // 工具名来自事件
  await expect(card.locator('.approval-args')).toContainText('demo.txt'); // 参数预览
  await expect(card.locator('.approval-approve')).toBeEnabled();
  await expect(card.locator('.approval-deny')).toBeEnabled();
});

test('点「批准」→ 已批准 + 按钮消失 + POST decision=approve_once', async ({ page }) => {
  const sent: unknown[] = [];
  await openCard(page, [...HEAD, approvalRequestedFrame('ap-1', 4)], sent);

  const card = page.locator('.approval-card');
  await expect(card).toBeVisible();
  await card.locator('.approval-approve').click();

  await expect(card.locator('.approval-title')).toHaveText('已批准');
  await expect(card.locator('.approval-actions')).toHaveCount(0);
  expect(sent).toEqual([
    { path: `/api/sessions/${SID}/approve`, body: { approval_id: 'ap-1', approved: true, decision: 'approve_once' } },
  ]);
});

test('点「拒绝」→ 已拒绝 + 按钮消失 + POST decision=deny', async ({ page }) => {
  const sent: unknown[] = [];
  await openCard(page, [...HEAD, approvalRequestedFrame('ap-1', 4)], sent);

  const card = page.locator('.approval-card');
  await expect(card).toBeVisible();
  await card.locator('.approval-deny').click();

  await expect(card.locator('.approval-title')).toHaveText('已拒绝');
  await expect(card.locator('.approval-actions')).toHaveCount(0);
  expect(sent).toEqual([
    { path: `/api/sessions/${SID}/approve`, body: { approval_id: 'ap-1', approved: false, decision: 'deny' } },
  ]);
});

test('permission/resolved 把卡片从待决队列移除（不渲染）', async ({ page }) => {
  const sent: unknown[] = [];
  const resolved: FrameSpec = {
    type: 'permission/resolved',
    data: { approval_id: 'ap-1', decision: 'approve_once', reason: '' },
    seq: 5,
    session_id: SID,
    run_id: RUN,
    step_id: 1,
    time: T,
  };
  await openCard(page, [...HEAD, approvalRequestedFrame('ap-1', 4), resolved], sent);

  await expect(page.locator('.approval-card')).toHaveCount(0);
  expect(sent).toEqual([]); // 无待决项 → 不该有决策请求
});

// ── OBS-015 回归锁：错误处理 ──────────────────────────────────────

/** POST /approve 返回 500 → 卡片保持 pending + 按钮仍可用 + 出现错误提示。
 *  这是 OBS-015 的核心断言：真失败时不能翻成「已批准」。 */
test('POST 500 → 卡片保持「需要审批」+ 按钮仍可用 + 出现错误提示', async ({ page }) => {
  const approveCalls: unknown[] = [];
  const frames = [...HEAD, approvalRequestedFrame('ap-1', 4)];
  routeApi(page, {
    onSessionPost: (route) => fulfillSse(route, frames),
    onApprovePost: async (route) => {
      approveCalls.push(route.request().postDataJSON());
      await route.fulfill({
        status: 500,
        body: JSON.stringify({ detail: 'Internal Server Error' }),
        contentType: 'application/json',
      });
    },
    events: frames,
  });
  await page.goto('/');
  await submitTask(page, '写个文件');

  const card = page.locator('.approval-card');
  await expect(card).toBeVisible();
  await expect(card.locator('.approval-title')).toHaveText('需要审批');

  // 点批准 → 500 → 保持 pending
  await card.locator('.approval-approve').click();
  await expect(card.locator('.approval-title')).toHaveText('需要审批');
  await expect(card.locator('.approval-error')).toBeVisible();
  await expect(card.locator('.approval-approve')).toBeEnabled();
  await expect(card.locator('.approval-deny')).toBeEnabled();
  expect(approveCalls).toHaveLength(1);

  // 重试：第二次点批准 → 仍然 500 → 仍然 pending
  await card.locator('.approval-approve').click();
  await expect(card.locator('.approval-title')).toHaveText('需要审批');
  await expect(card.locator('.approval-error')).toBeVisible();
  expect(approveCalls).toHaveLength(2);
});

/** POST /approve 返回 409 → 幂等成功，翻「已批准」。
 *  后端对同一 approval_id 的第二次决策返回 409。 */
test('POST 409 → 幂等成功，卡片翻「已批准」', async ({ page }) => {
  const approveCalls: unknown[] = [];
  const frames = [...HEAD, approvalRequestedFrame('ap-1', 4)];
  routeApi(page, {
    onSessionPost: (route) => fulfillSse(route, frames),
    onApprovePost: async (route) => {
      approveCalls.push(route.request().postDataJSON());
      await route.fulfill({
        status: 409,
        body: JSON.stringify({ detail: 'Approval already resolved' }),
        contentType: 'application/json',
      });
    },
    events: frames,
  });
  await page.goto('/');
  await submitTask(page, '写个文件');

  const card = page.locator('.approval-card');
  await expect(card).toBeVisible();

  // 点批准 → 409 → 幂等成功
  await card.locator('.approval-approve').click();
  await expect(card.locator('.approval-title')).toHaveText('已批准');
  await expect(card.locator('.approval-actions')).toHaveCount(0);
  expect(approveCalls).toHaveLength(1);
});
