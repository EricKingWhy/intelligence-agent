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
 *
 *  #1368 复审补强（correctness P1/P3 + standards P1）：流式档的控件让位（Esc 提示
 *  在场时动作簇更宽）、流式中 Escape 只关抽屉不打断 run、断点穿越后抽屉复位、
 *  桌面档空态按钮不折行。流式夹具复用 `fixtures.ts` 的 `routeStreaming`。
 */
import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  AGENT_PROFILES,
  MODELS,
  PERMISSION_MODES,
  REASONING_EFFORTS,
  openMore,
  routeApi,
  routeStreaming,
  rowOf,
  sessionRow,
  submitTask,
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

/** 折叠轨宽上界（56px 轨 + 内边距；取 80 作宽松判据）与展开抽屉宽下界。 */
const COLLAPSED_MAX = 80;
const EXPANDED_MIN = 200;

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

/** 控件行的**滚动视口**必须在动作簇左侧结束（`margin` 让位）。这条比几何相交更强：
 *  相交只说明"此刻滚到了哪里"，而这条说明"任何滚动位置都不会压"——按 DOM 结构，
 *  溢出的 pill 只会画在控件行内，而这一行的右缘本身就在簇左缘之左，所以逐 pill
 *  的重叠面积恒为 0，不需要再算一遍（复审 P3：那条循环在结构断言成立时数学上
 *  不可能失败，留着会被误读成第二道独立防线）。 */
async function expectControlsClearOfActions(page: Page, label: string) {
  const actions = (await page.locator('.composer-actions').boundingBox())!;
  const cbox = (await page.locator('.composer-controls').boundingBox())!;
  expect(Math.round(cbox.x + cbox.width), label).toBeLessThanOrEqual(Math.round(actions.x));
  return { actions, cbox };
}

test.describe('#1368 390：会话抽屉（P1-1 / P1-4）', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('折叠轨只留图标；抽屉展开后会话名横排可读，点会话切换并收起', async ({ page }) => {
    await routeApi(page, mock);
    await page.goto('/');

    const rail = page.locator('.session-rail');
    const toggle = page.locator('.rail-drawer-btn');

    // ── 折叠态：56px 轨道，会话正文不渲染（P1-1 的"消失"在这一档是刻意的降级）──
    expect(await railWidth(rail)).toBeLessThanOrEqual(COLLAPSED_MAX);
    await expect(toggle).toBeVisible();
    expect(await isVisible(page.locator('.session-item-body').first())).toBe(false);
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');

    // ── 展开态：名字必须**可读**——可见、单行、宽度够四个字以上 ──
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(await railWidth(rail)).toBeGreaterThanOrEqual(EXPANDED_MIN);

    const titles = page.locator('.session-item-title');
    await expect(titles.first()).toBeVisible();
    expect(await titles.first().innerText()).not.toBe('');
    // 竖排 = 高度是多行而每行只容一个字。单行是"横向可读"的机械判据。
    expect(await textLines(titles.first())).toBeLessThanOrEqual(1.5);
    expect((await titles.first().boundingBox())!.width).toBeGreaterThan(60);

    // ── 点会话：选中态落到点击的那一行，抽屉自动收起（覆盖层不能挡住刚选中的内容）──
    await rowOf(page, 's2').locator('.session-item').click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(await railWidth(rail)).toBeLessThanOrEqual(COLLAPSED_MAX);
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
      expect(await railWidth(rail)).toBeGreaterThanOrEqual(EXPANDED_MIN);
    };

    await open();
    await page.keyboard.press('Escape');
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(await railWidth(rail)).toBeLessThanOrEqual(COLLAPSED_MAX);

    await open();
    // 抽屉宽 ≤272 且靠左：取轨右缘外 20px 的同一行——一定落在遮罩上，且不靠近
    // 视口边缘（不依赖硬编码的 370/400，视口一变这条就静默点空）。
    const box = (await rail.boundingBox())!;
    await page.mouse.click(box.x + box.width + 20, box.y + 200);
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(await railWidth(rail)).toBeLessThanOrEqual(COLLAPSED_MAX);
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

  test('断点穿越后抽屉复位：开 → 拉宽到桌面 → 缩回窄档时仍是收起态（P3）', async ({ page }) => {
    await routeApi(page, mock);
    await page.goto('/');

    const rail = page.locator('.session-rail');
    const toggle = page.locator('.rail-drawer-btn');
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');

    await page.setViewportSize({ width: 1280, height: 900 });
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(rail).not.toHaveClass(/rail-drawer-open/);

    await page.setViewportSize({ width: 390, height: 844 });
    // 判别面：不复位时这里会自己开着（复审实测正是如此）。
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(rail).not.toHaveClass(/rail-drawer-open/);
    expect(await railWidth(rail)).toBeLessThanOrEqual(COLLAPSED_MAX);

    // 复位不是把状态锁死：窄档里开关照常可用
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  });
});

test.describe('#1368 390：控件排版（P1-2 / P1-3 / P2-3）', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('composer 控件行横向滚动，动作簇不压任何 pill（P1-2）', async ({ page }) => {
    await routeApi(page, mock);
    await page.goto('/');

    const controls = page.locator('.composer-controls');
    await expect(controls).toBeVisible();
    // #1405：先展开「更多」，让控件行真的包含 Agent / 推理后再量溢出。
    await openMore(page);
    expect(await controls.evaluate((el) => getComputedStyle(el).flexWrap)).toBe('nowrap');
    // 真的有可滚内容才谈"横向滚动"（否则这条断言是恒真的装饰）
    expect(await controls.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);

    const { actions } = await expectControlsClearOfActions(page, '控件行右缘 ≤ 动作簇左缘');
    // 非流式簇只有发送 + 附图：用 compact token（80 = 76 + 4 余量），不是流式那条。
    expect(actions.width).toBeLessThanOrEqual(80);
  });

  test('流式档（Esc 提示 + 停止按钮）控件行同样让位（P1-1）', async ({ page }) => {
    await routeStreaming(page);
    await page.goto('/');
    await submitTask(page, '跑一下');
    await expect(page.locator('.composer-esc-hint')).toBeVisible();
    await expect(page.locator('.composer-stop')).toBeVisible();
    await openMore(page);

    // base 那条 `padding-right` 在窄屏必须被 margin 取代（两条叠加实测压住「权限」
    // pill 67px）；这条断言把"归零"钉在计算值上，而不是只看几何结果。
    expect(
      await page.locator('.composer-controls').evaluate((el) => getComputedStyle(el).paddingRight),
    ).toBe('0px');

    const { actions } = await expectControlsClearOfActions(page, '流式档控件行右缘 ≤ 动作簇左缘');
    // 判别面：动作簇确实因为 Esc 提示更宽（否则这条用例可能只是在量非流式那一档）。
    expect(actions.width).toBeGreaterThan(100);
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

  test('顶栏只留 logo：产品名不渲染、顶栏不再被折行撑高（P2-3）', async ({ page }) => {
    await routeApi(page, mock);
    await page.goto('/');

    expect(await isVisible(page.locator('.appbar-title'))).toBe(false);
    const appbar = page.locator('.appbar');
    const box = await appbar.evaluate((el) => ({
      h: Math.round(el.getBoundingClientRect().height),
    }));
    // 高度就是"产品名不再折行"的机械读数：改前三行把顶栏撑到 67px（Lane A 实测），
    // 单行是 48px。**不断言 `.appbar` 整体的 scrollWidth**：右侧图标簇在 390 本来
    // 就溢出（P2-1 顶栏信息过载，本票范围外），把它写进这里等于要求本票修另一个问题。
    expect(box.h).toBeLessThanOrEqual(60);
    await expect(page.locator('.appbar-logo')).toBeVisible();
  });
});

test.describe('#1368 390：流式中 Escape 归属', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('Esc 只关抽屉，不打断在途 run（P2）', async ({ page }) => {
    let cancels = 0;
    await routeStreaming(page);
    // 后注册的 route 先匹配 ⇒ 这条盖住 routeApi 的缺省处理（与 #1195 同手法）
    await page.route('**/api/sessions/*/cancel', async (route) => {
      cancels += 1;
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"ok"}' });
    });

    await page.goto('/');
    await submitTask(page, '跑一下');
    await expect(page.locator('.composer-esc-hint')).toBeVisible();

    const toggle = page.locator('.rail-drawer-btn');
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');

    await page.keyboard.press('Escape');
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect.poll(() => cancels, { message: '关抽屉的 Esc 不得打断在途 run' }).toBe(0);

    // 判别面：抽屉已关，同一个键必须回到「中断」语义——守卫只是让路，不是把
    // Esc 中断整个一刀切掉。
    await page.keyboard.press('Escape');
    await expect.poll(() => cancels, { message: '抽屉已关的 Esc 必须照常 POST /cancel' }).toBe(1);
  });
});

test.describe('#1368 768：会话仍可切换', () => {
  test.use({ viewport: { width: 768, height: 900 } });

  test('抽屉在 768 同样可用，点会话完成切换', async ({ page }) => {
    await routeApi(page, mock);
    await page.goto('/');

    const rail = page.locator('.session-rail');
    const toggle = page.locator('.rail-drawer-btn');
    expect(await railWidth(rail)).toBeLessThanOrEqual(COLLAPSED_MAX);
    expect(await isVisible(page.locator('.session-item-body').first())).toBe(false);

    await toggle.click();
    expect(await railWidth(rail)).toBeGreaterThanOrEqual(EXPANDED_MIN);
    const row = rowOf(page, 's3');
    await expect(row.locator('.session-item-title')).toBeVisible();
    await row.locator('.session-item').click();
    await expect(page.locator('.session-item.selected')).toHaveAttribute('title', /s3/);
    expect(await railWidth(rail)).toBeLessThanOrEqual(COLLAPSED_MAX);
  });
});

test.describe('#1368 1280：桌面档空态按钮不折行（standards P1）', () => {
  test.use({ viewport: { width: 1280, height: 900 } });

  test('桌面 240px 轨里「图标 + 文字」各自单行、按钮不被撑高', async ({ page }) => {
    await routeApi(page, { sessions: [], projects: [] });
    await page.goto('/');

    // 390 的抽屉里文字放得下，这两枚按钮的折行**只在桌面档出现**——所以判据必须
    // 在 1280 量（复审实测：改前 83×38 / 74×40、标签各 2 行）。
    for (const cls of ['.rail-empty-btn-primary', '.rail-empty-btn-ghost']) {
      const btn = page.locator(cls);
      await expect(btn).toBeVisible();
      const label = btn.locator('.rail-empty-btn-label');
      await expect(label).toBeVisible();
      expect(await textLines(label), `${cls} 的标签必须单行`).toBeLessThanOrEqual(1.5);
      expect((await btn.boundingBox())!.height, `${cls} 不得被两行标签撑高`).toBeLessThanOrEqual(32);
    }
  });
});

test.describe('#1368 620：顶栏中间档（P2）', () => {
  test.use({ viewport: { width: 620, height: 900 } });

  test('标题省略，右簇完整留在顶栏内', async ({ page }) => {
    await routeApi(page, mock);
    await page.goto('/');

    // 改前实测（同树 A/B）：nowrap 的标题 min-content 163px 不可收缩，右簇被推到
    // appbar 之外（620 档 2 个图标完全在视口外，`.app-frame{overflow:hidden}` 直接
    // 裁掉，按钮不可点）。修法是左组 `min-width:0` + 标题省略号。
    const m = await page.evaluate(() => {
      const bar = document.querySelector('.appbar')!;
      const right = document.querySelector('.appbar-right')!.getBoundingClientRect();
      const title = document.querySelector('.appbar-title')!;
      return {
        barRight: Math.round(bar.getBoundingClientRect().right),
        rightRight: Math.round(right.right),
        titleWidth: Math.round(title.getBoundingClientRect().width),
        scrollWidth: bar.scrollWidth,
        clientWidth: bar.clientWidth,
      };
    });
    expect(m.rightRight, '右簇右缘不得越过顶栏右缘').toBeLessThanOrEqual(m.barRight);
    expect(m.scrollWidth, '顶栏不得横向溢出').toBeLessThanOrEqual(m.clientWidth);
    // 判别面：标题此刻确实是**被省略**的（整串 163px，620 档只剩 ~66px）——否则
    // 这条用例在"标题整个放得下"的布局里也绿，测不到收缩机制本身。
    expect(m.titleWidth).toBeLessThan(120);
    expect(m.titleWidth).toBeGreaterThan(0);
  });
});
