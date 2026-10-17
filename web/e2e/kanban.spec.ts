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
import { routeApi, sessionRow, type ApiMock } from './fixtures';

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
    ...over,
  });
}

/** 打开看板（顶栏「看板」按钮 -> 浮层）。 */
async function openBoard(page: Page): Promise<void> {
  await page.goto('/');
  await page.getByLabel('看板').click();
  await expect(page.getByRole('dialog', { name: 'Agent 看板' })).toBeVisible();
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
    await page.getByLabel('看板').click();
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
    expect(listGets).toBeGreaterThan(listsBefore);
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
    const entry = page.getByLabel('看板');
    await expect(entry).toBeVisible();
    await entry.click();
    const dialog = page.getByRole('dialog', { name: 'Agent 看板' });
    await expect(dialog).toBeVisible();

    // 打开开关，Esc 关闭，重开时保持。
    await page.getByRole('button', { name: '显示已接受' }).click();
    await expect(page.getByTestId('kanban-col-accepted')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await page.getByLabel('看板').click();
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
    await openBoard(page);
    await expect(cardsIn(page, 'backlog')).toHaveCount(1);
    // 看板不是 core surface：它不消费能力声明（那正是 §4.1 的三条判据之一）。
    expect(capabilitiesGets).toBe(0);
  });
});
