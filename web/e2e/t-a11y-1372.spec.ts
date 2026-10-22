/** #1372 验收回归：键盘可达 / 重放焦点归还 / tooltip 覆盖。
 *
 *  票面验收三条（必须真实 DOM 断言，不接受口述）：
 *  1. 9 会话、详细密度、Inspector 默认展开；允许 region 跳转键，首焦点到 composer
 *     ≤10 次按键。纯 Tab 路径只记录、不设阈值。
 *  2. 重放弹窗 Esc 后焦点回到触发按钮（行内 ⋯，而不是 body）。
 *  3. tooltip 全覆盖：顶栏 5 个（保留 4 + 更多）、rail 4 图标、会话行 ⋯、
 *     Inspector 统计行 6 个 tab、composer 5 chip、会话名下拉箭头。
 */

import { expect, type Locator, type Page, test } from '@playwright/test';
import {
  AGENT_PROFILES,
  MODELS,
  PERMISSION_MODES,
  REASONING_EFFORTS,
  T,
  clickTopBarMenuItem,
  routeApi,
  sessionRow,
  toggleReusableRule,
} from './fixtures';

const SESSIONS = Array.from({ length: 9 }, (_, i) =>
  sessionRow(`s-1372-${String(i).padStart(2, '0')}`, null, {
    first_user_message: `会话 ${i}`,
    event_count: i + 1,
  }),
);

const EVENTS = [
  { type: 'session/started', seq: 1, session_id: 's-1372-00', run_id: 'r1', time: T },
  { type: 'run/started', seq: 2, session_id: 's-1372-00', run_id: 'r1', time: T },
  {
    type: 'user/message',
    data: { content: '键盘可达性验收' },
    seq: 3, session_id: 's-1372-00', run_id: 'r1', step_id: 1, time: T,
  },
  { type: 'run/completed', data: {}, seq: 4, session_id: 's-1372-00', run_id: 'r1', time: T },
];

/** 默认条目挂 reasoning 能力：composer 的第 5 个 chip（推理档）才会渲染
 *  ——未声明能力的模型下它是**设计上隐藏**的（g-visual-qa 有专门用例）。 */
const MODELS_WITH_REASONING = [
  {
    ...MODELS[0],
    reasoning_effort: {
      supported: ['minimal', 'deep'],
      default: 'minimal',
      wire_mapping: { minimal: 'low', deep: 'high' },
    },
  },
  ...MODELS.slice(1),
];

async function openApp(page: Page, selectSession = true): Promise<void> {
  await routeApi(page, {
    sessions: SESSIONS,
    events: EVENTS,
    models: MODELS_WITH_REASONING,
    permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
  });
  await page.addInitScript(() => {
    localStorage.setItem('ahi.theme', 'dark');
    localStorage.setItem('ahi.traceDensity', 'detailed');
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  if (selectSession) {
    await page.locator('.session-item').first().click();
    await expect(page.locator('#composer-input')).toBeVisible();
  }
}

/** 从 body 起按 Tab，返回到达 composer 的按键数；`useSkipLink` 时激活「跳到输入框」。 */
async function countKeysToComposer(page: Page, useSkipLink: boolean): Promise<number> {
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  let presses = 0;
  const trail: string[] = [];
  for (let i = 0; i < 80; i += 1) {
    const focused = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null;
      return {
        id: el?.id ?? '',
        tag: el?.tagName ?? '',
        text: (el?.textContent ?? '').trim().slice(0, 24),
      };
    });
    trail.push(`${focused.tag}#${focused.id}:${focused.text}`);
    if (focused.id === 'composer-input') {
      // eslint-disable-next-line no-console
      console.log(`[#1372] key trail: ${trail.join(' → ')}`);
      return presses;
    }
    if (useSkipLink && focused.text === '跳到输入框') {
      await page.keyboard.press('Enter');
      presses += 1;
      continue;
    }
    await page.keyboard.press('Tab');
    presses += 1;
  }
  throw new Error(`80 次 Tab 内未到达 composer；trail=${trail.join(' → ')}`);
}

async function expectTip(page: Page, trigger: Locator, text: string): Promise<void> {
  await trigger.hover();
  const tip = page.locator('.tip-content');
  await expect(tip).toContainText(text);
  // 关闭后再验下一个：Radix 卸载 content，避免上一个 tooltip 的文本被误命中。
  // 用 Escape（Radix 的 DismissableLayer 语义）而不是只移鼠标——移出后指针可能
  // 落到另一个 trigger 上，count 仍是 1，会把"换了一个 tooltip"误判成没关闭。
  await page.keyboard.press('Escape');
  await expect(tip).toHaveCount(0);
}

test('#1372 AC1：首焦点到 composer ≤10 次按键（region 跳转键）；纯 Tab 路径只记录', async ({ page }) => {
  await openApp(page);

  // 重新加载：把 Chromium 的「顺序焦点导航起点」归零。click 会话后再 blur() 只把
  // activeElement 变成 body，**起点仍停在被点的会话行**——实测第一次 Tab 直接落到
  // 下一行会话，跳过 skip-link，会把 5 次虚报成 14 次。会话 id 已持久化，刷新后仍选中。
  await page.reload();
  await expect(page.locator('.session-item.selected')).toHaveCount(1);
  await expect(page.locator('#composer-input')).toBeVisible();

  const withSkip = await countKeysToComposer(page, true);
  // eslint-disable-next-line no-console
  console.log(`[#1372] 首焦点→composer（用跳转键）= ${withSkip} 次按键`);
  expect(withSkip, '允许 region 跳转键时，首焦点到 composer 必须 ≤10 次按键').toBeLessThanOrEqual(10);

  await page.reload();
  await expect(page.locator('#composer-input')).toBeVisible();
  const pureTab = await countKeysToComposer(page, false);
  // eslint-disable-next-line no-console
  console.log(`[#1372] 首焦点→composer（纯 Tab）= ${pureTab} 次按键（无阈值，仅供记录）`);
  expect(pureTab).toBeGreaterThan(withSkip);
});

test('#1372 AC1：会话区 / 主工作区跳转键落焦到 region，下一次 Tab 留在区内', async ({ page }) => {
  await openApp(page);
  const regions = [
    ['跳到会话区', '#region-sessions'],
    ['跳到主工作区', '#region-main'],
  ] as const;
  for (const [name, id] of regions) {
    const link = page.getByRole('link', { name });
    await link.focus();
    await page.keyboard.press('Enter');
    await expect(page.locator(id)).toBeFocused();
    // tabindex=-1 的容器只作落点；下一次 Tab 必须进入该区，而不是回到页面顶部。
    await page.keyboard.press('Tab');
    const inside = await page.evaluate(
      (sel) => document.querySelector(sel)?.contains(document.activeElement) ?? false,
      id,
    );
    expect(inside, `${name} 后下一次 Tab 应留在该 region 内`).toBe(true);
  }
});

test('#1372 改动 6：disabled 作用域 pill 用原生 title 兜底完整原因', async ({ page }) => {
  await openApp(page);
  await toggleReusableRule(page);
  // e2e 夹具的 effectiveProject 缺省为 null = 非 git 目录 ⇒「本项目」禁用。
  const project = page.getByRole('button', { name: '本项目', exact: true });
  await expect(project).toBeDisabled();
  await expect(project).toHaveAttribute('title', '当前会话不在 git 仓库内，无法登记项目级约束');
});

test('#1372：Inspector 收起时不再给出无法落焦的跳转键', async ({ page }) => {
  await openApp(page);
  const link = page.getByRole('link', { name: '跳到 Inspector' });
  await expect(link).toHaveCount(1);
  // 展开时可用：激活后焦点落在 region 容器上。
  await link.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#region-inspector')).toBeFocused();

  // 收起后该 region 是 visibility:hidden（不卸载），focus() 静默失败 ⇒ 链接不渲染。
  await clickTopBarMenuItem(page, '收起 Inspector');
  await expect(page.locator('.app-regions')).toHaveClass(/inspector-closed/);
  await expect(page.getByRole('link', { name: '跳到 Inspector' })).toHaveCount(0);

  // 自动值班：main（含会话区/工作区/Inspector）整体 hidden —— 四条都不可落焦。
  await clickTopBarMenuItem(page, '自动值班');
  await expect(page.getByLabel('自动值班面板')).toBeVisible();
  for (const name of ['跳到会话区', '跳到主工作区', '跳到 Inspector', '跳到输入框']) {
    await expect(page.getByRole('link', { name }), `${name}（自动值班时）`).toHaveCount(0);
  }
  await expect(page.getByRole('navigation', { name: '跳转' })).toHaveCount(0);
  await page.getByRole('button', { name: '返回工作区' }).click();

  // 整页 Inspector：rail/workspace visibility:hidden —— 只留 Inspector 一条。
  await clickTopBarMenuItem(page, '展开 Inspector');
  await page.locator('.detail-ctrl[aria-label="整页"]').click();
  await expect(page.locator('.app-regions')).toHaveClass(/inspector-fullpage/);
  await expect(page.getByRole('link', { name: '跳到 Inspector' })).toHaveCount(1);
  for (const name of ['跳到会话区', '跳到主工作区', '跳到输入框']) {
    await expect(page.getByRole('link', { name }), `${name}（整页时）`).toHaveCount(0);
  }
});

test('#1372：顶栏收纳后「更多工具」6 项零丢失（逐项打开真实入口）', async ({ page }) => {
  await openApp(page);

  const openMenu = () => page.getByRole('button', { name: '更多工具' }).click();
  await openMenu();
  for (const name of [
    'API 身份令牌',
    '上下文容量',
    '上下文窗口',
    '持久审批规则',
    '自动值班',
    '收起 Inspector',
  ]) {
    await expect(page.getByRole('menuitem', { name })).toBeVisible();
  }
  await page.keyboard.press('Escape');

  // 1) 身份令牌 → 配置面板
  await clickTopBarMenuItem(page, 'API 身份令牌');
  await expect(page.locator('.auth-panel')).toBeVisible();
  // 菜单关闭后焦点应自动落到输入框（不要求用户再点一次）；面板内 Esc 才生效。
  await expect(page.locator('.auth-panel input').first()).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.locator('.auth-panel')).toHaveCount(0);
  // r3 P3：面板关闭后焦点回到「更多工具」，不掉回 body（保存 / 清除走同一 helper）。
  await expect(page.getByRole('button', { name: '更多工具' })).toBeFocused();

  // 清除路径同样归还焦点（同一条关闭链的另一出口）。
  await clickTopBarMenuItem(page, 'API 身份令牌');
  await page.locator('.auth-panel input').first().fill('dummy');
  await page.locator('.auth-panel-clear').click();
  await expect(page.locator('.auth-panel')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '更多工具' })).toBeFocused();

  // 2) 上下文容量 → 看板
  await clickTopBarMenuItem(page, '上下文容量');
  await expect(page.getByRole('dialog', { name: '上下文容量' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: '上下文容量' })).toHaveCount(0);

  // 3) 上下文窗口 → 挡位选择器
  await clickTopBarMenuItem(page, '上下文窗口');
  await expect(page.getByRole('dialog', { name: '上下文窗口' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: '上下文窗口' })).toHaveCount(0);

  // 4) 持久审批规则 → 管理浮层
  await clickTopBarMenuItem(page, '持久审批规则');
  await expect(page.getByRole('dialog', { name: '持久审批规则' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: '持久审批规则' })).toHaveCount(0);

  // 5) 自动值班 → 独立面板
  await clickTopBarMenuItem(page, '自动值班');
  await expect(page.getByLabel('自动值班面板')).toBeVisible();
  await page.getByRole('button', { name: '返回工作区' }).click();
  await expect(page.getByLabel('自动值班面板')).toHaveCount(0);

  // 6) Inspector 开关：收起 → 展开
  await clickTopBarMenuItem(page, '收起 Inspector');
  await expect(page.locator('.app-regions')).toHaveClass(/inspector-closed/);
  await clickTopBarMenuItem(page, '展开 Inspector');
  await expect(page.locator('.app-regions')).not.toHaveClass(/inspector-closed/);
});

test('#1372 令牌面板已开时关闭菜单：焦点回到「更多工具」而不是 body', async ({ page }) => {
  await openApp(page);
  await clickTopBarMenuItem(page, 'API 身份令牌');
  await expect(page.locator('.auth-panel input').first()).toBeFocused();

  // 面板开着再打开菜单，用 Esc 关菜单（本次关闭与「打开令牌面板」无关）。
  await page.getByRole('button', { name: '更多工具' }).click();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('menu')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '更多工具' })).toBeFocused();
});

test('#1372 改动 1：会话行 Shift+F10 打开 ⋯ 菜单并把焦点交给首项', async ({ page }) => {
  await openApp(page);
  const rowButton = page.locator('.session-row').first().locator('.session-item');
  await rowButton.focus();
  await expect(rowButton).toHaveAttribute('aria-keyshortcuts', 'Shift+F10');
  await page.keyboard.press('Shift+F10');
  const menu = page.getByRole('menu');
  await expect(menu).toBeVisible();
  await expect(page.getByRole('menuitem').first()).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
});

test('#1372 AC2：重放弹窗 Esc 后焦点回到行内 ⋯ 触发按钮', async ({ page }) => {
  await openApp(page);

  const row = page.locator('.session-row').first();
  await row.hover();
  const trigger = row.locator('.rail-menu-btn');
  await expect(trigger).toBeVisible();
  await trigger.click();
  await page.getByRole('menuitem', { name: '重放会话…' }).click();

  const dialog = page.locator('[role="dialog"]');
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute('aria-modal', 'true');

  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();

  // Radix 在卸载后的 0ms 定时器里派发 onCloseAutoFocus；`toBeHidden` 可能在同一个
  // 任务里先返回。用可重试断言等焦点落定（AC 是行为，不是同一 tick 的时序）。
  await expect(trigger).toBeFocused();
  await expect(trigger).toHaveAttribute('aria-label', /操作/);
});

test('#1372 AC3：tooltip 全覆盖（顶栏 / rail / 会话行 / Inspector / composer / 会话名）', async ({ page }) => {
  test.setTimeout(60_000);
  // 新会话态：先把 5 个 composer chip 量全（#1444 后预算 chip 两态都在，这里取新建态）。
  await openApp(page, false);
  const chips: Array<[string, string]> = [
    ['button[aria-label="模型选择"]', '默认链'],
    ['button[aria-label="权限模式"]', '权限模式'],
    ['button[aria-label="Agent Profile"]', 'Agent Profile'],
    ['button[aria-label="Reasoning Effort"]', 'Reasoning Effort'],
    ['button[aria-label="预算"]', '这次任务的预算'],
  ];
  for (const [selector, text] of chips) {
    await expectTip(page, page.locator(selector), text);
  }

  // 选中会话：会话名下拉、行内 ⋯、Inspector tab、顶栏与 rail。
  await page.locator('.session-item').first().click();
  await expect(page.locator('#composer-input')).toBeVisible();

  await expectTip(page, page.locator('button[aria-label^="切换会话："]'), '点击切换');

  const row = page.locator('.session-row').first();
  await row.hover();
  await expectTip(page, row.locator('.rail-menu-btn'), '会话操作');

  const tabs: Array<[string, string]> = [
    ['Timeline', '时间线'],
    ['Overview', '概览'],
    ['Changes', '改动'],
    ['Terminal', '输出'],
    ['Artifacts', '产物'],
    ['Constraints', '约束'],
  ];
  for (const [label, text] of tabs) {
    await expectTip(page, page.locator(`.detail-tab[aria-label="${label}"]`), text);
  }

  const topbar: Array<[string, string]> = [
    ['记忆管理', '记忆管理'],
    ['看板', 'Agent 看板'],
    ['恢复列表', '恢复列表'],
    ['切换主题', '切换主题'],
    ['更多工具', '更多工具'],
  ];
  for (const [label, text] of topbar) {
    await expectTip(page, page.locator(`button[aria-label="${label}"]`), text);
  }

  const rail: Array<[string, string]> = [
    ['插件', '插件：MCP Servers'],
    ['显示已归档会话', '显示已归档会话'],
    ['新建项目', '新建项目'],
    ['新建会话', '新建会话'],
  ];
  for (const [label, text] of rail) {
    await expectTip(page, page.locator(`button[aria-label="${label}"]`), text);
  }
});
