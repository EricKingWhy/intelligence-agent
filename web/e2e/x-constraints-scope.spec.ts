/** 场景：#1191 / #1176 Phase 2 —— 约束作用域三层（登记三选一 + 面板分组）。
 *
 *  锁四件事：
 *  - **登记三选一**：勾选「将这条消息作为可复用规则」后出现 本会话/本项目/我的全局，
 *    默认「本会话」，选中项下方 hint 逐字（票面 §四.1/§四.2）；
 *  - **登记透传**：选「本项目」提交 → `/messages` 的 `protected_facts` 带 `scope:"project"`；
 *  - **面板三组**：SESSION/PROJECT/GLOBAL 分组 + 项目组 `git 根 · owner/repo` +
 *    被覆盖条目「被本会话覆盖」；非 git 不渲染 PROJECT 组（票面 §五）；
 *  - **非 git 置灰**：`projectAvailable=false` 时「本项目」不可选（票面 §七.4）。
 */

import { expect, test, type Page } from '@playwright/test';
import { RUN, SID, T, fulfillSse, routeApi, submitTask, type FrameSpec } from './fixtures';

const HEAD: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '跑个任务' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 5, session_id: SID, run_id: RUN, time: T },
];

const SECOND: FrameSpec[] = [
  { type: 'run/started', seq: 6, session_id: SID, run_id: `${RUN}-2`, time: T },
  { type: 'user/message', data: { content: '第二条' }, seq: 7, session_id: SID, run_id: `${RUN}-2`, step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 8, session_id: SID, run_id: `${RUN}-2`, time: T },
];

const SESSION_ROW = {
  fact_id: 'f-s',
  type: 'constraint',
  value: '会话级约束',
  status: 'active',
  source_event_id: 'ev-3',
  source_seq: 3,
  registered_at: T,
  revoked_at: null,
  revoked_by: null,
};
const PROJECT_ROW = {
  fact_id: 'prj-1',
  type: 'constraint',
  value: '项目级约束',
  status: 'active',
  source_event_id: null,
  source_seq: null,
  registered_at: T,
  revoked_at: null,
  revoked_by: null,
  scope: 'project',
  overridden_by: null,
};
const PROJECT_OVER = {
  ...PROJECT_ROW,
  fact_id: 'prj-2',
  value: '被本会话覆盖的项目约束',
  overridden_by: 'f-s',
};
const GLOBAL_ROW = {
  ...PROJECT_ROW,
  fact_id: 'glb-1',
  value: '全局级约束',
  scope: 'global',
};

const PROJECT_META = { label: 'EricKingWhy/intelligence-agent' };
const panel = (page: Page) => page.getByTestId('constraints-panel');

async function openConstraints(page: Page, mock: Record<string, unknown>): Promise<void> {
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

test('面板三组：SESSION/PROJECT/GLOBAL + git 根 + 被本会话覆盖', async ({ page }) => {
  await openConstraints(page, {
    protectedFacts: [SESSION_ROW, PROJECT_ROW, PROJECT_OVER, GLOBAL_ROW],
    effectiveProject: PROJECT_META,
  });

  await expect(panel(page)).toContainText('SESSION');
  await expect(panel(page)).toContainText('本会话');
  await expect(panel(page)).toContainText('PROJECT');
  await expect(panel(page)).toContainText('git 根 · EricKingWhy/intelligence-agent');
  await expect(panel(page)).toContainText('GLOBAL');
  await expect(panel(page)).toContainText('我的全局');
  // 覆盖标签出现在被覆盖的项目条目行。
  await expect(panel(page)).toContainText('被本会话覆盖');
  await expect(panel(page)).toContainText('会话级约束');
  await expect(panel(page)).toContainText('全局级约束');
});

test('非 git 目录：不渲染 PROJECT 组', async ({ page }) => {
  await openConstraints(page, {
    protectedFacts: [SESSION_ROW, GLOBAL_ROW],
    // effectiveProject 缺省 = null（非 git）
  });
  await expect(panel(page)).toContainText('SESSION');
  await expect(panel(page)).toContainText('GLOBAL');
  await expect(panel(page)).not.toContainText('PROJECT');
  await expect(panel(page)).not.toContainText('git 根');
});

test('非 git（projectAvailable=false）：登记三选一「本项目」置灰', async ({ page }) => {
  await routeApi(page, {
    onSessionPost: (route) => fulfillSse(route, HEAD),
    events: HEAD,
    // effectiveProject 缺省 = null ⇒ App 判定非 git ⇒ 本项目置灰
  });
  await page.goto('/');
  await submitTask(page, '跑个任务');

  await page.locator('#remember-as-procedural-rule').check();
  const group = page.getByRole('group', { name: '登记作用域' });
  await expect(group.getByRole('button', { name: '本项目' })).toBeDisabled();
  await expect(group.getByRole('button', { name: '本会话' })).toBeEnabled();
});

test('登记透传：勾选 + 选「本项目」→ /messages 的 protected_facts 带 scope=project', async ({ page }) => {
  let messagesBody: Record<string, unknown> | null = null;
  await routeApi(page, {
    onSessionPost: (route) => fulfillSse(route, HEAD),
    onMessagesPost: (route) => {
      messagesBody = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      return fulfillSse(route, SECOND);
    },
    events: HEAD,
    effectiveProject: PROJECT_META,
  });
  await page.goto('/');
  await submitTask(page, '跑个任务');
  await expect(page.getByLabel('Agent 任务')).toBeEnabled({ timeout: 5000 });

  await page.locator('#remember-as-procedural-rule').check();
  const group = page.getByRole('group', { name: '登记作用域' });
  // 默认「本会话」选中 + hint 逐字。
  await expect(group.getByRole('button', { name: '本会话' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.composer-scope-hint-text')).toHaveText(
    '只在当前会话生效，随会话结束自然失效。适合一次性的要求。',
  );

  await group.getByRole('button', { name: '本项目' }).click();
  await expect(page.locator('.composer-scope-hint-text')).toHaveText(
    '写入项目级存储，本仓库开的所有会话都生效，别的项目不受影响。',
  );

  await page.getByLabel('Agent 任务').fill('本项目都要 TDD');
  await page.getByLabel('Agent 任务').press('Enter');

  await expect.poll(() => messagesBody).not.toBeNull();
  expect(messagesBody!['remember_as_procedural_rule']).toBe(true);
  expect(messagesBody!['protected_facts']).toEqual([
    { fact_type: 'constraint', value: '本项目都要 TDD', scope: 'project' },
  ]);
});
