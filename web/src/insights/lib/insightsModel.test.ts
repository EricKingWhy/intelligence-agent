import { describe, expect, it } from 'vitest';
import type { ContextEventPoint, ContextRequestPoint, InsightsSessionRow } from '../../lib/api';
import {
  attachEventMarkers,
  compositionSegments,
  compositionTotal,
  filterSessions,
  formatActiveMs,
  formatCompact,
  formatCount,
  formatRate,
  groupSessions,
  heatLevel,
  heatmapColumns,
  localDayKey,
  sortSessions,
  thresholdMarks,
  trendScale,
} from './insightsModel';

const row = (overrides: Partial<InsightsSessionRow>): InsightsSessionRow => ({
  session_id: 's1',
  title: '会话一',
  workspace: null,
  billed_tokens: 100,
  turns: 1,
  last_active_at: '2026-10-20T10:00:00',
  ...overrides,
});

const request = (overrides: Partial<ContextRequestPoint>): ContextRequestPoint => ({
  seq: 1,
  time: '2026-10-20T10:00:00',
  prompt_tokens: 100,
  completion_tokens: 20,
  cached_tokens: 40,
  total_tokens: 120,
  duration_ms: 1000,
  ...overrides,
});

describe('compositionSegments', () => {
  it('keeps all six buckets and computes percentages over the snapshot total', () => {
    const segments = compositionSegments({
      messages: 50,
      system_prompt: 20,
      skills: 10,
      tools_system: 10,
      tools_mcp: 5,
      other: 5,
    });
    expect(segments.map((segment) => segment.key)).toEqual([
      'messages',
      'system_prompt',
      'skills',
      'tools_system',
      'tools_mcp',
      'other',
    ]);
    expect(segments[0].pct).toBe(50);
    expect(compositionTotal({
      messages: 50,
      system_prompt: 20,
      skills: 10,
      tools_system: 10,
      tools_mcp: 5,
      other: 5,
    })).toBe(100);
  });

  it('does not fabricate percentages when the snapshot is empty', () => {
    const segments = compositionSegments({
      messages: 0,
      system_prompt: 0,
      skills: 0,
      tools_system: 0,
      tools_mcp: 0,
      other: 0,
    });
    expect(segments.every((segment) => segment.pct === 0)).toBe(true);
  });
});

describe('filterSessions', () => {
  const rows = [
    row({ session_id: 'alpha', title: '修复登录', workspace: 'web', last_active_at: '2026-10-20T09:00:00' }),
    row({ session_id: 'beta', title: '上下文洞察', workspace: 'infra', last_active_at: '2026-10-19T09:00:00' }),
    row({ session_id: 'gamma', title: '无工作区', workspace: null, last_active_at: null }),
  ];

  it('matches id, title and workspace case-insensitively', () => {
    expect(filterSessions(rows, { text: 'ALPHA', day: null }).map((item) => item.session_id)).toEqual(['alpha']);
    expect(filterSessions(rows, { text: '登录', day: null }).map((item) => item.session_id)).toEqual(['alpha']);
    expect(filterSessions(rows, { text: 'INFRA', day: null }).map((item) => item.session_id)).toEqual(['beta']);
  });

  it('filters by local calendar day and excludes rows without a timestamp', () => {
    expect(filterSessions(rows, { text: '', day: '2026-10-20' }).map((item) => item.session_id)).toEqual(['alpha']);
    expect(filterSessions(rows, { text: '', day: '2026-10-18' })).toEqual([]);
  });

  it('treats whitespace-only search as no filter', () => {
    expect(filterSessions(rows, { text: '   ', day: null })).toHaveLength(3);
  });
});

describe('sortSessions', () => {
  const rows = [
    row({ session_id: 'old', billed_tokens: 900, last_active_at: '2026-10-01T00:00:00' }),
    row({ session_id: 'new', billed_tokens: 100, last_active_at: '2026-10-20T00:00:00' }),
    row({ session_id: 'mid', billed_tokens: 500, last_active_at: '2026-10-10T00:00:00' }),
  ];

  it('sorts by recent activity by default', () => {
    expect(sortSessions(rows, 'recent').map((item) => item.session_id)).toEqual(['new', 'mid', 'old']);
  });

  it('sorts by billed tokens with recent as tie-breaker', () => {
    expect(sortSessions(rows, 'tokens').map((item) => item.session_id)).toEqual(['old', 'mid', 'new']);
  });

  it('sorts by context size, puts unknown sizes last, and never invents a size', () => {
    const sorted = sortSessions(rows, 'context', { old: 120, new: 800 });
    expect(sorted.map((item) => item.session_id)).toEqual(['new', 'old', 'mid']);
  });
});

describe('groupSessions', () => {
  it('groups by workspace, keeps row order, and puts ungrouped last', () => {
    const groups = groupSessions([
      row({ session_id: 'b', workspace: 'beta' }),
      row({ session_id: 'a', workspace: 'alpha' }),
      row({ session_id: 'n', workspace: null }),
      row({ session_id: 'b2', workspace: 'beta' }),
    ]);
    expect(groups.map((group) => group.workspace)).toEqual(['beta', 'alpha', null]);
    expect(groups[0].rows.map((item) => item.session_id)).toEqual(['b', 'b2']);
  });

  it('treats an empty workspace string as ungrouped', () => {
    expect(groupSessions([row({ workspace: '   ' })])[0].workspace).toBeNull();
  });
});

describe('heatmapColumns', () => {
  it('builds eight Sunday-first columns ending in the week of today', () => {
    const columns = heatmapColumns(
      [
        { date: '2026-10-18', tokens: 30 },
        { date: '2026-10-20', tokens: 90 },
      ],
      '2026-10-20',
    );
    expect(columns).not.toBeNull();
    expect(columns).toHaveLength(8);
    expect(columns?.every((column) => column.cells.length === 7)).toBe(true);
    const last = columns![7];
    expect(last.cells[0].key).toBe('2026-10-18');
    expect(last.cells[2]).toMatchObject({ key: '2026-10-20', tokens: 90, future: false });
    expect(last.cells[3].future).toBe(true);
    expect(columns![0]).toMatchObject({ month: 8 });
    expect(columns![0].cells[0].key).toBe('2026-08-30');
    expect(columns![1].month).toBe(9);
    expect(columns!.find((column) => column.month === 10)?.cells[0].key).toBe('2026-10-04');
  });

  it('returns null for an invalid day key', () => {
    expect(heatmapColumns([], 'not-a-date')).toBeNull();
  });
});

describe('heatLevel', () => {
  it('maps zero to empty and quarters to four levels', () => {
    expect(heatLevel(0, 100)).toBe(0);
    expect(heatLevel(20, 100)).toBe(1);
    expect(heatLevel(40, 100)).toBe(2);
    expect(heatLevel(70, 100)).toBe(3);
    expect(heatLevel(100, 100)).toBe(4);
  });
});

describe('attachEventMarkers', () => {
  it('attaches each event to the first request at or after its seq', () => {
    const events: ContextEventPoint[] = [
      { seq: 2, time: '2026-10-20T10:01:00', type: 'compaction/start' },
      { seq: 5, time: '2026-10-20T10:02:00', type: 'context/compacted' },
      { seq: 99, time: '2026-10-20T10:03:00', type: 'context/window-changed' },
    ];
    const markers = attachEventMarkers(
      [request({ seq: 1 }), request({ seq: 3 }), request({ seq: 6 })],
      events,
    );
    expect(markers.map((marker) => marker?.seq ?? null)).toEqual([null, 2, 5]);
  });
});

describe('trendScale', () => {
  it('keeps only positive values in total mode', () => {
    expect(trendScale([request({ total_tokens: 10 }), request({ total_tokens: -4 })], 'total'))
      .toEqual({ maxUp: 10, maxDown: 0 });
  });

  it('separates positive and negative deltas in delta mode', () => {
    expect(trendScale([
      request({ total_tokens: 10 }),
      request({ total_tokens: -30 }),
      request({ total_tokens: 20 }),
    ], 'delta')).toEqual({ maxUp: 20, maxDown: 30 });
  });
});

describe('thresholdMarks', () => {
  it('derives labels from the payload ratios instead of hardcoding 70/85', () => {
    expect(thresholdMarks({ auto_compact: 0.6, hard_guard: 0.9 })).toEqual([
      { ratio: 0.6, label: '自动压缩 60%' },
      { ratio: 0.9, label: '硬上限 90%' },
    ]);
  });
});

describe('format helpers', () => {
  it('renders null cache data as an em dash, never 0%', () => {
    expect(formatRate(null)).toBe('—');
    expect(formatRate(0.1234)).toBe('12.3%');
  });

  it('keeps exact counts and compacts only large displays', () => {
    expect(formatCount(12345)).toBe('12,345');
    expect(formatCompact(12345)).toBe('1.2万');
    expect(formatCompact(9999)).toBe('9,999');
  });

  it('renders active duration in coarse human units', () => {
    expect(formatActiveMs(0)).toBe('—');
    expect(formatActiveMs(12_000)).toBe('12秒');
    expect(formatActiveMs(3_725_000)).toBe('1小时2分');
  });

  it('derives a local calendar day key and rejects invalid input', () => {
    expect(localDayKey('2026-10-20T23:30:00')).toBe('2026-10-20');
    expect(localDayKey(null)).toBeNull();
    expect(localDayKey('nope')).toBeNull();
  });
});
