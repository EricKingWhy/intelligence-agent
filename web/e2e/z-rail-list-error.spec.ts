/** F9：会话列表**加载失败**不得被渲染成「暂无会话，提交任务即可开始。」
 *
 *  真机证据（含整页可见文本逐字）见 `docs/LIVE_BROWSER_TEST_20260917.md` §8.5：后端不可达
 *  时项目区挂着「项目列表加载失败：… 重试」，同一屏的会话区却写着一句**可被执行的假陈述**
 *  ——用户此刻最想知道的是"我的会话还在不在"，而界面告诉他"没有会话"。
 *
 *  为什么必须在 e2e 里锁，而不是只在组件 SSR 测试里锁：组件测试只能证明"给了
 *  `sessionsError` 就渲染对"，**证明不了这个 prop 真的会被填上**——把
 *  `refreshSessions` 里的 `setSessionsError` 改回 `setError`（F9 修复前的写法），
 *  组件测试**照样全绿**。真正断掉的是 hook → App → 侧栏这条线，只有真网络能证明。
 *
 *  两个踩过的坑（都写在代码里，免得下一位重踩）：
 *  1. **URL 用谓词而不是通配**：`routeApi` 注册的是兜底通配路由，Playwright 后注册优先，
 *     但 HTML 通配是**整条 URL 匹配（含 query）**——`GET /api/sessions` 实际带
 *     `?include_archived=true`，写 `通配 + /api/sessions` 根本不匹配，覆盖静默失效，
 *     于是"失败"的用例其实拿到的是成功响应（红得莫名其妙）。
 *  2. **别在块注释里写"星号+斜杠"**：那个序列会提前结束注释，后面的字面量变成代码。
 */

import { expect, test, type Route } from '@playwright/test';
import { routeApi, sessionRow } from './fixtures';

const SESSIONS = [sessionRow('s1', null), sessionRow('s2', null)];
const RAIL_ERROR = '.rail-error';
const EMPTY_HINT = '暂无会话，提交任务即可开始。';

/** `GET /api/sessions` 的谓词（见文件头坑 1）。 */
const isSessionList = (url: URL) => url.pathname === '/api/sessions';

const down = (route: Route) =>
  route.fulfill({ status: 502, body: '{"detail":"gateway"}', contentType: 'application/json' });

/** 只让**列表**那条 GET 挂掉；同路径的 POST（建会话）交给 `routeApi` 的默认实现。 */
async function failSessionList(page: import('@playwright/test').Page): Promise<void> {
  await page.route(isSessionList, (route) =>
    route.request().method() === 'GET' ? down(route) : route.fallback(),
  );
}

test('会话列表加载失败：不说「暂无会话」，改说失败 + 可点重试（F9 主用例）', async ({ page }) => {
  await routeApi(page, { sessions: SESSIONS });
  await failSessionList(page);

  await page.goto('/');

  // 假陈述必须消失（这是本票的核心：失败不能说成"没有"）
  await expect(page.locator('.empty-hint', { hasText: EMPTY_HINT })).toHaveCount(0);
  // 换成真话：说清哪个列表失败、并给出可点的重试
  await expect(page.locator(RAIL_ERROR)).toHaveCount(1);
  await expect(page.locator(RAIL_ERROR)).toContainText('加载会话列表失败');
  await expect(page.getByRole('button', { name: '重试' })).toBeVisible();
});

test('后端整体不可达：项目与会话两条错误条**同时**在（不是只显示第一条）', async ({ page }) => {
  await routeApi(page, { sessions: SESSIONS });
  await failSessionList(page);
  await page.route('**/api/projects', down);

  await page.goto('/');

  await expect(page.locator(RAIL_ERROR)).toHaveCount(2);
  await expect(page.locator(RAIL_ERROR).first()).toContainText('项目列表加载失败');
  await expect(page.locator(RAIL_ERROR).nth(1)).toContainText('加载会话列表失败');
  await expect(page.getByRole('button', { name: '重试' })).toHaveCount(2);
  await expect(page.locator('.empty-hint', { hasText: EMPTY_HINT })).toHaveCount(0);
});

test('点重试：恢复成功后错误条消失、会话行出现（重试不是装饰）', async ({ page }) => {
  await routeApi(page, { sessions: SESSIONS });
  await failSessionList(page);

  await page.goto('/');
  await expect(page.locator(RAIL_ERROR)).toHaveCount(1);

  // 后端回来了：撤掉失败覆盖路由，`routeApi` 的默认实现接管
  await page.unroute(isSessionList);
  await page.getByRole('button', { name: '重试' }).click();

  await expect(page.locator(RAIL_ERROR)).toHaveCount(0);
  await expect(page.locator('.session-item')).toHaveCount(2);
  await expect(page.locator('.empty-hint', { hasText: EMPTY_HINT })).toHaveCount(0);
});

test('真正的空态不受影响：「暂无会话」照旧出现（反证修复没把空态删掉）', async ({ page }) => {
  await routeApi(page, { sessions: [] });

  await page.goto('/');

  await expect(page.locator('.empty-hint', { hasText: EMPTY_HINT })).toHaveCount(1);
  await expect(page.locator(RAIL_ERROR)).toHaveCount(0);
});
