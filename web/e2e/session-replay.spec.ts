/** 场景（#1322）：Web 会话「重放」入口——README 承诺的「可重放」在 Web 侧的入口。
 *
 *  契约来源：issue #1322（用户裁决 D3-A，主路径 A）；spec 03 §6 / ADR-0017 决策 4
 *  （逻辑回放、零副作用）；后端 `web/lineage.py::replay_session`。
 *
 *  本 spec 断言四件事，每件都**真的**考：
 *
 *  1. **入口在会话操作菜单里**（AC-A1）：行 kebab 菜单里有「重放会话…」。
 *  2. **点击前有二次确认且讲清零副作用**（AC-A1）：浮层文案含「零副作用」
 *     「不写入」——这是把 replay ≠ 重跑 这条语义在动作**之前**说给用户。
 *  3. **回放过程可见 + 完成后可查看结果**（AC-A3）：进度条 + 逐事件推进；结果里
 *     能看到冻结终态的 tool result（`→ result (frozen):`）。
 *  4. **失败说话**：后端 404 时原样贴出 detail，不停在假完成态。
 *
 *  真实浏览器里的视觉走查（入口 → 确认 → 回放中 → 结果）截图由走查脚本另存
 *  （见交付报告）；本 spec 是**行为**回归锁。
 */

import { expect, test } from '@playwright/test';
import { DEFAULT_REPLAY_STEPS, routeApi, rowOf, sessionRow, type ApiMock } from './fixtures';

const SID = 's-replay';

function railMock(over: Partial<ApiMock> = {}): ApiMock {
  return {
    sessions: [sessionRow(SID)],
    ...over,
  };
}

/** 打开某行的 kebab 菜单。 */
const openMenu = (page: Parameters<typeof rowOf>[0], sessionId: string) =>
  rowOf(page, sessionId).locator('.rail-menu-btn').click();

test('会话菜单有重放入口 → 二次确认 → 逐事件回放 → 结果可见', async ({ page }) => {
  await routeApi(page, railMock());
  await page.goto('/');

  // ── 1. 入口在会话操作菜单里 ──
  await openMenu(page, SID);
  const menuItem = page.getByRole('menuitem', { name: '重放会话…' });
  await expect(menuItem).toBeVisible();

  // ── 2. 二次确认：讲清"零副作用地重放" ──
  await menuItem.click();
  const dialog = page.locator('.replay-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('零副作用');
  await expect(dialog).toContainText('不写入');
  await expect(dialog).toContainText(SID.slice(0, 12));
  // 确认态还没有触发请求（进度条也不该在）
  await expect(dialog.locator('.replay-step')).toHaveCount(0);

  // ── 3. 开始重放 → 进度条 + 逐事件推进 → 结果 ──
  await dialog.getByRole('button', { name: '开始重放' }).click();
  await expect(dialog.locator('[role="progressbar"]')).toBeVisible();
  await expect(dialog).toHaveAttribute('data-replay-phase', 'done', { timeout: 10_000 });
  await expect(dialog.locator('.replay-step')).toHaveCount(DEFAULT_REPLAY_STEPS.length);
  await expect(dialog).toContainText('[user] 列出文件');
  // 冻结终态：tool result 不重执行，如实呈现已发生的事实
  await expect(dialog).toContainText('→ result (frozen):');
  await expect(dialog).toContainText(`回放完成 · ${DEFAULT_REPLAY_STEPS.length} 个事件`);
});

test('回放中状态可见（延迟响应撑开"正在读取事件"窗口）', async ({ page }) => {
  await routeApi(
    page,
    railMock({
      onReplayGet: async (route) => {
        await new Promise((r) => setTimeout(r, 500));
        return false; // 交给缺省分支回 200
      },
    }),
  );
  await page.goto('/');
  await openMenu(page, SID);
  await page.getByRole('menuitem', { name: '重放会话…' }).click();
  const dialog = page.locator('.replay-dialog');

  await dialog.getByRole('button', { name: '开始重放' }).click();
  // 请求在途：running 态 + "正在读取事件…" 可见
  await expect(dialog).toHaveAttribute('data-replay-phase', 'running');
  await expect(dialog).toContainText('正在读取事件');

  await expect(dialog).toHaveAttribute('data-replay-phase', 'done', { timeout: 10_000 });
  await expect(dialog).toContainText('回放完成');
});

test('后端拒绝：原样贴出 detail，不停在假完成态', async ({ page }) => {
  await routeApi(
    page,
    railMock({
      onReplayGet: (route) => {
        void route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ detail: `session '${SID}' not found` }),
        });
        return true;
      },
    }),
  );
  await page.goto('/');
  await openMenu(page, SID);
  await page.getByRole('menuitem', { name: '重放会话…' }).click();
  const dialog = page.locator('.replay-dialog');

  await dialog.getByRole('button', { name: '开始重放' }).click();
  await expect(dialog).toContainText(`session '${SID}' not found`);
  // 回到可重试态：没有"回放完成"这种假完成
  await expect(dialog).not.toContainText('回放完成');
  await expect(dialog.getByRole('button', { name: '重试' })).toBeVisible();
});
