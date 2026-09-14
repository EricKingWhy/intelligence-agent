/** #199：ModelPicker 两级飞出（provider → 模型子菜单）的 e2e。
 *
 * 语义变更说明（**不是**把断言改松，是断言对象换了）：
 *   旧实现是 Popover + cmdk 的**扁平可搜索列表**（`combobox` + `listbox` + N 个
 *   `option`）。用户裁定改成 ZCode 那种两级飞出、并要求删掉搜索框，于是结构变成
 *   **菜单 + 子菜单**：一级 = provider 行（`menuitem` + `aria-haspopup="menu"`），
 *   二级 = 该 provider 的模型（`menuitemradio`，带 `aria-checked`）。所以「已打开」
 *   的信号从 `[role="listbox"]` 变成 `[role="menu"]`，而"能选到目标模型"这件事
 *   由下面的键盘/鼠标两条路径各自锁住。
 *
 * 保留下来的原意（逐条对应旧断言）：
 *   - trigger 初始显示「默认链」，选中后显示模型名；
 *   - 键盘可达：Tab 到 trigger → Enter 打开 → 方向键移动 → `→` 进二级 → Enter 选；
 *   - Esc 关闭最上层临时表面、逐级退出，且**不改**未确认的选择（§19）；
 *   - 当前选中项在二级里有 `aria-checked="true"`，且一级把所在 provider 标出来
 *     （`data-current`）——这是两级结构新增的、比"平铺一长条"更有用的信息。
 *
 * 车道归属：Playwright（本仓组件测试是 SSR，交互只能真浏览器）。 */

import { expect, test } from '@playwright/test';
import { MODELS, SEARCHABLE_MODELS, openModelMenu, pressMenuItemKey, routeApi } from './fixtures';

test('#199：两级结构 + 鼠标悬停展开 + 选中回写 trigger', async ({ page }) => {
  routeApi(page, { sessions: [], events: [], models: MODELS });
  await page.goto('/');

  const trigger = page.locator('.composer-model[aria-label="模型选择"]');
  await expect(trigger).toBeVisible();
  await expect(trigger).toContainText('默认链');

  await trigger.click();
  // 一级：默认链 + 两个 provider（senseaudio / anthropic），没有二级内容
  const rootMenu = page.locator('[role="menu"]').first();
  await expect(rootMenu).toBeVisible();
  await expect(page.locator('[role="menuitem"][aria-haspopup="menu"]')).toHaveCount(2);
  await expect(page.locator('[role="menuitemradio"]')).toHaveCount(0);
  // 一级的 provider 行带上该组的模型数（信息密度：不堆数据，只给条数）
  await expect(
    page.locator('[role="menuitem"][aria-haspopup="menu"]', { hasText: 'senseaudio' }),
  ).toContainText('2 个模型');

  // 悬停展开二级（迟滞由 Radix 的 pointer-grace + 内部定时器负责），二级里是这个 provider 的模型
  const providerRow = page
    .locator('[role="menuitem"][aria-haspopup="menu"]', { hasText: 'senseaudio' })
    .first();
  await providerRow.hover();
  const subRows = page.locator('[role="menuitemradio"]');
  await expect(subRows).toHaveCount(2);
  await expect(subRows.first()).toContainText(MODELS[0].name);

  // 选中第二个模型 → 菜单关闭 + trigger 回写
  await subRows.nth(1).click();
  await expect(page.locator('[role="menu"]')).toHaveCount(0);
  await expect(trigger).toContainText('qwen-max');
});

test('#199：键盘 `→` 进二级、Esc 逐级退出；选中项带 aria-checked', async ({ page }) => {
  routeApi(page, { sessions: [], events: [], models: MODELS });
  await page.goto('/');

  const trigger = page.locator('.composer-model[aria-label="模型选择"]');
  // 键盘路径统一走 fixtures 的两个 helper：Radix 的移焦发生在 setTimeout 里，按键后
  // 立刻读 activeElement 会读到旧值；helper 用轮询等移焦落定，断言口径是
  // `role|aria-haspopup|文本`（成因详见 fixtures.pressMenuItemKey）。
  await openModelMenu(page);

  // 一级首项是「默认链」；下一项是第一个 provider 行（aria-haspopup=menu ⇒ 有二级）
  await pressMenuItemKey(page, 'ArrowDown', 'menuitem|menu|');

  // `→` 进二级：焦点落到第一个模型行
  await pressMenuItemKey(page, 'ArrowRight', 'menuitemradio|-|');
  await expect(page.locator('[role="menuitemradio"]').first()).toBeVisible();

  // `←` 回一级（设计稿 §3：`←`/Esc 回第一级/关闭）：二级收起、焦点回到那个 provider 行
  await pressMenuItemKey(page, 'ArrowLeft', 'menuitem|menu|');
  await expect(page.locator('[role="menuitemradio"]')).toHaveCount(0);

  // 再 `→` 进二级，这次 Enter 选中
  await pressMenuItemKey(page, 'ArrowRight', 'menuitemradio|-|');

  // Enter 选中它 → 菜单关闭、trigger 回写为该模型名
  await page.keyboard.press('Enter');
  await expect(page.locator('[role="menu"]')).toHaveCount(0);
  await expect(trigger).toContainText(MODELS[0].name);

  // 再打开：一级把当前模型所在的 provider 标成 data-current，二级里该项 aria-checked=true
  await openModelMenu(page);
  const currentProvider = page.locator('[role="menuitem"][data-current="true"]');
  await expect(currentProvider).toHaveCount(1);
  await expect(currentProvider).toContainText(MODELS[0].provider);
  await currentProvider.hover();
  await expect(page.locator('[role="menuitemradio"][aria-checked="true"]')).toContainText(
    MODELS[0].name,
  );

  // Esc 关掉整个菜单，选择不变
  await page.keyboard.press('Escape');
  await expect(page.locator('[role="menu"]')).toHaveCount(0);
  await expect(trigger).toContainText(MODELS[0].name);
});

test('#199：Esc 不写回未确认的选择（打开 → 移动 → Esc → trigger 不变）', async ({ page }) => {
  routeApi(page, { sessions: [], events: [], models: MODELS });
  await page.goto('/');

  const trigger = page.locator('.composer-model[aria-label="模型选择"]');
  await expect(trigger).toContainText('默认链');

  await openModelMenu(page);
  // 在一级与二级里各移动一次，然后逐级 Esc——没有确认过任何选择
  await pressMenuItemKey(page, 'ArrowDown', 'menuitem|menu|');
  await pressMenuItemKey(page, 'ArrowRight', 'menuitemradio|-|');
  await expect(page.locator('[role="menuitemradio"]').first()).toBeVisible();
  await page.keyboard.press('Escape');

  // 逐级关闭：子菜单先关，菜单仍在
  await expect(page.locator('[role="menuitemradio"]')).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(page.locator('[role="menu"]')).toHaveCount(0);
  // 未确认的选择不写回
  await expect(trigger).toContainText('默认链');
});

test('#199：长目录不再需要搜索框——provider 分组就是导航', async ({ page }) => {
  // SEARCHABLE_MODELS：5 个模型 / 4 个 provider。旧实现在这个规模会显示搜索框；
  // 两级结构下一级只有 4 行 provider，扫描成本远低于一长条 6 项。
  routeApi(page, { sessions: [], events: [], models: SEARCHABLE_MODELS });
  await page.goto('/');

  const trigger = page.locator('.composer-model[aria-label="模型选择"]');
  await trigger.click();
  const providers = page.locator('[role="menuitem"][aria-haspopup="menu"]');
  await expect(providers).toHaveCount(4);
  expect(await page.locator('.picker-search-wrap').count()).toBe(0);

  // 一级必须逐字给出每组条数（不堆数据，但也不藏），且各组合计 = 目录长度：
  // 否则"分组"可能只是画面上分了、实际漏掉了模型。
  const counts = await providers.evaluateAll((els) =>
    els.map((el) => Number(el.textContent?.match(/(\d+) 个模型/)?.[1] ?? 0)),
  );
  expect(counts.reduce((a, b) => a + b, 0)).toBe(SEARCHABLE_MODELS.length);
});
