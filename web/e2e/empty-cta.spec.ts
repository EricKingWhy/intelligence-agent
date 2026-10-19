/** #1370 P0-7：空状态主 CTA 的 **App 级接线**（组件级单测覆盖不到这一层）。
 *
 *  `Conversation.emptyCta.test.tsx` 锁的是「给了 `onNewTask` 就调它」；这里锁的是
 *  `App.tsx` 真的把**既有的** `handleNew` 注了进去、并且点下去的可见结果是焦点回到
 *  composer —— 删掉 `onNewTask={handleNew}` 会让 CTA 直接不渲染，这条会红。
 *
 *  为什么必须是新建态（无选中会话）：空态只在没有任何会话内容时出现。
 */
import { expect, test } from '@playwright/test';
import { AGENT_PROFILES, MODELS, PERMISSION_MODES, REASONING_EFFORTS, routeApi } from './fixtures';

test('空态主 CTA：可见、点击把焦点交回 composer', async ({ page }) => {
  await routeApi(page, {
    sessions: [],
    projects: [],
    models: MODELS,
    permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
  });
  await page.goto('/');

  const cta = page.locator('.conversation-empty .empty-cta');
  await expect(cta).toBeVisible();
  await expect(cta).toHaveText('开始新任务');

  // 焦点先被移开，否则"点击后聚焦"可能是浏览器本来就没动过焦点（假绿）。
  await page.locator('#composer-input').focus();
  await page.locator('.empty-hero').click();
  await expect(page.locator('#composer-input')).not.toBeFocused();

  await cta.click();
  await expect(page.locator('#composer-input')).toBeFocused();
});
