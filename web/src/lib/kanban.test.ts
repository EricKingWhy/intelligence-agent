/** #1304 看板纯函数层单测：列常量 / 一卡一列分组 / 列内排序 / 搜索 / 开关持久化。
 *
 *  这些是票面 AC-1、AC-2、AC-3、AC-4、AC-15 的**纯函数侧**判据——e2e 考的是渲染与
 *  交互，这里考的是语义本身（分支全覆盖：null 排序键、非法 product_state、空查询、
 *  localStorage 抛异常）。 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { SessionSummary } from '../types';
import {
  COLUMN_TESTIDS,
  KANBAN_COLUMNS,
  SHOW_ACCEPTED_KEY,
  UNRENDERED_STATE,
  cardTitle,
  columnOf,
  deliverableCount,
  filterByTitle,
  groupSessions,
  readShowAccepted,
  sortColumn,
  unknownStateCount,
  writeShowAccepted,
} from './kanban';

function summary(id: string, overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    session_id: id,
    event_count: 1,
    first_event_time: '2026-10-17T00:00:00Z',
    last_event_time: '2026-10-17T00:00:00Z',
    first_user_message: `任务 ${id}`,
    trace_id: null,
    trace_url: null,
    workspace: null,
    archived: false,
    corrupted: false,
    product_state: '',
    product_state_changed_at: null,
    attention_kind: null,
    attention_summary: null,
    last_user_message: null,
    last_agent_message: null,
    ...overrides,
  };
}

describe('#1304 KANBAN_COLUMNS', () => {
  it('列顺序是票面钉死的四态投影顺序，且不含 deliverable', () => {
    expect(KANBAN_COLUMNS).toEqual(['', 'executing', 'pending_verification', 'accepted']);
    expect(KANBAN_COLUMNS).not.toContain('deliverable');
  });

  it('每列都有 testid slug，且互不为前缀（AC-1 判据靠它才能精确计数）', () => {
    const slugs = KANBAN_COLUMNS.map((c) => COLUMN_TESTIDS[c]);
    expect(new Set(slugs).size).toBe(KANBAN_COLUMNS.length);
    for (const a of slugs) {
      for (const b of slugs) {
        if (a === b) continue;
        // Playwright 的 getByTestId 默认子串匹配：任一 slug 是另一条的前缀就会串台。
        expect(`kanban-col-${b}`.startsWith(`kanban-col-${a}`)).toBe(false);
      }
    }
  });
});

describe('#1304 columnOf', () => {
  it('四态各自映射到自己那一列（backlog 是空串，不是 "backlog"）', () => {
    expect(columnOf(summary('a', { product_state: '' }))).toBe('');
    expect(columnOf(summary('b', { product_state: 'executing' }))).toBe('executing');
    expect(columnOf(summary('c', { product_state: 'pending_verification' }))).toBe(
      'pending_verification',
    );
    expect(columnOf(summary('d', { product_state: 'accepted' }))).toBe('accepted');
  });

  it('deliverable 不属于任何渲染中的列（返回 null，不静默归到 backlog）', () => {
    expect(columnOf(summary('e', { product_state: 'deliverable' }))).toBeNull();
  });

  it('后端将来加的新状态也不许落到 backlog（一律 null）', () => {
    expect(columnOf(summary('f', { product_state: 'some_future_state' }))).toBeNull();
  });
});

describe('#1304 groupSessions', () => {
  it('一卡一列：每张卡恰好进一列，且四列键恒在（空列也返回）', () => {
    const rows = [
      summary('a', { product_state: '' }),
      summary('b', { product_state: 'executing' }),
      summary('c', { product_state: 'executing' }),
      summary('d', { product_state: 'deliverable' }),
    ];
    const grouped = groupSessions(rows);
    expect(Object.keys(grouped).sort()).toEqual([...KANBAN_COLUMNS].sort());
    expect(grouped[''].map((s) => s.session_id)).toEqual(['a']);
    expect(grouped.executing.map((s) => s.session_id)).toEqual(['b', 'c']);
    expect(grouped.pending_verification).toEqual([]);
    expect(grouped.accepted).toEqual([]);
    // deliverable 不进任何列，但也没被丢掉——计数单独报（见 deliverableCount）。
    const inColumns = KANBAN_COLUMNS.flatMap((c) => grouped[c].map((s) => s.session_id));
    expect(inColumns).toHaveLength(3);
    expect(inColumns).not.toContain('d');
  });

  it('列内按 product_state_changed_at 降序，null 排最后，再按 last_event_time 降序', () => {
    const rows = [
      summary('t1', { product_state: 'executing', product_state_changed_at: '2026-10-17T01:00:00Z' }),
      summary('t3', { product_state: 'executing', product_state_changed_at: '2026-10-17T03:00:00Z' }),
      summary('t2', { product_state: 'executing', product_state_changed_at: '2026-10-17T02:00:00Z' }),
      summary('n1', { product_state: 'executing', product_state_changed_at: null, last_event_time: '2026-10-17T05:00:00Z' }),
      summary('n2', { product_state: 'executing', product_state_changed_at: null, last_event_time: '2026-10-17T09:00:00Z' }),
    ];
    expect(groupSessions(rows).executing.map((s) => s.session_id)).toEqual([
      't3', 't2', 't1', 'n2', 'n1',
    ]);
  });

  it('两种 ISO 形状（Z 与 +00:00）混排时按真实时刻排，不按字典序', () => {
    // 'Z' 与 '+00:00' 的字典序与时间序相反：字符串比较会把 01:00Z 排在 02:00+00:00 之后。
    const rows = [
      summary('early', { product_state: '', product_state_changed_at: '2026-10-17T01:00:00Z' }),
      summary('late', { product_state: '', product_state_changed_at: '2026-10-17T02:00:00+00:00' }),
    ];
    expect(sortColumn(rows).map((s) => s.session_id)).toEqual(['late', 'early']);
  });

  it('不可解析的时刻按"没有这个键"处理，不猜一个 0（否则会排到最前）', () => {
    const rows = [
      summary('bad', { product_state: '', product_state_changed_at: 'not-a-time' }),
      summary('good', { product_state: '', product_state_changed_at: '2020-01-01T00:00:00Z' }),
    ];
    expect(sortColumn(rows).map((s) => s.session_id)).toEqual(['good', 'bad']);
  });
});

describe('#1304 deliverableCount', () => {
  it('只数 deliverable；0 条时为 0（AC-3：0 条不显示警示行）', () => {
    expect(deliverableCount([summary('a', { product_state: 'deliverable' })])).toBe(1);
    expect(
      deliverableCount([
        summary('a', { product_state: 'deliverable' }),
        summary('b', { product_state: 'executing' }),
        summary('c', { product_state: 'deliverable' }),
      ]),
    ).toBe(2);
    expect(deliverableCount([summary('a', { product_state: 'executing' })])).toBe(0);
  });

  it('UNRENDERED_STATE 与后端字面量一致', () => {
    expect(UNRENDERED_STATE).toBe('deliverable');
  });
});

describe('#1304 unknownStateCount (P4-4)', () => {
  it('只数四态与 deliverable 之外的未知状态（不与 deliverableCount 重复报警）', () => {
    expect(
      unknownStateCount([
        summary('a', { product_state: 'some_future_state' }),
        summary('b', { product_state: 'deliverable' }),
        summary('c', { product_state: 'executing' }),
        summary('d', { product_state: '' }),
        summary('e', { product_state: 'another_new_state' }),
      ]),
    ).toBe(2);
  });

  it('全已知状态时为 0（0 条不显示警示行）', () => {
    expect(
      unknownStateCount([
        summary('a', { product_state: '' }),
        summary('b', { product_state: 'executing' }),
        summary('c', { product_state: 'deliverable' }),
      ]),
    ).toBe(0);
  });
});

describe('#1304 cardTitle', () => {
  it('有 first_user_message 用它；为 null 渲染占位（不渲染空字符串）', () => {
    expect(cardTitle(summary('a', { first_user_message: '整理发布说明' }))).toBe('整理发布说明');
    expect(cardTitle(summary('b', { first_user_message: null }))).toBe('(未命名会话)');
  });
});

describe('#1304 filterByTitle', () => {
  const rows = [
    summary('a', { first_user_message: '整理发布说明' }),
    summary('b', { first_user_message: 'Fix the Release Notes' }),
    summary('c', { first_user_message: null }),
  ];

  it('大小写不敏感子串过滤（中英各一）', () => {
    expect(filterByTitle(rows, '发布').map((s) => s.session_id)).toEqual(['a']);
    expect(filterByTitle(rows, 'release').map((s) => s.session_id)).toEqual(['b']);
    expect(filterByTitle(rows, 'RELEASE').map((s) => s.session_id)).toEqual(['b']);
  });

  it('空查询 / 纯空白 = 不过滤（清空后全部恢复）', () => {
    expect(filterByTitle(rows, '')).toHaveLength(3);
    expect(filterByTitle(rows, '   ')).toHaveLength(3);
  });

  it('first_user_message 为 null 的卡不会被空查询命中之外的任何查询命中', () => {
    expect(filterByTitle(rows, '任务')).toHaveLength(0);
  });
});

describe('#1304 已接受开关持久化', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  /** 项目没有 jsdom 依赖（同 `railArchive.test.ts` / `density.test.ts` 的说明），
   *  用最小 stub 模拟本模块触碰的唯一面：`localStorage`。 */
  function installStorageStub(overrides: Partial<Storage> = {}): Map<string, string> {
    const store = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
      clear: () => void store.clear(),
      ...overrides,
    });
    return store;
  }

  it('键名与票面一致；写入 true/false 后读回同值', () => {
    expect(SHOW_ACCEPTED_KEY).toBe('ahi.kanban.showAccepted.v1');
    const store = installStorageStub();
    writeShowAccepted(true);
    expect(store.get(SHOW_ACCEPTED_KEY)).toBe('true');
    expect(readShowAccepted()).toBe(true);
    writeShowAccepted(false);
    expect(store.get(SHOW_ACCEPTED_KEY)).toBe('false');
    expect(readShowAccepted()).toBe(false);
  });

  it('缺省（没写过）= false', () => {
    installStorageStub();
    expect(readShowAccepted()).toBe(false);
  });

  it('手改/旧版本写的别的值一律回 false（没有"半个开关"）', () => {
    const store = installStorageStub();
    store.set(SHOW_ACCEPTED_KEY, '1');
    expect(readShowAccepted()).toBe(false);
    store.set(SHOW_ACCEPTED_KEY, 'yes');
    expect(readShowAccepted()).toBe(false);
  });

  it('localStorage 抛异常时读回默认、写不抛（隐私模式不崩看板）', () => {
    installStorageStub({
      getItem: () => {
        throw new Error('SecurityError: localStorage is disabled');
      },
      setItem: () => {
        throw new Error('SecurityError: localStorage is disabled');
      },
    });
    expect(readShowAccepted()).toBe(false);
    expect(() => writeShowAccepted(true)).not.toThrow();
  });
});
