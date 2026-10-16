/** #1256 子智能体可见性 e2e——顶栏 roster + 分栏打开完整子会话。
 *
 *  为什么这些必须走 e2e（组件测试补不到）：
 *   ① roster 是**父会话上下文**里的顶栏浮层，只有真 App 装配（useDelegations 拉
 *      `/delegations` + TopBar slot）才成立；
 *   ② 「分栏打开，父会话保持不动」考的是 App 的 `with-split` 布局与**独立选择态**——
 *      组件测试只钉各自渲染，管不到"左列没被换成子会话"。
 *
 *  数据面：父会话用**历史会话**（`GET /events`），委派记录由 `delegations` mock
 *  直供（真后端从父事件流派生，对账由 `tests/test_web_delegations.py` 钉）。
 */

import { expect, test } from '@playwright/test';
import { RUN, SID, T, routeApi, type FrameSpec } from './fixtures';

const CHILD = 'child-abc12345';

const EVENTS: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '安排个子智能体' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'agent/delegation-started', data: { child_session_id: CHILD, target: 'research_review', task: '查一下这个改动' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'agent/delegation-finished', data: { child_session_id: CHILD, target: 'research_review', status: 'completed', summary: '看过了' }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 6, session_id: SID, run_id: RUN, time: T },
];

const DELEGATIONS = [
  {
    child_session_id: CHILD, target: 'research_review', task: '查一下这个改动',
    status: 'completed', started_at: T, completed_at: T, summary: '看过了',
    used_tokens: 12_842, usage_state: 'usage_only',
  },
];

const sessionRow = {
  session_id: SID, event_count: EVENTS.length, first_event_time: T, last_event_time: T,
  first_user_message: '安排个子智能体', trace_id: null, trace_url: null,
};

async function openSession(page: import('@playwright/test').Page) {
  await page.locator('.session-item').first().click();
  await expect(page.locator('.timeline-row')).toHaveCount(EVENTS.length);
}

test('#1256 AC：有子智能体 → 顶栏 roster 入口显数量（无则完全不渲染）', async ({ page }) => {
  await routeApi(page, { sessions: [sessionRow], events: EVENTS, delegations: DELEGATIONS });
  await page.goto('/');
  await openSession(page);

  const trigger = page.locator('.agent-trigger');
  await expect(trigger).toBeVisible();
  await expect(page.locator('.roster-label')).toHaveText('1 个子智能体');
  // 无运行中的子智能体 → 不显 running 徽标
  await expect(page.locator('.roster-running-badge')).toHaveCount(0);
});

test('#1256 AC：无子智能体 → 不渲染入口（无证据不渲染）', async ({ page }) => {
  await routeApi(page, { sessions: [sessionRow], events: EVENTS, delegations: [] });
  await page.goto('/');
  await openSession(page);
  await expect(page.locator('.agent-trigger')).toHaveCount(0);
});

test('#1256 AC：展开 roster → 行显名/token 缩写/耗时/状态', async ({ page }) => {
  await routeApi(page, { sessions: [sessionRow], events: EVENTS, delegations: DELEGATIONS });
  await page.goto('/');
  await openSession(page);

  await page.locator('.agent-trigger').click();
  const row = page.locator('.agent-row');
  await expect(row).toHaveCount(1);
  await expect(row.locator('.agent-title')).toHaveText('research_review');
  await expect(row.locator('.agent-sub')).toHaveText('查一下这个改动');
  const stats = row.locator('.agent-stats');
  await expect(stats).toContainText('完成');
  await expect(stats).toContainText('12.8K tok');
  await expect(stats).toContainText('估算'); // usage_only 诚实标注
});

test('#1256 AC：chevron → 右栏打开完整子会话，**父会话保持不动**', async ({ page }) => {
  await routeApi(page, { sessions: [sessionRow], events: EVENTS, delegations: DELEGATIONS });
  await page.goto('/');
  await openSession(page);

  await page.locator('.agent-trigger').click();
  await page.locator('.row-arrow').click();

  const pane = page.locator('aside.subagent-split-pane');
  await expect(pane).toBeVisible();
  await expect(pane.locator('.split-title')).toHaveText('research_review');
  await expect(pane.locator('.split-subtitle')).toHaveText(CHILD.slice(0, 8));

  // 父会话保持不动：布局进 with-split，主列仍在、父会话工具栏/对话流仍渲染
  await expect(page.locator('.app-workspace.with-split')).toHaveCount(1);
  await expect(page.locator('.workspace-main')).toBeVisible();
  await expect(page.locator('.app-workspace')).toHaveCount(1); // 没有第二套 workspace
});

test('#1256 AC：关闭分栏 → 回到单列', async ({ page }) => {
  await routeApi(page, { sessions: [sessionRow], events: EVENTS, delegations: DELEGATIONS });
  await page.goto('/');
  await openSession(page);

  await page.locator('.agent-trigger').click();
  await page.locator('.row-arrow').click();
  await expect(page.locator('aside.subagent-split-pane')).toBeVisible();

  await page.locator('.close-pane').click();
  await expect(page.locator('aside.subagent-split-pane')).toHaveCount(0);
  await expect(page.locator('.app-workspace.with-split')).toHaveCount(0);
});
