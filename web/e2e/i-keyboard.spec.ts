/** 场景 I（spec 01 §22）：键盘可达——Ctrl/Cmd+K 唤起 palette、Esc 关闭、
 *  焦点落输入框；Composer ⌘+Enter 发送路径。 */

import { expect, test } from '@playwright/test';
import { routeApi, fulfillSse } from './fixtures';

test('palette 键盘唤起与关闭；Composer 键盘提交', async ({ page }) => {
  const frames = [
    { type: 'session/started', seq: 1, session_id: 'e2e-session-0002', run_id: 'e2e-run-0002', time: '2026-09-07T00:00:00Z' },
    { type: 'run/started', seq: 2, session_id: 'e2e-session-0002', run_id: 'e2e-run-0002', time: '2026-09-07T00:00:00Z' },
    { type: 'user/message', data: { content: '键盘提交' }, seq: 3, session_id: 'e2e-session-0002', run_id: 'e2e-run-0002', step_id: 1, time: '2026-09-07T00:00:00Z' },
    { type: 'run/completed', data: {}, seq: 4, session_id: 'e2e-session-0002', run_id: 'e2e-run-0002', time: '2026-09-07T00:00:00Z' },
  ];
  // 终态后 viewing 迁移重读历史（#22 对账）——sessions/events fixture 提供同一真相
  routeApi(page, {
    sessions: [{ session_id: 'e2e-session-0002', event_count: 4, first_event_time: '2026-09-07T00:00:00Z', last_event_time: '2026-09-07T00:00:00Z', first_user_message: '键盘提交', trace_id: null }],
    events: frames,
    onSessionPost: (route) => fulfillSse(route, frames),
  });

  await page.goto('/');
  await page.keyboard.press('Control+k');
  await expect(page.locator('.palette-input')).toBeVisible();
  await expect(page.locator('.palette-input')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.locator('.palette-input')).toBeHidden();

  // Composer ⌘/Ctrl+Enter 发送（键盘路径不依赖鼠标）
  await page.getByLabel('Agent 任务').fill('键盘提交');
  await page.getByLabel('Agent 任务').press('Control+Enter');
  await expect(page.locator('.session-item')).toContainText('键盘提交');
});
