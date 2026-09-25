import { expect, test, type Page } from '@playwright/test';
import { T, routeApi, sessionRow, type FrameSpec, type MemoryFixture } from './fixtures';

const semantic = (id: string, content: string, over: Partial<MemoryFixture> = {}): MemoryFixture => ({
  id,
  root_id: id,
  version: 1,
  content,
  scope: 'user_global',
  tier: 'profile',
  status: 'active',
  kind: 'semantic',
  project_id: null,
  source_type: 'automatic',
  source_session_id: 'source-session',
  source_event_ids: ['source-event'],
  payload: { kind: 'semantic', subject: '用户', fact: content, category: 'preference' },
  metadata: {},
  created_at: T,
  ...over,
});

const episodic = (id: string, content: string): MemoryFixture => ({
  id,
  root_id: id,
  version: 1,
  content,
  scope: 'project',
  tier: 'collection',
  status: 'active',
  kind: 'episodic',
  project_id: 'project-1',
  source_type: 'explicit_command',
  source_session_id: null,
  source_event_ids: [],
  payload: { kind: 'episodic', situation: 'test', action: 'run', outcome: 'ok', lesson: content },
  metadata: {},
  created_at: T,
});

const panel = (page: Page) => page.locator('.memory-panel');
const openPanel = async (page: Page) => {
  await page.getByRole('button', { name: '记忆管理' }).click();
  await expect(panel(page)).toBeVisible();
};

test('AC1: search and all list filters are sent to the API and render its result', async ({ page }) => {
  const requests: URL[] = [];
  await routeApi(page, {
    memories: [
      semantic('m-sem', 'specific semantic fact'),
      { id: 'legacy-epi', content: 'specific episodic fact', scope: 'user', metadata: {}, created_at: T },
    ],
    onMemoriesGet: (route) => {
      requests.push(new URL(route.request().url()));
      return false;
    },
  });
  await page.goto('/');
  await openPanel(page);

  await panel(page).getByRole('textbox', { name: '搜索记忆' }).fill('specific');
  await panel(page).getByRole('button', { name: '搜索' }).click();
  await expect(panel(page).locator('.memory-row')).toHaveCount(2);
  await panel(page).getByLabel('类型', { exact: true }).selectOption('semantic');
  await panel(page).getByLabel('状态', { exact: true }).selectOption('active');
  await panel(page).getByLabel('范围', { exact: true }).selectOption('user_global');
  await expect(panel(page).locator('.memory-row')).toHaveCount(1);
  await expect(panel(page).locator('.memory-row')).toContainText('specific semantic fact');
  await expect.poll(() => requests.some((url) => url.searchParams.get('q') === 'specific'
    && url.searchParams.get('kind') === 'semantic'
    && url.searchParams.get('status') === 'active'
    && url.searchParams.get('scope') === 'user_global')).toBe(true);
});

test('project memory operations carry its authorized context and bulk preview covers global plus project', async ({ page }) => {
  const requested: URL[] = [];
  const bulkUrls: URL[] = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/memories')) requested.push(url);
    if (url.pathname === '/api/memories/bulk-delete') bulkUrls.push(url);
  });
  await routeApi(page, {
    projects: [{ id: 'project-1', path: 'C:/project', title: 'Project One', session_ids: [] }],
    memories: [
      semantic('m-global', 'global fact'),
      episodic('m-project', 'project fact'),
    ],
  });
  await page.goto('/');
  await openPanel(page);

  await panel(page).getByLabel('项目范围').selectOption('project-1');
  await expect(panel(page).locator('.memory-row')).toHaveCount(1);
  await expect(panel(page).locator('.memory-row')).toContainText('project fact');
  await expect.poll(() => requested.some((url) => url.searchParams.get('project_id') === 'project-1')).toBe(true);

  const row = panel(page).locator('.memory-row').filter({ hasText: 'project fact' });
  await row.getByRole('button', { name: '编辑' }).click();
  await row.getByLabel('记忆正文').fill('edited project canonical');
  await row.getByLabel('经验').fill('edited project lesson');
  await row.getByRole('button', { name: '保存新版本' }).click();
  const updatedRow = panel(page).locator('.memory-row').filter({ hasText: 'edited project canonical' });
  await expect(updatedRow).toContainText('v2');
  await expect.poll(() => requested.some((url) => url.pathname.endsWith('/m-project')
    && url.searchParams.get('project_id') === 'project-1'
    && url.pathname.startsWith('/api/memories/'))).toBe(true);

  await updatedRow.getByRole('button', { name: '版本历史' }).click();
  await expect(updatedRow.locator('.memory-v2-history')).toContainText('v1');
  await expect.poll(() => requested.some((url) => url.pathname.endsWith('/m-project-v2/versions')
    && url.searchParams.get('project_id') === 'project-1')).toBe(true);

  await panel(page).getByRole('button', { name: '预览删除' }).click();
  const confirmation = panel(page).getByRole('group', { name: '确认批量删除' });
  await expect(confirmation).toContainText('全局和“Project One”项目中 2 条');
  await panel(page).getByRole('textbox', { name: '输入 DELETE 确认' }).fill('DELETE');
  await panel(page).getByRole('button', { name: '永久删除' }).click();
  await expect.poll(() => bulkUrls.at(-1)?.searchParams.get('project_id')).toBe('project-1');
});

test('deleting a project memory preserves its authorized context', async ({ page }) => {
  const deleteUrls: URL[] = [];
  page.on('request', (request) => {
    if (request.method() === 'DELETE' && request.url().includes('/api/memories/')) {
      deleteUrls.push(new URL(request.url()));
    }
  });
  await routeApi(page, {
    projects: [{ id: 'project-1', path: 'C:/project', title: 'Project One', session_ids: [] }],
    memories: [episodic('m-delete-project', 'project memory to delete')],
  });
  await page.goto('/');
  await openPanel(page);
  await panel(page).getByLabel('项目范围').selectOption('project-1');

  const row = panel(page).locator('.memory-row').filter({ hasText: 'project memory to delete' });
  await row.getByRole('button', { name: '删除这条记忆' }).click();
  await row.getByRole('button', { name: '确认删除' }).click();
  await expect(panel(page)).toContainText('已删除 1 条记忆');
  expect(deleteUrls).toHaveLength(1);
  expect(deleteUrls[0].searchParams.get('project_id')).toBe('project-1');
});

test('AC3 and AC8: editing validates fields, creates a user-edit version, and exposes history', async ({ page }) => {
  await routeApi(page, { memories: [semantic('m-edit', 'original fact')] });
  await page.goto('/');
  await openPanel(page);
  const original = panel(page).locator('.memory-row').filter({ hasText: 'original fact' });
  await original.getByRole('button', { name: '编辑' }).click();
  await original.getByLabel('事实').fill('');
  await original.getByRole('button', { name: '保存新版本' }).click();
  await expect(original.getByRole('alert')).toContainText('事实不能为空');

  await original.getByLabel('事实').fill('edited fact');
  await original.getByLabel('记忆正文').fill('canonical edited fact');
  await original.getByRole('button', { name: '保存新版本' }).click();
  const updated = panel(page).locator('.memory-row').filter({ hasText: 'canonical edited fact' });
  await expect(updated).toContainText('v2');
  await expect(updated).toContainText('user_edit');
  await updated.getByRole('button', { name: '版本历史' }).click();
  await expect(updated.locator('.memory-v2-history')).toContainText('v2');
  await expect(updated.locator('.memory-v2-history')).toContainText('v1');
});

test('AC8: a stale version conflict refreshes the row and leaves a dismissible explanation', async ({ page }) => {
  await routeApi(page, {
    memories: [semantic('m-stale', 'stale fact')],
    memoryEditConflictIds: ['m-stale'],
  });
  await page.goto('/');
  await openPanel(page);
  const row = panel(page).locator('.memory-row').filter({ hasText: 'stale fact' });
  await row.getByRole('button', { name: '编辑' }).click();
  await row.getByRole('button', { name: '保存新版本' }).click();
  const notice = panel(page).locator('.memory-error');
  await expect(notice).toContainText('已被其他操作更新或删除');
  await expect(row).toBeVisible();
  await notice.getByRole('button', { name: '知道了' }).click();
  await expect(notice).toHaveCount(0);
});

test('AC4: bulk delete previews an exact server count and sends the confirmation token', async ({ page }) => {
  let bulkBody: unknown;
  page.on('request', (request) => {
    if (request.url().endsWith('/api/memories/bulk-delete')) bulkBody = request.postDataJSON();
  });
  await routeApi(page, { memories: [semantic('m-one', 'first'), semantic('m-two', 'second')] });
  await page.goto('/');
  await openPanel(page);

  await panel(page).getByRole('button', { name: '预览删除' }).click();
  await expect(panel(page).getByRole('group', { name: '确认批量删除' })).toContainText('2 条');
  await panel(page).getByRole('textbox', { name: '输入 DELETE 确认' }).fill('DELETE');
  await panel(page).getByRole('button', { name: '永久删除' }).click();
  await expect(panel(page)).toContainText('已删除 2 条记忆');
  await expect(panel(page).locator('.memory-row')).toHaveCount(0);
  expect(bulkBody).toEqual({ kind: null, confirmation: 'DELETE' });
});

test('AC5: a failed setting update leaves the last server-confirmed value checked', async ({ page }) => {
  await routeApi(page, {
    memories: [],
    memorySettings: { extraction_enabled: true, recall_enabled: false },
    memorySettingsPatchError: { status: 503, detail: 'settings temporarily unavailable' },
  });
  await page.goto('/');
  await openPanel(page);

  const extraction = panel(page).getByRole('checkbox', { name: '自动提取' });
  await expect(extraction).toBeChecked();
  await extraction.click();
  await expect(panel(page).getByRole('alert')).toContainText('settings temporarily unavailable');
  await expect(extraction).toBeChecked();
  await expect(panel(page).getByRole('checkbox', { name: '自动召回' })).not.toBeChecked();
});

test('AC6 and AC7: update content is fetched only on expansion and recall shows redacted factors', async ({ page }) => {
  const events: FrameSpec[] = [
    { type: 'session/started', seq: 1, session_id: 's-memory', run_id: 'run-memory', time: T },
    { type: 'run/started', seq: 2, session_id: 's-memory', run_id: 'run-memory', time: T },
    { type: 'user/message', data: { content: 'Memory management demo' }, seq: 3, session_id: 's-memory', run_id: 'run-memory', step_id: 1, time: T },
    { type: 'memory/updated', data: { count: 1, memory_ids: ['m-notice'] }, seq: 4, session_id: 's-memory', run_id: 'run-memory', time: T },
    { type: 'memory/recalled', data: { memory_ids: ['m-notice'] }, seq: 5, session_id: 's-memory', run_id: 'run-memory', time: T },
  ];
  const recall = [{
    run_id: 'run-memory', seq: 5, time: T, memories: [{
      memory_id: 'm-notice', kind: 'semantic', scope: 'user_global', source_type: 'automatic',
      version: 1, source_session_id: 'source-session', source_event_ids: ['source-event'],
      ranking: { dense: 0.84, score: 0.91, private_prompt: 'must not render' },
    }],
  }];
  const requested: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/api/memories/m-notice') || request.url().includes('/memory-recalls')) requested.push(request.url());
  });
  await routeApi(page, {
    sessions: [sessionRow('s-memory')],
    projects: [{ id: 'project-1', path: 'C:/project', title: 'Project One', session_ids: ['s-memory'] }],
    events,
    memories: [semantic('m-notice', 'fresh authorized memory content', { scope: 'project', project_id: 'project-1' })],
    memoryRecalls: recall,
  });
  await page.goto('/');
  await page.locator('.session-item').first().click();

  const notice = page.locator('.memory-v2-activity-card').filter({ hasText: '已更新 1 条记忆' });
  await expect(notice).toBeVisible();
  await expect(notice).not.toContainText('fresh authorized memory content');
  expect(requested).toEqual([]);
  await notice.getByRole('button', { name: /查看更新/ }).click();
  await expect(notice).toContainText('fresh authorized memory content');
  const recalled = page.locator('.memory-v2-activity-card').filter({ hasText: '记忆召回说明' });
  await recalled.getByRole('button', { name: /查看原因/ }).click();
  await expect(recalled).toContainText('dense');
  await expect(recalled).toContainText('0.84');
  await expect(recalled).not.toContainText('must not render');
  expect(requested.some((url) => url.includes('/api/memories/m-notice'))).toBe(true);
  expect(requested.some((url) => url.includes('/api/memories/m-notice?project_id=project-1'))).toBe(true);
  expect(requested.some((url) => url.includes('/memory-recalls'))).toBe(true);
});

test('AC9: dialog keyboard escape restores focus to its opener', async ({ page }) => {
  await routeApi(page, { memories: [] });
  await page.goto('/');
  const opener = page.getByRole('button', { name: '记忆管理' });
  await opener.focus();
  await page.keyboard.press('Enter');
  await expect(panel(page)).toBeVisible();
  await expect.poll(() => panel(page).evaluate((dialog) => dialog.contains(document.activeElement))).toBe(true);
  await page.keyboard.press('Escape');
  await expect(panel(page)).toBeHidden();
  await expect(opener).toBeFocused();
});
