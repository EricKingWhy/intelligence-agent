/** 场景 H（spec 01 §22）：Trace Density 四档切换 + localStorage 持久化
 *  （冻结决策：四档 + 持久化；key = ahi.traceDensity）。 */

import { expect, test, type Page } from '@playwright/test';
import { routeApi } from './fixtures';

/** #1396：四档不再常驻，收进顶栏的密度下拉（触发器显示当前档）。
 *  菜单项是 `menuitemradio`（RadioGroup 语义），选中即关闭 ⇒ 每次改档都从开菜单开始。
 *  用独立的 `.appbar-density-trigger`，不是「更多工具」里 ≤560px 才显示的副本。 */
async function setDensity(page: Page, label: string): Promise<void> {
  await page.locator('.appbar-density-trigger').click();
  await page.getByRole('menuitemradio', { name: label }).click();
}

test('四档切换：data-density 即时生效，刷新后持久', async ({ page }) => {
  await routeApi(page, {});
  await page.goto('/');

  await setDensity(page, '紧凑');
  await expect(page.locator('html')).toHaveAttribute('data-density', 'compact');

  await setDensity(page, 'Raw');
  await expect(page.locator('html')).toHaveAttribute('data-density', 'raw');
  await expect(page.locator('html')).not.toHaveAttribute('data-density', 'compact');

  // 刷新后持久（localStorage ahi.traceDensity）
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-density', 'raw');
  await setDensity(page, '均衡'); // 恢复默认档，不污染其它场景
  await expect(page.locator('html')).toHaveAttribute('data-density', 'balanced');
});
