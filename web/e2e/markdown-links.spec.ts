/** #1096：对话区 markdown 链接化——完成态回答里的 [text](url) 与裸 URL 渲染为
 *  可点 <a>（新标签页 + rel），危险 scheme（javascript:）不链接化。 */

import { expect, test } from '@playwright/test';
import { RUN, SID, T, routeApi, fulfillSse, submitTask, type FrameSpec } from './fixtures';

const TEXT =
  '见 [文档](https://example.com/docs)，裸链 https://example.com/raw，危险 [x](javascript:alert(1))。';

test('完成态回答：markdown 链接与裸 URL 可点，javascript: 不链接化', async ({ page }) => {
  const frames: FrameSpec[] = [
    { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
    { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
    { type: 'user/message', data: { content: '给个出处' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'text/delta', data: { delta: TEXT }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'model/completed', data: { content: TEXT }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'run/completed', data: {}, seq: 6, session_id: SID, run_id: RUN, time: T },
  ];
  await routeApi(page, { onSessionPost: (route) => fulfillSse(route, frames), events: frames });

  await page.goto('/');
  await submitTask(page, '给个出处');

  const out = page.locator('.model-output').last();
  await expect(out).toContainText('文档');

  // 恰好两条链接：markdown [text](url) + 裸 URL（javascript: 那条不产生 <a>）
  const links = out.locator('a.md-link');
  await expect(links).toHaveCount(2);
  await expect(links.nth(0)).toHaveAttribute('href', 'https://example.com/docs');
  await expect(links.nth(0)).toHaveAttribute('target', '_blank');
  await expect(links.nth(0)).toHaveAttribute('rel', 'noopener noreferrer');
  await expect(links.nth(1)).toHaveAttribute('href', 'https://example.com/raw');

  // 安全回归：无 javascript: href；危险链接原文仍可见（不静默丢弃）
  await expect(out.locator('a[href^="javascript:"]')).toHaveCount(0);
  await expect(out).toContainText('javascript:alert(1)');
});
