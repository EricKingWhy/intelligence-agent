/** #1307 看板子智能体按需拉取（AC-11）。
 *
 *  契约：只有 `product_state === 'executing'` 的卡渲染「子智能体」折叠按钮；
 *  **首次展开**才发一次 `GET /api/sessions/{id}/delegations`，结果缓存到看板关闭
 *  （空结果同样缓存）；非执行中卡不渲染按钮、看板打开期间零请求。
 *
 *  ⚠ 请求计数用**本 spec 内的闭包数组**（`page.on('request')`）：`fixtures.ts`
 *  的钩子不是计数器（#1304 审查 P2-5）。该监听是**异步投递**的 ⇒ 正向计数用
 *  `expect.poll`；"零请求 / 不再发第二次"这类负向断言在动作落地后加一个短等，
 *  避免断言跑在回调投递之前（`kanban.spec.ts:283` 同款教训）。
 */

import { expect, test, type Page } from '@playwright/test';
import { routeApi, sessionRow, type ApiMock } from './fixtures';

const T = '2026-10-17T00:00:00Z';

/** 看板行：在 `sessionRow` 之上补 #1303 的四字段（真后端恒发全部四个）。
 *  本仓惯例：各看板 spec 各自定义自己的 `boardRow`/`openBoard`（`kanban.spec.ts`、
 *  `kanban-ws.spec.ts` 同款），不做跨 spec import，故意自包含——不抽取共享。 */
function boardRow(
  sessionId: string,
  productState: string,
  over: Record<string, unknown> = {},
): Record<string, unknown> {
  return sessionRow(sessionId, null, {
    product_state: productState,
    product_state_changed_at: null,
    attention_kind: null,
    attention_summary: null,
    ...over,
  });
}

async function openBoard(page: Page): Promise<void> {
  await page.goto('/');
  await page.getByLabel('看板').click();
  await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toBeVisible();
}

/** 委派行（形状 = 后端 `web/delegations.py` 的行 / `api.ts::DelegationEntry`）。
 *  一条 completed 带 90 秒耗时（验证 `durationText`），一条 failed 无时间戳
 *  （验证缺失时不编数字）——两行覆盖 token 缩写与四态标签两条路径。 */
const DELEGATIONS = [
  {
    child_session_id: 'child-1',
    target: 'research_review',
    task: '查一下这个改动',
    status: 'completed',
    started_at: T,
    completed_at: '2026-10-17T00:01:30Z',
    summary: '看过了',
    used_tokens: 12_842,
    usage_state: 'usage_only',
  },
  {
    child_session_id: 'child-2',
    target: 'build',
    task: '跑构建',
    status: 'failed',
    started_at: null,
    completed_at: null,
    used_tokens: 500,
    usage_state: 'ok',
  },
];

/** 记录所有 `GET /api/sessions/{id}/delegations` 的会话 id（按到达顺序）。 */
function countDelegationRequests(page: Page): string[] {
  const requests: string[] = [];
  page.on('request', (req) => {
    const match = new URL(req.url()).pathname.match(/^\/api\/sessions\/([^/]+)\/delegations$/);
    if (match) requests.push(decodeURIComponent(match[1]));
  });
  return requests;
}

const cardOf = (page: Page, sessionId: string) =>
  page.locator(`.kanban-card[data-session-id="${sessionId}"]`);

test.describe('#1307 看板子智能体按需拉取', () => {
  test('AC-11①：执行中卡首次展开 → 恰好一次 /delegations，行内复用 roster 格式化', async ({
    page,
  }) => {
    const requests = countDelegationRequests(page);
    await routeApi(page, {
      sessions: [boardRow('s-exec', 'executing')],
      delegations: DELEGATIONS,
    });
    await openBoard(page);

    const card = cardOf(page, 's-exec');
    const toggle = card.getByRole('button', { name: '子智能体' });
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');

    // 按需拉取：展开之前一个请求都不该有。
    await page.waitForTimeout(150);
    expect(requests).toHaveLength(0);

    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await expect.poll(() => requests.length).toBe(1);
    expect(requests[0]).toBe('s-exec');

    // 行内容：状态词（STATUS_LABEL）、token（tokenText）、耗时（durationText）
    // 全部来自 roster 的单一事实源，本票不复制映射。
    await expect(card.locator('.agent-row')).toHaveCount(2);
    await expect(card.getByText('完成')).toBeVisible();
    await expect(card.getByText('12.8K tok · 估算')).toBeVisible();
    await expect(card.getByText('耗时 1分30秒')).toBeVisible();
    await expect(card.getByText('失败')).toBeVisible();
    await expect(card.getByText('500 tok')).toBeVisible();

    // 折叠按钮不冒泡成"打开会话"：点完看板仍在。
    await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toBeVisible();
  });

  test('AC-11②：折叠再展开 → 仍只 1 次请求（缓存到看板关闭）', async ({ page }) => {
    const requests = countDelegationRequests(page);
    await routeApi(page, {
      sessions: [boardRow('s-exec', 'executing')],
      delegations: DELEGATIONS,
    });
    await openBoard(page);

    const card = cardOf(page, 's-exec');
    const toggle = card.getByRole('button', { name: '子智能体' });

    await toggle.click();
    await expect(card.locator('.agent-row')).toHaveCount(2);
    await expect.poll(() => requests.length).toBe(1);

    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(card.locator('.agent-row')).toHaveCount(0);

    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await expect(card.locator('.agent-row')).toHaveCount(2);
    // 负向断言：给异步投递的 request 回调留出窗口后仍是 1 次。
    await page.waitForTimeout(200);
    expect(requests).toHaveLength(1);
  });

  test('AC-11③：空结果 → 「无子智能体」，折叠再展开不发第二次请求', async ({ page }) => {
    const requests = countDelegationRequests(page);
    await routeApi(page, {
      sessions: [boardRow('s-exec', 'executing')],
      delegations: [],
    });
    await openBoard(page);

    const card = cardOf(page, 's-exec');
    const toggle = card.getByRole('button', { name: '子智能体' });

    await toggle.click();
    await expect(card.getByText('无子智能体')).toBeVisible();
    await expect(card.locator('.agent-row')).toHaveCount(0);
    await expect.poll(() => requests.length).toBe(1);

    // 空结果也是"结果"：缓存住，不因为列表为空就重拉。
    await toggle.click();
    await expect(card.getByText('无子智能体')).toHaveCount(0);
    await toggle.click();
    await expect(card.getByText('无子智能体')).toBeVisible();
    await page.waitForTimeout(200);
    expect(requests).toHaveLength(1);
  });

  test('AC-11② 的另一半：缓存到**看板关闭**为止——关掉再开重新拉一次', async ({ page }) => {
    const requests = countDelegationRequests(page);
    await routeApi(page, {
      sessions: [boardRow('s-exec', 'executing')],
      delegations: DELEGATIONS,
    });
    await openBoard(page);
    await cardOf(page, 's-exec').getByRole('button', { name: '子智能体' }).click();
    await expect.poll(() => requests.length).toBe(1);

    await page.getByRole('button', { name: '关闭看板' }).click();
    await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toHaveCount(0);
    // **不刷新页面**地重新打开：考的正是"卡片随看板卸载"这条路径（page.goto 会
    // 顺带清掉一切，绿了也证明不了缓存边界）。
    await page.getByLabel('看板').click();
    await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toBeVisible();
    await expect(cardOf(page, 's-exec').getByRole('button', { name: '子智能体' }))
      .toHaveAttribute('aria-expanded', 'false');
    await cardOf(page, 's-exec').getByRole('button', { name: '子智能体' }).click();
    // 卡片随看板卸载、本地态归零 ⇒ 重新展开必须再拉一次（这是"缓存到看板关闭"的边界，
    // 不是"永久缓存"）。折叠态也回到收起：重开时按钮先是 aria-expanded=false。
    await expect.poll(() => requests.length).toBe(2);
    await expect(cardOf(page, 's-exec').locator('.agent-row')).toHaveCount(2);
  });

  test('AC-11④：待验证/待办/已接受卡不渲染折叠按钮，对这些会话零请求', async ({ page }) => {
    const requests = countDelegationRequests(page);
    const mock: ApiMock = {
      sessions: [
        boardRow('s-backlog', ''),
        boardRow('s-exec', 'executing'),
        boardRow('s-verify', 'pending_verification'),
        boardRow('s-accepted', 'accepted'),
      ],
      delegations: DELEGATIONS,
    };
    await routeApi(page, mock);
    await openBoard(page);
    await page.getByRole('button', { name: '显示已接受' }).click();
    await expect(cardOf(page, 's-accepted')).toBeVisible();

    for (const id of ['s-backlog', 's-verify', 's-accepted']) {
      const card = cardOf(page, id);
      await expect(card).toBeVisible();
      await expect(card.getByRole('button', { name: '子智能体' })).toHaveCount(0);
      await expect(card.locator('button[aria-expanded]')).toHaveCount(0);
    }

    // 对照：执行中卡的一次展开证明网络管道是通的（否则"零请求"可能只是没测到），
    // 且这三个会话始终不在请求列表里。
    await cardOf(page, 's-exec').getByRole('button', { name: '子智能体' }).click();
    await expect.poll(() => requests.length).toBe(1);
    expect(requests).toEqual(['s-exec']);
  });
});
