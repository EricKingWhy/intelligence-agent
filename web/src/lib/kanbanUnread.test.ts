/** #1306 看板未读 / ack 纯函数层单测（AC-7 的语义侧判据）。
 *
 *  比较键选型的**消融对照**在第 5 条：`last_event_time` 推进但
 *  `product_state_changed_at` 不变时仍必须判已读——这正是 Orca 修掉的那个 bug
 *  （agent 每说一句话都刷新 updatedAt，读过的卡立刻重新变粗、闪烁）。 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { SessionSummary } from '../types';
import { KANBAN_ACK_KEY, markAcknowledged, readAck, unread, writeAck } from './kanbanUnread';

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
    ...overrides,
  };
}

/** 项目没有 jsdom（同 `kanban.test.ts` 的说明），只 stub 本模块碰到的 `localStorage`。 */
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

const CHANGED = '2026-10-17T03:00:00Z';

describe('#1306 unread()', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('1. product_state_changed_at 为 null（含不可解析值）永不未读', () => {
    expect(unread(summary('a', { product_state_changed_at: null }), {})).toBe(false);
    // 不可解析 = 没有这个键（同 `lib/kanban.ts::parseTime` 的口径），不猜成 0。
    expect(unread(summary('b', { product_state_changed_at: 'not-a-time' }), {})).toBe(false);
  });

  it('2. ack 缺失（没点过这张卡）= 未读', () => {
    expect(unread(summary('a', { product_state_changed_at: CHANGED }), {})).toBe(true);
  });

  it('3. ack 早于 changed_at = 未读（状态又跃迁了）', () => {
    expect(
      unread(summary('a', { product_state_changed_at: CHANGED }), {
        a: '2026-10-17T02:59:59Z',
      }),
    ).toBe(true);
  });

  it('4. ack 晚于/等于 changed_at = 已读', () => {
    expect(
      unread(summary('a', { product_state_changed_at: CHANGED }), {
        a: '2026-10-17T03:00:01Z',
      }),
    ).toBe(false);
    // 等于也算已读（Orca `acknowledgedAt < turnStartedAt` 的严格小于）。
    expect(
      unread(summary('a', { product_state_changed_at: CHANGED }), {
        a: CHANGED,
      }),
    ).toBe(false);
  });

  it('5. 消融对照：last_event_time 推进但 changed_at 不变 = 仍已读（比较键必须是后者）', () => {
    const acked = { a: '2026-10-17T03:00:05Z' };
    const before = summary('a', {
      product_state_changed_at: CHANGED,
      last_event_time: '2026-10-17T03:00:00Z',
    });
    const afterSameStateEvent = summary('a', {
      product_state_changed_at: CHANGED, // 同态事件不刷新它
      last_event_time: '2026-10-17T09:00:00Z', // agent 又说了好几句话
    });
    expect(unread(before, acked)).toBe(false);
    expect(unread(afterSameStateEvent, acked)).toBe(false);
  });

  it('ack 值损坏（非 ISO 字符串）按未读处理：宁可再提醒一次，不静默吞掉', () => {
    expect(unread(summary('a', { product_state_changed_at: CHANGED }), { a: 'garbage' })).toBe(
      true,
    );
  });
});

describe('#1306 ack 读写', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('键名与票面一致；写入后读回同一张表', () => {
    expect(KANBAN_ACK_KEY).toBe('ahi.kanban.ack.v1');
    const store = installStorageStub();
    writeAck({ a: '2026-10-17T03:00:00Z' });
    expect(store.get(KANBAN_ACK_KEY)).toBe('{"a":"2026-10-17T03:00:00Z"}');
    expect(readAck()).toEqual({ a: '2026-10-17T03:00:00Z' });
  });

  it('缺省（没写过）= 空表', () => {
    installStorageStub();
    expect(readAck()).toEqual({});
  });

  it('损坏 JSON / 非对象 / 非字符串值：类型守卫回空表或丢弃坏条目，不抛', () => {
    const store = installStorageStub();
    store.set(KANBAN_ACK_KEY, '{not json');
    expect(readAck()).toEqual({});
    store.set(KANBAN_ACK_KEY, '["a"]');
    expect(readAck()).toEqual({});
    store.set(KANBAN_ACK_KEY, 'null');
    expect(readAck()).toEqual({});
    store.set(KANBAN_ACK_KEY, '{"a":1,"b":"2026-10-17T03:00:00Z"}');
    expect(readAck()).toEqual({ b: '2026-10-17T03:00:00Z' });
  });

  it('localStorage 抛异常时读回空表、写不抛（隐私模式不崩看板）', () => {
    installStorageStub({
      getItem: () => {
        throw new Error('SecurityError: localStorage is disabled');
      },
      setItem: () => {
        throw new Error('SecurityError: localStorage is disabled');
      },
    });
    expect(readAck()).toEqual({});
    expect(() => writeAck({ a: 'x' })).not.toThrow();
    expect(() => markAcknowledged('a', {})).not.toThrow();
  });

  it('markAcknowledged 在旧表上追加一条并落盘，返回新表（不改入参）', () => {
    installStorageStub();
    const prev = { a: '2026-10-17T03:00:00Z' };
    const next = markAcknowledged('b', prev, new Date('2026-10-18T00:00:00Z'));
    expect(next).toEqual({ a: '2026-10-17T03:00:00Z', b: '2026-10-18T00:00:00.000Z' });
    expect(prev).toEqual({ a: '2026-10-17T03:00:00Z' });
    expect(readAck()).toEqual(next);
    // 刚点过的卡立刻算已读（与 changed_at 早于 now 的真值时序一致）。
    expect(unread(summary('b', { product_state_changed_at: CHANGED }), next)).toBe(false);
  });
});
