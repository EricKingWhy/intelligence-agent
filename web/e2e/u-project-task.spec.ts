/** 场景 U（WS-6 / #169 前端半；**#367 [W-23] 选项 A 重写**，用户 2026-10-06 批准）：
 *  项目内新建任务——入口 → 确认面（prompt 优先）→ 创建即启动 → 落组。
 *
 *  契约来源：#367 [W-23] 选项 A——**取代** #204 的「弹窗职责收窄为选目录 + 空会话」
 *  裁定：prompt 进弹窗（唯一必填项），创建即带任务启动（launch 走后端默认 true），
 *  弹窗 = `TaskCreationDialog`（aria-label 新建任务）。旧 spec 锁的 #204 形态
 *  （aria-label「在此项目中新建任务」、无任务输入框、「创建会话」、launch=false、
 *  弹窗内权限档 picker）全部随 `StartTaskInProjectDialog` 删除（805ff39d）退场。
 *  本 spec 锁可在无后端环境确定复现的部分：
 *  - AC9 项目行 kebab 第一项 =「在此项目中新建任务」；空项目占位区替换为该入口按钮；
 *  - AC10 确认面逐字出现「Agent 将直接读写该目录：<路径>」；prompt（任务描述）
 *    是**唯一必填项**——为空时「创建任务」不可用（#204 的「没有任务内容输入框」
 *    被选项 A 反转）；自主度三档（问/计划/自动，默认「问」）；#204 的权限档
 *    picker **不在弹窗里**（被自主度取代）；
 *  - AC11 创建体带 `cwd`、`task`（= prompt 原文）、`autonomy`（默认 ask）；
 *    **默认不发 `model` / `sandbox_backend` / `permission_mode`**（auto/缺省 = 不发键）；
 *    URL 不再拼 `launch` query（#204 的 launch=false 被反转为创建即启动）；
 *  - AC11b 完成目标是可选文本框，提交时拼进首条 prompt
 *    （`完成目标：<验收>\n\n<prompt>`，Codex `/goal` 自然语言三要素同型）；
 *  - AC12 422 留在确认面：不关对话框、不打全局横幅、后端 detail 原样可见
 *    （submitTask 的 ownError 通道，前缀「提交失败：」）、可重试；
 *  - AC13 以上全部由本 spec 覆盖（mock 语义按真后端带 task 的 SSE 启动流，见 fixtures）。
 *
 *  车道：Playwright e2e + page.route（同 r-project-groups.spec.ts 约定），`--workers=2`。
 */

import { expect, test, type Page } from '@playwright/test';
import { routeApi, sessionRow, type ApiMock } from './fixtures';

const ALPHA = 'D:/repos/alpha';
const BETA = 'D:/repos/beta';

/** 真后端 GET /api/permission-modes 的三档（id 是封闭枚举，未知值 → 422）。
 *  实际消费方是**弹窗之外**的 Composer dock：底部控制行用这份清单渲染权限
 *  控件，清单为空时控件整个隐藏（Composer.tsx）。#367 选项 A 的弹窗只认自主度
 *  三档（组件内置，不吃这条端点）——「弹窗里没有权限档 picker」的判别力来自
 *  弹窗结构本身，与这条 mock 无关；留着它是为了让 dock 在每个用例页面上正常在场。 */
const REAL_PERMISSION_MODES = [
  { id: 'read-only', display_name: '只读', description: '可读文件和运行只读工具，不可写入。', icon: 'lock' },
  {
    id: 'workspace-write',
    display_name: '工作区写入',
    description: '可读写工作区内文件；高危工具仍需审批。',
    icon: 'pencil',
  },
  {
    id: 'danger-full-access',
    display_name: '完全访问',
    description: '所有工具无需审批，含网络/系统副作用。仅在可信环境使用。',
    icon: 'unlock',
  },
];

/** 每个用例一份数组（fixture 的 cwd 分支会 unshift 新行——共享数组会串味）。 */
function baseSessions() {
  return [sessionRow('s3', { id: 'p2', title: '项目 beta' })];
}

/** alpha 是**空项目**（AC9 的空态入口），beta 有一条会话。 */
function baseMock(over: Partial<ApiMock> = {}): ApiMock {
  return {
    sessions: baseSessions(),
    projects: [
      { id: 'p1', path: ALPHA, title: '项目 alpha', session_ids: [] },
      { id: 'p2', path: BETA, title: '项目 beta', session_ids: ['s3'] },
    ],
    permissionModes: REAL_PERMISSION_MODES,
    ...over,
  };
}

const project = (page: Page, title: string) =>
  page.locator('.rail-project').filter({ hasText: title });
const dialog = (page: Page) => page.locator('.project-dialog[aria-label="新建任务"]');

/** 打开某项目行的 kebab 菜单（限定在 `.rail-project-head` 里：项目块**包含**它的
 *  会话行，不加限定会同时命中每个会话行的菜单 → strict mode violation）。 */
async function openProjectMenu(page: Page, title: string) {
  await project(page, title).locator('.rail-project-head .rail-menu-btn').click();
}

/** AC9 主入口：菜单第一项 → 确认面（#367 的 TaskCreationDialog）在场。 */
async function openStartDialog(page: Page, title: string) {
  await openProjectMenu(page, title);
  await page.getByRole('menuitem', { name: '在此项目中新建任务' }).click();
  await expect(dialog(page)).toBeVisible({ timeout: 10_000 });
}

/** 捕获 POST /api/sessions 的请求（URL + 请求体——AC11 断言用）。 */
function captureSessionPosts(page: Page): { url: string; body: Record<string, unknown> }[] {
  const posts: { url: string; body: Record<string, unknown> }[] = [];
  page.on('request', (req) => {
    if (req.method() !== 'POST') return;
    if (new URL(req.url()).pathname !== '/api/sessions') return;
    try {
      posts.push({ url: req.url(), body: req.postDataJSON() as Record<string, unknown> });
    } catch {
      /* 非 JSON 体（本 spec 不该出现）：忽略而不是让监听器抛错 */
    }
  });
  return posts;
}

test('AC9/AC10 菜单第一项是入口；确认面逐字明示路径、prompt 必填、自主度三档、无权限档 picker', async ({
  page,
}) => {
  await routeApi(page, baseMock());
  await page.goto('/');

  await openProjectMenu(page, '项目 alpha');
  // 第一项就是这个入口（重命名/删除在它下面——顺序是 AC 的一部分）
  await expect(page.getByRole('menuitem').first()).toHaveText('在此项目中新建任务', { timeout: 10_000 });
  await page.getByRole('menuitem', { name: '在此项目中新建任务' }).click();

  const box = dialog(page);
  await expect(box).toBeVisible({ timeout: 10_000 });
  // AC10①：路径明示行**逐字**出现——标签与路径必须连续（#367 保留了这个安全责任）。
  const callout = box.locator('.project-path-callout');
  await expect(callout).toContainText(`Agent 将直接读写该目录：${ALPHA}`, { timeout: 10_000 });
  // AC10②（#367 反转 #204）：prompt 是唯一必填项——任务描述输入框在场，且为空时
  // 「创建任务」不可用（创建即带任务启动，空任务无从启动）。
  const prompt = box.getByLabel('任务描述');
  await expect(prompt).toBeVisible({ timeout: 10_000 });
  const create = box.getByRole('button', { name: '创建任务' });
  await expect(create).toBeDisabled();
  await prompt.fill('给登录接口加上限流');
  await expect(create).toBeEnabled({ timeout: 10_000 });
  // AC10③：自主度三档（抄 Copilot Interactive / Plan / Autopilot 的命名与语义），
  // 默认「问」。radiogroup 逐档断言存在，不靠总数。
  const autonomy = box.getByRole('radiogroup', { name: '自主度' });
  for (const opt of ['问', '计划', '自动']) {
    await expect(autonomy.getByRole('radio', { name: opt })).toBeVisible({ timeout: 10_000 });
  }
  await expect(autonomy.getByRole('radio', { name: '问' })).toBeChecked();
  // #204 的权限档 picker 已被自主度取代——弹窗里不该再有它（区分新旧两代确认面）。
  await expect(box.locator('.composer-control[aria-label="权限模式"]')).toHaveCount(0);
});

test('AC11 创建即启动：请求带 cwd/task/autonomy、默认不发 model/sandbox_backend/permission_mode，会话落组', async ({
  page,
}) => {
  await routeApi(page, baseMock());
  const posts = captureSessionPosts(page);
  await page.goto('/');

  await openStartDialog(page, '项目 alpha');
  const box = dialog(page);
  await box.getByLabel('任务描述').fill('给登录接口加上限流');
  await box.getByRole('button', { name: '创建任务' }).click();

  await expect.poll(() => posts.length).toBe(1);
  // #367 反转 #204：launch 走后端默认（创建即启动），URL 不再拼 launch=false。
  expect(new URL(posts[0].url).searchParams.get('launch')).toBeNull();
  expect(posts[0].body.cwd).toBe(ALPHA);
  expect(posts[0].body.task).toBe('给登录接口加上限流');
  expect(posts[0].body.autonomy).toBe('ask'); // 默认档 = 问
  // 坑点锁：默认不发键——模型 Auto / 运行位置 默认 / 未改权限档 ⇒ 三键都不出现。
  // （auto 的语义就是"不发键、走后端默认链"，发了就不是默认了。）
  expect('model' in posts[0].body).toBe(false);
  expect('sandbox_backend' in posts[0].body).toBe(false);
  expect('permission_mode' in posts[0].body).toBe(false);

  // AC11 落组：新会话出现在「项目 alpha」下（fixture 按真后端语义真的改状态：
  // 自动入组，所以这条断言考的是界面跟着后端走，而不是读了塞进去的假值）。
  await expect(project(page, '项目 alpha').locator('.session-item-id')).toHaveCount(1, { timeout: 10_000 });
  // 创建成功即关闭确认面（失败路径见 AC12）。
  await expect(dialog(page)).toHaveCount(0, { timeout: 10_000 });
});

test('AC11b 完成目标拼进首条 prompt；改自主度后请求带所选档', async ({ page }) => {
  await routeApi(page, baseMock());
  const posts = captureSessionPosts(page);
  await page.goto('/');

  await openStartDialog(page, '项目 alpha');
  const box = dialog(page);
  await box.getByLabel('任务描述').fill('跑一遍回归测试');
  await box.getByLabel('完成目标').fill('pytest 全绿');
  await box.getByRole('radio', { name: '自动' }).click();
  await box.getByRole('button', { name: '创建任务' }).click();

  await expect.poll(() => posts.length).toBe(1);
  // 验收项拼在任务描述前面（#367：零服务端契约变更），整串相等锁死拼法——
  // 改成别的拼接形态（顺序、分隔符）这条必须变红。
  expect(posts[0].body.task).toBe('完成目标：pytest 全绿\n\n跑一遍回归测试');
  expect(posts[0].body.autonomy).toBe('auto'); // 改档 → 请求带所选档
  expect(posts[0].body.cwd).toBe(ALPHA);
});

test('AC12 422 留在确认面：后端 detail 原样可见、不关对话框、无全局横幅、重试即成功', async ({
  page,
}) => {
  const mock = baseMock({
    cwdSessionError: { status: 422, detail: `目录不存在：${ALPHA}` },
  });
  await routeApi(page, mock);
  await page.goto('/');

  await openStartDialog(page, '项目 alpha');
  const box = dialog(page);
  await box.getByLabel('任务描述').fill('给登录接口加上限流');
  await box.getByRole('button', { name: '创建任务' }).click();

  // 后端 detail 原样出现在浮层里——submitTask 的 ownError 通道把失败原因交回弹窗
  // （前缀「提交失败：」是 submitTask 的统一文案；「创建会话失败：」是旧空会话流程
  // 的前缀，随 #367 退场）。用 toHaveText（整串相等）：后半段一旦被改写（哪怕改成
  // 更"友好"的话）这条断言必须变红。
  await expect(box.locator('.project-error')).toHaveText(`提交失败：目录不存在：${ALPHA}`, { timeout: 10_000 });
  await expect(box).toBeVisible({ timeout: 10_000 });
  await expect(page.locator('.app-error')).toHaveCount(0); // 不弹全局横幅

  // 可重试：失败原因解除后同一条路径成功 → 落组 + 关闭。
  mock.cwdSessionError = undefined;
  await box.getByRole('button', { name: '创建任务' }).click();
  await expect(project(page, '项目 alpha').locator('.session-item-id')).toHaveCount(1, { timeout: 10_000 });
  await expect(box).toHaveCount(0, { timeout: 10_000 });
});

test('AC9 空项目占位区替换为入口按钮（旧的"去未分组找 cwd 匹配会话"文案不再出现）', async ({
  page,
}) => {
  await routeApi(page, baseMock());
  await page.goto('/');

  const alpha = project(page, '项目 alpha');
  await expect(alpha).not.toContainText('从未分组会话的');
  const entry = alpha.locator('.rail-empty-action');
  await expect(entry).toHaveText('在此项目中新建任务 →', { timeout: 10_000 });

  await entry.click();
  await expect(dialog(page)).toBeVisible({ timeout: 10_000 });
  await expect(dialog(page).locator('.project-path-callout')).toContainText(ALPHA, { timeout: 10_000 });
});
