/** 场景 U（WS-6 / #169 前端半；#204 裁定收窄）：项目内新建会话——入口 → 确认面 →
 *  launch=false 创建 → 落组 + 权限 pill 一致。
 *
 *  契约来源：`docs/design/WEB_UI_BATCH_REDESIGN.md` §5（#204 裁定）+ 后端
 *  `web/app.py` launch=false 分支。本 spec 锁可在无后端环境确定复现的部分：
 *  - AC9 项目行 kebab 第一项 =「在此项目中新建任务」；空项目占位区替换为该入口按钮；
 *  - AC10 确认面逐字出现「Agent 将直接读写该目录：<路径>」+ 权限档三选；
 *    **没有「任务内容」输入框**（#204 裁定 §1：弹窗职责收窄为选目录 + 设默认
 *    权限 + 创建会话）；
 *  - AC11 创建体带 `cwd`、**不带 task**、不带 `launch`（query 参数在 URL 上）；
 *    **默认档不发 `permission_mode`**（后端"显式传非 danger 档 → 切交互式审批"）；
 *  - AC11b 权限一致性（#204 裁定 §3，**#236 起换源**）：创建后 composer 权限 pill
 *    必须显示该会话的档位（会话真值 = `session/started` 投影）。夹具把同一个值同时
 *    注入回执与事件（真后端同源），所以这里锁的是**用户可见结果**；「pill 到底读哪条
 *    通道」的判别性锁在 `control-row.spec.ts`（那条路径没有创建回执）；
 *  - AC12 422 留在确认面：不关对话框、不打全局横幅、后端 detail 原样可见、可重试；
 *  - AC13 以上全部由本 spec 覆盖（mock 语义按真后端 launch=false，见 fixtures）。
 *
 *  权限档夹具用**真后端的三个 id/显示名**（`tooling/contract.py::PERMISSION_MODE_DESCRIPTIONS`）。
 *  车道：Playwright e2e + page.route（同 r-project-groups.spec.ts 约定），`--workers=2`。
 */

import { expect, test, type Page } from '@playwright/test';
import { routeApi, sessionRow, type ApiMock } from './fixtures';

const ALPHA = 'D:/repos/alpha';
const BETA = 'D:/repos/beta';

/** 真后端 GET /api/permission-modes 的三档（id 是封闭枚举，未知值 → 422）。
 *  `icon` 是 #214 起的契约字段（语义名，恒在；未声明时 null）——这里照抄真值，
 *  否则弹窗那条 picker 的图标槽永远空着，AC4 在第四个调用点上就没法断言。 */
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
const dialog = (page: Page) => page.locator('.project-dialog[aria-label="在此项目中新建任务"]');

/** 打开某项目行的 kebab 菜单（限定在 `.rail-project-head` 里：项目块**包含**它的
 *  会话行，不加限定会同时命中每个会话行的菜单 → strict mode violation）。 */
async function openProjectMenu(page: Page, title: string) {
  await project(page, title).locator('.rail-project-head .rail-menu-btn').click();
}

/** AC9 主入口：菜单第一项 → 确认面在场。 */
async function openStartDialog(page: Page, title: string) {
  await openProjectMenu(page, title);
  await page.getByRole('menuitem', { name: '在此项目中新建任务' }).click();
  await expect(dialog(page)).toBeVisible();
}

/** 捕获 POST /api/sessions 的请求（URL + 请求体——launch 在 URL 上，AC13 断言用）。 */
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

test('AC9/AC10 菜单第一项是入口；确认面逐字明示路径、权限档默认工作区写入、没有任务输入框', async ({
  page,
}) => {
  await routeApi(page, baseMock());
  await page.goto('/');

  await openProjectMenu(page, '项目 alpha');
  // 第一项就是这个入口（重命名/删除在它下面——顺序是 AC 的一部分）
  await expect(page.getByRole('menuitem').first()).toHaveText('在此项目中新建任务');
  await page.getByRole('menuitem', { name: '在此项目中新建任务' }).click();

  const box = dialog(page);
  await expect(box).toBeVisible();
  // AC10①：路径明示行**逐字**出现——标签与路径必须连续。
  const callout = box.locator('.project-path-callout');
  await expect(callout).toContainText(`Agent 将直接读写该目录：${ALPHA}`);
  // #204 裁定 §1：「任务内容」输入框**不存在**（弹窗职责收窄）。
  await expect(box.getByLabel('任务内容')).toHaveCount(0);
  // AC10②：权限档是**三选**（FE-R11-05 之后单选 picker 多了一个首项「默认（未选）」，
  // 所以这里是 1 + 3。三档**逐档**断言存在，不靠总数。）
  await page.locator('.project-dialog .composer-control[aria-label="权限模式"]').click();
  const listbox = page.locator('[role="listbox"]:visible').last();
  await expect(listbox.getByRole('option')).toHaveCount(4);
  for (const mode of ['只读', '工作区写入', '完全访问']) {
    await expect(listbox.getByRole('option', { name: mode })).toBeVisible();
  }
  // #214 AC4：这一处调用点（`StartTaskInProjectDialog.tsx` 的 `toCatalogOptions`）也必须
  // 传 `catalogIcon`——漏传就是静默空槽（本批 REVIEW 时正是这处漏了）。三档都声明了
  // `icon` ⇒ 每个选项行的图标槽里必须有 svg；判据只看"槽里有字形"，不认具体字形
  // （换一个 lucide 图标不算回归）。槽仍恒渲染：首行「默认（未选）」不是目录条目，
  // 它一直是空槽。
  for (const mode of ['只读', '工作区写入', '完全访问']) {
    await expect(
      listbox.getByRole('option', { name: mode }).locator('.picker-item-icon svg'),
    ).toHaveCount(1);
  }
  await expect(listbox.getByRole('option', { name: '默认（未选）' }).locator('.picker-item-icon svg')).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(page.locator('.project-dialog .composer-control[aria-label="权限模式"]')).toContainText('工作区写入');
  // #204 裁定 §1：提交按钮可用（没有任务内容可判空，只看 pending），文案是「创建会话」。
  await expect(box.getByRole('button', { name: '创建会话' })).toBeEnabled();
});

test('AC11 launch=false 创建：请求带 cwd、不带 task、URL 带 launch=false，会话落组 + 权限 pill 一致', async ({
  page,
}) => {
  const mock = baseMock({
    // 生效档位与请求不同（模拟后端归一化/接管）。夹具把 read-only 同时写进回执与
    // `session/started`（真后端同源），所以本条只锁**用户可见结果**：pill 显示 read-only。
    // "pill 读事件还是读回执"的判别性锁在 control-row.spec.ts（那条路径没有创建回执）。
    emptySessionPermissionOverride: 'read-only',
  });
  await routeApi(page, mock);
  const posts = captureSessionPosts(page);
  await page.goto('/');

  await openStartDialog(page, '项目 alpha');
  await dialog(page).getByRole('button', { name: '创建会话' }).click();

  await expect.poll(() => posts.length).toBe(1);
  // launch=false 在 URL query 上（后端 `launch: bool = True` 的默认值不碰既有契约）。
  expect(new URL(posts[0].url).searchParams.get('launch')).toBe('false');
  expect(posts[0].body.cwd).toBe(ALPHA);
  // #204 裁定 §2：launch=false 的请求体**没有 task**（矛盾组合 = 422）。
  expect('task' in posts[0].body).toBe(false);
  // 坑点锁（PRD §4.3）：默认档必须"不发键"——发了就是切交互式审批，
  // 每次工具调用都会弹审批卡。这条断言红了不要改 mock，要改的是那段提交逻辑。
  expect('permission_mode' in posts[0].body).toBe(false);

  // AC11 落组：新会话出现在「项目 alpha」下。
  await expect(project(page, '项目 alpha').locator('.session-item-id')).toHaveCount(1);
  // AC11b（#204 裁定 §3，**#236 起换源**）：pill 必须显示该会话的档位（read-only）。
  // 夹具把 read-only 同时写进回执与 `session/started`（真后端同源），所以这条锁的是
  // 用户可见结果；"pill 读事件还是读回执"的判别性锁在 control-row.spec.ts（无回执路径）。
  await expect(page.locator('.composer-dock .composer-control[aria-label="权限模式"]')).toContainText('只读');
  // 确认面关闭：用户接下来要在 chat 输入框发第一条消息（不自动发起 run）。
  await expect(dialog(page)).toHaveCount(0);
  // #204 裁定 §1：焦点落到 chat 输入框——用户立刻可以打字（createEmptySession
  // 成功路径的 focus()）。
  await expect(page.locator('#composer-input')).toBeFocused();
});

test('AC11 主动改档才发 permission_mode；创建后 pill 与弹窗选择一致', async ({ page }) => {
  await routeApi(page, baseMock());
  const posts = captureSessionPosts(page);
  await page.goto('/');

  await openStartDialog(page, '项目 alpha');
  const box = dialog(page);
  await page.locator('.project-dialog .composer-control[aria-label="权限模式"]').click();
  // `:visible` 限定当前打开的浮层：关闭动画期间上一层 listbox 仍在 DOM 里。
  const listbox = page.locator('[role="listbox"]:visible').last();
  await expect(listbox).toBeVisible();
  await listbox.getByRole('option', { name: /只读/ }).click();
  await expect(page.locator('.project-dialog .composer-control[aria-label="权限模式"]')).toContainText('只读');

  await box.getByRole('button', { name: '创建会话' }).click();

  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].body.permission_mode).toBe('read-only'); // 改档 → 显式发键（切交互式审批）
  expect(posts[0].body.cwd).toBe(ALPHA);
  // #236：pill 显示该会话的档位（read-only）——真值来自 `session/started` 投影。
  await expect(page.locator('.composer-dock .composer-control[aria-label="权限模式"]')).toContainText('只读');
});

test('AC12 422 留在确认面：后端 detail 原样可见、不关对话框、无全局横幅、重试即成功', async ({
  page,
}) => {
  const mock = baseMock({
    emptySessionError: { status: 422, detail: `目录不存在：${ALPHA}` },
  });
  await routeApi(page, mock);
  await page.goto('/');

  await openStartDialog(page, '项目 alpha');
  const box = dialog(page);
  await box.getByRole('button', { name: '创建会话' }).click();

  // 后端 detail 原样出现在浮层里——**不是** Composer 那条 422 旧语义。
  // 用 toHaveText（整串相等）：前缀是「创建会话失败：」+ 后端原文，后半段一旦被
  // 改写（哪怕改成更"友好"的话）这条断言必须变红。
  await expect(box.locator('.project-error')).toHaveText(`创建会话失败：目录不存在：${ALPHA}`);
  await expect(box).toBeVisible();
  await expect(page.locator('.app-error')).toHaveCount(0); // 不弹全局横幅

  // 可重试：失败原因解除后同一条路径成功 → 落组 + 关闭。
  mock.emptySessionError = undefined;
  await box.getByRole('button', { name: '创建会话' }).click();
  await expect(project(page, '项目 alpha').locator('.session-item-id')).toHaveCount(1);
  await expect(box).toHaveCount(0);
});

test('AC9 空项目占位区替换为入口按钮（旧的"去未分组找 cwd 匹配会话"文案不再出现）', async ({
  page,
}) => {
  await routeApi(page, baseMock());
  await page.goto('/');

  const alpha = project(page, '项目 alpha');
  await expect(alpha).not.toContainText('从未分组会话的');
  const entry = alpha.locator('.rail-empty-action');
  await expect(entry).toHaveText('在此项目中新建任务 →');

  await entry.click();
  await expect(dialog(page)).toBeVisible();
  await expect(dialog(page).locator('.project-path-callout')).toContainText(ALPHA);
});
