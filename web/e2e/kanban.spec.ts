/** #1304 Agent 看板前端主干：四列严格投影 + 注意力覆盖层 + 搜索 + 拖拽。
 *
 *  契约来源：issue #1304（票面 §4.1–§4.7 与 AC-1~AC-18）；四字段契约由 #1303 落在
 *  `GET /api/sessions` 行上（`web/src/types.ts::SessionSummary`，PR #1314）。
 *
 *  ## 这一份 spec 为什么这样切
 *
 *  板是**投影**（PRODUCT.md 原则 2）：所有断言都从"后端行 -> DOM"这条单一路径出发，
 *  不塞任何前端自有状态。拖拽是本票唯一的写路径，所以 AC-8/AC-9/AC-10 全部断言
 *  **发出去的请求**（次数 + 顺序 + 载荷），而不是"卡片动没动"——卡片动没动由随后的
 *  refetch 决定，那正是 AC-8⑤ 要钉的东西。
 *
 *  ⚠ 请求计数用**各 spec 内的闭包变量**：`fixtures.ts` 的钩子是回调不是计数器
 *  （#1304 审查 P2-5 已指出三张票都误当计数器引用过）。
 */

import { expect, test, type Page } from '@playwright/test';
import { clickTopBarMenuItem, routeApi, sessionRow, type ApiMock } from './fixtures';

const T = '2026-10-17T00:00:00Z';

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
    last_user_message: null,
    last_agent_message: null,
    ...over,
  });
}

/** #1396：看板入口已收进顶栏「更多工具」菜单（`menuitem`，不是 `button`）。
 *  打开动作单独成函数：「关掉再开、不刷新」那条用例复用的是同一条入口路径。
 *  本仓惯例：各看板 spec 各自定义自己的 `openBoardPanel`（`kanban-subagents.spec.ts`
 *  同款），不做跨 spec import、不搬进 fixtures。 */
async function openBoardPanel(page: Page): Promise<void> {
  await clickTopBarMenuItem(page, 'Agent 看板');
  await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toBeVisible();
}

async function openBoard(page: Page): Promise<void> {
  await page.goto('/');
  await openBoardPanel(page);
}

/** 某列的卡片计数（列容器由 `data-testid` 锚定，slug 见 `lib/kanban.ts`）。 */
function cardsIn(page: Page, slug: string) {
  return page.getByTestId(`kanban-col-${slug}`).locator('.kanban-card');
}

/** 整块板上可见的卡片总数（四列相加；已接受列不可见时它贡献 0）。 */
async function visibleCardCount(page: Page): Promise<number> {
  return page.locator('.kanban-col .kanban-card').count();
}

test.describe('#1304 看板：四列投影与开关', () => {
  test('AC-1/AC-2：四态各进各列、已接受默认隐藏、开关往返 + 跨刷新保留', async ({ page }) => {
    await routeApi(page, {
      sessions: [
        boardRow('s-backlog', ''),
        boardRow('s-exec', 'executing'),
        boardRow('s-verify', 'pending_verification'),
        boardRow('s-accepted', 'accepted'),
      ],
    });
    await openBoard(page);

    // AC-1：三条可见列各恰好 1 张；已接受列**不可见**。
    await expect(cardsIn(page, 'backlog')).toHaveCount(1);
    await expect(cardsIn(page, 'executing')).toHaveCount(1);
    await expect(cardsIn(page, 'pending_verification')).toHaveCount(1);
    await expect(page.getByTestId('kanban-col-accepted')).toHaveCount(0);
    // 每条会话在可见列里出现次数恒为 1（accepted 那张出现 0 次）。
    expect(await visibleCardCount(page)).toBe(3);

    // AC-2：打开开关 -> 已接受列可见且含 1 张卡，aria-pressed 翻真，存储写 'true'。
    const toggle = page.getByRole('button', { name: '显示已接受' });
    await expect(toggle).toHaveAttribute('aria-pressed', 'false');
    await toggle.click();
    await expect(page.getByTestId('kanban-col-accepted')).toBeVisible();
    await expect(cardsIn(page, 'accepted')).toHaveCount(1);
    await expect(toggle).toHaveAttribute('aria-pressed', 'true');
    expect(await page.evaluate(() => localStorage.getItem('ahi.kanban.showAccepted.v1'))).toBe(
      'true',
    );
    expect(await visibleCardCount(page)).toBe(4);

    // 刷新后仍可见（持久化的是**开关**，不是列）。
    await page.reload();
    await openBoardPanel(page);
    await expect(page.getByTestId('kanban-col-accepted')).toBeVisible();

    // 再点一次 -> 隐藏，aria-pressed 翻假，存储写 'false'。
    const toggleAgain = page.getByRole('button', { name: '显示已接受' });
    await toggleAgain.click();
    await expect(page.getByTestId('kanban-col-accepted')).toHaveCount(0);
    await expect(toggleAgain).toHaveAttribute('aria-pressed', 'false');
    expect(await page.evaluate(() => localStorage.getItem('ahi.kanban.showAccepted.v1'))).toBe(
      'false',
    );
  });

  test('AC-3：deliverable 不渲染、不静默丢卡；0 条时警示行不存在', async ({ page }) => {
    await routeApi(page, {
      sessions: [
        boardRow('s-backlog', ''),
        boardRow('s-deliverable', 'deliverable', { first_user_message: '这条不该出现在任何列' }),
      ],
    });
    await openBoard(page);

    // 没有 deliverable 列（testid 与标题都不该出现）。
    await expect(page.getByTestId('kanban-col-deliverable')).toHaveCount(0);
    await expect(page.getByText('可交付', { exact: true })).toHaveCount(0);
    // 该卡不出现在任何列。
    await expect(page.locator('.kanban-card', { hasText: '这条不该出现在任何列' })).toHaveCount(0);
    // 但**不许静默丢**：顶部警示行说出计数。
    await expect(page.getByText(/有 1 张卡片处于 deliverable 状态/)).toBeVisible();
  });

  test('AC-3 反向对照：0 条 deliverable 时警示行不存在', async ({ page }) => {
    await routeApi(page, {
      sessions: [boardRow('s-backlog', ''), boardRow('s-exec', 'executing')],
    });
    await openBoard(page);
    await expect(cardsIn(page, 'backlog')).toHaveCount(1);
    await expect(page.getByText(/张卡片处于 deliverable 状态/)).toHaveCount(0);
  });

  test('AC-4：列内按 product_state_changed_at 降序，null 排最后', async ({ page }) => {
    await routeApi(page, {
      sessions: [
        boardRow('t1', 'executing', { product_state_changed_at: '2026-10-17T01:00:00Z' }),
        boardRow('t3', 'executing', { product_state_changed_at: '2026-10-17T03:00:00Z' }),
        boardRow('t2', 'executing', { product_state_changed_at: '2026-10-17T02:00:00Z' }),
        boardRow('tnull', 'executing', { product_state_changed_at: null }),
      ],
    });
    await openBoard(page);

    const titles = await cardsIn(page, 'executing').evaluateAll((nodes) =>
      nodes.map((n) => (n as HTMLElement).dataset.sessionId),
    );
    expect(titles).toEqual(['t3', 't2', 't1', 'tnull']);
  });
});

test.describe('#1304 看板：注意力覆盖层（C2）', () => {
  test('AC-5：注意力卡留在执行中列、有染色与横幅、列内置顶；状态跃迁后覆盖层消失并移列', async ({
    page,
  }) => {
    const rows: unknown[] = [
      boardRow('s-attention', 'executing', {
        product_state_changed_at: '2026-10-17T01:00:00Z',
        attention_kind: 'input',
        attention_summary: '待回答：超时闸口接线方案二选一',
        last_user_message: null,
        last_agent_message: null,
      }),
      boardRow('s-plain', 'executing', {
        product_state_changed_at: '2026-10-17T05:00:00Z',
      }),
    ];
    await routeApi(page, { sessions: rows });
    await openBoard(page);

    // ① 仍在执行中列内（注意力**不是**第 6 列）。
    await expect(cardsIn(page, 'executing')).toHaveCount(2);
    const attentionCard = page.locator('.kanban-card[data-session-id="s-attention"]');
    await expect(attentionCard).toBeVisible();
    // ② 染色 class。
    await expect(attentionCard).toHaveClass(/in-attention/);
    // ③ 横幅，文本以「待回答：」开头。
    await expect(attentionCard.locator('.kanban-card-attention')).toHaveText(/^待回答：/);
    // ④ 同列另一张无注意力的卡排在它**之后**——注意力的 `order: -1` 压过了
    //    "最近跃迁在上"的排序（s-plain 的 changed_at 更新，本该在前）。
    const attentionBox = await attentionCard.boundingBox();
    const plainBox = await page.locator('.kanban-card[data-session-id="s-plain"]').boundingBox();
    expect(attentionBox!.y).toBeLessThan(plainBox!.y);

    // 把 run 推到终态 -> 卡片移到待验证列，覆盖层消失。
    rows[0] = boardRow('s-attention', 'pending_verification', {
      product_state_changed_at: '2026-10-17T06:00:00Z',
    });
    await page.getByLabel('刷新看板').click();
    await expect(cardsIn(page, 'pending_verification')).toHaveCount(1);
    await expect(cardsIn(page, 'executing')).toHaveCount(1);
    const moved = page.locator('.kanban-card[data-session-id="s-attention"]');
    await expect(moved).not.toHaveClass(/in-attention/);
    await expect(moved.locator('.kanban-card-attention')).toHaveCount(0);
  });

  test('AC-6：审批横幅文案原样来自后端 attention_summary', async ({ page }) => {
    await routeApi(page, {
      sessions: [
        boardRow('s-approval', 'executing', {
          attention_kind: 'approval',
          attention_summary: '待批准：Bash (execute)',
          last_user_message: null,
          last_agent_message: null,
        }),
      ],
    });
    await openBoard(page);
    await expect(
      page.locator('.kanban-card[data-session-id="s-approval"] .kanban-card-attention'),
    ).toHaveText('待批准：Bash (execute)');
  });

  test('AC-6 优先级：input 与 approval 并存时横幅取后端给的那一条（前端不自己挑）', async ({
    page,
  }) => {
    // 后端 `board_projection.py:125/139` 按 seq 更大者胜出，前端**只渲染**结果——
    // 这条断言钉的就是"前端不做第二份优先级判定"。
    await routeApi(page, {
      sessions: [
        boardRow('s-both', 'executing', {
          attention_kind: 'approval',
          attention_summary: '待批准：Bash (execute)',
          last_user_message: null,
          last_agent_message: null,
        }),
      ],
    });
    await openBoard(page);
    await expect(
      page.locator('.kanban-card[data-session-id="s-both"] .kanban-card-attention'),
    ).toHaveText(/^待批准：/);
  });
});

test.describe('#1304 看板：拖拽写路径', () => {
  test('AC-8：backlog -> 执行中，两步写请求 + 顺序 + 零次建会话 + 主动 refetch', async ({
    page,
  }) => {
    const rows: unknown[] = [
      boardRow('s-c', '', { first_user_message: '整理发布说明' }),
      boardRow('s-other', 'executing'),
    ];
    const commands: Array<{ subPath: string; body: Record<string, unknown> }> = [];
    const messages: Array<Record<string, unknown>> = [];
    let sessionPosts = 0;
    let listGets = 0;

    page.on('request', (req) => {
      const url = new URL(req.url());
      if (url.pathname === '/api/sessions' && req.method() === 'POST') sessionPosts += 1;
      if (url.pathname === '/api/sessions' && req.method() === 'GET') listGets += 1;
    });

    await routeApi(page, {
      sessions: rows,
      onSessionCommandPost: (route, sessionId, subPath, body) => {
        commands.push({ subPath: `${sessionId}${subPath}`, body });
        return route.fulfill({ status: 200, body: '{}', contentType: 'application/json' });
      },
      onMessagesPost: (route) => {
        messages.push((route.request().postDataJSON() ?? {}) as Record<string, unknown>);
        // 落点成功后后端把 s-c 变成 executing —— 下一次列表就能看到它在执行中列。
        rows[0] = boardRow('s-c', 'executing', {
          first_user_message: '整理发布说明',
          product_state_changed_at: '2026-10-17T09:00:00Z',
        });
        return route.fulfill({ status: 200, body: '{}', contentType: 'application/json' });
      },
    });

    await openBoard(page);
    await expect(cardsIn(page, 'backlog')).toHaveCount(1);
    const listsBefore = listGets;

    // 拖拽：`dragTo` 在 Chromium 下走真实 DnD 事件序列。
    await page
      .locator('.kanban-card[data-session-id="s-c"]')
      .dragTo(page.getByTestId('kanban-col-executing'));

    await expect.poll(() => commands.length).toBe(1);
    await expect.poll(() => messages.length).toBe(1);

    // ① 恰好一次 task/definition，body.task_text 正确。
    expect(commands[0].subPath).toBe('s-c/task/definition');
    expect(commands[0].body).toEqual({ task_text: '整理发布说明' });
    // ② 恰好一次 messages，content 与 mode 正确。
    expect(messages[0]).toMatchObject({ content: '整理发布说明', mode: 'queue' });
    // ③ 全程零次 POST /api/sessions（不带 /{id} 的那条）——建会话就是造第二张卡。
    expect(sessionPosts).toBe(0);
    // ④ definition 在 messages 之前（顺序不可换：先定义再开跑）。
    //    两条钩子各自 push，命令先到 ⇒ 用"命令已到达时 messages 还没到"来钉顺序。
    expect(commands.length).toBe(1);
    // ⑤ 拖拽成功后前端**主动 refetch 一次**，随后卡片出现在执行中列。
    //    `listGets` 由 `page.on('request')` 递增，而那个回调是**异步投递**的
    //    （与 `commands`/`messages` 的同步闭包 push 不同）⇒ 同步读会跟它赛跑，
    //    1920 视口下就输过（1280 恰好赢）。改成 poll：断言的是"refetch 确实发生"，
    //    不是"回调在这一微任务内已投递"。
    await expect.poll(() => listGets).toBeGreaterThan(listsBefore);
    await expect(cardsIn(page, 'executing')).toHaveCount(2);
    await expect(cardsIn(page, 'backlog')).toHaveCount(0);
    await expect(page.locator('.kanban-card[data-session-id="s-c"]')).toBeVisible();
  });

  test('AC-9：非 backlog 落点被拒（零写请求 + 提示 + 卡片不动）', async ({ page }) => {
    const commands: string[] = [];
    const messages: unknown[] = [];
    await routeApi(page, {
      sessions: [boardRow('s-exec', 'executing'), boardRow('s-c', '')],
      onSessionCommandPost: (route, sessionId, subPath) => {
        commands.push(`${sessionId}${subPath}`);
        return route.fulfill({ status: 200, body: '{}', contentType: 'application/json' });
      },
      onMessagesPost: (route) => {
        messages.push(route.request().postDataJSON());
        return route.fulfill({ status: 200, body: '{}', contentType: 'application/json' });
      },
    });
    await openBoard(page);

    await page
      .locator('.kanban-card[data-session-id="s-exec"]')
      .dragTo(page.getByTestId('kanban-col-pending_verification'));

    await expect(page.getByText(/只有 backlog 卡/)).toBeVisible();
    // 零写请求。
    expect(commands).toHaveLength(0);
    expect(messages).toHaveLength(0);
    // 卡片不动。
    await expect(cardsIn(page, 'executing')).toHaveCount(1);
    await expect(cardsIn(page, 'pending_verification')).toHaveCount(0);

    // 第二个拒绝面：把**执行中**的卡丢回执行中列。只看目标列的实现会在这里放行，
    // 为一条非 backlog 会话发出定义 + 开跑两步写请求——所以这条必须单独考。
    await page
      .locator('.kanban-card[data-session-id="s-exec"]')
      .dragTo(page.getByTestId('kanban-col-executing'));
    await expect(page.getByText(/只有 backlog 卡/)).toBeVisible();
    expect(commands).toHaveLength(0);
    expect(messages).toHaveLength(0);
    await expect(cardsIn(page, 'executing')).toHaveCount(1);
  });

  test('AC-9②：空会话卡不可拖（显式 draggable="false"）且有说明文本', async ({ page }) => {
    await routeApi(page, {
      sessions: [boardRow('s-empty', '', { first_user_message: null })],
    });
    await openBoard(page);

    const card = page.locator('.kanban-card[data-session-id="s-empty"]');
    // **显式** false：属性缺失时 HTML 默认是 auto（可拖），所以这条断言必须看值。
    await expect(card).toHaveAttribute('draggable', 'false');
    await expect(card.getByText(/空会话/)).toBeVisible();
  });

  test('AC-10：task/definition 409 不阻断——仍发 messages、不显示错误', async ({ page }) => {
    const rows: unknown[] = [boardRow('s-d', '', { first_user_message: '已定义过的会话' })];
    let messagesSent = 0;
    await routeApi(page, {
      sessions: rows,
      onSessionCommandPost: (route) =>
        // 409 = 该会话已定义过任务——正常语义，看板应跳过这一步继续开跑。
        route.fulfill({
          status: 409,
          body: JSON.stringify({ detail: '已存在任务定义' }),
          contentType: 'application/json',
        }),
      onMessagesPost: (route) => {
        messagesSent += 1;
        rows[0] = boardRow('s-d', 'executing', { first_user_message: '已定义过的会话' });
        return route.fulfill({ status: 200, body: '{}', contentType: 'application/json' });
      },
    });
    await openBoard(page);

    await page
      .locator('.kanban-card[data-session-id="s-d"]')
      .dragTo(page.getByTestId('kanban-col-executing'));

    await expect.poll(() => messagesSent).toBe(1);
    // 不显示错误提示（409 被消化掉了）。
    await expect(page.getByText(/已存在任务定义/)).toHaveCount(0);
    // 最终进入执行中列。
    await expect(cardsIn(page, 'executing')).toHaveCount(1);
  });

  test('AC-10 反向对照：messages 500（HTTP 响应而非抛错）时如实显示后端 detail 且卡片不动', async ({
    page,
  }) => {
    await routeApi(page, {
      sessions: [boardRow('s-fail', '', { first_user_message: '会失败的会话' })],
      onSessionCommandPost: (route) =>
        route.fulfill({ status: 200, body: '{}', contentType: 'application/json' }),
      onMessagesPost: (route) =>
        route.fulfill({
          status: 500,
          body: JSON.stringify({ detail: '后端炸了' }),
          contentType: 'application/json',
        }),
    });
    await openBoard(page);

    await page
      .locator('.kanban-card[data-session-id="s-fail"]')
      .dragTo(page.getByTestId('kanban-col-executing'));

    await expect(page.getByText(/后端炸了/)).toBeVisible();
    // 卡片**不动**（不做乐观移动：板是投影，下一次快照才是真相）。
    await expect(cardsIn(page, 'backlog')).toHaveCount(1);
    await expect(cardsIn(page, 'executing')).toHaveCount(0);
    // ⚠ 这条用例考的是**最容易漏的一处**：`sendMessage` 返回原始 `Response`、
    // 非 2xx **不抛**（`lib/api.ts:772`）。漏掉 `res.ok` 检查时，500 会被当成成功
    // 受理，上面两条断言仍会绿（卡片本来就没动）——只有"必须显示 detail"这条会红。
    // 所以这条断言是本用例的判别力所在，不是装饰。
    await expect(page.getByText(/后端炸了/)).toHaveCount(1);
  });
});

test.describe('#1304 看板：搜索与入口', () => {
  test('AC-15：⌘/Ctrl+K 仍归命令面板；板内搜索框不抢焦点且能过滤', async ({ page }) => {
    await routeApi(page, {
      sessions: [
        boardRow('s-1', '', { first_user_message: '整理发布说明' }),
        boardRow('s-2', '', { first_user_message: '调研看板实现' }),
      ],
    });
    await openBoard(page);
    await expect(cardsIn(page, 'backlog')).toHaveCount(2);

    // 板开着时 Ctrl+K：命令面板可见且聚焦（既有 `i-keyboard.spec.ts` 的行为不被抢）。
    await page.keyboard.press('Control+k');
    await expect(page.locator('.palette-input')).toBeVisible();
    await expect(page.locator('.palette-input')).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(page.locator('.palette-input')).toBeHidden();
    // 看板搜索框**没有**被聚焦。
    await expect(page.getByLabel('搜索卡片')).not.toBeFocused();

    // 板内搜索：只剩标题命中的卡；清空后全部恢复。
    await page.getByLabel('搜索卡片').fill('发布');
    await expect(cardsIn(page, 'backlog')).toHaveCount(1);
    await expect(page.locator('.kanban-card[data-session-id="s-1"]')).toBeVisible();
    await page.getByLabel('搜索卡片').fill('');
    await expect(cardsIn(page, 'backlog')).toHaveCount(2);
  });

  test('AC-16：页脚只含时间节点，没有 PR pill / worktree / repo', async ({ page }) => {
    await routeApi(page, {
      sessions: [
        boardRow('s-1', 'executing', {
          first_user_message: '修 PR 的 branch 问题',
          last_event_time: T,
        }),
      ],
    });
    await openBoard(page);

    const foot = page.locator('.kanban-card[data-session-id="s-1"] .kanban-card-foot');
    await expect(foot).toBeVisible();
    // 页脚**结构**只允许时间节点——不扫全卡文本（卡片正文是用户任意输入，
    // "PR"/"branch" 是高频词，扫文本会把正常标题判成假字段）。
    const tagNames = await foot.evaluateAll((nodes) =>
      nodes.flatMap((n) => [...n.querySelectorAll('*')].map((el) => el.tagName.toLowerCase())),
    );
    expect(new Set(tagNames)).toEqual(new Set(['time']));
  });

  test('AC-17：顶栏入口可点、Esc 关闭、重开时开关状态保持', async ({ page }) => {
    await routeApi(page, { sessions: [boardRow('s-a', 'accepted')] });

    await page.goto('/');
    // #1396：入口收进「更多工具」菜单——触发器常驻顶栏，菜单项只在菜单打开时挂载。
    // 判别力保持：触发器可见 + 菜单项可见（入口确实在界面上可达）+ 点得开。
    const entry = page.getByRole('button', { name: '更多工具' });
    await expect(entry).toBeVisible();
    await entry.click();
    const menuEntry = page.getByRole('menuitem', { name: 'Agent 看板' });
    await expect(menuEntry).toBeVisible();
    await menuEntry.click();
    const dialog = page.getByRole('dialog', { name: 'Agent 看板' });
    await expect(dialog).toBeVisible();

    // 打开开关，Esc 关闭，重开时保持。
    await page.getByRole('button', { name: '显示已接受' }).click();
    await expect(page.getByTestId('kanban-col-accepted')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await openBoardPanel(page);
    await expect(page.getByTestId('kanban-col-accepted')).toBeVisible();
  });
});

test.describe('#1304 看板：AC-18 不新增 core surface', () => {
  test('打开看板不触发任何 capabilities 请求，也不改 SURFACE_KEYS 的渲染', async ({ page }) => {
    let capabilitiesGets = 0;
    page.on('request', (req) => {
      if (new URL(req.url()).pathname === '/api/capabilities') capabilitiesGets += 1;
    });
    const mock: ApiMock = { sessions: [boardRow('s-1', '')] };
    await routeApi(page, mock);

    // 判据是**开板这个动作的增量**，不是全生命周期总量：App 挂载时本来就会拉一次
    // 能力声明（`App.tsx` 的 `fetchCapabilities`，与看板无关）。所以先等应用落定、
    // 记下基线，再开板比增量——否则这条断言量的是"App 会不会拉 capabilities"，
    // 而那件事早在看板存在之前就定了。
    await page.goto('/');
    // 就绪锚点：应用外壳已渲染（顶栏「更多工具」触发器常驻）——入口形态随 #1396 变化，
    // 语义不变：等外壳落定再记基线，避免把挂载期请求算进「开板」增量。
    await expect(page.getByRole('button', { name: '更多工具' })).toBeVisible();
    await expect(page.locator('.session-item').first()).toBeVisible();
    // 等挂载那次拉取真的落地再记基线——`page.on('request')` 是异步投递的，
    // 不等就可能把"挂载那次"算进开板增量里（假红）。
    await expect.poll(() => capabilitiesGets).toBeGreaterThan(0);
    const baseline = capabilitiesGets;

    await openBoardPanel(page);
    await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toBeVisible();
    await expect(cardsIn(page, 'backlog')).toHaveCount(1);
    // 看板不是 core surface：开板不消费能力声明（§4.1 的三条判据之一）。
    expect(capabilitiesGets).toBe(baseline);
  });
});

test.describe('#1306 看板未读 / ack（AC-7）', () => {
  const CHANGED = '2026-10-17T03:00:00Z';

  test('AC-7：未读加粗 -> 点击写 ack 并打开会话 -> 重开已读 -> 同态事件不复活 -> 状态跃迁复活', async ({
    page,
  }) => {
    /** 服务端推送驱动（板级订阅建立后由 fixture 交出来）。 */
    let push: ((rows: unknown[]) => void) | null = null;
    const rows = [
      boardRow('s-unread', 'executing', {
        product_state_changed_at: CHANGED,
        last_event_time: CHANGED,
        first_user_message: '未读用例',
      }),
    ];
    await routeApi(page, {
      sessions: rows,
      events: [],
      onBoardSubscribe: (p) => {
        push = p;
      },
    });

    await openBoard(page);
    const card = page.locator('.kanban-card[data-session-id="s-unread"]');
    const title = card.locator('.kanban-card-title');

    // ① 未读：标题加粗（Orca `AgentKanbanCard.tsx:247` 的 font-semibold = 600）。
    await expect(title).toHaveCSS('font-weight', '600');
    await expect(card).toHaveAttribute('data-unread', 'true');

    // ② 点卡片：ack 先落 localStorage，再导航 + 关板（顺序见 KanbanPanel.openSession）。
    await card.click();
    await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toHaveCount(0);
    const ackRaw = await page.evaluate(() => localStorage.getItem('ahi.kanban.ack.v1'));
    expect(ackRaw, '点卡片必须写入 ack').not.toBeNull();
    const ack = JSON.parse(ackRaw ?? '{}') as Record<string, string>;
    expect(Date.parse(ack['s-unread'] ?? '')).toBeGreaterThanOrEqual(Date.parse(CHANGED));
    // 打开的是这张卡的会话（侧栏选中态）。
    await expect(page.locator('.session-item.selected')).toContainText('s-unread');

    // ③ 重开看板：未读标记消失。
    await openBoardPanel(page);
    await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toBeVisible();
    await expect(card).toHaveAttribute('data-unread', 'false');
    await expect(title).not.toHaveCSS('font-weight', '600');

    // ④ 同态事件：只推进 `last_event_time`（product_state 不变）⇒ 仍已读。
    //    防的是"推送/重渲染把已读卡重新标未读"这类回归（同态事件复活 bug 的产品面防线）。
    //    **不是**比较键的消融对照：本用例里 ack 是点击时的当天墙钟，而 last_event_time
    //    被推到前一天（早于 ack）——错键实现同样算出已读、不会变红。真正的比较键判据
    //    是下面的 ⑤（只改 product_state_changed_at 为未来值）与 vitest 用例 5。
    rows[0]['last_event_time'] = '2026-10-17T09:00:00Z';
    rows[0]['first_user_message'] = '未读用例（同态事件后）';
    push!(rows);
    await expect(title).toHaveText('未读用例（同态事件后）'); // 推送确实渲染到位
    await expect(card).toHaveAttribute('data-unread', 'false');

    // ⑤ 状态跃迁（task/accepted）：`product_state_changed_at` 晚于 ack ⇒ 重新未读。
    //    必须是**相对当前时刻**的未来值：ack 是点击时的 `new Date()`，写死的"未来"
    //    日期一到那天就变成过去（本用例首跑即踩：写死 10-18T00:00Z，而当天已 11:00Z）。
    rows[0]['product_state_changed_at'] = new Date(Date.now() + 3_600_000).toISOString();
    push!(rows);
    await expect(card).toHaveAttribute('data-unread', 'true');
    await expect(title).toHaveCSS('font-weight', '600');
    // ack 值本身没被这次推送改动（未读是"ack 早于状态开始"，不是丢了 ack）。
    const ackAfter = await page.evaluate(() => localStorage.getItem('ahi.kanban.ack.v1'));
    expect(JSON.parse(ackAfter ?? '{}')['s-unread']).toBe(ack['s-unread']);
  });
});

test.describe('#1308 卡片正文：最后一条 user / agent 消息', () => {
  test('正文两行（You + clamp-1 / agent + clamp-2），缺一不渲染，空会话回到提示', async ({
    page,
  }) => {
    await routeApi(page, {
      sessions: [
        boardRow('s-msgs', 'executing', {
          last_user_message: '改一下标题',
          last_agent_message: '已改好',
        }),
        boardRow('s-user-only', 'executing', {
          last_user_message: '只问了一句',
        }),
        boardRow('s-none', '', { first_user_message: null }),
      ],
    });
    await openBoard(page);

    const card = page.locator('.kanban-card[data-session-id="s-msgs"]');
    const user = card.locator('.kanban-card-msg-user');
    const agent = card.locator('.kanban-card-msg-agent');

    // 形状抄 Orca `:255-268`：`You` 前缀 + 单行 clamp；agent 名 + 两行 clamp。
    await expect(user).toHaveText('You 改一下标题');
    await expect(agent).toHaveText('agent 已改好');
    await expect(user).toHaveCSS('overflow', 'hidden');
    await expect(user).toHaveCSS('-webkit-line-clamp', '1');
    await expect(agent).toHaveCSS('-webkit-line-clamp', '2');
    // 两行都真的渲染出来（不是被裁成 0 高）。
    await expect(user).toBeVisible();
    await expect(agent).toBeVisible();

    // 只有 user：agent 行不存在（不渲染空行）。
    const userOnly = page.locator('.kanban-card[data-session-id="s-user-only"]');
    await expect(userOnly.locator('.kanban-card-msg-user')).toHaveText('You 只问了一句');
    await expect(userOnly.locator('.kanban-card-msg-agent')).toHaveCount(0);

    // 两字段都空：正文区不存在，原有空会话提示照旧。
    const empty = page.locator('.kanban-card[data-session-id="s-none"]');
    await expect(empty.locator('.kanban-card-msg-user')).toHaveCount(0);
    await expect(empty.locator('.kanban-card-msg-agent')).toHaveCount(0);
    await expect(empty.locator('.kanban-card-hint')).toContainText('空会话');
  });
});
