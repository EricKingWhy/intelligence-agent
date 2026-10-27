/** FE-04 e2e：useSession 竞态守卫在 catch 分支缺失。
 *
 * 缺陷模式：用户在旧请求（超时/失败）之后立即发起新请求，
 * 旧请求的 catch 可能在 gen 已被新请求自增后才触发，
 * 把新会话正在跑的 UI 态污染成 idle + 报错。
 *
 * 场景（真正交错竞态）：
 *   1. 发任务 A → mock 挂起网络失败，由测试门闩控制
 *   2. 在任务 A 尚在途时（不等网络失败）立即发任务 B
 *   3. 任务 B 的 mock 立即返回成功 SSE 流
 *   4. 任务 B 的消息显示后释放任务 A 的网络失败 → catch 触发
 *   5. 修复前：旧请求的失败处理会把当前 UI 设成 idle 并显示错误
 *      修复后：gen 守卫 return，不污染 → 绿灯
 *
 * 车道归属：Playwright e2e（同 continuation.spec.ts 约定）。 */

import { expect, test } from '@playwright/test';
import { DEFAULT_SESSION_CLAIM_TIMEOUT_MS } from '../src/lib/wsStream';
import { fulfillSse, routeApi } from './fixtures';

const SUCCESS_FRAMES = [
  { type: 'session/started', seq: 1, session_id: 'race-session-b', run_id: 'race-run-b', time: '2026-09-08T00:00:00Z' },
  { type: 'run/started', seq: 2, session_id: 'race-session-b', run_id: 'race-run-b', time: '2026-09-08T00:00:00Z' },
  { type: 'user/message', data: { content: '任务B' }, seq: 3, session_id: 'race-session-b', run_id: 'race-run-b', step_id: 1, time: '2026-09-08T00:00:00Z' },
];
test('竞态守卫：旧请求迟到失败不污染新会话', async ({ page }) => {
  test.setTimeout(90_000);
  let postCount = 0;
  let secondPostHit = false;
  let releaseFirstPost!: () => void;
  const firstPostGate = new Promise<void>((resolve) => {
    releaseFirstPost = resolve;
  });
  page.once('close', releaseFirstPost);

  await routeApi(page, {
    sessions: [],
    events: [],
    onSessionPost: async (route) => {
      postCount++;
      if (postCount === 1) {
        await firstPostGate;
        if (page.isClosed()) return;
        await route.abort('failed');
        return;
      }
      await fulfillSse(route, SUCCESS_FRAMES);
      secondPostHit = true;
    },
    onWs: () => ({
      events: SUCCESS_FRAMES,
      hasActiveRun: true,
      ending: 'keep',
      pingIntervalMs: 2000,
    }),
  });

  await page.goto('/');

  // 发任务 A（将延迟失败）
  await page.getByLabel('Agent 任务').fill('任务A');
  await page.getByLabel('发送').click();

  // 不等任务 A 失败——立即发任务 B（制造交错竞态）
  await page.getByLabel('Agent 任务').fill('任务B');
  await page.getByLabel('发送').click();

  // 等待任务 B 的 POST 被拦截
  await expect.poll(() => secondPostHit).toBe(true);
  await expect(page.getByRole('tabpanel', { name: 'Chat' }).locator('.msg-bubble-user')).toContainText('任务B');
  await expect(page.locator('.composer-stop')).toBeVisible();

  // A 已进入会话认领；网络失败先落定。
  const lateFailure = page.waitForEvent('requestfailed', (request) =>
    request.method() === 'POST' &&
    new URL(request.url()).pathname === '/api/sessions' &&
    request.postData()?.includes('任务A') === true,
  );
  releaseFirstPost();
  await lateFailure;
  // A 的列表认领最多等待 DEFAULT_SESSION_CLAIM_TIMEOUT_MS；等这条 catch 路径收口后再断言。
  await page.waitForTimeout(DEFAULT_SESSION_CLAIM_TIMEOUT_MS + 100);
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));

  // 关键断言：UI 不应显示任务 A 的 setError 污染
  // 修复前：旧请求的网络失败会让新会话显示错误并回到 idle。
  // 修复后：gen 守卫丢弃旧请求的迟到失败
  const errorText = page.locator('[role="alert"], .error, [data-testid="error-message"]');
  await expect(errorText).toHaveCount(0);
  await expect(page.locator('.composer-stop')).toBeVisible();
});
