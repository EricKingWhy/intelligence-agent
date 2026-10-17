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

  // 选中即收面板：否则浮层会盖住分栏头部（含「关闭分栏」按钮）的指针事件。
  await expect(page.locator('.roster')).toHaveCount(0);

  const pane = page.locator('aside.subagent-split-pane');
  await expect(pane).toBeVisible();
  await expect(pane.locator('.split-title')).toHaveText('research_review');
  await expect(pane.locator('.split-subtitle')).toHaveText(CHILD.slice(0, 8));
  // 票面 §5.2 分栏头：状态 pill（图标+状态色）+ `12.8K tok · 估算 · 完成`（诚实口径）。
  await expect(pane.locator('.subagent-split-head .act-status')).toBeVisible();
  await expect(pane.locator('.subagent-split-head .act-status')).toHaveClass(/act-status-success/);
  await expect(pane.locator('.split-usage')).toHaveText('12.8K tok · 估算 · 完成');

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

// ── Correctness P2-2：AC3 实时翻转链 ──────────────────────────────────────

const RUNNING_EVENTS: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '安排个子智能体' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'agent/delegation-started', data: { child_session_id: CHILD, target: 'research_review', task: '查一下这个改动' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
];

const FINISHED: FrameSpec = {
  type: 'agent/delegation-finished',
  data: { child_session_id: CHILD, target: 'research_review', status: 'completed', summary: '看过了' },
  seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T,
};

const runningRow = () => ({
  child_session_id: CHILD, target: 'research_review', task: '查一下这个改动',
  status: 'running', started_at: T, completed_at: null,
  used_tokens: 12_842, usage_state: 'usage_only',
});

test('#1256 P2-2：finished 经 WS 到达 → 投影 status 翻转 → roster 重拉 → 行显「完成」', async ({ page }) => {
  // frameGate：增量帧只在用例放行后下发——「finished 何时到达」由测试掌握，无竞态。
  let release!: () => void;
  const midGate = new Promise<void>((res) => { release = res; });
  // 真后端从父事件流派生 roster：finished 落盘后 /delegations 即翻 completed。
  let roster: unknown[] = [runningRow()];

  await routeApi(page, {
    sessions: [{ ...sessionRow, event_count: RUNNING_EVENTS.length }],
    events: RUNNING_EVENTS,
    delegations: () => roster.map((r) => ({ ...r })),
    onWs: () => ({ frames: [FINISHED], ending: 'keep', frameGate: midGate }),
  });
  await page.goto('/');
  await page.locator('.session-item').first().click();
  await expect(page.locator('.timeline-row')).toHaveCount(RUNNING_EVENTS.length);

  await page.locator('.agent-trigger').click();
  const stats = page.locator('.agent-stats');
  await expect(stats).toContainText('运行中');

  // 子会话完成：后端快照先翻转，再放行 WS 增量帧——前端投影 status 翻转 ⇒
  // signature `${child}:${status}` 变化 ⇒ useDelegations 重拉 ⇒ 行显「完成」。
  roster = [{ ...runningRow(), status: 'completed', completed_at: T }];
  release();
  await expect(stats).toContainText('完成');
});

// ── Correctness P3-3：AC9 键盘路径 ─────────────────────────────────────────

test('#1256 AC9：键盘路径：行聚焦 Enter → 主视图打开子会话（与点击行主体同语义）', async ({ page }) => {
  const childRow = { ...sessionRow, session_id: CHILD, first_user_message: '子任务' };
  await routeApi(page, { sessions: [sessionRow, childRow], events: EVENTS, delegations: DELEGATIONS });
  await page.goto('/');
  await openSession(page);

  await page.locator('.agent-trigger').click();
  await page.locator('.agent-row').focus();
  await page.keyboard.press('Enter');

  // 主视图打开子会话：左列选中项切到 child（handleSelect 语义）
  await expect(page.locator('.session-item.selected')).toHaveAttribute('title', new RegExp(CHILD));
  // 选中即收面板（与点击同纪律）
  await expect(page.locator('.roster')).toHaveCount(0);
});
