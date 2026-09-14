/** 搜索框显示契约：**模型选择器没有搜索框**（用户裁定）+ 档位下拉按目录长度显示/隐藏。
 *
 * 背景（两段，别混为一谈）：
 *
 * 1. **模型选择器**：用户 2026-09-13 裁定「才几个模型没有必要使用搜索框」，并要求改成
 *    ZCode 那种两级飞出。所以模型选择器的搜索框是**删除**，不是隐藏——本 spec 原来
 *    那两条（短/长目录的 `.model-picker-search-wrap`）已随实现一起作废，改为锁
 *    「一级/二级都没有搜索框」这条**新**契约（旧断言不能留：它锁的是一个不再存在的
 *    控件，留着只会让"没人再实现它"看起来像回归）。
 *
 * 2. **档位下拉（OptionPicker）**：目录可能很长（权限/档位/深度由后端下发，实测有
 *    7 条以上的目录），搜索保留，并按长度显示/隐藏——短目录（≤5 条）隐藏、长目录显示。
 *    这条契约来自 F-DEFER-1：class 挂了但 CSS 里从来没有 `.hidden { display:none }`
 *    规则，修复前短目录下搜索框一直显示。现在 class 正名为 `.picker-search-wrap`
 *    （#201 合并三个 picker 时一并改的），规则与阈值都没变。
 *
 * ⚠ 两条实测陷阱（探针验证，仍然成立）：
 *   1. cmdk 把 `role="combobox"` 放在 CommandInput 本身。搜索框隐藏 = 该 role 也从
 *      a11y 树里消失。**不能用 combobox 当「浮层已开」的信号**，否则短目录用例必然
 *      假失败。正确信号是 `[role="listbox"]`（CommandList，恒可见）。
 *   2. cmdk 要求 CommandInput 始终留在 DOM（不能条件卸载），所以断言用
 *      `toBeHidden()` 而非 `toHaveCount(0)`——元素必须在场，只是不可见。
 *
 * 阈值速查（源码）：`OptionPicker` 用 `options.length <= 5`。
 *
 * 车道归属：Playwright e2e（浮层内内容在关闭态不渲染，SSR/单测断不到）。 */

import { expect, test } from '@playwright/test';
import { LONG_MODELS, PERMISSION_MODES, longCatalog, openModelMenu, routeApi } from './fixtures';

// 长目录统一来自 fixtures（避免多处内联后静默漂移，见 fixtures.ts「长目录 fixture」段）。
const LONG_MODES = longCatalog('mode', 6);

const searchWrap = (page: import('@playwright/test').Page) =>
  page.locator('.picker-search-wrap');

test('#199：模型选择器没有搜索框（一级与二级都没有）', async ({ page }) => {
  // 用**长**目录：旧实现在长目录下会显示搜索框，所以这条能真正证明搜索框被删掉，
  // 而不是"目录太短所以没显示"（后者是假绿——正是本 spec 要防的那种）。
  routeApi(page, { sessions: [], events: [], models: LONG_MODELS });
  await page.goto('/');

  const trigger = page.locator('.composer-model[aria-label="模型选择"]');
  await expect(trigger).toBeVisible();
  // 键盘路径走 fixtures.openModelMenu（等初焦落到菜单项）：与 model-picker.spec 同一前置条件
  await openModelMenu(page);

  // 一级打开：菜单语义（两级飞出 = 菜单 + 子菜单）
  const menu = page.locator('[role="menu"]').first();
  await expect(menu).toBeVisible();
  expect(await searchWrap(page).count()).toBe(0);

  // 展开二级（悬停 provider 行）：二级也没有搜索框
  const providerRow = page.locator('[role="menuitem"][aria-haspopup="menu"]').first();
  await providerRow.hover();
  await expect(page.locator('[role="menuitemradio"]').first()).toBeVisible();
  expect(await searchWrap(page).count()).toBe(0);
});

test('短目录：OptionPicker（权限模式）搜索框不可见', async ({ page }) => {
  routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: PERMISSION_MODES, // 3 条 ≤ 5 → 隐藏
  });
  await page.goto('/');

  const trigger = page.locator('.composer-control[aria-label="权限模式"]');
  await expect(trigger).toBeVisible();
  await trigger.focus();
  await page.keyboard.press('Enter');

  await expect(page.locator('[role="listbox"]')).toBeVisible();
  await expect(searchWrap(page)).toHaveCount(1);
  await expect(searchWrap(page)).toBeHidden();
});

test('长目录：OptionPicker（权限模式）搜索框可见且可过滤', async ({ page }) => {
  routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: LONG_MODES, // 6 > 5
  });
  await page.goto('/');

  const trigger = page.locator('.composer-control[aria-label="权限模式"]');
  await trigger.focus();
  await page.keyboard.press('Enter');

  await expect(page.locator('[role="listbox"]')).toBeVisible();
  await expect(searchWrap(page)).toHaveCount(1);
  await expect(searchWrap(page)).toBeVisible();
  // 反证「隐藏」是显示层面的事而非卸载：长目录下输入框真的能用
  await page.locator('.picker-search').fill('mode 3');
  await expect(page.locator('[role="option"]')).toHaveCount(1);
});
