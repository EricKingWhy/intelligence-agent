// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  bulkDeleteMemoryRecords,
  countMemoryRoots,
  deleteMemoryRecord,
  listMemoryRecords,
  getMemoryRecord,
  getMemoryVersions,
  getSessionMemoryRecalls,
  editMemoryRecord,
  updateMemorySettings,
} from './memoryV2Api';

const v2Record = (overrides: Record<string, unknown> = {}) => ({
  id: 'm-1',
  root_id: 'root-1',
  version: 2,
  content: 'User prefers concise reports',
  scope: 'user_global',
  tier: 'profile',
  status: 'active',
  kind: 'semantic',
  project_id: null,
  source_type: 'user_edit',
  source_session_id: 's-1',
  source_event_ids: ['evt-1'],
  payload: {
    kind: 'semantic',
    subject: 'user',
    fact: 'prefers concise reports',
    category: 'preference',
  },
  metadata: {},
  created_at: '2026-09-25T10:00:00Z',
  ...overrides,
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('Memory V2 API adapter', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('sends server filters and parses V2 plus compatible V1 list rows', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([
      v2Record(),
      {
        id: 'legacy-1',
        content: 'Legacy fact',
        scope: 'user',
        metadata: {},
        created_at: '2026-09-24T10:00:00Z',
      },
    ]));
    vi.stubGlobal('fetch', fetchMock);

    const records = await listMemoryRecords(25, 50, {
      q: ' concise ',
      kind: 'semantic',
      status: 'active',
      scope: 'user_global',
      project_id: 'project 1',
    });

    const url = new URL(String(fetchMock.mock.calls[0][0]), 'http://localhost');
    expect(url.pathname).toBe('/api/memories');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      limit: '25',
      offset: '50',
      q: 'concise',
      kind: 'semantic',
      status: 'active',
      scope: 'user_global',
      project_id: 'project 1',
    });
    expect(records.map((record) => record.id)).toEqual(['m-1', 'legacy-1']);
    expect(records[0]).toMatchObject({
      root_id: 'root-1',
      version: 2,
      kind: 'semantic',
      tier: 'profile',
      payload: { fact: 'prefers concise reports' },
    });
    expect(records[1].kind).toBeNull();
  });

  it('counts distinct V2 roots from authorized pages and excludes V1 rows', async () => {
    const firstPage: unknown[] = Array.from({ length: 198 }, (_, index) =>
      v2Record({ id: `v${index}`, root_id: `root-${index}` }),
    );
    firstPage.push(v2Record({ id: 'v0-old', root_id: 'root-0', version: 1 }));
    firstPage.push({
      id: 'legacy-1', content: 'legacy', scope: 'user', metadata: {},
      created_at: '2026-09-24T10:00:00Z',
    });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(firstPage))
      .mockResolvedValueOnce(jsonResponse([
        v2Record({ id: 'v197-old', root_id: 'root-197', version: 1 }),
        v2Record({ id: 'v198', root_id: 'root-198' }),
        v2Record({ id: 'v199', root_id: 'root-199' }),
      ]));
    vi.stubGlobal('fetch', fetchMock);

    await expect(countMemoryRoots()).resolves.toEqual({ count: 200, exact: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[1][0])).toContain('offset=200');
  });

  it('reads a current authorized record using an encoded memory id', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(v2Record()));
    vi.stubGlobal('fetch', fetchMock);

    await expect(getMemoryRecord('m/one')).resolves.toMatchObject({ id: 'm-1' });
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/memories/m%2Fone');
  });

  it('passes the trusted project context through record operations and bulk deletion', async () => {
    const projectRecord = v2Record({ project_id: 'project-1', scope: 'project' });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(projectRecord))
      .mockResolvedValueOnce(jsonResponse([projectRecord]))
      .mockResolvedValueOnce(jsonResponse(projectRecord))
      .mockResolvedValueOnce(jsonResponse({ id: 'm-1', deleted: true }))
      .mockResolvedValueOnce(jsonResponse({ affected_count: 1, deleted_version_count: 2 }));
    vi.stubGlobal('fetch', fetchMock);

    await getMemoryRecord('m-1', 'project-1');
    await getMemoryVersions('m-1', 'project-1');
    await editMemoryRecord('m-1', {
      expected_version: 2,
      content: 'edited',
      payload: { kind: 'semantic', subject: 'user', fact: 'edited', category: 'preference' },
    }, 'project-1');
    await expect(deleteMemoryRecord('m-1', 'project-1')).resolves.toBe(true);
    await bulkDeleteMemoryRecords(null, 'project-1');

    for (const [input] of fetchMock.mock.calls) {
      expect(new URL(String(input), 'http://localhost').searchParams.get('project_id')).toBe('project-1');
    }
  });

  it('counts global and selected-project roots once each for scoped bulk-delete preview', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost');
      const row = url.searchParams.has('project_id')
        ? v2Record({ id: 'm-project', root_id: 'root-project', scope: 'project', project_id: 'project-1' })
        : v2Record({ id: 'm-global', root_id: 'root-global' });
      return Promise.resolve(jsonResponse([row]));
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(countMemoryRoots('semantic', 'project-1')).resolves.toEqual({ count: 2, exact: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(new URL(String(fetchMock.mock.calls[1][0]), 'http://localhost').searchParams.get('project_id')).toBe('project-1');
  });

  it('sends confirmed bulk deletes and settings updates using backend contract shapes', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ affected_count: 3, deleted_version_count: 5 }))
      .mockResolvedValueOnce(jsonResponse({ extraction_enabled: false, recall_enabled: true }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(bulkDeleteMemoryRecords(null)).resolves.toEqual({
      affected_count: 3,
      deleted_version_count: 5,
    });
    await expect(updateMemorySettings({ extraction_enabled: false })).resolves.toEqual({
      extraction_enabled: false,
      recall_enabled: true,
    });

    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'POST' });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      kind: null,
      confirmation: 'DELETE',
    });
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'PATCH' });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      extraction_enabled: false,
    });
  });

  it('rejects full deleted version records outside the content-free tombstone contract', async () => {
    const deleted = v2Record({
      id: 'm-deleted', status: 'deleted', content: 'must stay hidden', payload: null, version: 4,
    });
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([deleted]));
    vi.stubGlobal('fetch', fetchMock);

    const versions = await getMemoryVersions('root/1');

    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/memories/root%2F1/versions');
    expect(versions).toEqual([]);
  });

  it('parses authorized tombstone summaries without accepting content or hash fields', async () => {
    const tombstone = {
      id: 'm-deleted',
      root_id: 'root-deleted',
      scope: 'user_global',
      project_id: null,
      status: 'deleted',
      deleted_at: '2026-09-25T10:00:00Z',
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse([tombstone]))
      .mockResolvedValueOnce(jsonResponse([v2Record({
        id: tombstone.id,
        root_id: tombstone.root_id,
        scope: tombstone.scope,
        project_id: tombstone.project_id,
        status: 'deleted',
        content: 'must stay hidden',
      })]))
      .mockResolvedValueOnce(jsonResponse([{ ...tombstone, scope: 'project' }]));
    vi.stubGlobal('fetch', fetchMock);

    const [record] = await listMemoryRecords(20, 0, { status: 'deleted' });
    const malformed = await listMemoryRecords(20, 0, { status: 'deleted' });
    const invalidProject = await listMemoryRecords(20, 0, { status: 'deleted' });

    expect(record).toMatchObject({
      id: 'm-deleted', root_id: 'root-deleted', status: 'deleted',
      is_tombstone: true, content: '', deleted_at: tombstone.deleted_at,
      created_at: tombstone.deleted_at, payload: null,
    });
    expect(malformed).toEqual([]);
    expect(invalidProject).toEqual([]);
    expect(String(fetchMock.mock.calls[0][0])).toContain('status=deleted');
  });

  it('sends the expected version and parses only approved recall explanation fields', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(v2Record({ version: 3, source_type: 'user_edit' })))
      .mockResolvedValueOnce(jsonResponse([{
        run_id: 'run-1', seq: 17, time: '2026-09-25T10:00:00Z', memories: [{
          memory_id: 'm-1', kind: 'semantic', scope: 'user_global', source_type: 'automatic',
          version: 3, source_session_id: null, source_event_ids: ['evt-1'],
          ranking: { dense: 0.8, score: 0.9, raw_evidence: 'private' },
          content: 'private content',
        }],
      }]));
    vi.stubGlobal('fetch', fetchMock);

    await editMemoryRecord('m-1', {
      expected_version: 2,
      content: 'edited',
      payload: { kind: 'semantic', subject: 'user', fact: 'prefers concise reports', category: 'preference' },
    });
    const recalls = await getSessionMemoryRecalls('session/1');

    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({ expected_version: 2 });
    expect(String(fetchMock.mock.calls[1][0])).toBe('/api/sessions/session%2F1/memory-recalls');
    expect(recalls[0].memories[0]).toEqual(expect.objectContaining({
      memory_id: 'm-1', ranking: { dense: 0.8, score: 0.9 },
    }));
    expect(recalls[0].memories[0]).not.toHaveProperty('content');
  });

  it('uses the current API token on memory requests', async () => {
    localStorage.setItem('ahi.apiToken', 'test-token');
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await listMemoryRecords();

    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get('Authorization')).toBe(
      'Bearer test-token',
    );
  });

  it('refuses confirmation when exact count would exceed the list offset ceiling', async () => {
    const fullPage = Array.from({ length: 200 }, (_, index) =>
      v2Record({ id: `v${index}`, root_id: `root-${index}` }),
    );
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const offset = Number(new URL(String(input), 'http://localhost').searchParams.get('offset'));
      return Promise.resolve(jsonResponse(fullPage.map((row) => ({
        ...row,
        id: `m-${offset}-${row.id}`,
        root_id: `root-${offset}-${row.id}`,
      }))));
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(countMemoryRoots()).resolves.toEqual({ count: 10_200, exact: false });
    expect(fetchMock).toHaveBeenCalledTimes(51);
  });
});
