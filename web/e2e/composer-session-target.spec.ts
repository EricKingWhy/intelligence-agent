/** #1312：Composer 会话级目标状态行 + 命令面板「新建会话」。
 *
 * 三条断言对 AC1/AC3：
 *  1. 选中会话 → 状态行显示「正在向会话 <8 位 id> 追问」（不是完整 id）；
 *  2. 「改为新会话」→ 回到新建态，状态行变「将创建新会话」；
 *  3. Ctrl+K → 搜「新建」→ 执行「新建会话」→ 同一条 handleNew 路径（清选中 + 聚焦输入框）。
 *
 * 车道归属：Playwright e2e（同 continuation.spec.ts 约定）。
 */
import { expect, test, type Page } from '@playwright/test';
import { routeApi, sessionRow, T, type FrameSpec } from './fixtures';

const SID = 'sess-target-1312';
const ROW = sessionRow(SID, null, { first_user_message: '会话级目标状态行' });

const FRAMES: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: 'rt-1312', time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: 'rt-1312', time: T },
  { type: 'user/message', data: { content: '会话级目标状态行' }, seq: 3, session_id: SID, run_id: 'rt-1312', step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 4, session_id: SID, run_id: 'rt-1312', time: T },
];

const target = (page: Page) => page.locator('[data-testid="composer-session-target"]');

async function openSession(page: Page): Promise<void> {
  await routeApi(page, { sessions: [ROW], events: FRAMES });
  await page.goto('/');
  await page.locator('.session-item').first().click();
  await expect(target(page)).toContainText('正在向会话');
}

test('选中会话：状态行显示 8 位 id，点「改为新会话」回到新建态', async ({ page }) => {
  await openSession(page);

  // 8 位前缀（sess-tar）可见；完整 id 不出现在状态行里。
  await expect(target(page)).toContainText('正在向会话 sess-tar 追问');
  await expect(page.getByRole('button', { name: '改为新会话' })).toBeVisible();
  await expect(target(page)).not.toContainText(SID);

  await page.getByRole('button', { name: '改为新会话' }).click();
  await expect(target(page)).toHaveText('将创建新会话');
});

test('命令面板：Ctrl+K 搜「新建」→「新建会话」可执行（复用 handleNew）', async ({ page }) => {
  await openSession(page);

  await page.keyboard.press('Control+k');
  await expect(page.locator('.palette-input')).toBeVisible();
  await page.locator('.palette-input').fill('新建');

  const item = page.locator('.palette-content .palette-item').filter({ hasText: '新建会话' });
  await expect(item).toHaveCount(1);
  await item.click();

  await expect(page.locator('.palette-content')).toBeHidden();
  await expect(target(page)).toHaveText('将创建新会话');
  // handleNew 的落点就是 composer（#1177 §A），命令面板入口同样直达。
  await expect(page.getByLabel('Agent 任务')).toBeFocused();
});
