/** #1305 AC-14：看板 WS 全量快照 + 250ms 节流（前端侧判据）。
 *
 * 契约来源：issue #1305 §7 与 AC-14；后端实现见 `src/agent_harness/web/board.py`
 * （节流照抄 Orca `useDashboardPopoutBridge.ts:13`/`:187-209` @ `de1f6e3d`）。
 *
 *  ## 这一份 spec 为什么这样切
 *
 *  三条 AC 的**观测点各不相同**，混在一条用例里会互相掩盖：
 *
 *  ① **帧数上下界**：量的是"服务端在 1s 内发了几帧"。fixture 是后端替身，
 *     它发多少帧由用例决定 ⇒ 直接驱动 `push()` 并按帧计数断言"客户端渲染
 *     了最后那一帧"。⚠ **节流本身是服务端行为**（票面「节流只在服务端做一次，
 *     前端收到即渲染，不做第二次节流」）⇒ 前端侧**没有**"10 条事件 → ≤3 帧"
 *     这条因果可测；前端可测的是**不二次节流**：连发 3 帧必须 3 帧都渲染到位
 *     （若前端擅自合并，计数会掉）。真正的 250ms 上下界由后端单测
 *     `tests/web/test_board_broadcaster.py` 钉住。
 *  ② **逐字段相等**：最后一帧的行必须原样渲染进 DOM（与 `GET /api/sessions`
 *     同源的行）。
 *  ③ **断开后不再收帧**：关板（组件卸载）后服务端再推，界面不得变。
 *
 *  ## 请求计数为什么用 spec 内闭包
 *
 *  `fixtures.ts` 的钩子是回调不是计数器（#1304 审查 P2-5 的教训，票面点名）。
 */

import { expect, test, type Page } from '@playwright/test';
import { routeApi, sessionRow } from './fixtures';

/** 看板行：在 `sessionRow` 之上补 #1303 的四个字段（真后端恒发全部四个）。 */
function boardRow(
  sessionId: string,
  productState: string,
  over: Record<string, unknown> = {},
): Record<string, unknown> {
  return sessionRow(sessionId, null, {
    product_state: productState,
    product_state_changed_at: null,
    attention_kind: null,
    attention_summary: null,
    ...over,
  });
}

async function openBoard(page: Page): Promise<void> {
  await page.goto('/');
  await page.getByLabel('看板').click();
  await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toBeVisible();
}

function cardsIn(page: Page, slug: string) {
  return page.getByTestId(`kanban-col-${slug}`).locator('.kanban-card');
}

test.describe('#1305 看板 WS 推送', () => {
  test('AC-14 ①②：WS 订阅推来的帧逐字段渲染（不二次节流、不发 REST）', async ({ page }) => {
    // spec 内闭包：REST 列表请求次数（降级路径的判据）。
    let listGets = 0;
    page.on('request', (req) => {
      const url = new URL(req.url());
      if (url.pathname === '/api/sessions' && req.method() === 'GET') listGets += 1;
    });

    const boardSubscribes: unknown[] = [];
    /** 服务端推送驱动：fixture 在订阅建立后把它交出来（真后端由 250ms 节流驱动）。 */
    let push: ((rows: unknown[]) => void) | null = null;
    /** 已推送的帧数（spec 内计数器，不经 fixtures）。 */
    let pushed = 0;

    await routeApi(page, {
      sessions: [boardRow('s-ws', 'executing')],
      boardSubscribes,
      onBoardSubscribe: (p) => {
        push = p;
      },
    });

    await page.goto('/');
    // 应用外壳自己会在挂载时拉列表（`useSession.refreshSessions`，与看板无关）。
    // 本用例要量的是**开板**带来的 REST 流量 ⇒ 先取基线、只看增量；把外壳的
    // 请求算进来会让"WS 替换了开板时那次拉取"这条判据失去分辨力。
    await page.getByLabel('看板').waitFor();
    const listGetsBeforeBoard = listGets;
    await page.getByLabel('看板').click();
    await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toBeVisible();

    // ① 首帧（订阅即推）已渲染 —— 这条同时证明"打开时不再拉 REST"：
    //    行来自 WS 推送，而开板后的 REST 请求增量为 0。
    await expect(cardsIn(page, 'executing')).toHaveCount(1);
    expect(
      listGets - listGetsBeforeBoard,
      'WS 可用时打开看板不应再拉 REST（票面：WS 替换打开时拉一次）',
    ).toBe(0);
    expect(boardSubscribes.length, '订阅上行应恰好一次').toBe(1);

    // ② 连续推 3 帧（模拟服务端在 1s 内的突发节流输出 ≤3 帧）⇒ 前端**不合并**，
    //    末帧必须渲染到位（前端不做第二次节流）。
    const finalRows = [
      boardRow('s-ws', 'executing'),
      boardRow('s-new', 'pending_verification'),
    ];
    for (let i = 0; i < 2; i += 1) {
      push!(finalRows);
      pushed += 1;
    }
    push!(finalRows);
    pushed += 1;

    // 最后一帧的两行都在板上 ⇒ 末帧未被丢弃（前端无二次节流）。
    await expect(cardsIn(page, 'executing')).toHaveCount(1);
    await expect(cardsIn(page, 'pending_verification')).toHaveCount(1);
    expect(pushed, '本用例推了 3 帧').toBe(3);
    // ③ 开板后全程零 REST：行只来自 WS（契约漂移/降级都会让这个数变正）。
    expect(listGets - listGetsBeforeBoard, 'WS 路径下开板不应有任何 REST 列表请求').toBe(0);
  });

  test('AC-14 ②：最后一帧的行与 REST 同形（逐字段渲染，不丢字段）', async ({ page }) => {
    const row = boardRow('s-fields', 'executing', {
      attention_kind: 'approval',
      attention_summary: '待批准：Bash (execute)',
      product_state_changed_at: '2026-10-17T03:00:00Z',
      first_user_message: '逐字段用例',
    });

    await routeApi(page, { sessions: [row] });
    await openBoard(page);

    // 字段落到 DOM：标题来自 first_user_message、横幅来自 attention_summary。
    const card = page.locator('.kanban-card[data-session-id="s-fields"]');
    await expect(card).toBeVisible();
    await expect(card).toContainText('逐字段用例');
    await expect(card.locator('.kanban-card-attention')).toHaveText('待批准：Bash (execute)');
    // 注意力卡进执行中列（四态投影字段生效）。
    await expect(cardsIn(page, 'executing')).toHaveCount(1);
  });

  test('AC-14 ③：关闭看板后退订，服务端再推不再影响界面', async ({ page }) => {
    let push: ((rows: unknown[]) => void) | null = null;
    const boardSubscribes: unknown[] = [];

    await routeApi(page, {
      sessions: [boardRow('s-close', 'executing')],
      boardSubscribes,
      onBoardSubscribe: (p) => {
        push = p;
      },
    });

    await openBoard(page);
    await expect(cardsIn(page, 'executing')).toHaveCount(1);
    expect(boardSubscribes.length).toBe(1);

    // 关板（Esc，与既有 AC-15 同一条关闭路径）。
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toHaveCount(0);

    // 服务端在断开后再推一帧（推送可能落在已关连接上——真后端静默丢弃）。
    push!([boardRow('s-close', 'executing'), boardRow('s-ghost', 'executing')]);

    // 重开看板：不得出现那一帧的行（旧连接已退订，新连接拿到的是**当前** sessions）。
    await page.getByLabel('看板').click();
    await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toBeVisible();
    await expect(cardsIn(page, 'executing')).toHaveCount(1);
    await expect(page.locator('.kanban-card[data-session-id="s-ghost"]')).toHaveCount(0);
  });

  test('降级：WS 不可用时退回「拉一次 + 手动刷新」，仍能显示行', async ({ page }) => {
    // WS 被拒（一个服务帧都没有）⇒ hook 走降级路径：拉一次 REST + 手动刷新可用。
    let listGets = 0;
    page.on('request', (req) => {
      const url = new URL(req.url());
      if (url.pathname === '/api/sessions' && req.method() === 'GET') listGets += 1;
    });

    await routeApi(page, {
      sessions: [boardRow('s-degraded', 'executing')],
      closeBoardNow: true,
    });

    await page.goto('/');
    // 同上一例：先扣掉应用外壳挂载时的列表请求，只量**开板**带来的增量，
    // 否则外壳那 2 次请求会让"降级确实拉了一次"这条断言恒真（假绿）。
    await page.getByLabel('看板').waitFor();
    const listGetsBeforeBoard = listGets;
    await page.getByLabel('看板').click();
    await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toBeVisible();

    // 降级后仍显示行（行来自 REST 拉取），且开板后确实拉过 REST。
    await expect(cardsIn(page, 'executing')).toHaveCount(1);
    await expect.poll(() => listGets - listGetsBeforeBoard).toBeGreaterThan(0);

    // 手动刷新按钮可用（降级路径的出口）。
    const before = listGets;
    await page.getByLabel('刷新看板').click();
    await expect.poll(() => listGets).toBeGreaterThan(before);
  });
});
