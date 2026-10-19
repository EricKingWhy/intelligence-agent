// @vitest-environment jsdom
/** #1028 P2 修回：归属 chip 的 cwd 必须过**身份闸**。
 *
 *  审查发现（双轴同一定位）：`useSession` 在会话→会话切换时**不**同步清空
 *  `conversation`（`loadingHistory` 只置 true，待 `getSessionEvents` resolve 后才
 *  `setConversation`）。窗口期内 `conversation.session_id` 仍是**旧会话**，若不校验
 *  `conversation.session_id === selectedId`，chip 会把「新会话的项目名」与「旧会话的
 *  cwd」拼在一起（跨会话残影 = 第二真相，不变量 #22）；加载失败时该错配会**持续**。
 *
 *  本文件渲染**真 App**（唯一 mock 数据 hook），驱动「选中 s-B、但 conversation 仍是
 *  s-A」的窗口态，断言 chip 不出现 s-A 的旧 cwd；并给一条**正证**（身份相符时 cwd
 *  正常显形），防止「chip 根本不渲染」造成的假绿。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConversationState, SessionSummary } from './types';

const NOOP = () => {};

// ── 可控数据层：selectedId 与 conversation 各由用例设置 ──
const H = vi.hoisted(() => ({
  selectedId: 's-B' as string | null,
  conversation: null as ConversationState | null,
}));

const summaryB: SessionSummary = {
  session_id: 's-B',
  event_count: 3,
  first_event_time: '2026-10-13T10:00:00+08:00',
  last_event_time: '2026-10-13T10:01:00+08:00',
  first_user_message: '任务 B',
  trace_id: null,
  trace_url: null,
  workspace: { id: 'p1', title: '项目 X' },
  archived: false,
  corrupted: false,
  product_state: '',
  product_state_changed_at: null,
  attention_kind: null,
  attention_summary: null,
  last_user_message: null,
  last_agent_message: null,
};

vi.mock('./hooks/useSession', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useSession')>();
  return {
    ...actual,
    useSession: () => ({
      sessions: [summaryB],
      selectedId: H.selectedId,
      conversation: H.conversation,
      loadingHistory: true,
      historySkeleton: true,
      streaming: false,
      reconnecting: false,
      error: null,
      sessionsError: null,
      titlesById: {},
      recoverState: {
        status: 'idle', message: null, conflict: false, repaired: 0,
        terminalRepaired: false, stillUnterminated: false, stillDangling: false,
        pendingDecisions: null,
      },
      selectSession: NOOP,
      submitTask: NOOP,
      sendMessage: NOOP,
      cancelStream: NOOP,
      removeSession: NOOP,
      setArchived: NOOP,
      recover: NOOP,
      submitDecisions: NOOP,
      refreshSessions: NOOP,
      changeModel: NOOP,
      fork: NOOP,
      sendSteer: NOOP,
      flushQueue: NOOP,
      cancelItem: NOOP,
    }),
  };
});

vi.mock('./hooks/useProjects', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useProjects')>();
  return {
    ...actual,
    useProjects: () => ({
      projects: [],
      loadError: null,
      refresh: NOOP,
      actions: new Proxy({}, { get: () => NOOP }) as never,
    }),
  };
});

// 观测点隔离：StepDetail 时间线每帧对全部事件各调一次 summarizeEvent（与本文件无关），
// 换成空壳以收敛渲染面（同 App.test.tsx 手法）。
vi.mock('./components/StepDetail', () => ({ StepDetail: () => null }));

import { initConversation } from './lib/projection';
import App from './App';

function conversationFor(sessionId: string, cwd: string | null): ConversationState {
  const conv = initConversation(sessionId);
  conv.session_cwd = cwd;
  return conv;
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function mount(): Promise<void> {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(<App />);
  });
}

function chipText(): string | null {
  return container!.querySelector('.composer-owner-chip')?.textContent ?? null;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: NOOP, removeEventListener: NOOP,
    addListener: NOOP, removeListener: NOOP, dispatchEvent: () => false,
  }));
  vi.stubGlobal('ResizeObserver', class {
    observe = NOOP; unobserve = NOOP; disconnect = NOOP;
  });
  Element.prototype.scrollIntoView = NOOP;
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })),
  );
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  if (container) container.remove();
  root = null;
  container = null;
  H.selectedId = 's-B';
  H.conversation = null;
  vi.unstubAllGlobals();
});

const OLD_CWD = '/old/session-A';

describe('#1028 P2 归属 chip 的 cwd 身份闸', () => {
  it('切会话加载窗口：conversation 仍是旧会话时，chip 不出现旧 cwd', async () => {
    H.selectedId = 's-B';
    // conversation 还是 s-A 的（events 尚未 resolve）——正是窗口态。
    H.conversation = conversationFor('s-A', OLD_CWD);
    await mount();

    const text = chipText();
    // chip 仍在（有新会话可显形），但**不得**携带旧会话的 cwd。
    expect(text).not.toBeNull();
    expect(text).toContain('项目「项目 X」');
    expect(text).not.toContain(OLD_CWD);
  });

  it('正证：conversation 身份相符时，chip 正常显形该会话 cwd', async () => {
    H.selectedId = 's-B';
    H.conversation = conversationFor('s-B', '/repos/beta');
    await mount();

    const text = chipText();
    expect(text).toContain('项目「项目 X」');
    expect(text).toContain('/repos/beta');
  });

  it('P3-2/P4 空白新建态：无选中会话、无项目语境时不渲染 chip', async () => {
    H.selectedId = null;
    H.conversation = null;
    await mount();

    expect(chipText()).toBeNull();
  });
});
