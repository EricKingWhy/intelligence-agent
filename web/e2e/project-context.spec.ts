/** #1028：通用「新建会话」不带项目语境 → 会话与选中项目脱钩且无归属提示。
 *
 *  项目成员规则是**会话 cwd 与项目路径 canonical 精确相等**（后端 `workspace/index.py`），
 *  不匹配的会话被静默剪掉。本 spec 锁两条修复后的用户可见行为：
 *  - AC1：点中项目行后走**通用**「新建会话」（不是项目菜单里的「在此项目中新建任务」），
 *    新会话带 `cwd: project.path`，并落进该项目分组；
 *  - AC2/AC3：composer 区常驻归属 chip 显示「项目名 + cwd」；未归属会话明示
 *    「未分组（cwd: …）」——chip 就是那条"当前会话不属于任何项目"的警示。
 *
 *  AC4（以非项目 cwd 强行 attach → 409 可读文案）在 `r-project-groups.spec.ts:251`
 *  已有专门用例，本 spec 不重复（那是既有的边界语义，本票刻意不改）。
 *
 *  车道：Playwright e2e + page.route（同 r-project-groups.spec.ts 约定），`--workers=2`。
 */

import { expect, test, type Page } from '@playwright/test';
import {
  fulfillSse,
  routeApi,
  RUN,
  sessionRow,
  SID,
  submitTask,
  T,
  type ApiMock,
  type FrameSpec,
} from './fixtures';

const ALPHA = 'D:/repos/alpha';

const project = (page: Page, title: string) =>
  page.locator('.rail-project').filter({ hasText: title });

/** 打开某个项目行的 kebab 操作菜单（#1177 §A：菜单第一项是「新建会话」）。 */
async function openProjectMenu(page: Page, title: string) {
  await project(page, title).locator('.rail-project-head .rail-menu-btn').click();
}

/** 捕获 POST /api/sessions 的请求体（AC1 断言用）。 */
function captureSessionPosts(page: Page): Record<string, unknown>[] {
  const posts: Record<string, unknown>[] = [];
  page.on('request', (req) => {
    if (req.method() !== 'POST') return;
    if (new URL(req.url()).pathname !== '/api/sessions') return;
    try {
      posts.push(req.postDataJSON() as Record<string, unknown>);
    } catch {
      /* 非 JSON 体：忽略，不让监听器抛错 */
    }
  });
  return posts;
}

test('AC1/AC2 点中项目行 → 通用新建继承项目 cwd、会话落组、composer 显示归属 chip', async ({
  page,
}) => {
  const mock: ApiMock = {
    sessions: [],
    projects: [{ id: 'p1', path: ALPHA, title: '项目 alpha', session_ids: [] }],
  };
  await routeApi(page, mock);
  const posts = captureSessionPosts(page);
  await page.goto('/');

  // 点中项目行（= 选中项目语境 + 展开，两个语义共存）
  const alpha = project(page, '项目 alpha');
  await alpha.locator('.rail-project-toggle').click();
  // 选中态可见（AC1 的语境是显式的，不是隐式的）
  await expect(alpha.locator('.rail-project-toggle')).toHaveClass(/project-active/);

  // 走**通用**「新建会话」入口（不是项目菜单里的入口）
  await page.getByRole('button', { name: '新建会话' }).click();
  // 新建态下 chip 已经预告"将在哪个项目里新建"
  await expect(page.locator('.composer-ownership')).toContainText('项目「项目 alpha」');
  await submitTask(page, '给登录接口加上限流');

  // AC1：请求带 cwd = 项目路径
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].cwd).toBe(ALPHA);

  // AC1 落组：新会话出现在「项目 alpha」下
  await expect(alpha.locator('.session-item-id')).toHaveCount(1, { timeout: 10_000 });

  // AC2：composer 归属 chip 同时显示项目名与 cwd
  await expect(page.locator('.composer-ownership')).toContainText('项目「项目 alpha」');
  await expect(page.locator('.composer-ownership')).toContainText(ALPHA);
});

test('AC1 对照：未选中项目时通用新建**不**带 cwd（回归基线，证明继承不是恒发）', async ({
  page,
}) => {
  // 有一个项目但**不点它**（所以无项目语境）。fixture 的 POST 默认分支要求 cwd，
  // 这里用 `onSessionPost` 自铺一条不读 cwd 的流，只为观察"发没发 cwd 键"。
  const frames: FrameSpec[] = [
    { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
    { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
    { type: 'user/message', data: { content: '随便问一句' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'run/completed', data: {}, seq: 4, session_id: SID, run_id: RUN, time: T },
  ];
  const sessions = [sessionRow('s-seed', null)];
  const mock: ApiMock = {
    sessions,
    projects: [{ id: 'p1', path: ALPHA, title: '项目 alpha', session_ids: [] }],
    onSessionPost: (route) => {
      // 认领新会话需要 GET /api/sessions 里出现新行（基线 + 差分），fixture 的
      // `sessionState` 与这里的数组是同一引用，push 即让差分可见。
      sessions.unshift(sessionRow('cwd-new', null));
      return fulfillSse(route, frames);
    },
  };
  await routeApi(page, mock);
  const posts = captureSessionPosts(page);
  await page.goto('/');

  await page.getByRole('button', { name: '新建会话' }).click();
  await submitTask(page, '随便问一句');

  await expect.poll(() => posts.length).toBe(1);
  // 未选项目 → 不发 cwd 键（后端用自己的默认目录，既有行为不变）。
  expect('cwd' in posts[0]).toBe(false);
});

test('AC3 未归属会话：composer 与列表行都明示「未分组（cwd: …）」', async ({ page }) => {
  // 一条 workspace=null 的会话，其 durable log 里有 session/started.cwd。
  const mock: ApiMock = {
    sessions: [sessionRow('s-ungrouped', null)],
    projects: [],
    events: [
      {
        type: 'session/started',
        data: { cwd: '/home/hatch' },
        seq: 1,
        session_id: 's-ungrouped',
        time: T,
      },
    ],
  };
  await routeApi(page, mock);
  await page.goto('/');

  await page.locator('.session-item', { hasText: 's-ungrouped' }).click();

  // 列表行就地标注 cwd（AC3 的归属警示载体）
  await expect(page.locator('.session-item-owner')).toContainText('未分组（cwd: /home/hatch）');
  // composer chip 同步显形
  await expect(page.locator('.composer-ownership')).toContainText('未分组（cwd: /home/hatch）');
});

test('P3 修回：再次点击已 active 的项目行取消语境 → 通用新建不再带 cwd（可逆）', async ({
  page,
}) => {
  // 复用 AC1 对照的"不读 cwd"流，只为观察第二次点击取消后"发没发 cwd 键"。
  const frames: FrameSpec[] = [
    { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
    { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
    { type: 'user/message', data: { content: '随便问一句' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'run/completed', data: {}, seq: 4, session_id: SID, run_id: RUN, time: T },
  ];
  const sessions = [sessionRow('s-seed', null)];
  const mock: ApiMock = {
    sessions,
    projects: [{ id: 'p1', path: ALPHA, title: '项目 alpha', session_ids: [] }],
    onSessionPost: (route) => {
      sessions.unshift(sessionRow('cwd-cancel', null));
      return fulfillSse(route, frames);
    },
  };
  await routeApi(page, mock);
  const posts = captureSessionPosts(page);
  await page.goto('/');

  const toggle = project(page, '项目 alpha').locator('.rail-project-toggle');
  // 第一次点击 = 选中项目语境（通用新建将继承 cwd）
  await toggle.click();
  await expect(toggle).toHaveClass(/project-active/);
  // 再次点击同一行 = 取消语境（可逆语义）
  await toggle.click();
  await expect(toggle).not.toHaveClass(/project-active/);

  // 取消后走通用新建：不再带 cwd 键（回到"未选项目"基线）
  await page.getByRole('button', { name: '新建会话' }).click();
  await submitTask(page, '随便问一句');
  await expect.poll(() => posts.length).toBe(1);
  expect('cwd' in posts[0]).toBe(false);
});

test('#1269 武装态显式提示：菜单「新建会话」武装 → 行上出现「新建会话目标」；再点行取消后消失', async ({
  page,
}) => {
  await routeApi(page, {
    sessions: [],
    projects: [{ id: 'p1', path: ALPHA, title: '项目 alpha', session_ids: [] }],
  });
  await page.goto('/');

  const alpha = project(page, '项目 alpha');
  const toggle = alpha.locator('.rail-project-toggle');

  // 初始未武装：无提示（新加的元素不能常显）。
  await expect(alpha.locator('.rail-project-armed-hint')).toHaveCount(0);

  // 菜单「新建会话」= 显式武装项目语境（#1028 的锚定入口）。
  await openProjectMenu(page, '项目 alpha');
  await page.getByRole('menuitem', { name: '新建会话' }).click();
  await expect(toggle).toHaveClass(/project-active/);
  // #1269：武装不再只隐含在低对比高亮里——行上给出显式「新建会话目标」提示。
  await expect(alpha.locator('.rail-project-armed-hint')).toBeVisible();
  await expect(alpha.locator('.rail-project-armed-hint')).toHaveText('新建会话目标');
  // 可访问名称把 toggle 的可逆性讲清（否则用户看不出"再点一次取消"）。
  await expect(toggle).toHaveAttribute('aria-label', /再点一次取消/);

  // 再点项目行 = 取消语境（#1028 P3 可逆语义）；提示随之消失。
  await toggle.click();
  await expect(toggle).not.toHaveClass(/project-active/);
  await expect(alpha.locator('.rail-project-armed-hint')).toHaveCount(0);
});
