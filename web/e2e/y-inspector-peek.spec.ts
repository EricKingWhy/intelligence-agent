/** 场景 #183：Inspector 升级——清单+详情同框、peek 升级链（↑↓ / Esc / Space /
 *  钉住 / 整页 / 拖宽）。
 *
 *  为什么这些必须走 e2e：本仓组件测试是 SSR（只有首帧），而本票的九条 AC 里有
 *  八条是**交互**——选中项移动、键位、钉住跨会话、整页往返、拖宽不持久化。
 *  SSR 测试锁结构（`StepDetail.peek.test.tsx`），这里锁行为，两者不重叠。
 *
 *  场景数据用**历史会话**（`GET /events` 加载）而不是流式建会话：键盘用的会话应当
 *  是静止的，否则"↑↓ 之后哪一行被选中"会被流式新增行与贴底跟随搅在一起，测出来的
 *  是时序巧合而不是选中语义。
 */

import { expect, test } from '@playwright/test';
import { RUN, SID, T, routeApi, type FrameSpec } from './fixtures';

/** 6 条事件的静止会话：足够让 ↑↓ 有"上/下"两个方向可走。 */
const EVENTS: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '看看这个' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'tool/call', data: { tool_call_id: 'tc-1', tool_name: 'bash', args: { command: 'ls' } }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'tool/result', data: { tool_call_id: 'tc-1', content: '{"ok":true}' }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 6, session_id: SID, run_id: RUN, time: T },
];

const session = (id: string, title: string) => ({
  session_id: id, event_count: EVENTS.length, first_event_time: T, last_event_time: T,
  first_user_message: title, trace_id: null, trace_url: null,
});

/** 打开会话（Inspector 在 ≥1200px 默认展开），返回 Timeline 行 locator。 */
async function openSession(page: import('@playwright/test').Page, which = 0) {
  await page.locator('.session-item').nth(which).click();
  const rows = page.locator('.timeline-row');
  await expect(rows).toHaveCount(EVENTS.length);
  return rows;
}

/** 与 EVENTS 同形，只在 run/started 上带**最长档位名**（内置档位里最长的一个）。
 *  档位名直接进头部徽标，长度就是头部宽度需求的来源。 */
const LONG_PROFILE: FrameSpec[] = EVENTS.map((f) =>
  f.type === 'run/started' ? { ...f, data: { agent_profile: 'research_review' } } : f,
);

test('AC1：点一行即预览；↑↓ 移动选中，详情实时跟随且清单不消失', async ({ page }) => {
  await routeApi(page, { sessions: [session(SID, '看看这个')], events: EVENTS });
  await page.goto('/');
  const rows = await openSession(page);

  // 鼠标点击即选中即预览（AC3：不做键盘唯一）
  await rows.nth(1).click();
  await expect(rows.nth(1)).toHaveAttribute('aria-current', 'true');
  const peek = page.locator('.detail-peek');
  await expect(peek).toBeVisible();
  await expect(peek.locator('.detail-peek-kind')).toHaveText('run/started');

  // ↑ 回到第一条 → 详情跟着换（不重新挂载：容器还是同一个）
  await page.keyboard.press('ArrowUp');
  await expect(rows.nth(0)).toHaveAttribute('aria-current', 'true');
  await expect(peek.locator('.detail-peek-kind')).toHaveText('session/started');

  // ↓ 两次 → 到第三条（user/message）
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await expect(rows.nth(2)).toHaveAttribute('aria-current', 'true');
  await expect(peek.locator('.detail-peek-kind')).toHaveText('user/message');

  // 清单仍在（同框）——此前 focus 非 run 时清单整个被替换掉
  await expect(rows).toHaveCount(EVENTS.length);
  // 只有一行是当前项（aria 如实，不是"每行都亮"）
  await expect(page.locator('.timeline-row[aria-current="true"]')).toHaveCount(1);

  // 两端不环绕：在第一条上再按 ↑ 仍停在第一条
  await rows.nth(0).click();
  await page.keyboard.press('ArrowUp');
  await expect(rows.nth(0)).toHaveAttribute('aria-current', 'true');
});

test('AC2：Esc 关预览但面板与清单都还在（关闭不卸载）', async ({ page }) => {
  await routeApi(page, { sessions: [session(SID, '看看这个')], events: EVENTS });
  await page.goto('/');
  const rows = await openSession(page);

  await rows.nth(2).click();
  const peek = page.locator('.detail-peek');
  await expect(peek).toBeVisible();

  await page.keyboard.press('Escape');

  // 关掉：视觉上不可见，但**仍在 DOM 里**（hidden 而不是卸载）
  await expect(peek).toBeHidden();
  await expect(peek).toHaveCount(1);
  // 面板与清单原封不动
  await expect(page.locator('.step-detail')).toBeVisible();
  await expect(rows).toHaveCount(EVENTS.length);
  // 选中项保留：再按 Space 能立刻把同一个目标的预览召回来（下面那条用例直接接着验）
  await page.keyboard.press('Space');
  await expect(peek).toBeVisible();
  await expect(peek.locator('.detail-peek-kind')).toHaveText('user/message');
});

test('AC2：Space 快按=保持打开，按住=松手关闭（Linear peek 语义）', async ({ page }) => {
  await routeApi(page, { sessions: [session(SID, '看看这个')], events: EVENTS });
  await page.goto('/');
  const rows = await openSession(page);

  await rows.nth(1).click();
  const peek = page.locator('.detail-peek');
  await page.keyboard.press('Escape');
  await expect(peek).toBeHidden();

  // 快按：松开后仍然开着
  await page.keyboard.press('Space');
  await expect(peek).toBeVisible();

  // 按住 ~450ms（阈值 250ms）再松手：临时预览，松手即关
  await page.keyboard.down('Space');
  await page.waitForTimeout(450);
  await page.keyboard.up('Space');
  await expect(peek).toBeHidden();

  // 关掉预览不影响选中项与清单
  await expect(rows.nth(1)).toHaveAttribute('aria-current', 'true');
  await expect(rows).toHaveCount(EVENTS.length);
});

test('AC4：钉住跨会话——未钉住切换会话收起，钉住后保持打开', async ({ page }) => {
  const OTHER = 'e2e-session-0183b';
  await routeApi(page, {
    sessions: [session(SID, '第一个'), session(OTHER, '第二个')],
    events: EVENTS,
  });
  await page.goto('/');
  await openSession(page, 0);
  await expect(page.locator('.step-detail')).toBeVisible();

  // 未钉住：切到另一个会话 → 面板收起（仍是 DOM，只是 0 宽不可见）
  await page.locator('.session-item').nth(1).click();
  await expect(page.locator('.app-regions')).toHaveClass(/inspector-closed/);
  await expect(page.locator('.step-detail')).toBeHidden();

  // 打开面板 → 钉住
  await page.getByRole('button', { name: '展开 Inspector' }).click();
  await expect(page.locator('.step-detail')).toBeVisible();
  const pin = page.locator('.detail-ctrl[aria-label="钉住"]');
  await expect(pin).toHaveAttribute('aria-pressed', 'false');
  await pin.click();
  await expect(pin).toHaveAttribute('aria-pressed', 'true');

  // 钉住后：来回切会话都不收起
  await page.locator('.session-item').nth(0).click();
  await expect(page.locator('.app-regions')).not.toHaveClass(/inspector-closed/);
  await expect(page.locator('.step-detail')).toBeVisible();
  await page.locator('.session-item').nth(1).click();
  await expect(page.locator('.app-regions')).not.toHaveClass(/inspector-closed/);

  // 钉住是视图状态、不持久化：刷新后回到未钉住
  await page.reload();
  await expect(page.locator('.detail-ctrl[aria-label="钉住"]')).toHaveAttribute('aria-pressed', 'false');
});

test('AC5：整页往返（面板按钮 + 命令面板两个入口）', async ({ page }) => {
  await routeApi(page, { sessions: [session(SID, '看看这个')], events: EVENTS });
  await page.goto('/');
  await openSession(page);

  const expand = page.locator('.detail-ctrl[aria-label="整页"]');
  await expect(expand).toHaveAttribute('aria-pressed', 'false');
  await expand.click();

  // 整页：面板占满，rail 与中心列收起但仍挂载
  await expect(page.locator('.app-regions')).toHaveClass(/inspector-fullpage/);
  await expect(page.locator('.step-detail')).toBeVisible();
  await expect(page.locator('.session-rail')).toBeHidden();
  await expect(expand).toHaveAttribute('aria-pressed', 'true');

  // Esc 先退回整页（分层：这一层不通，用户会以为卡死在整页里）
  await page.locator('.timeline-row').first().click();
  await page.keyboard.press('Escape');
  await expect(page.locator('.app-regions')).not.toHaveClass(/inspector-fullpage/);
  await expect(page.locator('.session-rail')).toBeVisible();

  // 第二入口：命令面板
  await page.keyboard.press('Control+k');
  await page.locator('.palette-input').fill('整页');
  await expect(page.locator('.palette-item', { hasText: '整页打开 Inspector' })).toBeVisible();
  await page.locator('.palette-item', { hasText: '整页打开 Inspector' }).click();
  await expect(page.locator('.app-regions')).toHaveClass(/inspector-fullpage/);

  // 退回（同一个按钮）
  await expand.click();
  await expect(page.locator('.app-regions')).not.toHaveClass(/inspector-fullpage/);
  await expect(page.locator('.app-workspace')).toBeVisible();
});

test('AC6：#197 手柄方向=向右变窄/向左变宽，夹取到 320/480 且不压垮中心列；刷新后回到 340（不持久化）', async ({
  page,
}) => {
  await routeApi(page, { sessions: [session(SID, '看看这个')], events: EVENTS });
  await page.goto('/');
  await openSession(page);

  const resizer = page.locator('.detail-resizer');
  // 初始宽度 340（#197）：刻意高于下限 320，否则"向右拖"在默认宽度下就被夹住、
  // 看不出有没有生效。
  await expect(resizer).toHaveAttribute('aria-valuenow', '340');
  await expect(resizer).toHaveAttribute('aria-valuemin', '320');
  await expect(resizer).toHaveAttribute('aria-valuemax', '480');

  /* 每次拖拽前**重新量**手柄位置：拖宽会把手柄本身挪走，用上一次的坐标再按下去
     会落在面板内容上（第一次拖动后 x 就不再是初始值）。 */
  const dragBy = async (dx: number) => {
    const box = await resizer.boundingBox();
    if (!box) throw new Error('拖宽手柄没有布局盒');
    const y = box.y + Math.min(120, box.height / 2);
    await page.mouse.move(box.x + 3, y);
    await page.mouse.down();
    await page.mouse.move(box.x + 3 + dx, y, { steps: 8 });
    await page.mouse.up();
  };

  // 面板右停靠、手柄在它的**左缘** ⇒ 指针向右 = 手柄把中心列推宽 = 面板变窄。
  // 这条与"向右拖变大"的旧断言相反，就是 #197 本身（旧行为反直觉）。
  //
  // 注意这条用例**证明不了**"上限会被中心列压低"（AC6 的 `CENTER_MIN_W`）：三栏栅格
  // 只在 ≥1201px 生效，那里 available = viewport − 240 ≥ 961，`available − 360`
  // 恒 > 480 ⇒ 上限恒为 480。要构造"上限真的咬住"的宽度得先改布局（<1200px 的折叠
  // 分支把面板列写死成 280px）。所以这里锁的是**接线**（拖拽确实走到夹取、中心列没被
  // 挤没），而夹取公式本身由 `src/lib/inspectorPanel.test.ts` 的
  // `clampInspectorWidth(480, 700) === 340` 锁住——两处各管一段，别互相冒充。
  // 右拖到远超下限的位置：夹取到 320（下限）。
  await dragBy(600);
  await expect(resizer).toHaveAttribute('aria-valuenow', '320');
  // 中心列没被压垮（AC6 的"不得压垮"）
  const workspace = await page.locator('.app-workspace').boundingBox();
  expect(workspace?.width ?? 0).toBeGreaterThanOrEqual(360);

  // 往左拖 = 变宽，夹到上限 480
  await dragBy(-600);
  await expect(resizer).toHaveAttribute('aria-valuenow', '480');

  // 不持久化（不变量 #22）：拖到一个**不在夹取边界上**的宽度，刷新后必须回到默认。
  // 特意避开 320/480：边界值分不清"拖过"与"根本没拖动"，用它证明不了刷新抹掉了改动。
  await dragBy(100); // 向右变窄：480 − 100 = 380
  await expect(resizer).toHaveAttribute('aria-valuenow', '380');
  await page.reload();
  await openSession(page);
  await expect(page.locator('.detail-resizer')).toHaveAttribute('aria-valuenow', '340');
});

test('AC7：面板控制都键盘可达（Tab 到拖宽手柄可用方向键调宽）', async ({ page }) => {
  await routeApi(page, { sessions: [session(SID, '看看这个')], events: EVENTS });
  await page.goto('/');
  await openSession(page);

  const resizer = page.locator('.detail-resizer');
  await resizer.focus();
  await expect(resizer).toBeFocused();
  // 键盘与指针**同源**（#197）：ArrowRight 把"手柄"向右推 ⇒ 面板变窄。只翻指针
  // 会让同一根分隔条上留下两套矛盾方向。
  await page.keyboard.press('ArrowRight');
  await expect(resizer).toHaveAttribute('aria-valuenow', '324'); // 340 − 16
  await page.keyboard.press('ArrowLeft');
  await expect(resizer).toHaveAttribute('aria-valuenow', '340');

  // 三个按钮都在 Tab 序列里，且名字可读
  await expect(page.locator('.detail-ctrl[aria-label="钉住"]')).toBeVisible();
  await expect(page.locator('.detail-ctrl[aria-label="整页"]')).toBeVisible();
  await expect(page.locator('button[aria-label="关闭 Inspector"]')).toBeVisible();

  // 关闭按钮 = 收起面板（不卸载）
  await page.locator('button[aria-label="关闭 Inspector"]').click();
  await expect(page.locator('.app-regions')).toHaveClass(/inspector-closed/);
  await expect(page.getByRole('button', { name: '展开 Inspector' })).toBeVisible();
});

test('AC8：头部必须容得下最长档位名（不溢出面板、控制按钮不被顶出去）', async ({ page }) => {
  /* 真机验收 P2-2（docs/LIVE_BROWSER_TEST_20260916.md F5）。修复前实测（面板 340、
   * 档位 research_review）：`.detail-header` scrollWidth 368 > clientWidth 308，
   * `N runs · M 事件` 被压成 30px×90px 的**7 行竖条**（头部因此从 36 高变 99 高），
   * `.detail-header-actions` 被顶到面板右缘之外 44px —— 关闭按钮在视口外，点不到。
   *
   * 为什么档位名要专门测：它是**后端下发的标识符**，长度不受前端控制，而它直接进
   * 头部这一条定长 flex 行。默认档位（`main` / 旧数据「档位未知」）短，掩盖了问题；
   * 内置档位里 `research_review` 最长，就该用它当输入。
   *
   * 三个宽度都要过：340 是默认值，320/480 是 AC6 的上下夹取边界——只在默认宽度
   * 上量，等于假设用户从不拖面板。 */
  await routeApi(page, { sessions: [session(SID, '看看这个')], events: LONG_PROFILE });
  await page.goto('/');
  await openSession(page);
  await expect(page.locator('.detail-profile-badge')).toHaveText('research_review');

  const geometry = () =>
    page.evaluate(() => {
      const header = document.querySelector('.detail-header');
      if (!header) throw new Error('没有 .detail-header');
      const hb = header.getBoundingClientRect();
      const kids = [...header.children].map((c) => {
        const r = c.getBoundingClientRect();
        return { cls: (c.className || '').split(' ')[0], right: r.right - hb.right, height: Math.round(r.height) };
      });
      return { sw: header.scrollWidth, cw: header.clientWidth, right: Math.round(hb.right), kids };
    });

  const resizer = page.locator('.detail-resizer');
  const widths: Array<[string, () => Promise<void>]> = [
    ['默认 340', async () => {}],
    ['最小 320', async () => {
      for (let i = 0; i < 5; i++) await page.keyboard.press('ArrowRight');
      await expect(resizer).toHaveAttribute('aria-valuenow', '320');
    }],
    ['最大 480', async () => {
      for (let i = 0; i < 12; i++) await page.keyboard.press('ArrowLeft');
      await expect(resizer).toHaveAttribute('aria-valuenow', '480');
    }],
  ];

  await resizer.focus();
  for (const [label, step] of widths) {
    await step();
    const g = await geometry();
    // 行内不许有横向溢出：定长项不收缩、弹性项真能截断，两者都对才可能相等。
    expect(g.sw, `${label}：头部横向溢出`).toBeLessThanOrEqual(g.cw + 1);
    // 每个子项都待在头部右缘之内（含交互控件——越界 = 点不到）。
    for (const kid of g.kids) {
      if (kid.height === 0) continue; // 窄宽下退出头部的项
      expect(kid.right, `${label}：${kid.cls} 越出头部右缘`).toBeLessThanOrEqual(1);
    }
    // 状态徽标锁单行（F1：它曾被压成 48px 宽、逐字折三行 → 高 66）
    const badge = g.kids.find((k) => k.cls === 'run-badge');
    expect(badge?.height, `${label}：状态徽标折行`).toBeLessThanOrEqual(30);
    // 范围文本要么退出、要么单行；不得再被压成多行竖条（曾出现 90px 高）
    const runId = g.kids.find((k) => k.cls === 'detail-run-id');
    if (runId && runId.height > 0) expect(runId.height, `${label}：范围被压成竖条`).toBeLessThanOrEqual(30);
  }

  /* 范围文本的**去留边界**要显式锁住（复审 P2-1）：窄面板（≤360 容器宽，含默认
   * 340）它整体退出头部、宽面板（480）必须完整可见。不锁的话，"默认宽度下它其实是
   * 隐藏的"这件事没有任何测试会发现——`g-visual-qa.spec.ts` 对它用的是 `toHaveText`，
   * 隐藏元素照样通过。这是本批对 UI-03「头标显示 N runs · M 事件」的**有意偏离**
   * （340 + 长档位名下所有需宽项无法共存，见 CSS 注释里的取舍），所以更要锁死方向，
   * 免得将来有人无声地改回去又改坏溢出。 */
  // 上一步停在 480；两个方向都按"多按几次撞夹取边界"来走（步长 16，夹取 320/480）
  for (let i = 0; i < 12; i++) await page.keyboard.press('ArrowRight');
  await expect(resizer).toHaveAttribute('aria-valuenow', '320');
  await expect(page.locator('.detail-run-id')).toBeHidden();
  for (let i = 0; i < 12; i++) await page.keyboard.press('ArrowLeft');
  await expect(resizer).toHaveAttribute('aria-valuenow', '480');
  await expect(page.locator('.detail-run-id')).toBeVisible();
  // 单 run / 6 事件的 fixture（EVENTS）→ 文案逐字（含中文单位，不是裸 run_id）
  await expect(page.locator('.detail-run-id')).toHaveText('1 runs · 6 事件');

  // 最窄处仍然**真的能点**（Playwright 会做可操作性检查：视口外/被遮挡会超时）
  for (let i = 0; i < 12; i++) await page.keyboard.press('ArrowRight');
  await expect(resizer).toHaveAttribute('aria-valuenow', '320');
  await page.locator('button[aria-label="关闭 Inspector"]').click();
  await expect(page.locator('.app-regions')).toHaveClass(/inspector-closed/);
});

test('AC9：窄面板下**子会话**头部仍显示 child id（窄宽退出只对 run 头部生效）', async ({ page }) => {
  /* 复审 P3：`.detail-run-id` 是**两个**头部共用的类——run 头部（`N runs · M 事件`，
   * AC8 里窄宽退出）与子会话头部（`childSessionId` 前 8 字符，钻取视图里**唯一**的
   * 会话标识）。容器查询若写成无作用域的 `.detail-run-id { display:none }`，会把后者
   * 一起删掉——而这条钻取路径此前**零 e2e 覆盖**，删了不会有人发现。
   * 本用例同时是 `StepDetail` 子会话头部的第一条行为覆盖：点委派节点的
   * 「Inspect 子会话」→ 头部原位换成子会话视图，child id 必须看得见。
   *
   * 注：child 视图里**没有** `.detail-resizer`（实测 count=0），所以这里的"窄面板"
   * 就是默认 340（≤360 ⇒ 容器查询处于生效状态），不需要拖宽——这正是要锁的场景。 */
  const CHILD = 'child-abc12345';
  const frames: FrameSpec[] = [
    EVENTS[0],
    EVENTS[1],
    EVENTS[2],
    { type: 'agent/delegation-started', data: { child_session_id: CHILD, target: '研究', task: '查一下' }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'run/completed', data: {}, seq: 5, session_id: SID, run_id: RUN, time: T },
  ];
  await routeApi(page, {
    sessions: [{ session_id: SID, event_count: frames.length, first_event_time: T, last_event_time: T, first_user_message: '看看这个', trace_id: null, trace_url: null }],
    events: frames,
  });
  await page.goto('/');
  await page.locator('.session-item').first().click();
  await expect(page.locator('.timeline-row')).toHaveCount(frames.length);
  await expect(page.locator('.detail-resizer')).toHaveAttribute('aria-valuenow', '340'); // 窄面板（≤360）

  await page.locator('.deleg-open-btn').first().click();

  const header = page.locator('.detail-header');
  await expect(header.locator('.child-back-btn')).toBeVisible(); // 确实进了子会话视图
  await expect(header.locator('.detail-header-actions')).toHaveCount(0); // 它不是 run 头部
  await expect(header.locator('.detail-run-id')).toBeVisible();
  await expect(header.locator('.detail-run-id')).toHaveText(CHILD.slice(0, 8));
});
