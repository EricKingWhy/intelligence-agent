/** F2：ModelPicker Combobox 升级——a11y 键盘导航 e2e 实测。
 *
 * 验收项（HANDOFF_REMAINING_TICKETS.md F2）：
 *   - 语义升级：打开后 listbox + combobox + option 角色在场（cmdk 注入）；
 *   - 键盘导航：Tab 进 trigger → Enter/Space 打开 → 方向键移动 active →
 *     Enter 选档 → 浮层关闭 → trigger 文本更新；
 *   - 搜索过滤：键入字符缩小列表；
 *   - Esc 关闭浮层（§19「Esc 关闭最上层临时表面」）。
 *
 * 车道归属：vitest 车道只跑 SSR 契约（无 DOM 环境），交互实测在 Playwright
 * （同 i-keyboard.spec.ts 的约定）。本文件不入 vitest 四门禁，归夜间/手动 e2e 车道。 */

import { expect, test } from '@playwright/test';
import { MODELS, routeApi } from './fixtures';

test('ModelPicker Combobox：角色语义 + 键盘导航 + 搜索 + Esc', async ({ page }) => {
  routeApi(page, {
    sessions: [],
    events: [],
    models: MODELS,
  });

  await page.goto('/');
  const trigger = page.locator('.composer-model[aria-label="模型选择"]');
  await expect(trigger).toBeVisible();
  // trigger 当前显示默认链
  await expect(trigger).toContainText('默认链');

  // 键盘打开浮层（Enter）—— 浮层内 cmdk 自动把焦点送到 combobox 输入
  await trigger.focus();
  await page.keyboard.press('Enter');
  // combobox 角色（cmdk Input 注入）
  await expect(page.locator('[role="combobox"]')).toBeVisible();
  // listbox 角色（cmdk List 注入）
  await expect(page.locator('[role="listbox"]')).toBeVisible();
  // option 角色在场（cmdk Item 注入）—— 至少 4 个（默认链 + 3 模型）
  await expect(page.locator('[role="option"]')).toHaveCount(4);

  // 搜索过滤：键入「claude」只剩匹配项（cmdk filter 把不匹配的 option 隐藏）
  await page.keyboard.type('claude');
  await expect(page.locator('[role="option"]')).toHaveCount(1);
  await expect(page.locator('[role="option"]')).toContainText('claude-sonnet-4');

  // Esc 清空搜索？不——cmdk Esc 不清空；Radix Popover 的 Esc 关闭浮层（§19）
  await page.keyboard.press('Escape');
  await expect(page.locator('[role="combobox"]')).toBeHidden();
  // trigger 文本未变（未确认选择）
  await expect(trigger).toContainText('默认链');

  // 再次打开 + 方向键 + Enter 选档 → trigger 文本更新
  await trigger.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('[role="combobox"]')).toBeVisible();
  // 清空搜索（cmdk 重新挂载会重置 search 状态）
  await page.locator('[role="combobox"]').fill('');
  // ArrowDown 让第一个 option 变 active（默认链之后第一个模型 = deepseek-v4-flash-0731）
  // cmdk 默认会自动选第一个 option 为 active；ArrowDown 选下一个
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(page.locator('[role="combobox"]')).toBeHidden();
  // trigger 显示已选模型名（非默认链）
  await expect(trigger).not.toContainText('默认链');
});
