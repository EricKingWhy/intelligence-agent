/** 场景 L：401 引导横幅（后端起身份令牌要求时前端该做什么）。
 *
 * 起因：控制面清点（45 个 `<button>` 逐个核对）发现 `auth-banner-close`
 * （aria-label「关闭提示」）是**全 UI 唯一从未被点过**的按钮。它在本地开发环境
 * **不可达**：后端只在配置了 `jwt_secret` 时才校验并要求令牌
 * （`web/app.py:594` fail-open，`config.py:47` 默认 `None`），故横幅永不出现。
 * 与其重启后端加密钥（改动后端状态、且超出前端范围），这里按后端**已冻结的契约
 * 形状**（`lib/auth.ts` 文档：匿名 → 401 `{"detail":"Missing identity token"}`）造 401。
 *
 * 覆盖分工（两层，别混为一谈）：
 *  - `lib/api.test.ts` 的「401 → UnauthorizedError + onUnauthorized 广播」组：
 *    分类与广播（含 body 非 JSON 的回退文案）；
 *  - 本文件：广播**之后**的 UI——横幅出现、可关、以及再次 401 时的真实反应。 */

import { expect, test } from '@playwright/test';
import { clickTopBarMenuItem, mockAuthRequired } from './fixtures';

test('401 → 引导横幅出现；可点「关闭提示」关掉；再有 401 会重新出现', async ({ page }) => {
  // 后端起身份令牌要求（会话列表恒 401）——夹具与 t-contrast.spec.ts 的 #1136 auth-banner
  // 用例共用（见 fixtures.ts 的 mockAuthRequired）。
  await mockAuthRequired(page);

  await page.goto('/');
  const banner = page.locator('.auth-banner');
  await expect(banner).toBeVisible();
  // 断言这是**鉴权**横幅（文案是静态的，detail 的传播由 api.test.ts 的单测覆盖）
  await expect(banner).toContainText('身份令牌');
  // #1372 改动 6：入口收进「更多工具」后，横幅必须指向真实位置（不再写"钥匙图标"）。
  await expect(banner).toContainText('更多工具');
  await expect(banner).toContainText('API 身份令牌');

  // 唯一出口：关闭按钮（本用例存在的理由——该按钮此前零覆盖）
  await page.getByRole('button', { name: '关闭提示', exact: true }).click();
  await expect(banner).toBeHidden();
  // 关的是横幅、不是整页：顶栏必须还在（否则「点崩了」也会让 toBeHidden 通过）。
  // #1372 顶栏收纳后，令牌入口在「更多工具」菜单里，先确认顶栏本身还在。
  await expect(page.getByRole('button', { name: '更多工具' })).toBeVisible();

  // 真实行为（此前注释把这里说反了）：关闭**不是**永久忽略。
  // 走应用内真实路径「配置令牌」→ `onTokenChange` 先 setAuthRequired(false) 并
  // 立即 refreshSessions()（App.tsx:151-154）；后端仍 401 → 广播 → 横幅回来。
  await clickTopBarMenuItem(page, 'API 身份令牌');
  await page.locator('.auth-panel input').first().fill('eyJhbGciOiJIUzI1NiJ9.dummy.sig');
  await page.locator('.auth-panel-save').click();
  await expect(banner).toBeVisible();
});
