/** 场景：#1190 / #1176 Phase 1 —— Inspector「约束管理」页（受保护事实投影 + 撤销）。
 *
 *  锁四件事：
 *  - **只读投影如实**：状态 pill-tabs 默认「生效中」，列表默认只显示 active（revoked /
 *    superseded 归「已撤销」页）；类型下拉按 type 过滤——筛选纯展示层，数据来自后端；
 *  - **撤销走二次确认**：点「撤销」先弹 Linear 风确认框，确认后**重拉对齐**（不乐观本地
 *    更新，不变量 #22）——行翻成「已撤销」、按钮 disabled 且文案不再是「撤销」；
 *  - **空态说出来**：零约束时整页不消失，显式「没有符合条件的约束」；
 *  - **无权限如实**：403 → 「无权限查看约束」（不冒充空列表，不静默）。
 */

import { expect, test } from '@playwright/test';
import { RUN, SID, T, fulfillSse, routeApi, submitTask, type FrameSpec } from './fixtures';

/** 一轮正常跑完（无审批）——Inspector 有会话可投影、run 已终态不重连。 */
const HEAD: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '跑个任务' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'model/completed', data: { text: '好了' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 5, session_id: SID, run_id: RUN, time: T },
];

type FactRow = {
  fact_id: string;
  type: string;
  value: unknown;
  status: 'active' | 'revoked' | 'superseded';
  source_event_id: string;
  source_seq: number;
  registered_at: string | null;
  revoked_at: string | null;
  revoked_by: string | null;
};

const ACTIVE_FACT: FactRow = {
  fact_id: 'f-active',
  type: 'constraint',
  value: '不要改动生产环境配置',
  status: 'active',
  source_event_id: 'ev-3',
  source_seq: 3,
  registered_at: T,
  revoked_at: null,
  revoked_by: null,
};

const REVOKED_FACT: FactRow = {
  fact_id: 'f-revoked',
  type: 'user_goal',
  value: '先修好登录页对比度',
  status: 'revoked',
  source_event_id: 'ev-4',
  source_seq: 4,
  registered_at: T,
  revoked_at: T,
  revoked_by: 'local',
};

const SUPERSEDED_FACT: FactRow = {
  fact_id: 'f-superseded',
  type: 'authorization',
  value: '允许写 workspace',
  status: 'superseded',
  source_event_id: 'ev-5',
  source_seq: 5,
  registered_at: T,
  revoked_at: null,
  revoked_by: null,
};

const panel = (page: import('@playwright/test').Page) => page.getByTestId('constraints-panel');

async function openConstraints(
  page: import('@playwright/test').Page,
  mock: Record<string, unknown>,
): Promise<void> {
  await routeApi(page, {
    onSessionPost: (route) => fulfillSse(route, HEAD),
    events: HEAD,
    ...mock,
  });
  await page.goto('/');
  await submitTask(page, '跑个任务');
  await page
    .getByRole('tablist', { name: '运行详情视图' })
    .getByRole('tab', { name: '我的要求' })
    .click();
  await expect(panel(page)).toBeVisible();
}

test('AC1：页眉/会话行在场，默认「生效中」只列 active', async ({ page }) => {
  await openConstraints(page, {
    protectedFacts: [ACTIVE_FACT, REVOKED_FACT, SUPERSEDED_FACT],
  });

  await expect(panel(page).locator('.cn-eyebrow')).toHaveText('我的要求');
  await expect(panel(page).locator('.cn-title')).toHaveText('我的要求');

  // 默认 tab = 生效中（issue §四.3）。
  await expect(panel(page).getByRole('tab', { name: /生效中/ })).toHaveAttribute(
    'aria-selected',
    'true',
  );

  // 默认只显示 active：值在场，撤销/取代两条不在。
  await expect(panel(page)).toContainText('不要改动生产环境配置');
  await expect(panel(page)).not.toContainText('先修好登录页对比度');
  await expect(panel(page)).not.toContainText('允许写 workspace');

  // 切到「已撤销」→ revoked + superseded 两条都在，active 退场。
  await panel(page).getByRole('tab', { name: /已撤销/ }).click();
  await expect(panel(page)).toContainText('先修好登录页对比度');
  await expect(panel(page)).toContainText('允许写 workspace');
  await expect(panel(page)).not.toContainText('不要改动生产环境配置');
});

test('AC2：类型 chip 按 type 过滤（纯展示层，不重拉）', async ({ page }) => {
  const second: FactRow = { ...ACTIVE_FACT, fact_id: 'f-active-2', type: 'evidence_ref', value: '证据：pytest 全绿' };
  await openConstraints(page, { protectedFacts: [ACTIVE_FACT, second] });

  await expect(panel(page)).toContainText('不要改动生产环境配置');
  await expect(panel(page)).toContainText('证据：pytest 全绿');

  // #1419 R-2：类型筛选由原生 `<select>` 换成 chip 组（`aria-pressed` 表达选中）。
  // #1445：chip 的**显示名**是中文类型名；原始枚举只作筛选值，不再出现在可访问名里。
  await panel(page)
    .getByRole('group', { name: '按类型筛选' })
    .getByRole('button', { name: '证据引用' })
    .click();
  await expect(panel(page)).toContainText('证据：pytest 全绿');
  await expect(panel(page)).not.toContainText('不要改动生产环境配置');
});

test('AC3：撤销走二次确认 → 重拉对齐（行变已撤销、按钮 disabled）', async ({ page }) => {
  // 计 POST 次数用 `page.on('request')`，**不**注入 `onProtectedFactRevokePost`——那个
  // 拦截口会顶掉夹具的缺省处理，而后者的语义才是本用例要考的（撤销真的翻行状态、
  // 后续 GET 读到新状态）。
  let revokeCalls = 0;
  page.on('request', (req) => {
    if (
      req.method() === 'POST' &&
      /\/api\/sessions\/[^/]+\/protected-facts\/[^/]+\/revoke$/.test(new URL(req.url()).pathname)
    ) {
      revokeCalls += 1;
    }
  });
  await openConstraints(page, { protectedFacts: [ACTIVE_FACT] });

  await panel(page).getByRole('button', { name: '撤销' }).click();

  // 二次确认框（Linear 风）：标题 + 事实值 + 事实 id。
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('撤销这条要求？');
  await expect(dialog).toContainText('不要改动生产环境配置');
  await expect(dialog).toContainText('f-active');

  await dialog.getByRole('button', { name: '确认撤销' }).click();

  // #1190 AC：撤销后重拉对齐 —— 真相在后端，前端不做乐观本地更新。
  // mock 的 revoke 真的翻行状态（见 fixtures `protectedFactsFor`），GET 重拉读到
  // 已撤销 → 默认「生效中」页里这条消失（不是本地隐藏，是后端说它没了）。
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(panel(page)).toContainText('没有符合条件的要求');
  // 计数断言放在 UI 落定**之后**，避免点击后同步读计数的轻抖。
  expect(revokeCalls).toBe(1);

  // 「已撤销」页能看到它 + 撤销按钮 disabled（终态不可再撤销）。
  await panel(page).getByRole('tab', { name: /已撤销/ }).click();
  await expect(panel(page)).toContainText('不要改动生产环境配置');
  // AC3：元信息行可见撤销时间 · 操作人（fixtures revoke 写 revoked_by='local'）。
  await expect(panel(page)).toContainText('本人操作');
  await expect(panel(page).getByRole('button', { name: '已撤销' })).toBeDisabled();
});

test('AC4：零约束 → 空态说出「没有符合条件的约束」（整页不消失）', async ({ page }) => {
  await openConstraints(page, { protectedFacts: [] });
  await expect(panel(page)).toContainText('我的要求');
  await expect(panel(page)).toContainText('没有符合条件的要求');
});

test('AC5：403 → 「无权限查看约束」（不冒充空列表）', async ({ page }) => {
  await openConstraints(page, {
    onEffectiveConstraintsGet: async (route: import('@playwright/test').Route) => {
      await route.fulfill({
        status: 403,
        contentType: 'application/json',
        body: JSON.stringify({ detail: '非本机来源' }),
      });
      return true;
    },
  });
  await expect(panel(page)).toContainText('无权限查看要求');
  await expect(panel(page)).not.toContainText('没有符合条件的要求');
});
