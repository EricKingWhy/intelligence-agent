/** 场景：#184 Inspector PERMISSION 段（权限档 / 待审批 / 裁决结果）。
 *
 *  锁三件事：
 *  - **权限档如实**：唯一带运行时证据的来源是审批请求里的 `policy`；没有审批事件时
 *    显示 `—` 并说明原因（AC4：拿不到就说拿不到，不填 0、不拿 composer 的选择冒充）；
 *  - **"没有"要说出来**（AC2）：零待审批显示「无待审批」、零裁决显示「尚无裁决」，
 *    整段不消失——段消失会被读成"这个会话没有权限概念"；
 *  - **能进审批面**（AC5）：待决审批的审批面在场。#421 起首个**非失效**待决审批
 *    由内联位移进常驻模态（模态打开时整个应用 inert，Inspector 不可点）——原
 *    「点待审批行 → 脉冲高亮」对模态候选是设计内 no-op 降级（审批面已在眼前，
 *    见 Conversation.tsx 该段注释），审批面断言相应改为模态常驻可见。
 */

import { expect, test } from '@playwright/test';
import { RUN, SID, T, fulfillSse, routeApi, submitTask, type FrameSpec } from './fixtures';

/** 与后端 `session/approval.py:56-72` 同形状的审批请求帧。 */
function approvalRequested(approvalId: string, seq: number, policy = 'read-only'): FrameSpec {
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
      policy,
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

/** PERMISSION 段在 Inspector 的 Overview（= `chat`）tab 里，默认 tab 是 Timeline。 */
const permissionSection = (page: import('@playwright/test').Page) =>
  page.locator('[data-section="permission"]');

async function openInspectorOverview(page: import('@playwright/test').Page, frames: FrameSpec[]) {
  await routeApi(page, { onSessionPost: (route) => fulfillSse(route, frames), events: frames });
  await page.goto('/');
  await submitTask(page, '写个文件');
  await page
    .getByRole('tablist', { name: 'Inspector 视图' })
    .getByRole('tab', { name: 'Overview' })
    .click();
  await expect(permissionSection(page)).toBeVisible();
}

test('AC1/AC5：有待审批 → 段内出现权限档 + 待审批行；审批面（模态）常驻可见', async ({
  page,
}) => {
  // #421 后审批一到位模态就开、整个应用 inert——「先提交任务再点 Overview」不再
  // 可行。改为把审批事件挪到**第二轮**（经 /messages 续聊端点送入）：第一轮先正常
  // 跑完（无审批）→ 开 Overview → 续聊把审批送进来。「先开着 Inspector 遇上审批」
  // 是真实用户路径，权限区块的信息断言（AC1）一条不少。
  const firstRun: FrameSpec[] = [
    ...HEAD,
    { type: 'model/completed', data: { text: '好了' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'run/completed', data: {}, seq: 5, session_id: SID, run_id: RUN, time: T },
  ];
  const secondRun: FrameSpec[] = [
    { type: 'run/started', seq: 6, session_id: SID, run_id: 'e2e-run-0002', time: T },
    {
      type: 'user/message',
      data: { content: '再写一个' },
      seq: 7,
      session_id: SID,
      run_id: 'e2e-run-0002',
      step_id: 2,
      time: T,
    },
    { ...approvalRequested('ap-1', 8), run_id: 'e2e-run-0002', step_id: 2 },
  ];
  await routeApi(page, {
    // durable log 只放第一轮：run 1 终结后的 viewing 历史对账会重读 GET /events，
    // 若带上审批帧，审批会在 tab 点击之前就投影出模态、前置动作又被 inert 锁死。
    events: firstRun,
    onSessionPost: (route) => fulfillSse(route, firstRun),
    onMessagesPost: (route) => fulfillSse(route, secondRun),
    // 第二轮 SSE 无终态帧 → 客户端必然重连接流。剧本回放全量 + 声明在途 run、
    // 连接保持：若按缺省剧本答 has_active_run:false，重连链会耗尽额度 give-up 落
    // viewing 重读历史（无审批），模态在断言窗口内被撤掉——mock 必须与真后端
    // 「审批等待期 run 仍在途」的语义一致。
    onWs: () => ({ events: [...firstRun, ...secondRun], hasActiveRun: true, ending: 'keep' }),
  });
  await page.goto('/');
  await submitTask(page, '写个文件');
  // 审批尚未出现：先打开 Inspector Overview（模态一开就点不到了）。
  await page
    .getByRole('tablist', { name: 'Inspector 视图' })
    .getByRole('tab', { name: 'Overview' })
    .click();
  await expect(permissionSection(page)).toBeVisible();

  // 第二轮把审批送进来 → 模态接管审批面（#421）。
  await submitTask(page, '再写一个');
  const modalCard = page.locator('.approval-modal-content .approval-card');
  await expect(modalCard).toBeVisible();

  const section = permissionSection(page);
  // 权限档 = 审批请求携带的生效阈值（不是 composer 里"下次运行"的选择）。
  // 模态打开后 Inspector 处于 inert 背景，但段仍渲染在文档里——信息性断言
  // （文本 / 计数）不依赖可交互性，照常可断。
  await expect(section).toContainText('权限档');
  await expect(section).toContainText('read-only');
  await expect(section).toContainText('1 条');

  const row = section.locator('.detail-permission-row');
  await expect(row).toHaveCount(1);
  await expect(row).toContainText('write');
  await expect(row).toContainText('workspace-write');

  // AC5（#421 后的形态）：待决审批的「审批面」就是常驻模态——它在断言开始前
  // 已在眼前；行本身处于 inert 背景不可点，跳转对模态候选是设计内 no-op。
  await expect(modalCard.locator('.approval-title')).toHaveText('需要审批');
});

test('AC5 反向联动（#510 补 #441 移除的覆盖）：点待审批行 → 内联失效卡脉冲高亮', async ({
  page,
}) => {
  // #421 后首个**非失效**待决审批进常驻模态、模态打开时应用 inert ⇒ 跳转对模态候选
  // 是设计内 no-op，反向联动的可达对象只剩失效孤儿（同 Conversation.jumpPulse 单测
  // 的夹具口径）。让 ap-1 随 run 终结判失效（projection.markPendingApprovalsStale：
  // run 已终结仍未配对 ⇒ stale）——全失效 ⇒ 无模态 ⇒ 应用可交互，且内联卡挂着
  // data-approval-key（模态候选不挂，Conversation.tsx:438-441）。
  const frames: FrameSpec[] = [
    ...HEAD,
    approvalRequested('ap-1', 4),
    { type: 'run/completed', data: {}, seq: 5, session_id: SID, run_id: RUN, time: T },
  ];
  await openInspectorOverview(page, frames);

  const section = permissionSection(page);
  const row = section.locator('.detail-permission-row');
  await expect(row).toHaveCount(1);
  await expect(row).toContainText('已失效');

  // 脉冲落点是包住卡片的 [data-approval-key] 容器（旧用例 3a579242 同口径：类加在
  // 容器上而非 .approval-card）；断言 class 属性，不截屏比对。
  const target = page.locator('[data-approval-key="ap-1"]');
  await expect(target).toBeVisible();
  await row.click();
  await expect(target).toHaveClass(/stream-jump-pulse/);
  // 900ms 后由 timeout 摘除（Conversation.tsx:150）——类来过又走，装置非 vacuous。
  await expect(target).not.toHaveClass(/stream-jump-pulse/);
});

test('AC2/AC4：零审批的会话段不消失——权限档 `—` 并说明原因，两项"没有"都说出来', async ({
  page,
}) => {
  const frames: FrameSpec[] = [
    ...HEAD,
    { type: 'model/completed', data: { text: '好了' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'run/completed', data: {}, seq: 5, session_id: SID, run_id: RUN, time: T },
  ];
  await openInspectorOverview(page, frames);

  const section = permissionSection(page);
  await expect(section).toContainText('无待审批');
  await expect(section).toContainText('尚无裁决');
  // AC4：权限档拿不到 → `—`（**不是** 0、不是任何占位档位名），并说明为什么拿不到。
  // 按行取（`.detail-val-muted` 在段内还有"无待审批/尚无裁决"两处，不能整段模糊匹配）。
  await expect(section.locator('.detail-row', { hasText: '权限档' })).toContainText('—');
  await expect(section).toContainText('本会话无审批事件，生效阈值无从得知');
  // 零待审批 → 没有可点的行（不画假按钮）。
  await expect(section.locator('.detail-permission-row')).toHaveCount(0);
});

test('AC1：决议后 → 移出待审批并留痕到"已裁决"（工具名 + 人话裁决 + 理由）', async ({ page }) => {
  const frames: FrameSpec[] = [
    ...HEAD,
    approvalRequested('ap-1', 4),
    {
      type: 'permission/resolved',
      data: { approval_id: 'ap-1', decision: 'deny', reason: '用户拒绝' },
      seq: 5,
      session_id: SID,
      run_id: RUN,
      step_id: 1,
      time: T,
    },
  ];
  await openInspectorOverview(page, frames);

  const section = permissionSection(page);
  await expect(section).toContainText('无待审批');
  await expect(section).toContainText('1 条');
  const decision = section.locator('.detail-permission-decision');
  await expect(decision).toHaveCount(1);
  await expect(decision).toContainText('拒绝');
  await expect(decision).toContainText('write');
  await expect(decision).toContainText('用户拒绝');
  // 出队后权限档仍可得（从事件流读，不是从队列读）——这正是"决议即出队"的坑。
  await expect(section).toContainText('read-only');
});
