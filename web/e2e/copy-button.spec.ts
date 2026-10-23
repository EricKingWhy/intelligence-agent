import { expect, test } from '@playwright/test';
import { routeApi } from './fixtures';

test('does not show copied feedback when the clipboard fallback fails', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async () => { throw new Error('clipboard unavailable'); } },
    });
    Object.defineProperty(document, 'execCommand', { configurable: true, value: () => false });
  });

  const frames = [
    { type: 'session/started', seq: 1, session_id: 'e2e-copy-session', run_id: 'e2e-copy-run', time: '2026-10-23T00:00:00Z' },
    { type: 'run/started', seq: 2, session_id: 'e2e-copy-session', run_id: 'e2e-copy-run', time: '2026-10-23T00:00:00Z' },
    { type: 'user/message', data: { content: 'show a code block' }, seq: 3, session_id: 'e2e-copy-session', run_id: 'e2e-copy-run', step_id: 1, time: '2026-10-23T00:00:00Z' },
    { type: 'model/completed', data: { content: '```text\ncopy me\n```' }, seq: 4, session_id: 'e2e-copy-session', run_id: 'e2e-copy-run', step_id: 1, time: '2026-10-23T00:00:00Z' },
    { type: 'run/completed', data: {}, seq: 5, session_id: 'e2e-copy-session', run_id: 'e2e-copy-run', time: '2026-10-23T00:00:00Z' },
  ];
  await routeApi(page, {
    sessions: [{ session_id: 'e2e-copy-session', event_count: 5, first_event_time: '2026-10-23T00:00:00Z', last_event_time: '2026-10-23T00:00:00Z', first_user_message: 'show a code block', trace_id: null, trace_url: null }],
    events: frames,
  });

  await page.goto('/');
  await page.locator('.session-item').first().click();
  const button = page.getByRole('button', { name: '复制代码' });
  await expect(button).toBeVisible();
  await button.click();

  await expect(button).not.toHaveClass(/copied/);
  await expect(button.locator('.copy-btn-label')).toHaveCount(0);
  await expect(button).toHaveAttribute('aria-label', '复制代码');
});
