/** #1399 定版 / #1465 修复：会话行的结构性改动回归锁。
 *
 *  **仲裁（#1465，2026-10-22）**：会话行布局以 #1399 定版为准 —— 标题独占首行、
 *  12 位会话 ID 在第二行 meta（ID · 事件数 · 时间）；#1370 P0-7 的「id 移到标题行尾」
 *  共行要求作废。依据 = 协调员 2026-10-22 仲裁（#1465 issuecomment-6332704502）
 *  + 已合入 main 的 #1399 定版实现（`857f9245`：`SessionList.tsx` 把 ID 移进
 *  `.session-item-meta`）。本文件此前锁的是共行布局
 *  （spec 末改 `c6a9dfd8` 早于 `857f9245`），故基线红在「id 与标题纵向重叠」。
 *
 *  此前这些改动只有截图：把 `SessionList` 改回"标题 / id / 徽标"三行、删掉
 *  `min-height: 44px`、或删掉分组间的发丝线，除 t-contrast 仍能找到一个
 *  `.session-item-id` 外全绿。这里用布局几何锁住三件事：
 *   1. ID 在标题**下方**的 meta 行（正向锁：`idTop ≥ titleBottom`，且 ID 落在
 *      `.session-item-meta` 容器内 —— 把 ID 塞回 `.session-item-head` 必红）；
 *   2. 展开态行高 ≥44px（票面 44px）；
 *   3. 同组相邻行有发丝线，每组末行没有（`border-bottom` 用 `:last-child` 收口）。
 *
 *  收起态（≤820px）是图标轨，行高与可点面积（≥24px）由本文件的窄轨用例锁住；
 *  `w-session-delete.spec.ts` 只锁了宽度 ≥24。 */

import { expect, test } from '@playwright/test';
import { routeApi, sessionRow } from './fixtures';

test('#1399 会话行：标题独占首行、id 在 meta 行、行高 ≥44、分组发丝线只画在组内', async ({ page }) => {
  const A = 'e2e-1370-row-alpha';
  const B = 'e2e-1370-row-beta';
  const C = 'e2e-1370-row-gamma';
  const sessions = [
    sessionRow(A, null, { first_user_message: '项目里的第一条会话' }),
    sessionRow(B, null, { first_user_message: '项目里的第二条会话' }),
    sessionRow(C, null, { first_user_message: '未分组的会话' }),
  ];
  await routeApi(page, {
    sessions,
    projects: [{ id: 'p-1370', path: '/tmp/1370', title: '项目 1370', session_ids: [A, B] }],
  });
  await page.goto('/');

  await expect(page.locator('.session-items .session-row')).toHaveCount(3);

  const layout = await page.evaluate(() => {
    const row = document.querySelector('.session-items .session-row');
    if (!row) throw new Error('no session row');
    const title = row.querySelector('.session-item-title');
    const id = row.querySelector('.session-item-id');
    const item = row.querySelector('.session-item');
    if (!title || !id || !item) throw new Error('session row is missing title/id/item');
    const meta = row.querySelector('.session-item-meta');
    if (!meta) throw new Error('session row is missing meta');
    const t = title.getBoundingClientRect();
    const i = id.getBoundingClientRect();
    const it = item.getBoundingClientRect();
    const m = meta.getBoundingClientRect();
    return {
      titleBottom: t.bottom,
      idTop: i.top,
      idBottom: i.bottom,
      metaTop: m.top,
      metaBottom: m.bottom,
      idParent: id.parentElement?.className ?? '',
      height: it.height,
      minHeight: Number.parseFloat(getComputedStyle(item).minHeight),
    };
  });

  // 1. #1399 定版：ID 在标题下方的 meta 行（正向锁）。
  expect(layout.idTop, 'id 在标题下方（#1399 定版）').toBeGreaterThanOrEqual(layout.titleBottom);
  // 证据：ID 就是 meta 行的一员（同容器 + 纵向落在 meta 盒内），不是被挪回标题行。
  expect(layout.idParent, 'id 在 meta 行').toContain('session-item-meta');
  expect(layout.idTop, 'id 与 meta 同行').toBeGreaterThanOrEqual(layout.metaTop);
  expect(layout.idBottom, 'id 与 meta 同行').toBeLessThanOrEqual(layout.metaBottom);
  // 2. 展开态行高（票面 44px）。
  expect(layout.height).toBeGreaterThanOrEqual(44);
  expect(layout.minHeight).toBeGreaterThanOrEqual(44);

  // 3. 发丝线：项目组两行 = solid/none；未分组只剩一行 = none。
  const borders = await page.evaluate(() => ({
    project: Array.from(document.querySelectorAll('.rail-project-rows .session-row')).map(
      (el) => getComputedStyle(el).borderBottomStyle,
    ),
    ungrouped: Array.from(
      document.querySelectorAll('section[aria-label="未分组"] > div > .session-row'),
    ).map((el) => getComputedStyle(el).borderBottomStyle),
  }));
  expect(borders.project).toEqual(['solid', 'none']);
  expect(borders.ungrouped).toEqual(['none']);
});

test('#1370 窄轨：会话行可点面积 ≥24×24 且不画发丝线', async ({ page }) => {
  const A = 'e2e-1370-narrow-alpha';
  const B = 'e2e-1370-narrow-beta';
  await routeApi(page, {
    sessions: [
      sessionRow(A, null, { first_user_message: '窄轨会话一' }),
      sessionRow(B, null, { first_user_message: '窄轨会话二' }),
    ],
    projects: [],
  });
  await page.setViewportSize({ width: 800, height: 900 });
  await page.goto('/');
  // 列表是挂载后异步拉的：先等行真的渲染出来，再量几何。
  await expect(page.locator('.session-items .session-row .session-item')).toHaveCount(2);

  const geometry = await page.evaluate(() => {
    const items = Array.from(document.querySelectorAll<HTMLElement>('.session-items .session-row .session-item'));
    if (items.length === 0) throw new Error('no narrow session item');
    return items.map((item) => {
      const rect = item.getBoundingClientRect();
      return {
        width: rect.width,
        height: rect.height,
        minHeight: Number.parseFloat(getComputedStyle(item).minHeight),
        borderBottom: getComputedStyle(item.closest('.session-row')!).borderBottomStyle,
      };
    });
  });

  // 图标轨没有文字撑宽，横向 padding 就是唯一可点面积；行高必须是 24px 的收起态，
  // 不能被展开态的 44px 接管（`min-height` 精确断言，删掉窄轨规则就会红）。
  for (const row of geometry) {
    expect(row.width).toBeGreaterThanOrEqual(24);
    expect(row.minHeight).toBe(24);
    expect(row.height).toBe(24);
    // 同组 ≥2 行：窄轨规则缺席时首行会拿到分组发丝线的 solid。
    expect(row.borderBottom).toBe('none');
  }
});
