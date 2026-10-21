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
import { RUN, SID, T, clickTopBarMenuItem, inspectorCeiling, routeApi, type FrameSpec } from './fixtures';

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

/* 本文件是 Inspector 的**交互**用例：每条都要真的点、拖、按键盘，单条在本机
 * （2 vCPU）实测 5–20s，两 worker 并行 + dev server 编译时更慢——30s 默认超时
 * 会把"跑得慢"报成"断言失败"（实测 AC2/AC4 在并行跑全量时红、单跑绿）。
 * 放宽的是**时限**，断言一字未动。 */
test.describe.configure({ timeout: 90_000 });

test('AC1：点卡片就地展开；↑↓ 移动选中且 peek 不出现；Enter 开合', async ({ page }) => {
  await routeApi(page, { sessions: [session(SID, '看看这个')], events: EVENTS });
  await page.goto('/');
  const rows = await openSession(page);

  // 鼠标点击 = 选中 + 就地展开；不再开右侧 peek（#1419 的新交互）。
  await rows.nth(1).click();
  await expect(rows.nth(1)).toHaveAttribute('aria-current', 'true');
  const expanded = page.locator('.timeline-card.expanded');
  await expect(expanded).toHaveCount(1);
  await expect(expanded.locator('.tl-drill')).toContainText('run/started');
  await expect(page.locator('.detail-peek')).toBeHidden();

  // ↑ 回到第一条：选中移动，但展开卡不被清掉（展开是独立视图状态）。
  await page.keyboard.press('ArrowUp');
  await expect(rows.nth(0)).toHaveAttribute('aria-current', 'true');
  await expect(expanded).toHaveCount(1);
  await expect(page.locator('.detail-peek')).toBeHidden();

  // ↓ 两次 → 第三条（user/message）。
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await expect(rows.nth(2)).toHaveAttribute('aria-current', 'true');
  await expect(page.locator('.detail-peek')).toBeHidden();

  // Enter 开合当前选中卡片。
  await page.keyboard.press('Enter');
  await expect(rows.nth(2)).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('.timeline-card.expanded')).toHaveCount(2);
  await page.keyboard.press('Enter');
  await expect(rows.nth(2)).toHaveAttribute('aria-expanded', 'false');
  await expect(page.locator('.timeline-card.expanded')).toHaveCount(1);

  // 清单仍在（同框）——此前 focus 非 run 时清单整个被替换掉。
  await expect(rows).toHaveCount(EVENTS.length);
  await expect(page.locator('.timeline-row[aria-current="true"]')).toHaveCount(1);
});

test('AC2：Esc 分层——先收展开卡，再关 peek；面板与清单都还在', async ({ page }) => {
  await routeApi(page, { sessions: [session(SID, '看看这个')], events: EVENTS });
  await page.goto('/');
  const rows = await openSession(page);

  await rows.nth(2).click();
  const expanded = page.locator('.timeline-card.expanded');
  await expect(expanded).toHaveCount(1);

  // Space 语义不变：召出同一选中项的 peek。
  await page.keyboard.press('Space');
  const peek = page.locator('.detail-peek');
  await expect(peek).toBeVisible();

  // 第一层 Esc：只收展开卡，peek 与面板不动。
  await page.keyboard.press('Escape');
  await expect(expanded).toHaveCount(0);
  await expect(peek).toBeVisible();
  await expect(page.locator('.step-detail')).toBeVisible();

  // 第二层 Esc：关 peek（hidden 而不是卸载），面板与清单原封不动。
  await page.keyboard.press('Escape');
  await expect(peek).toBeHidden();
  await expect(peek).toHaveCount(1);
  await expect(page.locator('.step-detail')).toBeVisible();
  await expect(rows).toHaveCount(EVENTS.length);

  // 选中项保留：Space 能立刻把同一个目标的预览召回来。
  await page.keyboard.press('Space');
  await expect(peek).toBeVisible();
  await expect(peek.locator('.detail-peek-kind')).toHaveText('user/message');
});

test('AC2：Space 快按=保持打开，按住=松手关闭（Linear peek 语义）', async ({ page }) => {
  await routeApi(page, { sessions: [session(SID, '看看这个')], events: EVENTS });
  await page.goto('/');
  const rows = await openSession(page);

  await rows.nth(1).click();
  await page.keyboard.press('Escape'); // 先收掉点击产生的展开卡
  const peek = page.locator('.detail-peek');
  await expect(peek).toBeHidden();

  // 快按：松开后仍然开着
  await page.keyboard.press('Space');
  await expect(peek).toBeVisible({ timeout: 10000 });

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
  await clickTopBarMenuItem(page, '展开 Inspector');
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

  // Esc 先退回整页（分层：这一层不通，用户会以为卡死在整页里）。
  // #1419 后点击卡片会先展开一层，所以第一下 Esc 收展开、第二下才退整页。
  await page.locator('.timeline-row').first().click();
  await expect(page.locator('.timeline-card.expanded')).toHaveCount(1);
  await page.keyboard.press('Escape');
  await expect(page.locator('.timeline-card.expanded')).toHaveCount(0);
  await expect(page.locator('.app-regions')).toHaveClass(/inspector-fullpage/);
  await page.keyboard.press('Escape');
  await expect(page.locator('.app-regions')).not.toHaveClass(/inspector-fullpage/);
  await expect(page.locator('.session-rail')).toBeVisible();

  // 第二入口：命令面板
  await page.keyboard.press('Control+k');
  await page.locator('.palette-input').fill('整页');
  await expect(page.locator('.palette-item', { hasText: '整页打开运行详情' })).toBeVisible();
  await page.locator('.palette-item', { hasText: '整页打开运行详情' }).click();
  await expect(page.locator('.app-regions')).toHaveClass(/inspector-fullpage/);

  // 退回（同一个按钮）
  await expand.click();
  await expect(page.locator('.app-regions')).not.toHaveClass(/inspector-fullpage/);
  await expect(page.locator('.app-workspace')).toBeVisible();
});

test('AC6：#197 手柄方向=向右变窄/向左变宽，夹取到 320/820 且不压垮中心列；刷新后回到 520（不持久化）', async ({
  page,
}) => {
  await routeApi(page, { sessions: [session(SID, '看看这个')], events: EVENTS });
  await page.goto('/');
  await openSession(page);

  const resizer = page.locator('.detail-resizer');
  // 初始宽度 520（#1419 效果图口径；#197 时为 340）：刻意高于下限 320，否则"向右拖"
  // 在默认宽度下就被夹住、看不出有没有生效。
  await expect(resizer).toHaveAttribute('aria-valuenow', '520');
  await expect(resizer).toHaveAttribute('aria-valuemin', '320');
  await expect(resizer).toHaveAttribute('aria-valuemax', '820');

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
  // 上限是 `min(820, available − 360)`（AC6 的中心列保护）。#1419 把上限从 480
  // 提到 820 后，这条夹取**真的会咬住**：available 在三栏栅格里随视口走（1280 视口
  // 1020 ⇒ 上限 660，1920 视口 1678 ⇒ 820）。所以期望上限现量，不写死 820。
  const expectedMax = await inspectorCeiling(page);

  // 右拖到远超下限的位置：夹取到 320（下限）。
  await dragBy(600);
  await expect(resizer).toHaveAttribute('aria-valuenow', '320');
  // 中心列没被压垮（AC6 的"不得压垮"）
  const workspace = await page.locator('.app-workspace').boundingBox();
  expect(workspace?.width ?? 0).toBeGreaterThanOrEqual(360);

  // 往左拖 = 变宽，夹到 `min(820, available − 360)`
  await dragBy(-600);
  await expect(resizer).toHaveAttribute('aria-valuenow', String(expectedMax));

  // 不持久化（不变量 #22）：拖到一个**不在夹取边界上**的宽度，刷新后必须回到默认。
  // 特意避开 320/上限：边界值分不清"拖过"与"根本没拖动"，用它证明不了刷新抹掉了改动。
  await dragBy(100); // 向右变窄：上限 − 100
  await expect(resizer).toHaveAttribute('aria-valuenow', String(expectedMax - 100));
  await page.reload();
  await openSession(page);
  await expect(page.locator('.detail-resizer')).toHaveAttribute('aria-valuenow', '520');
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
  await expect(resizer).toHaveAttribute('aria-valuenow', '504'); // 520 − 16
  await page.keyboard.press('ArrowLeft');
  await expect(resizer).toHaveAttribute('aria-valuenow', '520');

  // 三个按钮都在 Tab 序列里，且名字可读
  await expect(page.locator('.detail-ctrl[aria-label="钉住"]')).toBeVisible();
  await expect(page.locator('.detail-ctrl[aria-label="整页"]')).toBeVisible();
  await expect(page.locator('button[aria-label="关闭运行详情"]')).toBeVisible();

  // 关闭按钮 = 收起面板（不卸载）
  await page.locator('button[aria-label="关闭运行详情"]').click();
  await expect(page.locator('.app-regions')).toHaveClass(/inspector-closed/);
  // #1372：开关收进「更多工具」菜单；菜单关闭即卸载，先打开再断言。
  await page.getByRole('button', { name: '更多工具' }).click();
  await expect(page.getByRole('menuitem', { name: '展开 Inspector' })).toBeVisible();
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
   * 三个宽度都要过：520 是默认值，320/820 是 AC6 的上下夹取边界——只在默认宽度
   * 上量，等于假设用户从不拖面板。 */
  await routeApi(page, { sessions: [session(SID, '看看这个')], events: LONG_PROFILE });
  await page.goto('/');
  await openSession(page);
  await expect(page.locator('.detail-profile-badge')).toHaveText('research_review');

  /* #1419：头部改成分组结构（`.detail-header-main` 装标题/副行，`.detail-header-actions`
   * 装控件），"有没有越界"要看**两级**直接子项，只看一级会把越界的副行漏掉。 */
  const geometry = () =>
    page.evaluate(() => {
      const header = document.querySelector('.detail-header');
      if (!header) throw new Error('没有 .detail-header');
      const hb = header.getBoundingClientRect();
      const row = (c: Element) => {
        const r = c.getBoundingClientRect();
        return { cls: (c.className || '').split(' ')[0], right: r.right - hb.right, height: Math.round(r.height) };
      };
      const kids = [...header.children].flatMap((c) => [row(c), ...[...c.children].map(row)]);
      const box = (sel: string) => {
        const el = header.querySelector(sel);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { right: r.right - hb.right, height: Math.round(r.height) };
      };
      return {
        sw: header.scrollWidth,
        cw: header.clientWidth,
        right: Math.round(hb.right),
        kids,
        badge: box('.run-badge'),
        runId: box('.detail-run-id'),
      };
    });

  const resizer = page.locator('.detail-resizer');
  /* 上限 = `min(820, available − 360)`；available 随视口变（1280 → 1020 ⇒ 660，
   * 1920 → 1678 ⇒ 820）。按组件同口径现量，别写死 820。 */
  const ceiling = await inspectorCeiling(page);
  /* 步长 16（Shift 64）。这里一律用 **Shift + 越界** 撞夹取边界：夹取会把越过的
   * 部分吃掉，所以不需要精确步数——而 30 次以上的单击（每次触发整面板重渲染）
   * 会把用例推到 30s 超时（实测）。 */
  const slam = async (key: 'ArrowRight' | 'ArrowLeft', times: number) => {
    for (let i = 0; i < times; i++) await page.keyboard.press(`Shift+${key}`);
  };
  /* 320 ↔ 上限 需要多少次 Shift+方向键：`ceil(跨度/64) + 1`（多出来的一下正好被夹取
   * 吃掉，用来证明"越界被夹住"）。按视口现算——1920 的上限 820 比 1280 的 660 远。 */
  const span = Math.ceil((ceiling - 320) / 64) + 1;
  const widths: Array<[string, () => Promise<void>]> = [
    ['默认 520', async () => {}],
    ['最小 320', async () => {
      await slam('ArrowRight', 4); // 520 − 4×64 < 320 ⇒ 夹到 320
      await expect(resizer).toHaveAttribute('aria-valuenow', '320');
    }],
    ['最大', async () => {
      await slam('ArrowLeft', span); // 320 + span×64 > 上限 ⇒ 夹到上限
      await expect(resizer).toHaveAttribute('aria-valuenow', String(ceiling));
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
    expect(g.badge?.height, `${label}：状态徽标折行`).toBeLessThanOrEqual(30);
    // 范围文本要么退出、要么单行；不得再被压成多行竖条（曾出现 90px 高）
    if (g.runId && g.runId.height > 0) expect(g.runId.height, `${label}：范围被压成竖条`).toBeLessThanOrEqual(30);
  }

  /* 范围文本的**去留边界**要显式锁住（复审 P2-1）：窄面板（≤360 容器宽）它整体
   * 退出头部、宽面板必须完整可见。不锁的话，"窄面板下它其实是隐藏的"这件事没有
   * 任何测试会发现——`g-visual-qa.spec.ts` 对它用的是 `toHaveText`，隐藏元素照样通过。
   * #1419 后默认宽度 520 > 360，容器查询在默认宽度下**不生效**，所以这条路径必须
   * 主动拖窄才走得到；文案也从「N runs · M 事件」换成了效果图口径的裸 run_id。 */
  // 上一步停在上限；两个方向都按"越界撞夹取"来走（Shift 步长 64，夹取 320/上限）
  await slam('ArrowRight', span);
  await expect(resizer).toHaveAttribute('aria-valuenow', '320');
  await expect(page.locator('.detail-run-id')).toBeHidden();
  await slam('ArrowLeft', span);
  await expect(resizer).toHaveAttribute('aria-valuenow', String(ceiling));
  await expect(page.locator('.detail-run-id')).toBeVisible();
  await expect(page.locator('.detail-run-id')).toHaveText('e2e-run-0001');

  // 最窄处仍然**真的能点**（Playwright 会做可操作性检查：视口外/被遮挡会超时）
  await slam('ArrowRight', span);
  await expect(resizer).toHaveAttribute('aria-valuenow', '320');
  await page.locator('button[aria-label="关闭运行详情"]').click();
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
   * 注：child 视图里**没有** `.detail-resizer`（实测 count=0），所以必须先把它拖窄；
   * #1419 后默认 520 > 360，不拖窄的话容器查询根本不生效，这条用例会变成空转。 */
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
  await expect(page.locator('.detail-resizer')).toHaveAttribute('aria-valuenow', '520');
  // 520 → 320（步长 16，13 次）后才进入容器查询生效的窄面板区间。
  await page.locator('.detail-resizer').focus();
  for (let i = 0; i < 13; i++) await page.keyboard.press('ArrowRight');
  await expect(page.locator('.detail-resizer')).toHaveAttribute('aria-valuenow', '320');

  await page.locator('.deleg-open-btn').first().click();

  const header = page.locator('.detail-header');
  await expect(header.locator('.child-back-btn')).toBeVisible(); // 确实进了子会话视图
  await expect(header.locator('.detail-header-actions')).toHaveCount(0); // 它不是 run 头部
  await expect(header.locator('.detail-run-id')).toBeVisible();
  await expect(header.locator('.detail-run-id')).toHaveText(CHILD.slice(0, 8));
});
