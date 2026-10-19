// @vitest-environment jsdom
/** #1371 审查 C-P2-2：loading 状态位的 hook 级回归锁。
 *
 * 已有的组件级用例（`SessionList.test.tsx` / `Conversation.test.tsx`）注入的是
 * **props**，锁的是渲染分支；真正有出错空间的是 hook 里的两个状态位：
 *
 *  - `sessionsLoading`：初值 true；**首次** settle（成功或失败）置 false，之后
 *    的刷新不再闪骨架（`sessionsLoadedRef` 守卫）；
 *  - `loadingHistory`：首帧带 localStorage 会话时初值 true；idle / live 迁移必须
 *    复位（否则骨架永不退场——审查 C-P2-1）。
 *
 * 另锁 `shouldShowHistorySkeleton` 的渲染期派生：idle→viewing / A→B 的那一帧
 * （effect 还没跑）也必须判为加载中，不能先画「暂无对话」或上一会话内容。
 */
import { act, useEffect } from 'react';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentEvent, ConversationState, SessionSummary } from '../types';
import { shouldShowHistorySkeleton, useSession } from './useSession';
import { SELECTED_SESSION_KEY } from '../lib/sessionRestore';

const SID = 's-loading';

// ── api mock：listSessions / getSessionEvents 都可换装可控 promise ───────────
const apiMock = vi.hoisted(() => ({
  listSessions: vi.fn(),
  getSessionEvents: vi.fn(),
}));

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    listSessions: (...args: unknown[]) => apiMock.listSessions(...args),
    getSessionEvents: (...args: unknown[]) => apiMock.getSessionEvents(...args),
    getSessionRecovery: vi.fn(async () => []),
    listSessionQueue: vi.fn(async () => ({ items: [], steers: [] })),
  };
});

const summary = (id: string): SessionSummary => ({
  session_id: id,
  event_count: 2,
  first_event_time: '2026-10-19T00:00:00+08:00',
  last_event_time: '2026-10-19T00:00:01+08:00',
  first_user_message: null,
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
});

/** 已收口的 durable 历史（不会有在途 run ⇒ 不触发接流）。 */
const settledHistory = (sid: string): AgentEvent[] => [
  { type: 'session/started', seq: 1, session_id: sid, run_id: 'r-1' } as AgentEvent,
  { type: 'run/completed', seq: 2, session_id: sid, run_id: 'r-1' } as AgentEvent,
];

/** 最小投影夹具：只锁骨架判据读的 session_id（事件/轮次不参与本判定）。 */
const convOf = (sid: string): ConversationState => ({ session_id: sid }) as ConversationState;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** 本文件用到的 hook 面（显式列字段：契约写在用法上，不靠 `ReturnType` 反射）。 */
interface HookView {
  sessions: SessionSummary[];
  sessionsLoading: boolean;
  sessionsError: string | null;
  refreshSessions: () => Promise<boolean>;
  selectedId: string | null;
  conversation: ConversationState | null;
  loadingHistory: boolean;
  historySkeleton: boolean;
  selectSession: (id: string | null) => void;
}

const captured = { hook: null as HookView | null };
/** 每次**渲染**的状态快照（在渲染期写入，不含 effect 之后的值）。
 *
 *  冷启动首帧的回归锁必须读这里：`loadingHistory` 的初值只活到第一次提交，
 *  effect 一落地就被 `switchingSession` 覆盖成同一个 true——只断言 effect 之后
 *  的值，把初值改回 false 也不会红（变异实测）。 */
const renders: Array<{ loadingHistory: boolean; historySkeleton: boolean }> = [];
let container: HTMLDivElement | null = null;
let root: Root | null = null;

function Harness() {
  const api = useSession();
  renders.push({
    loadingHistory: api.loadingHistory,
    historySkeleton: api.historySkeleton,
  });
  useEffect(() => {
    captured.hook = api;
  });
  return null;
}

async function mount(): Promise<void> {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(createElement(Harness));
  });
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  renders.length = 0;
  apiMock.listSessions.mockReset();
  apiMock.getSessionEvents.mockReset();
  apiMock.listSessions.mockResolvedValue([]);
  apiMock.getSessionEvents.mockResolvedValue(settledHistory(SID));
});

afterEach(async () => {
  if (root) await act(async () => root!.unmount());
  container?.remove();
  root = null;
  container = null;
  captured.hook = null;
});

describe('sessionsLoading — 骨架只在首次请求 settle 前挂载', () => {
  it('首次 resolve 前置 true；settle 后置 false；后续刷新期间保持 false', async () => {
    const first = deferred<SessionSummary[]>();
    apiMock.listSessions.mockImplementationOnce(() => first.promise);

    await mount();
    expect(captured.hook!.sessionsLoading).toBe(true);

    await act(async () => {
      first.resolve([summary('s-1')]);
    });
    expect(captured.hook!.sessionsLoading).toBe(false);
    expect(captured.hook!.sessions.map((s) => s.session_id)).toEqual(['s-1']);

    // 第二次刷新悬挂中：旧列表继续显示，骨架不得重挂（否则每次刷新都闪）。
    const second = deferred<SessionSummary[]>();
    apiMock.listSessions.mockImplementationOnce(() => second.promise);
    await act(async () => {
      void captured.hook!.refreshSessions();
    });
    expect(captured.hook!.sessionsLoading).toBe(false);
    expect(captured.hook!.sessions.map((s) => s.session_id)).toEqual(['s-1']);

    await act(async () => {
      second.resolve([summary('s-1'), summary('s-2')]);
    });
    expect(captured.hook!.sessionsLoading).toBe(false);
  });

  it('首次请求失败也算 settle：收起骨架，失败走 sessionsError', async () => {
    apiMock.listSessions.mockRejectedValueOnce(new Error('get sessions 502'));

    await mount();
    expect(captured.hook!.sessionsLoading).toBe(false);
    expect(captured.hook!.sessionsError).toContain('get sessions 502');
  });
});

describe('loadingHistory — 初值、复位与派生骨架', () => {
  it('localStorage 带会话：首帧 loadingHistory=true，历史落地后 false', async () => {
    window.localStorage.setItem(SELECTED_SESSION_KEY, SID);
    const events = deferred<AgentEvent[]>();
    apiMock.getSessionEvents.mockImplementation(() => events.promise);

    await mount();
    // 首帧（effect 落地前）就必须是 true：localStorage 恢复的会话在第一次绘制时
    // 历史还没开始读，靠 effect 补会先画一帧「暂无对话」。
    expect(renders[0]).toEqual({ loadingHistory: true, historySkeleton: true });
    expect(captured.hook!.loadingHistory).toBe(true);
    expect(captured.hook!.historySkeleton).toBe(true);

    await act(async () => {
      events.resolve(settledHistory(SID));
    });
    expect(captured.hook!.loadingHistory).toBe(false);
    expect(captured.hook!.historySkeleton).toBe(false);
  });

  it('历史未落地就回 idle：loadingHistory 必须复位（否则骨架永不退场）', async () => {
    window.localStorage.setItem(SELECTED_SESSION_KEY, SID);
    const events = deferred<AgentEvent[]>();
    apiMock.getSessionEvents.mockImplementation(() => events.promise);

    await mount();
    expect(captured.hook!.loadingHistory).toBe(true);

    await act(async () => {
      captured.hook!.selectSession(null);
    });
    expect(captured.hook!.selectedId).toBeNull();
    expect(captured.hook!.conversation).toBeNull();
    expect(captured.hook!.loadingHistory).toBe(false);
    expect(captured.hook!.historySkeleton).toBe(false);
  });

  it('空闲态点开会话：events 未返回时 historySkeleton=true（不闪空态）', async () => {
    await mount();
    expect(captured.hook!.selectedId).toBeNull();

    const events = deferred<AgentEvent[]>();
    apiMock.getSessionEvents.mockImplementation(() => events.promise);

    await act(async () => {
      captured.hook!.selectSession(SID);
    });
    expect(captured.hook!.conversation).toBeNull();
    expect(captured.hook!.historySkeleton).toBe(true);

    await act(async () => {
      events.resolve(settledHistory(SID));
    });
    expect(captured.hook!.historySkeleton).toBe(false);
  });
});

describe('shouldShowHistorySkeleton — 渲染期派生（effect 落地前的那一帧）', () => {
  it('idle→viewing：loadingHistory 还没置位也判加载中', () => {
    expect(
      shouldShowHistorySkeleton({ kind: 'viewing', sessionId: 'B' }, null, false),
    ).toBe(true);
  });

  it('A→B 切换：旧会话投影在场也判加载中（不先画 A 的内容）', () => {
    const convA = convOf('A');
    expect(
      shouldShowHistorySkeleton({ kind: 'viewing', sessionId: 'B' }, convA, false),
    ).toBe(true);
  });

  it('live→viewing 自迁移：投影已是该会话 ⇒ 不闪骨架（保留后台对账语义）', () => {
    const convA = convOf('A');
    expect(
      shouldShowHistorySkeleton({ kind: 'viewing', sessionId: 'A' }, convA, false),
    ).toBe(false);
    expect(shouldShowHistorySkeleton({ kind: 'viewing', sessionId: 'A' }, convA, true)).toBe(
      true,
    );
  });

  it('live / idle 不参与：流帧自己画内容，idle 无会话', () => {
    const convA = convOf('A');
    expect(shouldShowHistorySkeleton({ kind: 'live', sessionId: 'A' }, convA, false)).toBe(
      false,
    );
    expect(shouldShowHistorySkeleton({ kind: 'idle' }, null, false)).toBe(false);
  });
});
