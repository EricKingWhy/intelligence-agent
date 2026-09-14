/** #194：Composer 的「Esc 停止」提示必须与右侧停止按钮同处，**不得压在档位行上**。
 *
 *  为什么必须是 e2e：这是**布局事实**（两个矩形的相交关系），jsdom 没有布局，
 *  组件测试（SSR）拿不到 rect。所以本 spec 的全部断言都是真实浏览器里的
 *  `getBoundingClientRect()` 相交判定。
 *
 *  旧行为不是"提示位置不好看"，而是**两个 affordance 被放在相反两侧**：
 *  `.composer-esc-hint` 绝对定位在 dock 左下角（`left: var(--space-lg)`），
 *  而档位行的模型选择器正好占同一锚点——实测在 1280/1920 两档都与它相交。
 *
 *  除了"提示不压档位行"，这里还锁**动作簇与档位行不相交**（比只锁提示更强）：
 *  簇比按钮宽，窄工作区下档位行会顶到右端钻到提示底下（放宽 Inspector 到上限
 *  时实测复现）。修复靠 `.composer-dock:has(.composer-esc-hint) .composer-controls`
 *  的右预留 + flex 换行；把这条断言留在这里，是为了让"以后加档位控件"时有人
 *  立刻看到账。
 *
 *  没锁什么（如实划界）：不断言换行与否（窄工作区下换行是**允许**的诚实结果，
 *  只断言不相交）；不断言像素坐标（字号/字体度量会动，矩形关系不会）。
 */

import { expect, test, type Page } from '@playwright/test';
import {
  AGENT_PROFILES,
  CONTEXT_PROVIDERS,
  MODELS,
  PERMISSION_MODES,
  REASONING_EFFORTS,
  RUN,
  SID,
  T,
  fulfillSse,
  routeApi,
  submitTask,
  type FrameSpec,
} from './fixtures';

/** 未终态的流：run 一直 running ⇒ `streaming` 恒真、Esc 提示与停止按钮常驻
 *  （与 o-wait-hint.spec.ts 同一手法：mock 只给有限帧，模式停在 live）。 */
const LIVE_FRAMES: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '跑一下' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
];

/** 已收口的会话（非流式）：用来锁"非流式下提示必须不存在"。 */
const DONE_FRAMES: FrameSpec[] = [
  ...LIVE_FRAMES,
  { type: 'model/completed', data: { content: '跑完了' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 5, session_id: SID, run_id: RUN, time: T },
];

const sessionRow = {
  session_id: SID,
  event_count: LIVE_FRAMES.length,
  first_event_time: T,
  last_event_time: T,
  first_user_message: '跑一下',
  trace_id: null,
  trace_url: null,
};

async function routeStreaming(page: Page, frames: FrameSpec[] = LIVE_FRAMES) {
  routeApi(page, {
    sessions: [sessionRow],
    models: MODELS,
    permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
    contextProviders: CONTEXT_PROVIDERS,
    onSessionPost: (route) => fulfillSse(route, frames),
    onStreamGet: (route) => fulfillSse(route, frames),
    events: frames,
  });
}

/** 在页面里量「谁和谁相交」——相交判定必须在有布局的地方做，不能拿快照里的
 *  近似值在 Node 侧比。返回的是**越界名单**（空数组 = 干净），不是布尔值：
 *  红的时候能直接看出是哪个控件被压住了。 */
async function overlaps(page: Page) {
  return page.evaluate(() => {
    const hit = (a: DOMRect | null, b: DOMRect | null) =>
      !!a && !!b && a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
    const controls = Array.from(document.querySelectorAll('.composer-controls > *'));
    const hint = document.querySelector('.composer-esc-hint')?.getBoundingClientRect() ?? null;
    const actions = document.querySelector('.composer-actions')?.getBoundingClientRect() ?? null;
    const label = (el: Element) => el.getAttribute('aria-label') ?? el.className;
    return {
      hintVsControls: controls.filter((el) => hit(hint, el.getBoundingClientRect())).map(label),
      actionsVsControls: controls.filter((el) => hit(actions, el.getBoundingClientRect())).map(label),
    };
  });
}

test('#194：流式中提示与停止按钮同侧，且不压任何档位控件（默认宽度与 Inspector 上限两种布局）', async ({
  page,
}) => {
  await routeStreaming(page);
  await page.goto('/');
  await submitTask(page, '跑一下');

  const hint = page.locator('.composer-esc-hint');
  const stop = page.locator('.composer-stop');
  await expect(hint).toBeVisible();
  await expect(stop).toBeVisible();
  // 提示在右、按钮在右，且提示在按钮**左边**（同一处，不是一个在左一个在右）
  const [hintBox, stopBox, dockBox] = await Promise.all([
    hint.boundingBox(),
    stop.boundingBox(),
    page.locator('.composer-dock').boundingBox(),
  ]);
  if (!hintBox || !stopBox || !dockBox) throw new Error('动作簇没有布局盒');
  expect(hintBox.x).toBeGreaterThan(dockBox.x + dockBox.width / 2);
  expect(hintBox.x + hintBox.width).toBeLessThanOrEqual(stopBox.x + 1); // +1：1px 舍入

  expect(await overlaps(page)).toEqual({ hintVsControls: [], actionsVsControls: [] });

  /* 再用"窄工作区"复现一次：把 Inspector 拖到上限 480（向左拖 = 变宽，#197 语义），
     此时中心列只剩 ~560px、档位行会顶到右端——正是动作簇必须让位的那种布局。
     不用更小的视口代替：e2e 只在 1280/1920 两档跑，拖宽能造出比两者都窄的工作区。 */
  const resizer = page.locator('.detail-resizer');
  await expect(resizer).toBeVisible();
  const box = await resizer.boundingBox();
  if (!box) throw new Error('拖宽手柄没有布局盒');
  const y = box.y + Math.min(120, box.height / 2);
  await page.mouse.move(box.x + 3, y);
  await page.mouse.down();
  await page.mouse.move(box.x + 3 - 600, y, { steps: 8 });
  await page.mouse.up();
  await expect(resizer).toHaveAttribute('aria-valuenow', '480');

  // 提示仍与按钮同侧，且没有任何档位控件被压住
  await expect(hint).toBeVisible();
  expect(await overlaps(page)).toEqual({ hintVsControls: [], actionsVsControls: [] });
});

test('#194：非流式不渲染提示；发送按钮仍在（提示只属于流式态）', async ({ page }) => {
  await routeStreaming(page, DONE_FRAMES);
  await page.goto('/');
  await page.locator('.session-item').first().click();

  await expect(page.locator('.model-output').last()).toContainText('跑完了');
  await expect(page.locator('.composer-esc-hint')).toHaveCount(0);
  await expect(page.locator('.composer-stop')).toHaveCount(0);
  await expect(page.locator('.composer-send')).toBeVisible();
  // 非流式下档位行不该有那段右预留（预留只属于提示在场时）
  expect(await overlaps(page)).toEqual({ hintVsControls: [], actionsVsControls: [] });
});

test('#194：提示只是提示——真实中断仍由 Esc 与停止按钮触发（各一次 POST /cancel）', async ({
  page,
}) => {
  let cancels = 0;
  await routeStreaming(page);
  // 后注册的 route 先匹配 ⇒ 这条盖住 routeApi 的缺省处理
  await page.route('**/api/sessions/*/cancel', async (route) => {
    cancels += 1;
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"ok"}' });
  });
  /* 停止按钮带 `animation: breathe`（scale 1↔1.12，无限循环）——它**永远不是**
     "stable"，Playwright 的默认可点性检查会一直重试到超时（本票实测：从未被点过
     的控件，所以此前没人发现）。开 reduced motion 让全局兜底
     （`index.css:359` 把 animation 压到 0.01ms/1 次）停掉动画——顺带证明那条兜底
     真的覆盖到这个按钮，而不是用 `force: true` 把检查关掉、把问题留在那里。 */
  await page.emulateMedia({ reducedMotion: 'reduce' });

  await page.goto('/');
  await submitTask(page, '跑一下');
  await expect(page.locator('.composer-esc-hint')).toBeVisible();

  // 提示自己不可点（pointer-events: none）——它是可见性，不是按钮
  const hintPointer = await page.evaluate(
    () => getComputedStyle(document.querySelector('.composer-esc-hint')!).pointerEvents,
  );
  expect(hintPointer).toBe('none');

  // 停止按钮必须真的可点（动作簇整体 pointer-events: none，只有按钮放开）
  await page.locator('.composer-stop').click();
  await expect.poll(() => cancels, { message: '停止按钮必须发出 POST /cancel' }).toBe(1);

  // Esc 走的是 App 全局键绑定（不经过本组件），路径必须还在
  await page.keyboard.press('Escape');
  await expect.poll(() => cancels, { message: 'Esc 必须发出 POST /cancel' }).toBe(2);
});
