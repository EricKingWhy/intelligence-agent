/** #1368（P0-4）：窄视口（≤820px）的会话抽屉与控件排版回归。
 *
 *  这套断言的**用户可见症状**（Lane A 审计 P1-1 / P1-2 / P1-3 / P1-4、P2-3）：
 *  1. 56px 图标轨里会话名整体消失，用户看不到自己有哪些会话、也无法切换；
 *  2. 图标轨里按钮文字被挤成逐字竖排（新 / 建 / 项 / 目）；
 *  3. 390 下 composer「Agent」pill 与附件/发送按钮重叠；
 *  4. 记忆卡片「详细信息」被挤成竖向单字一列；
 *  5. 390 顶栏产品名折行撑高。
 *
 *  判据取**可机械测量**的量（宽度、行数、矩形相交面积），不比对截图：
 *  截图会随主题/字体漂移，而"名字是横的、控件不重叠、抽屉能开能关"是产品契约。
 *
 *  ⚠ 抽屉开关是**呈现层**：展开/收起不改会话数据流。所以"切换会话"这条必须真的
 *  点中行并断言选中态，不能只断言 aria 属性——否则一个只改 aria 不改行为的实现
 *  也会绿。
 */
import { expect, test, type Locator } from '@playwright/test';
import {
  AGENT_PROFILES,
  MODELS,
  PERMISSION_MODES,
  REASONING_EFFORTS,
  routeApi,
  sessionRow,
  type ProjectFixture,
} from './fixtures';

const P1: ProjectFixture = { id: 'p1', path: 'D:/repos/alpha', title: '项目 alpha', session_ids: ['s2', 's1'] };
const P2: ProjectFixture = { id: 'p2', path: 'D:/repos/beta', title: '项目 beta', session_ids: ['s3'] };

/** 三条会话各自带 first_user_message：会话行标题就是它（`session-item-title`）。 */
const sessions = () => [
  sessionRow('s1', { id: 'p1', title: '项目 alpha' }, { first_user_message: '修复登录页的重定向循环' }),
  sessionRow('s2', { id: 'p1', title: '项目 alpha' }, { first_user_message: '给事件流加上重放能力' }),
  sessionRow('s3', { id: 'p2', title: '项目 beta' }, { first_user_message: '排查 sandbox 超时' }),
];

const mock = { sessions: sessions(), projects: [P1, P2], models: MODELS, permissionModes: PERMISSION_MODES, agentProfiles: AGENT_PROFILES, reasoningEfforts: REASONING_EFFORTS };

const railWidth = async (rail: Locator) => Math.round((await rail.boundingBox())!.width);

/** 元素是否被 CSS 藏起来（display:none / 零尺寸）。`toBeHidden()` 对"父级被藏"
 *  同样成立，这里用它断言"图标轨里不渲染文字"，不关心是哪条规则藏掉的。 */
const isVisible = (locator: Locator) => locator.evaluate((el) => {
  const cs = getComputedStyle(el);
  if (cs.display === 'none' || cs.visibility === 'hidden') return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
});

/** 文本占了几行（按**计算出的行高**，不是硬编码像素：字号随 token 变化）。 */
const textLines = (locator: Locator) => locator.evaluate((el) => {
  const cs = getComputedStyle(el);
  const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.4;
  return Math.round((el.getBoundingClientRect().height / lh) * 10) / 10;
});

test.describe('#1368 390：会话抽屉（P1-1 / P1-4）', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('折叠轨只留图标；抽屉展开后会话名横排可读，点会话切换并收起', async ({ page }) => {
    await routeApi(page, mock);
    await page.goto('/');

    const rail = page.locator('.session-rail');
    const toggle = page.locator('.rail-drawer-btn');

    // ── 折叠态：56px 轨道，会话正文不渲染（P1-1 的"消失"在这一档是刻意的降级）──
    expect(await railWidth(rail)).toBeLessThanOrEqual(80);
    await expect(toggle).toBeVisible();
    expect(await isVisible(page.locator('.session-item-body').first())).toBe(false);
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');

    // ── 展开态：名字必须**可读**——可见、单行、宽度够四个字以上 ──
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(await railWidth(rail)).toBeGreaterThanOrEqual(200);

    const titles = page.locator('.session-item-title');
    await expect(titles.first()).toBeVisible();
    expect(await titles.first().innerText()).not.toBe('');
    // 竖排 = 高度是多行而每行只容一个字。单行是"横向可读"的机械判据。
    expect(await textLines(titles.first())).toBeLessThanOrEqual(1.5);
    expect((await titles.first().boundingBox())!.width).toBeGreaterThan(60);

    // ── 点会话：选中态落到点击的那一行，抽屉自动收起（覆盖层不能挡住刚选中的内容）──
    const row = page.locator('.session-row').filter({ has: page.locator('.session-item-id', { hasText: 's2' }) });
    await row.locator('.session-item').click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(await railWidth(rail)).toBeLessThanOrEqual(80);
    await expect(page.locator('.session-item.selected')).toHaveAttribute('title', /s2/);
  });

  test('Escape 与点抽屉外都能收起', async ({ page }) => {
    await routeApi(page, mock);
    await page.goto('/');

    const rail = page.locator('.session-rail');
    const toggle = page.locator('.rail-drawer-btn');
    const open = async () => {
      await toggle.click();
      await expect(toggle).toHaveAttribute('aria-expanded', 'true');
      expect(await railWidth(rail)).toBeGreaterThanOrEqual(200);
    };

    await open();
    await page.keyboard.press('Escape');
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(await railWidth(rail)).toBeLessThanOrEqual(80);

    await open();
    // 抽屉宽 ≤272 且靠左：370,400 一定在抽屉之外（不给"点遮罩"造第二个元素）
    await page.mouse.click(370, 400);
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(await railWidth(rail)).toBeLessThanOrEqual(80);
  });

  test('空态按钮只留图标：文字不渲染，因此不可能竖排（P1-4）', async ({ page }) => {
    await routeApi(page, { sessions: [], projects: [] });
    await page.goto('/');

    const primary = page.locator('.rail-empty-btn-primary');
    await expect(primary).toBeVisible();
    expect(await isVisible(page.locator('.rail-empty-btn-label').first())).toBe(false);
    // 图标态是方形命中区（文字在展开的抽屉里仍显示，见下一条断言之外的契约）
    const box = (await primary.boundingBox())!;
    expect(box.width).toBeLessThanOrEqual(40);

    // 展开抽屉后文字回来：空态指引「新建项目」必须能在窄视口被读到
    await page.locator('.rail-drawer-btn').click();
    await expect(primary.locator('.rail-empty-btn-label')).toBeVisible();
    expect(await textLines(primary.locator('.rail-empty-btn-label'))).toBeLessThanOrEqual(1.5);
  });
});

test.describe('#1368 390：控件排版（P1-2 / P1-3 / P2-3）', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('composer 控件行横向滚动，动作簇不压任何 pill（P1-2）', async ({ page }) => {
    await routeApi(page, mock);
    await page.goto('/');

    const controls = page.locator('.composer-controls');
    await expect(controls).toBeVisible();
    expect(await controls.evaluate((el) => getComputedStyle(el).flexWrap)).toBe('nowrap');
    // 真的有可滚内容才谈"横向滚动"（否则这条断言是恒真的装饰）
    expect(await controls.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);

    const actions = (await page.locator('.composer-actions').boundingBox())!;
    const controls$ = await controls.locator('> *').all();
    expect(controls$.length).toBeGreaterThan(0);
    for (const item of controls$) {
      const r = (await item.boundingBox())!;
      const w = Math.max(0, Math.min(actions.x + actions.width, r.x + r.width) - Math.max(actions.x, r.x));
      const h = Math.max(0, Math.min(actions.y + actions.height, r.y + r.height) - Math.max(actions.y, r.y));
      expect(Math.round(w * h), 'pill 与动作簇的重叠面积').toBe(0);
    }
  });

  test('记忆卡「详细信息」横排不折行（P1-3）', async ({ page }) => {
    await routeApi(page, {
      ...mock,
      memories: [
        {
          id: 'm1',
          root_id: 'm1',
          version: 1,
          content: '用户偏好：所有 UI 文案用中文，日期用 ISO 格式，不要美式 AM/PM。',
          scope: 'user_global',
          tier: 'profile',
          status: 'active',
          kind: 'semantic',
          project_id: null,
          source_type: 'automatic',
          source_session_id: 's1',
          source_event_ids: ['e1'],
          payload: { kind: 'semantic', subject: '用户', fact: '用中文', category: 'preference' },
          metadata: {},
        },
      ],
    });
    await page.goto('/');

    // 记忆入口在 390 的顶栏图标簇里、被挤出可视区（P2-1 的遗留问题，不在本票范围），
    // 所以按 DOM 触发——本用例只要面板内的排版证据，不要"入口是否可点"的证据。
    await page.evaluate(() => document.querySelector<HTMLElement>('[aria-label="记忆管理"]')?.click());
    await page.locator('.memory-panel').waitFor();

    const details = page.locator('.memory-row-head .memory-expand').last();
    await expect(details).toBeVisible();
    await expect(details).toContainText('详细信息');
    const span = details.locator('span');
    expect(await textLines(span)).toBeLessThanOrEqual(1.5);
    expect((await span.boundingBox())!.width).toBeGreaterThan(30);
  });

  test('顶栏只留 logo：产品名不渲染，顶栏不横向溢出（P2-3）', async ({ page }) => {
    await routeApi(page, mock);
    await page.goto('/');

    expect(await isVisible(page.locator('.appbar-title'))).toBe(false);
    const appbar = page.locator('.appbar');
    const box = await appbar.evaluate((el) => ({
      scrollW: el.scrollWidth,
      clientW: el.clientWidth,
      h: Math.round(el.getBoundingClientRect().height),
    }));
    expect(box.scrollW).toBeLessThanOrEqual(box.clientW + 1);
    expect(box.h).toBeLessThanOrEqual(60);
    await expect(page.locator('.appbar-logo')).toBeVisible();
  });
});

test.describe('#1368 768：会话仍可切换', () => {
  test.use({ viewport: { width: 768, height: 900 } });

  test('抽屉在 768 同样可用，点会话完成切换', async ({ page }) => {
    await routeApi(page, mock);
    await page.goto('/');

    const rail = page.locator('.session-rail');
    const toggle = page.locator('.rail-drawer-btn');
    expect(await railWidth(rail)).toBeLessThanOrEqual(80);
    expect(await isVisible(page.locator('.session-item-body').first())).toBe(false);

    await toggle.click();
    expect(await railWidth(rail)).toBeGreaterThanOrEqual(200);
    const row = page.locator('.session-row').filter({ has: page.locator('.session-item-id', { hasText: 's3' }) });
    await expect(row.locator('.session-item-title')).toBeVisible();
    await row.locator('.session-item').click();
    await expect(page.locator('.session-item.selected')).toHaveAttribute('title', /s3/);
    expect(await railWidth(rail)).toBeLessThanOrEqual(80);
  });
});
