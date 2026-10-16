// @vitest-environment jsdom
/** #1204 App 层路由钉：`USER_INPUT_REQUESTED` 的两支**必须按 kind 分流挂载**——
 *  `kind=ask_user` → 对话流内嵌 `AskUserCard`；`kind=protected_fact_conflict`（及其它）
 *  → 原模态 `ConstraintResolutionDialog`。组件测试只钉各自渲染，管不到"App 挂哪一支"；
 *  本文件用真 App + 真 Conversation + 真投影，按事件 kind 断言挂的是哪一支、且互斥。
 *
 *  mock 写法照抄 App.test.tsx / App.stuckPauseRouting.test.tsx：只 mock 数据层。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { projectHistory } from './lib/projection';
import type { AgentEvent, ConversationState } from './types';

const H = vi.hoisted(() => ({
  conv: null as ConversationState | null,
  set: null as unknown as (c: ConversationState) => void,
}));

const NOOP = () => {};

vi.mock('./hooks/useSession', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useSession')>();
  const React = await import('react');
  return {
    ...actual,
    useSession: () => {
      const [conv] = React.useState(H.conv);
      return {
        sessions: [],
        selectedId: 's1',
        conversation: conv,
        loadingHistory: false,
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
        changePermission: NOOP,
        resumePausedRun: NOOP,
      };
    },
  };
});

vi.mock('./hooks/useProjects', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useProjects')>();
  return {
    ...actual,
    useProjects: () => ({
      projects: [], loadError: null, refresh: NOOP,
      actions: new Proxy({}, { get: () => NOOP }) as never,
    }),
  };
});

import App from './App';

const SID = 's1';
const T = '2026-10-16T00:00:00.000Z';

function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
  return { data: {}, seq: null, run_id: null, step_id: null, session_id: SID, time: T, ...partial } as AgentEvent;
}

/** 造一段"有一轮用户消息 + 一个待回答请求"的会话事件——Conversation 只有在
 *  `turns.length > 0` 时才走对话流渲染区（空态不渲染内嵌卡片）。 */
function events(kind: 'ask_user' | 'protected_fact_conflict'): AgentEvent[] {
  const requestData = kind === 'ask_user'
    ? {
        request_id: `${kind}-1`, kind, run_id: 'r1',
        questions: [
          {
            question: '新版本什么时候发布？', header: '发布决策', recommended: 0,
            options: [{ label: '今晚低峰期' }, { label: '明早 9 点' }],
          },
        ],
      }
    : {
        request_id: `${kind}-1`, kind, run_id: 'r1',
        fact_id: 'fact-1', old_value: 'Use Python', candidate: 'Use TypeScript',
        question: 'Choose how to resolve this conflict.',
        source_event_id: 'source-1', source_event_seq: 0,
        choices: [
          { id: 'replace_persistently', label: 'Replace persistently' },
          { id: 'current_task_only', label: 'This task only' },
          { id: 'keep_existing', label: 'Keep existing' },
          { id: 'custom', label: 'Custom reply' },
        ],
      };
  return [
    ev({ type: 'session/started', seq: 1, run_id: 'r1' }),
    ev({ type: 'run/started', seq: 2, run_id: 'r1' }),
    ev({ type: 'user/message', seq: 3, run_id: 'r1', step_id: 1, data: { content: '走起', step: 1 } }),
    ev({ type: 'user/input-requested', seq: 4, run_id: 'r1', data: requestData }),
    ev({
      type: 'run/paused', seq: 5, run_id: 'r1',
      data: { reason: 'user_input', input_request_id: `${kind}-1` },
    }),
  ];
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function mount(kind: 'ask_user' | 'protected_fact_conflict'): Promise<HTMLDivElement> {
  H.conv = projectHistory(SID, events(kind));
  const el = document.createElement('div');
  document.body.appendChild(el);
  container = el;
  root = createRoot(el);
  await act(async () => {
    root!.render(<App />);
  });
  return el;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: NOOP, removeEventListener: NOOP,
    addListener: NOOP, removeListener: NOOP, dispatchEvent: () => false,
  }));
  vi.stubGlobal('ResizeObserver', class { observe = NOOP; unobserve = NOOP; disconnect = NOOP; });
  Element.prototype.scrollIntoView = NOOP;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes('/api/capabilities') ? [] : {};
    return new Response(JSON.stringify(body), {
      status: 200, headers: { 'content-type': 'application/json' },
    });
  }));
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  if (container) container.remove();
  document.body.innerHTML = '';
  root = null;
  container = null;
  H.conv = null;
  vi.unstubAllGlobals();
});

describe('#1204 — App 按 kind 分流挂载提问 UI', () => {
  it('kind=ask_user ⇒ 对话流内嵌 AskUserCard，不挂模态约束弹窗', async () => {
    const el = await mount('ask_user');
    expect(el.querySelector('.ask-user-card')).not.toBeNull();
    expect(document.querySelector('.constraint-input-dialog')).toBeNull();
  });

  it('kind=protected_fact_conflict ⇒ 原模态约束弹窗，不挂 AskUserCard', async () => {
    await mount('protected_fact_conflict');
    expect(document.querySelector('.constraint-input-dialog')).not.toBeNull();
    expect(document.querySelector('.ask-user-card')).toBeNull();
  });
});
