/** #1450: cancellation is a stopped ToolCard and idle Timeline result.
 * This deterministic historical event replay exercises the browser consumers; it does not
 * invoke or claim coverage of the live runtime cancellation path. */

import { expect, test } from '@playwright/test';
import { writeFile } from 'node:fs/promises';
import { RUN, SID, T, routeApi, type FrameSpec } from './fixtures';

const RESULT_CONTENT = JSON.stringify({
  ok: false,
  error_code: 'COMMAND_CANCELLED',
  message: 'cancelled',
  data: { cancelled: true },
});

const EVENTS: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
  { type: 'user/message', data: { content: '检查取消结果' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
  {
    type: 'tool/call',
    data: { tool_call_id: 'tc-cancel', tool_name: 'bash', args: { command: 'sleep 1' } },
    seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T,
  },
  {
    type: 'tool/output_delta',
    data: { tool_call_id: 'tc-cancel', channel: 'stdout', delta: 'partial output' },
    seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T,
  },
  {
    type: 'tool/result',
    data: { tool_call_id: 'tc-cancel', content: RESULT_CONTENT },
    seq: 6, session_id: SID, run_id: RUN, step_id: 1, time: T,
  },
  { type: 'run/completed', data: {}, seq: 7, session_id: SID, run_id: RUN, time: T },
];

test('fixture replay: stopped card, interrupted Inspector title, excluded failure filter, preserved Raw', async ({
  page,
}, testInfo) => {
  await routeApi(page, {
    sessions: [{
      session_id: SID,
      event_count: EVENTS.length,
      first_event_time: T,
      last_event_time: T,
      first_user_message: '检查取消结果',
      trace_id: null,
      trace_url: null,
    }],
    events: EVENTS,
  });
  await page.goto('/');
  await page.locator('.session-item').click();

  const card = page.locator('.act-card').filter({ hasText: 'sleep 1' }).first();
  await expect(card.locator('.act-status-stopped')).toBeVisible();
  await expect(card.locator('.act-error-line')).toHaveCount(0);

  // The tool card exposes its raw event at L2; click according to its current disclosure level.
  const cardButton = card.locator('.act-node');
  for (let i = 0; i < 3 && (await cardButton.getAttribute('aria-level')) !== '2'; i++) {
    await cardButton.click();
  }
  await expect(card.locator('.act-raw-label', { hasText: 'tool/result 原始事件' })).toBeVisible();
  await expect(card.locator('.act-raw')).toContainText('COMMAND_CANCELLED');
  await expect(card.locator('.act-raw')).toContainText('cancelled');

  const resultRow = page.locator('.timeline-row').filter({ hasText: 'tool/result' });
  await expect(resultRow).toContainText('跑 sleep 1 · 已中断');
  const failFilter = page.locator('.tl-chip-fail');
  await expect(failFilter).toContainText('0');

  const fixturePath = testInfo.outputPath('tool-result-cancellation-fixture.json');
  await writeFile(fixturePath, JSON.stringify(EVENTS, null, 2), 'utf8');
  await testInfo.attach('tool-result-cancellation-fixture.json', {
    path: fixturePath,
    contentType: 'application/json',
  });
  const overviewPath = testInfo.outputPath('tool-result-cancellation-overview.png');
  await page.screenshot({ path: overviewPath, fullPage: true });
  await testInfo.attach('tool-result-cancellation-overview.png', {
    path: overviewPath,
    contentType: 'image/png',
  });
  await card.locator('.act-raw-section').filter({ hasText: 'tool/result 原始事件' }).scrollIntoViewIfNeeded();
  const rawPath = testInfo.outputPath('tool-result-cancellation-raw.png');
  await page.screenshot({ path: rawPath, fullPage: true });
  await testInfo.attach('tool-result-cancellation-raw.png', {
    path: rawPath,
    contentType: 'image/png',
  });

  await failFilter.click();
  await expect(page.locator('.timeline-row')).toHaveCount(0);
  await expect(page.locator('.tl-empty')).toBeVisible();
});
