/** #1370 P0-7：会话行的结构性改动回归锁。
 *
 *  此前这些改动只有截图：把 `SessionList` 改回"标题 / id / 徽标"三行、删掉
 *  `min-height: 44px`、或删掉分组间的发丝线，除 t-contrast 仍能找到一个
 *  `.session-item-id` 外全绿。这里用布局几何锁住三件事：
 *   1. 标题与 id 在同一行（纵向重叠，id 在标题右侧）；
 *   2. 展开态行高 ≥44px（票面 44px）；
 *   3. 同组相邻行有发丝线，每组末行没有（`border-bottom` 用 `:last-child` 收口）。
 *
 *  收起态（≤820px）是图标轨，行高/可点面积由 `w-session-delete.spec.ts` 的
 *  24px 下限单独锁，不在本用例里重复。 */

import { expect, test } from '@playwright/test';
import { routeApi, sessionRow } from './fixtures';

test('#1370 会话行：标题与 id 同行、行高 ≥44、分组发丝线只画在组内', async ({ page }) => {
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
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');

  await expect(page.locator('.session-items .session-row')).toHaveCount(3);

  const layout = await page.evaluate(() => {
    const row = document.querySelector('.session-items .session-row');
    if (!row) throw new Error('no session row');
    const title = row.querySelector('.session-item-title');
    const id = row.querySelector('.session-item-id');
    const item = row.querySelector('.session-item');
    if (!title || !id || !item) throw new Error('session row is missing title/id/item');
    const t = title.getBoundingClientRect();
    const i = id.getBoundingClientRect();
    const it = item.getBoundingClientRect();
    return {
      titleTop: t.top,
      titleBottom: t.bottom,
      titleLeft: t.left,
      idTop: i.top,
      idBottom: i.bottom,
      idLeft: i.left,
      height: it.height,
      minHeight: Number.parseFloat(getComputedStyle(item).minHeight),
    };
  });

  // 1. 同一行：纵向重叠，id 收在标题右侧。
  expect(layout.idTop, 'id 与标题纵向重叠').toBeLessThan(layout.titleBottom);
  expect(layout.idBottom, 'id 与标题纵向重叠').toBeGreaterThan(layout.titleTop);
  expect(layout.idLeft, 'id 在标题右侧').toBeGreaterThan(layout.titleLeft);
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
