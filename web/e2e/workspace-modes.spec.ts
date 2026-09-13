/** 场景：#180 —— Workspace 模式条里的 Split/Preview 是**诚实占位**，不是可用功能。
 *
 *  背景（真机验收 SID 侧栏 subagent 实测）：三个按钮都能点、`aria-pressed` 也正确
 *  切换，但选 Split / Preview **不产生任何分栏**，只在对话区上方插一条"未来升级点"
 *  提示条——即"看着像功能、点了没反应"的最差一档。产品裁决走**路线 A（诚实占位）**：
 *  预留位保留可见性（路线图可读）但不可选中。
 *
 *  本 spec 锁的就是"不可选中"这件事本身，以及**删干净**了——如果哪天有人把
 *  `disabled` 去掉、或把那条占位提示条改回来（等价于恢复"点了没事发生"），这里要红：
 *
 *  - AC1：Split / Preview `disabled` + `aria-disabled="true"` + 说明文本可达；
 *  - AC2：**不存在**"选中后什么也没发生"的状态——点不动、也没有占位提示条；
 *  - AC3：Chat 是唯一 `aria-pressed="true"` 的模式（冻结决策：它永远是主阅读面）。
 *
 *  无障碍口径：`disabled` 元素不可聚焦，所以"为什么不能点"不能只写进 `title`
 *  （键盘用户与多数屏幕阅读器都读不到）——它必须进 accessible name，这里按名字断言。
 */

import { expect, test } from '@playwright/test';
import { routeApi } from './fixtures';

const NOTE = 'Phase 1d 预留，尚未实现';

test('Split/Preview 是 disabled 的预留位：不可选中、有说明、不留"点了没事"的路径', async ({
  page,
}) => {
  routeApi(page, {});
  await page.goto('/');

  const bar = page.getByRole('toolbar', { name: 'Workspace 模式' });
  await expect(bar).toBeVisible();

  const chat = bar.getByRole('button', { name: 'Chat' });
  const split = bar.getByRole('button', { name: `Split（${NOTE}）` });
  const preview = bar.getByRole('button', { name: `Preview（${NOTE}）` });

  // AC3：Chat 是唯一被按下的模式——它就是当前（也是唯一）的阅读面。
  await expect(chat).toHaveAttribute('aria-pressed', 'true');
  await expect(split).toBeDisabled();
  await expect(preview).toBeDisabled();

  // AC1：`disabled` 与 `aria-disabled` 双写（票面要求），说明文本进 accessible name。
  await expect(split).toHaveAttribute('aria-disabled', 'true');
  await expect(preview).toHaveAttribute('aria-disabled', 'true');
  await expect(split).toHaveAttribute('title', `${NOTE}（当前只有 Chat）`);

  // AC1 反面：预留位没有 aria-pressed——它不是"没被选中的模式"，而是"还不能选"。
  await expect(split).not.toHaveAttribute('aria-pressed', /.+/);
  await expect(preview).not.toHaveAttribute('aria-pressed', /.+/);

  // AC2：点一个 disabled 按钮（force 绕过 Playwright 的可操作性检查，模拟真实鼠标
  // 落在禁用元素上）——不许出现任何"选中"或占位提示条的痕迹。
  await split.click({ force: true });
  await expect(split).not.toHaveClass(/sel/);
  await expect(page.locator('.workspace-scaffold')).toHaveCount(0);
  await expect(bar.locator('button.sel')).toHaveCount(1);
  await expect(page.locator('.workspace-mode.sel')).toHaveText('Chat');
});
