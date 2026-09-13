/** 场景：#182 —— 中心列的面由**能力声明**决定；Split / Preview 已删除。
 *
 *  历史（#180 路线 A）：那条"Workspace 模式"条上，Split/Preview 是 `disabled` 的诚实
 *  占位。本票按 Brief 与 `BENCHMARK_SYNTHESIS` 的取舍把它们**删掉**（要的是 tabs 不是
 *  分屏），换成真正由 `GET /api/capabilities` 驱动的 tab 集：
 *
 *  - AC1：Split / Preview / 预留位字样**全部不存在**（删干净，不留"点了没事发生"）；
 *  - AC2/AC3：tab 集 = `Chat`（恒存在）+ 能力声明为真的面；数据拿不到时落 PRD 缺省
 *    语义（chat + timeline），**Chat 永不因此消失**；
 *  - AC4：声明为真**但尚无实现**的面不渲染——渲染一个没有实现的 tab 就是"看着像
 *    功能、点了没反应"，比不渲染更差；
 *  - AC5：`role="tablist"` / `aria-selected` 如实 / roving tabindex（方向键语义的
 *    纯函数单测在 `src/lib/capabilities.test.ts`，这里锁真实 DOM 接线）；
 *  - AC7：三区几何不变、Chat 面板照常渲染。
 */

import { expect, test } from '@playwright/test';
import { capabilityFixture, routeApi } from './fixtures';

const tabs = (page: import('@playwright/test').Page) =>
  page.getByRole('tablist', { name: '工作区面' });
const tabLabels = (page: import('@playwright/test').Page) => tabs(page).getByRole('tab').allTextContents();

test('AC1：Split / Preview 与预留位残留已全部删除', async ({ page }) => {
  routeApi(page, {});
  await page.goto('/');

  // 旧的那条"模式"工具条不再存在（它被能力驱动的 tab 条取代）。
  await expect(page.getByRole('toolbar', { name: 'Workspace 模式' })).toHaveCount(0);
  await expect(page.locator('.workspace-mode, .workspace-mode-bar')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /Split|Preview/ })).toHaveCount(0);
  // 占位说明文案也一并消失——留着它等于留着"这里本来该有功能"的暗示。
  await expect(page.getByText('Phase 1d 预留，尚未实现')).toHaveCount(0);
  await expect(page.getByText('未来升级点')).toHaveCount(0);
  // 取而代之的是 tab 条（`role="tablist"`，不是一个 button 工具栏）。
  await expect(tabs(page)).toBeVisible();
});

test('AC2/AC3：能力目录为空（后端 CAPABILITIES=""）→ 恰好只剩 Chat，且它被选中', async ({
  page,
}) => {
  routeApi(page, {}); // capabilities 缺省 = 空列表（后端的真实默认响应）
  await page.goto('/');

  expect(await tabLabels(page)).toEqual(['Chat']);
  const chat = tabs(page).getByRole('tab', { name: 'Chat' });
  await expect(chat).toHaveAttribute('aria-selected', 'true');
  // roving tabindex：整条只占一个 Tab 停靠点（选中项 0，其余 -1）。
  await expect(chat).toHaveAttribute('tabindex', '0');
});

test('AC4 + 端点真被消费：声明为 true 的面今天不渲染，但声明确实被读了', async ({ page }) => {
  // 后端把 changes / terminal 都声明为真——按声明它们"该出现"，但「文件/改动」与
  // 「输出」的实现在 #189 / #190，今天还没有。
  //
  // 这条用例同时回答一个**不能靠 tab 集回答**的问题：前端到底有没有调这个端点？
  // 今天三种 mock（空目录 / 声明为真 / 端点 404）渲染出来的 tab 集**都是** `['Chat']`，
  // 所以"压根没调 / 响应被忽略"这类回归会让整套用例照绿——必须数请求。
  //
  // ⚠ 这条守卫**只对当前交付状态成立**：#189 / #190 落地后必须把它改成"出现"
  //    （票面 #189/#190 已记这一条），否则就是在保护一个已经变假的事实。它今天拦住的
  //    是相反的错：有人先把 tab 接上、面板却还没实现（那正是"看着像功能、点了没反应"）。
  let calls = 0;
  routeApi(page, {
    capabilities: [
      capabilityFixture({ chat: true, timeline: true, changes: true, terminal: true }),
    ],
    onCapabilitiesGet: () => {
      calls += 1;
      return false; // 交回默认分支（回上面那份声明）
    },
  });
  await page.goto('/');

  expect(await tabLabels(page)).toEqual(['Chat']);
  // StrictMode 在 dev 下会双调用 effect（React 既定行为），所以**不锁精确次数**；
  // 锁两件真事：(a) 端点确实被消费了；(b) 消费完之后没有继续重拉——依赖写错会变成
  // 请求循环，而那种 bug 靠 tab 集看不出来。
  await expect.poll(() => calls).toBeGreaterThanOrEqual(1);
  const settled = calls;
  await page.waitForTimeout(400);
  expect(calls).toBe(settled);
});

test('AC6（骨架期口径）：tab 集恰好等于"声明为真 **且有实现**"的面', async ({ page }) => {
  // 票面 AC6 要求"两组 mock（真/假）各断言 tab 集**恰好**符合声明"。今天只有 Chat
  // 有实现，所以"符合声明"的可观测形式是：**声明为真但无实现 → 不出现**；
  // 完整的"声明为真 → 出现"要等 #189 / #190（票面已记）。这里把两组显式并排，
  // 让这个差别的**唯一原因是 implemented 标记**这件事一眼可查。
  routeApi(page, {
    capabilities: [capabilityFixture({ chat: true, timeline: true, changes: false, terminal: false })],
  });
  await page.goto('/');
  const declaredFalse = await tabLabels(page);

  await page.unroute('**/api/**');
  routeApi(page, {
    capabilities: [capabilityFixture({ chat: true, timeline: true, changes: true, terminal: true })],
  });
  await page.goto('/');
  const declaredTrue = await tabLabels(page);

  expect(declaredFalse).toEqual(['Chat']);
  expect(declaredTrue).toEqual(['Chat']); // 同一答案 —— 因为两个面都还没有实现
});

test('AC3：能力接口不可用 → 降级为缺省语义，Chat 永不消失', async ({ page }) => {
  // 老后端没有这个端点（404）——降级不阻塞主流程，也不该让主阅读面消失。
  routeApi(page, { capabilitiesError: { status: 404, detail: 'Not Found' } });
  await page.goto('/');

  expect(await tabLabels(page)).toEqual(['Chat']);
  await expect(tabs(page).getByRole('tab', { name: 'Chat' })).toHaveAttribute(
    'aria-selected',
    'true',
  );
});

test('AC5：方向键在只有一个面时原地不动，不把焦点丢出 tab 条', async ({ page }) => {
  routeApi(page, {});
  await page.goto('/');

  const chat = tabs(page).getByRole('tab', { name: 'Chat' });
  await chat.focus();
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowLeft');
  // 单 tab 时环绕回到自己：仍选中、仍持有焦点（焦点被吞会让键盘用户直接掉出这条）。
  await expect(chat).toHaveAttribute('aria-selected', 'true');
  await expect(chat).toBeFocused();
});

test('AC7：三区几何不变，Chat 的对话与输入框照常渲染', async ({ page }) => {
  routeApi(page, {});
  await page.goto('/');

  // 中心列多出的那一层（tabpanel 容器）不得改动三区几何：仍是 240 | 1fr | 320。
  const columns = await page
    .locator('.app-regions')
    .evaluate((el) => getComputedStyle(el).gridTemplateColumns);
  const tracks = columns.split(' ').filter((t) => t.endsWith('px'));
  expect(tracks).toHaveLength(3);
  expect(tracks[0]).toBe('240px');
  expect(tracks[2]).toBe('320px');

  // Conversation 与 Composer 都还在（新容器必须把 flex 纵向布局原样传给它们）。
  await expect(page.getByLabel('Agent 任务')).toBeVisible();
  await expect(page.locator('.conversation')).toBeVisible();
});
