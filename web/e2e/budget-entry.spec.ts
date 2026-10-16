/** #426：预算入口纯 UI 全链——设置预算 → 启动 → run/paused(budget) → PausedPanel → UI 恢复。
 *  #536：交互步骤改走单入口 popover（设计稿 §2.3）；**wire 断言逐字未动**——
 *  换的是输入形态，不是提交契约。
 *
 *  票面验收「纯 UI 路径：设置预算 → 发多 turn 任务 → 触发暂停 → PausedPanel 恢复，
 *  全链零 API 手工调用」的 e2e 落点：所有请求都由界面发出，mock 只按真后端契约应答
 *  （创建=SSE 帧、恢复=SSE 帧、接流=WS 快照+keep）；断言打在**请求体**上——
 *  budget.run 三维逐字对齐 `RunBudgetRequest`（extra="forbid"，app.py:179-206）；
 *  不设置预算 ⇒ 载荷不含 budget 键（默认行为不变）。
 *
 *  与单测的分工：amend/api 单测锁映射与 wire 形状（金钉），这里锁**接线**——
 *  App 真的把三个输入的草稿交给了 toCreateBudget、PausedPanel 真的在 run/paused
 *  后出现、恢复按钮真的发出同 run CAS 请求并在 run/resumed 后收起。 */

import { expect, test, type Route } from '@playwright/test';
import { RUN, SID, T, fulfillSse, routeApi, submitTask, type FrameSpec } from './fixtures';

/** 启动即到 tokens 维预算顶的帧剧本（末帧 run/paused 非终态 ⇒ 客户端转 WS 接流保持在场）。 */
function pausedFrames(): FrameSpec[] {
  return [
    { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
    { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
    { type: 'user/message', data: { content: '跑一个长任务' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'model/started', data: { model: 'e2e-model' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
    {
      type: 'run/paused',
      seq: 5,
      session_id: SID,
      run_id: RUN,
      time: T,
      data: {
        reason: 'budget_exhausted',
        trigger_dimension: 'run.max_total_tokens',
        budget_version: 1,
        consumed: { agent_turns: 2, total_tokens: 1234 },
        limits: { run: { max_agent_turns_total: 3, max_total_tokens: 1234 } },
        resume_requirements: ['抬高 run.max_total_tokens 的绝对 ceiling 后同 run 恢复'],
      },
    },
  ];
}

test('#426 纯 UI 全链：预算三项随启动请求提交 → 预算暂停 → PausedPanel 恢复抬高同维', async ({ page }) => {
  const frames = pausedFrames();
  // durable log = /events 与 WS 快照的同源真相（fixtures 纪律）。恢复帧在恢复请求
  // 应答时补进这份 log——真后端里 run/resumed 及其后的事件都是 durable 的，恢复后的
  // 历史重载必须能看到它们，否则投影会退回"暂停中"。
  const history: FrameSpec[] = [...frames];
  let createBody: Record<string, unknown> | null = null;
  let resumeBody: Record<string, unknown> | null = null;

  await routeApi(page, {
    onSessionPost: async (route: Route) => {
      createBody = route.request().postDataJSON() as Record<string, unknown>;
      await fulfillSse(route, frames);
    },
    events: history,
    // 暂停 run 的传输语义（runmanager.py:342 `_drive` finally → finish() → DONE）：
    // POST 流在 run/paused 后**干净收尾**（非终态 ⇒ 客户端视为"流异常收尾"）；
    // 重连订阅时 run task 已结束 ⇒ `get_active()` 为 None ⇒ 快照 `has_active_run:
    // false`（websocket.py:240），客户端据此自行收流（wsStream.ts:259）。
    // 客户端在重连额度耗尽后退回 viewing 并重读历史——面板（App.tsx:818 要求
    // `!streaming`）正是在那一步出现的。
    onWs: () => ({ events: history, hasActiveRun: false }),
  });
  // fixtures 的 routeApi 不认识 /resume（真实端点 POST /api/sessions/{id}/resume，
  // api.ts:1274）。Playwright 后注册的路由先匹配 ⇒ 这里盖过 catch-all；响应 = SSE 帧
  // （与真后端 `_run_stream_response` 同形），seq 接着暂停帧单调续。
  await page.route(new RegExp(`/api/sessions/${SID}/resume$`), async (route: Route) => {
    resumeBody = route.request().postDataJSON() as Record<string, unknown>;
    const resumeFrames: FrameSpec[] = [
      { type: 'run/resumed', data: {}, seq: 6, session_id: SID, run_id: RUN, time: T },
      { type: 'text/delta', data: { delta: '预算已抬高，继续。' }, seq: 7, session_id: SID, run_id: RUN, step_id: 2, time: T },
      { type: 'run/completed', data: {}, seq: 8, session_id: SID, run_id: RUN, time: T },
    ];
    history.push(...resumeFrames);
    await fulfillSse(route, resumeFrames);
  });

  await page.goto('/');

  // ① 纯 UI 设置预算三维（#536 单入口 popover——设计稿 §2.3，替换 #426 三平铺）。
  //    面板内三维控件的 aria-label 沿用 #426 旧名（turns / tokens 自定义 / datetime）。
  await page.locator('.composer-budget-trigger[aria-label="预算"]').click();
  const turnsInput = page.getByLabel('预算上限（Agent turns，留空为默认）');
  const resetButton = page.getByRole('button', { name: '恢复默认' });
  await expect(resetButton).not.toBeVisible();
  await turnsInput.fill('3');
  // tokens 走「自定义」档：点开档位 → 点自定义行（onChange 返回 false，面板不关，
  // 数值输入框出现在 footer）→ 填自定义值。
  await page.locator('.composer-control[aria-label="预算 tokens 档位"]').click();
  await page.locator('[role="listbox"]:visible').last().getByText('自定义', { exact: true }).click();
  await page.getByLabel('预算上限（总 tokens，留空为默认）').fill('1234');
  // deadline 走高级路径（datetime-local 直填，#536 §2.2 保留项）。
  const deadlineDetails = page.locator('.budget-picker-advanced');
  const deadlineInput = page.getByLabel('预算截止时间（留空为不设）');
  await expect(deadlineInput).not.toBeVisible();
  await deadlineDetails.locator('summary').click();
  await expect(deadlineInput).toBeVisible();
  const deadlineLocal = '2026-10-01T12:30';
  await deadlineInput.fill(deadlineLocal);
  await expect(resetButton).toBeVisible();
  await resetButton.click();
  await expect(turnsInput).toHaveValue('');
  await expect(page.getByLabel('预算上限（总 tokens，留空为默认）')).not.toBeVisible();
  await expect(deadlineInput).toHaveValue('');
  await expect(resetButton).not.toBeVisible();
  await expect(page.locator('.composer-budget-trigger .composer-trigger-current')).toHaveText('默认');
  await expect(page.locator('.budget-picker-status-text')).toHaveText('当前：默认（后端默认，全不设限）');

  await turnsInput.fill('3');
  await page.locator('.composer-control[aria-label="预算 tokens 档位"]').click();
  await page.locator('[role="listbox"]:visible').last().getByText('自定义', { exact: true }).click();
  await page.getByLabel('预算上限（总 tokens，留空为默认）').fill('1234');
  await deadlineInput.fill(deadlineLocal);
  await submitTask(page, '跑一个长任务');

  // ② 创建请求体：budget.run 三维逐字对齐 RunBudgetRequest（deadline = UTC 瞬时文本）。
  //    #509 竞态①：submitTask 只到 fill+click 为止，click resolve ≠ CDP 拦截层已在
  //    Node 侧跑完路由 handler——拦截分发是异步的，与测试的下一语句无顺序保证，负载下
  //    （--workers=2）同步 expect 会取到 null 瞬时红（无重试）。改 web-first：poll 等
  //    「请求体已被捕获」这个确定性状态，再断言形状（断言语义只增不减）。
  await expect.poll(() => createBody).not.toBeNull();
  expect((createBody as Record<string, unknown>).budget).toEqual({
    run: {
      max_agent_turns_total: 3,
      max_total_tokens: 1234,
      deadline_at: new Date(deadlineLocal).toISOString(),
    },
  });

  // ③ 预算暂停 → PausedPanel 在场（恢复前置条件逐字来自 run/paused 载荷）。
  //    面板出现在客户端退回 viewing 并重读历史之后（见上面 onWs 的传输语义注释），
  //    重连退避（500ms×2³）走完需要几秒 ⇒ 放宽这一条 expect 的超时。
  const panel = page.locator('.pause-panel');
  await expect(panel).toBeVisible({ timeout: 15_000 });
  await expect(panel).toContainText('已在预算到顶处暂停');
  await expect(panel).toContainText('抬高 run.max_total_tokens 的绝对 ceiling 后同 run 恢复');

  // ④ UI 抬高卡住的那一维 → 恢复同一 run
  await panel.getByLabel(/恢复用的绝对 ceiling/).fill('9999');
  await panel.getByRole('button', { name: '恢复同一 run' }).click();

  // ⑤ 面板收起（run/resumed 清 run_paused）之后，恢复请求体必已发出：同 run CAS
  //    + budget_increase + 抬高的绝对值（字段名由 resumeTarget 从触发维度导出）。
  //    #509 竞态②：面板卸载由 resumePausedRun **发起时**的乐观 setMode('live') 驱动
  //    （useSession.ts:1494），先于拦截层捕获 resumeBody，与 run/resumed 响应无因果
  //    ⇒ toHaveCount(0) 通过后 resumeBody 仍可能是 null（因果链不闭合）。poll 把
  //    「形状相等」本身变成重试条件：null 时不等、捕获后逐字相等才放行。
  await expect(panel).toHaveCount(0);
  await expect.poll(() => resumeBody).toEqual({
    run_id: RUN,
    resume_basis: 'budget_increase',
    budget: { expected_version: 1, run: { max_total_tokens: 9999 } },
  });

  // ⑥ 全链终态：恢复后的回答呈现（零手工 API——请求全部由界面发出）
  await expect(page.locator('.model-output').last()).toContainText('预算已抬高，继续。');
});

test('#426 不设置预算：创建载荷不含 budget 键（默认行为不变）', async ({ page }) => {
  const frames: FrameSpec[] = [
    { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
    { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
    { type: 'user/message', data: { content: '普通任务' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'model/started', data: { model: 'e2e-model' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'text/delta', data: { delta: '完成。' }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'model/completed', data: { model: 'e2e-model' }, seq: 6, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'run/completed', data: {}, seq: 7, session_id: SID, run_id: RUN, time: T },
  ];
  let createBody: Record<string, unknown> | null = null;
  await routeApi(page, {
    onSessionPost: async (route: Route) => {
      createBody = route.request().postDataJSON() as Record<string, unknown>;
      await fulfillSse(route, frames);
    },
    events: frames,
  });

  await page.goto('/');
  // 预算单入口在场（#536 壳），全空 = 后端默认（面板内三项都不填）
  await expect(page.locator('.composer-budget-trigger[aria-label="预算"]')).toBeVisible();
  await submitTask(page, '普通任务');

  // #509 竞态①（同上）：poll 到捕获为止——这里的守卫还不可省，null 上
  // not.toHaveProperty 会平凡通过，先锁「请求体已捕获」再断言「无 budget 键」。
  await expect.poll(() => createBody).not.toBeNull();
  expect(createBody).not.toHaveProperty('budget');
  await expect(page.locator('.model-output').last()).toContainText('完成。');
});

test('#1198 visual: light/dark panel matches the pinned mockup', async ({ page }, testInfo) => {
  const mockup = await page.request.get(
    'https://gist.githubusercontent.com/EricKingWhy/b087e3ca29188c86f6a068a55ee89b06/raw/b65e328d4907c44b02a1f2c2483c9c77e7c6ea71/budget-picker-mockup.html',
  );
  expect(mockup.ok()).toBeTruthy();

  const baselinePage = await page.context().newPage();
  await baselinePage.setContent(await mockup.text());
  await baselinePage.getByRole('button', { name: '优化版 v2' }).click();
  const baselineBox = await baselinePage.locator('#panel').boundingBox();
  expect(baselineBox).not.toBeNull();
  await baselinePage.close();

  await routeApi(page, { events: [] });
  await page.goto('/');
  for (const theme of ['light', 'dark'] as const) {
    await page.evaluate((value) => localStorage.setItem('ahi.theme', value), theme);
    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
    await page.locator('.composer-budget-trigger[aria-label="预算"]').click();

    const panel = page.locator('.budget-picker-content--v2');
    await expect(panel).toBeVisible();
    await expect(panel).toHaveCSS('opacity', '1');
    const layout = await panel.evaluate((element) => {
      const box = element.getBoundingClientRect();
      return {
        width: box.width,
        height: box.height,
        left: box.left,
        right: box.right,
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth,
        documentWidth: document.documentElement.scrollWidth,
        viewportWidth: document.documentElement.clientWidth,
      };
    });
    const contrast = await panel.evaluate((root) => {
      const parse = (value: string): [number, number, number, number] => {
        const parts = value.match(/[\d.]+/g)?.map(Number) ?? [];
        return [parts[0] ?? 0, parts[1] ?? 0, parts[2] ?? 0, parts[3] ?? 1];
      };
      const over = (foreground: number[], background: number[]) => {
        const alpha = foreground[3] + background[3] * (1 - foreground[3]);
        return [
          ...[0, 1, 2].map((index) =>
            alpha === 0 ? 0 : (foreground[index] * foreground[3] + background[index] * background[3] * (1 - foreground[3])) / alpha,
          ),
          alpha,
        ];
      };
      const luminance = (rgb: number[]) => {
        const channels = rgb.map((value) => {
          const channel = value / 255;
          return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
        });
        return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
      };
      const ratio = (foreground: string, element: Element) => {
        const layers: number[][] = [];
        for (let node: Element | null = element; node; node = node.parentElement) {
          const background = parse(getComputedStyle(node).backgroundColor);
          if (background[3] > 0) layers.push(background);
        }
        const background = layers.reverse().reduce<number[]>((current, layer) => over(layer, current), [255, 255, 255, 1]);
        const color = over(parse(foreground), background);
        const [lighter, darker] = [luminance(color), luminance(background)].sort((a, b) => b - a);
        return (lighter + 0.05) / (darker + 0.05);
      };
      const failures: string[] = [];
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const text = node.textContent?.trim();
        const element = node.parentElement;
        if (!text || !element || !element.getClientRects().length) continue;
        const value = ratio(getComputedStyle(element).color, element);
        if (value < 4.5) failures.push(`${text}: ${value.toFixed(2)}`);
      }
      for (const input of root.querySelectorAll<HTMLInputElement>('input[placeholder]')) {
        if (!input.getClientRects().length) continue;
        const value = ratio(getComputedStyle(input, '::placeholder').color, input);
        if (value < 4.5) failures.push(`${input.placeholder}: ${value.toFixed(2)}`);
      }
      return failures;
    });

    const screenshotPath = testInfo.outputPath(`budget-picker-v2-${theme}.png`);
    await page.screenshot({ path: screenshotPath });
    await testInfo.attach(`budget-picker-v2-${theme}`, { path: screenshotPath, contentType: 'image/png' });
    console.log(`#1198 ${theme}: panel ${layout.width}x${layout.height}; mockup ${baselineBox!.width}x${baselineBox!.height}`);

    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth);
    expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
    expect(layout.left).toBeGreaterThanOrEqual(0);
    expect(layout.right).toBeLessThanOrEqual(layout.viewportWidth);
    expect(Math.abs(layout.width - baselineBox!.width)).toBeLessThanOrEqual(8);
    expect(Math.abs(layout.height - baselineBox!.height)).toBeLessThanOrEqual(8);
    expect(contrast).toEqual([]);
  }
});
