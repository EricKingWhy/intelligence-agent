// @vitest-environment jsdom
/** #1264 Shape A：自动路径的「正在压缩」必须过**会话身份闸**。
 *
 *  审查发现（Standards 轴 P1）：`App.tsx` 把 `conversation.compaction_in_progress`
 *  直送 Composer。但 `useSession` 在会话→会话切换时**不**同步清空 `conversation`
 *  （`loadingHistory` 只置 true，待 `getSessionEvents` resolve 后才换引用，
 *  `useSession.ts` `shouldShowHistoryLoading` 就是按这个窗口判的）。窗口期内
 *  `conversation.session_id` 仍是**旧会话**：从「压缩在途的 s-A」切到 s-B 时，
 *  B 的面板上会挂着 A 的「正在压缩上下文…」，而 `compact.compacting` 是 B 自己的
 *  状态、清不掉它（跨会话残影 = 第二真相，不变量 #22）。
 *
 *  本文件渲染**真 App**（只 mock 数据 hook），驱动窗口态并断言状态行不出现；
 *  并给正证（身份相符时正常显形），防止「状态行根本不渲染」造成的假绿。
 *  mock 写法照抄 `App.ownershipChip.test.tsx`（同一个身份闸的另一处消费点）。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConversationState, SessionSummary } from './types';

const NOOP = () => {};

const H = vi.hoisted(() => ({
  selectedId: 's-B' as string | null,
  conversation: null as ConversationState | null,
}));

const summaryB: SessionSummary = {
  session_id: 's-B',
  event_count: 1,
  first_event_time: '2026-10-17T10:00:00+08:00',
  last_event_time: '2026-10-17T10:00:01+08:00',
  first_user_message: '任务 B',
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
      streaming: false,
      reconnecting: false,
      error: null,
      sessionsError: null,
      titlesById: {},
      sendFailure: null,
      runFailedBeforePlan: null,
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

vi.mock('./components/StepDetail', () => ({ StepDetail: () => null }));

import { initConversation } from './lib/projection';
import App from './App';

function conversationFor(sessionId: string, compacting: boolean): ConversationState {
  const conv = initConversation(sessionId);
  conv.compaction_in_progress = compacting;
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

/** 后端真有压缩在途时，Composer 状态行渲染 `data-testid="slash-compacting"`。 */
function compactingRow(): Element | null {
  return container!.querySelector('[data-testid="slash-compacting"]');
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

describe('#1264 自动路径「正在压缩」的会话身份闸', () => {
  it('切会话加载窗口：conversation 仍是旧会话（压缩在途）时，状态行不出现', async () => {
    H.selectedId = 's-B';
    // s-A 的 bracket 已开未合 —— 正是窗口态。
    H.conversation = conversationFor('s-A', true);
    await mount();

    expect(compactingRow()).toBeNull();
  });

  it('正证：conversation 身份相符且真有压缩在途时，状态行显形', async () => {
    H.selectedId = 's-B';
    H.conversation = conversationFor('s-B', true);
    await mount();

    expect(compactingRow()?.textContent).toContain('正在精简早期对话');
  });

  it('正证：身份相符但没有压缩在途时，状态行不出现（非推断）', async () => {
    H.selectedId = 's-B';
    H.conversation = conversationFor('s-B', false);
    await mount();

    expect(compactingRow()).toBeNull();
  });
});
