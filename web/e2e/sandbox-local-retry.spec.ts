/** #1644 AC2：显式选 Docker 但 docker 不可用时，创建失败卡片给出
 *  「改用 local 重试」，点击后以原 payload 重发且强制 sandbox_backend=local。
 *
 *  契约来源：
 *  - 后端 409 detail 形状 = `domain_errors.sandbox_unavailable_http_error`
 *    （message / backend / reason / details / remediation / available_backends）；
 *  - 前端只在 409 且 backend="docker" 且 available_backends 含 "local" 时渲染按钮，
 *    零后端改动、不自动回落；
 *  - 文案以本票为基准（通用句 + Windows 追加句，与运行位置 local 选项同源）。
 *
 *  车道：Playwright e2e + page.route（同 u-project-task.spec.ts 约定）。
 */

import { expect, test, type Page } from '@playwright/test';
import { fulfillSse, routeApi, RUN, SID, T, type ApiMock, type FrameSpec } from './fixtures';

const ALPHA = 'D:/repos/alpha';

/** 票面定义的两条文案：逐字复制，不 import src（e2e 锁的是最终渲染，不是常量）。
 *  若票面文案变更，**本文件两常量必须同步改**；`TaskCreationDialog.test.tsx` 的
 *  `toContain(LOCAL_BACKEND_NOTICE)` 是唯一同源锚点（e2e 不 import src：不同 tsconfig
 *  工程，且同源会让「两处同时改错」检不出来）。 */
const LOCAL_NOTICE = 'local 后端直接在本机文件系统运行，无操作系统级文件/网络隔离。';
const NTFS_NOTICE =
  'Windows 上 local 另存在 NTFS 硬链接逃逸风险（工作区内恶意内容可逃逸到宿主），见 #1048。';

/** 捕获 POST /api/sessions 的请求体（按发生顺序）。 */
function captureSessionPosts(page: Page): Record<string, unknown>[] {
  const posts: Record<string, unknown>[] = [];
  page.on('request', (req) => {
    if (req.method() !== 'POST') return;
    if (new URL(req.url()).pathname !== '/api/sessions') return;
    try {
      posts.push(req.postDataJSON() as Record<string, unknown>);
    } catch {
      /* 非 JSON 体（本 spec 不该出现）：忽略而不是让监听器抛错 */
    }
  });
  return posts;
}

/** 第二次（local）创建成功后的启动流：与真后端帧序一致（fixture 默认 POST 分支同款）。 */
const successFrames: FrameSpec[] = [
  { type: 'session/started', data: { cwd: ALPHA }, seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  {
    type: 'user/message',
    data: { content: '给登录接口加上限流' },
    seq: 3,
    session_id: SID,
    run_id: RUN,
    step_id: 1,
    time: T,
  },
  { type: 'run/completed', data: {}, seq: 4, session_id: SID, run_id: RUN, time: T },
];

test('显式选 Docker 不可用：409 卡片明示 local 风险并「改用 local 重试」，重发带 sandbox_backend=local', async ({
  page,
}) => {
  let sessionPosts = 0;
  const mock: ApiMock = {
    sessions: [],
    projects: [{ id: 'p1', path: ALPHA, title: '项目 alpha', session_ids: [] }],
    sandboxBackends: [
      { backend: 'local', available: true, reason: null, details: {} },
      { backend: 'docker', available: true, reason: null, details: {} },
    ],
    onSessionPost: async (route) => {
      sessionPosts += 1;
      if (sessionPosts === 1) {
        await route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({
            detail: {
              message: 'Docker sandbox unavailable: docker SDK not installed',
              backend: 'docker',
              reason: 'sdk_missing',
              details: {},
              remediation: 'pip install docker',
              available_backends: ['local'],
            },
          }),
        });
        return;
      }
      await fulfillSse(route, successFrames);
    },
  };
  await routeApi(page, mock);
  const posts = captureSessionPosts(page);
  await page.goto('/');

  // 项目菜单 -> 「新建任务…」（#1177 §A，省略号 = 会弹窗）。
  const alpha = page.locator('.rail-project').filter({ hasText: '项目 alpha' });
  await alpha.locator('.rail-project-head .rail-menu-btn').click();
  await page.getByRole('menuitem', { name: '新建任务…' }).click();
  const box = page.locator('.project-dialog[aria-label="新建任务"]');
  await expect(box).toBeVisible({ timeout: 10_000 });

  await box.getByLabel('任务描述').fill('给登录接口加上限流');
  // 运行位置在「高级设置」里，默认折叠（#1177 §B）。
  await box.locator('.project-advanced-toggle').click();
  await box.locator('[aria-label="运行位置"]').click();
  // AC5：local 选项描述必须逐字带通用句（与 409 卡片同一常量）。
  await expect(
    page.locator('[cmdk-item]').filter({ hasText: LOCAL_NOTICE }),
  ).toBeVisible();
  await page.locator('[cmdk-item]').filter({ hasText: 'Docker' }).click();

  await box.getByRole('button', { name: '创建并启动' }).click();

  // ---- 第一次 POST：显式 docker，后端 409 ----
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].sandbox_backend).toBe('docker');
  expect(posts[0].task).toBe('给登录接口加上限流');

  const card = box.locator('.project-error');
  await expect(card).toBeVisible({ timeout: 10_000 });
  await expect(card).toContainText('Docker sandbox unavailable');
  await expect(card).toContainText(LOCAL_NOTICE);
  await expect(card).toContainText(NTFS_NOTICE);
  const retry = card.getByRole('button', { name: '改用 local 重试' });
  await expect(retry).toBeVisible();

  // ---- 点击切换动作：原 payload 重发，只把运行位置换成 local ----
  await retry.click();
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1].sandbox_backend).toBe('local');
  expect(posts[1].task).toBe(posts[0].task);
  expect(posts[1].cwd).toBe(ALPHA);
  expect(posts[1].autonomy).toBe(posts[0].autonomy);

  // 第二次成功后确认面关闭（创建即启动语义不变）。
  await expect(box).toHaveCount(0, { timeout: 10_000 });
});
