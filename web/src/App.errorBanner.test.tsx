// @vitest-environment jsdom
/** #1509/#1520 App 层回归钉：通用错误横幅的可感知/可关闭/身份化关闭，与
 *  作答失败、取消排队失败的动作位挂载。
 *
 *  hook 的槽位归属在 `useSession.actionErrors.test.tsx` 锁；本文件用真 App + 真投影
 *  锁 App 侧的接线：
 *  - 横幅 `role="alert"`（#1520 A11Y-5：仅由横幅承载的错误可被辅助技术感知）；
 *  - 关闭按钮在场，点击后横幅卸载、焦点按归宿链回到触发控件、不落 body；
 *  - 关闭只影响同错误身份（实例序号）：新实例重新出现（#1520 AC3）；
 *  - 作答失败（`answerFailure`）→ 只挂对应卡片/模态的 alert 位，顶部横幅不重复
 *    （#1509 AC1/AC3：与既有内联 alert 配合只播报一次）；请求身份不匹配不挂；
 *  - 取消排队失败（`queueCancelError`）→ 挂 Composer 队列区（动作位），会话身份
 *    不匹配或队列已空不挂，顶部横幅不重复。
 *
 *  mock 写法照抄 App.askUserRouting.test.tsx：只 mock 数据层（useSession 的
 *  错误/身份槽由 H 显式供数）。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { projectHistory } from './lib/projection';
import type { AgentEvent, ConversationState } from './types';

const H = vi.hoisted(() => ({
  conv: null as ConversationState | null,
  /** 触发一次 App 重渲染（H 上的槽位是渲染期读取的普通字段，不走 useState）。 */
  rerender: null as unknown as () => void,
  error: null as string | null,
  errorSeq: 0,
  answerFailure: null as { sessionId: string; requestId: string; message: string } | null,
  queueCancelError: null as { sessionId: string; message: string } | null,
}));

const NOOP = () => {};

vi.mock('./hooks/useSession', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useSession')>();
  const React = await import('react');
  return {
    ...actual,
    useSession: () => {
      const [conv] = React.useState(H.conv);
      const [, force] = React.useState(0);
      H.rerender = () => force((n) => n + 1);
      return {
        sessions: [],
        selectedId: 's1',
        conversation: conv,
        loadingHistory: false,
        historySkeleton: false,
        streaming: false,
        reconnecting: false,
        error: H.error,
        errorSeq: H.errorSeq,
        answerFailure: H.answerFailure,
        queueCancelError: H.queueCancelError,
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

/** 已收口的最小会话（一轮对话 + 完成）——横幅/队列用例的基线投影。 */
function settledConv(): ConversationState {
  return projectHistory(SID, [
    ev({ type: 'session/started', seq: 1, run_id: 'r1' }),
    ev({ type: 'run/started', seq: 2, run_id: 'r1' }),
    ev({ type: 'user/message', seq: 3, run_id: 'r1', step_id: 1, data: { content: '走起', step: 1 } }),
    ev({ type: 'run/completed', seq: 4, run_id: 'r1' }),
  ]);
}

/** 挂起待回答的 ask_user 会话（卡片挂载条件：user/input-requested + run/paused 对齐）。 */
function askUserConv(): ConversationState {
  return projectHistory(SID, [
    ev({ type: 'session/started', seq: 1, run_id: 'r1' }),
    ev({ type: 'run/started', seq: 2, run_id: 'r1' }),
    ev({ type: 'user/message', seq: 3, run_id: 'r1', step_id: 1, data: { content: '走起', step: 1 } }),
    ev({
      type: 'user/input-requested', seq: 4, run_id: 'r1',
      data: {
        request_id: 'ask_user-1', kind: 'ask_user', run_id: 'r1',
        questions: [
          {
            question: '新版本什么时候发布？', header: '发布决策', recommended: 0,
            options: [{ label: '今晚低峰期' }, { label: '明早 9 点' }],
          },
        ],
      },
    }),
    ev({
      type: 'run/paused', seq: 5, run_id: 'r1',
      data: { reason: 'user_input', input_request_id: 'ask_user-1' },
    }),
  ]);
}

/** 挂起待裁决的计划审批会话（#1356 同一条恢复通道的第三支）。 */
function planApprovalConv(): ConversationState {
  return projectHistory(SID, [
    ev({ type: 'session/started', seq: 1, run_id: 'r1' }),
    ev({ type: 'run/started', seq: 2, run_id: 'r1' }),
    ev({ type: 'user/message', seq: 3, run_id: 'r1', step_id: 1, data: { content: '走起', step: 1 } }),
    ev({
      type: 'user/input-requested', seq: 4, run_id: 'r1',
      data: {
        request_id: 'plan-1', kind: 'plan_approval', run_id: 'r1',
        plan_markdown: '# 计划\n\n## 目标\n把 A 改成 B。',
        touched_files: ['src/a.py'],
        artifact_id: 'art-1',
        plan_sha256: 'deadbeef',
      },
    }),
    ev({
      type: 'run/paused', seq: 5, run_id: 'r1',
      data: { reason: 'user_input', input_request_id: 'plan-1' },
    }),
  ]);
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function mount(): Promise<HTMLDivElement> {
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
  H.conv = settledConv();
  H.error = null;
  H.errorSeq = 0;
  H.answerFailure = null;
  H.queueCancelError = null;
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  if (container) container.remove();
  document.body.innerHTML = '';
  root = null;
  container = null;
  H.conv = null;
  H.error = null;
  H.errorSeq = 0;
  H.answerFailure = null;
  H.queueCancelError = null;
  vi.unstubAllGlobals();
});

describe('#1520 — 通用错误横幅可感知、可关闭、身份化关闭', () => {
  it('横幅带 role=alert，且带关闭按钮（aria-label）', async () => {
    H.error = '连接中断（exhausted）：重试 3 次未成功';
    H.errorSeq = 1;
    await mount();
    const banner = document.querySelector('.app-error');
    expect(banner).not.toBeNull();
    expect(banner!.getAttribute('role')).toBe('alert');
    const close = banner!.querySelector('button[aria-label="关闭提示"]');
    expect(close).not.toBeNull();
  });

  it('点击关闭：横幅卸载，焦点回到触发控件，任何情况下不落 body', async () => {
    await mount();
    // 先把焦点放在一个稳定控件上（工作区 tab），模拟"错误发生时用户所在处"。
    const trigger = (document.querySelector('[role="tab"]') ?? document.querySelector('button')) as HTMLElement;
    trigger.focus();
    H.error = '连接中断（exhausted）：重试 3 次未成功';
    H.errorSeq = 1;
    await act(async () => { H.rerender(); });
    const close = document.querySelector('.app-error button[aria-label="关闭提示"]') as HTMLButtonElement;
    expect(close).not.toBeNull();
    await act(async () => { close.click(); });
    expect(document.querySelector('.app-error')).toBeNull();
    expect(document.activeElement).not.toBe(document.body);
    expect(document.activeElement).toBe(trigger);
  });

  it('关闭只影响同错误身份：同实例保持隐藏，新实例（序号递增）重新出现', async () => {
    H.error = '连接中断（exhausted）：重试 3 次未成功';
    H.errorSeq = 1;
    await mount();
    const close = document.querySelector('.app-error button[aria-label="关闭提示"]') as HTMLButtonElement;
    await act(async () => { close.click(); });
    expect(document.querySelector('.app-error')).toBeNull();
    // 同实例的重渲染不复活横幅。
    await act(async () => { H.rerender(); });
    expect(document.querySelector('.app-error')).toBeNull();
    // 新的错误实例（即使文案相同）必须重新出现——关闭不等于错误已解决。
    H.errorSeq = 2;
    await act(async () => { H.rerender(); });
    expect(document.querySelector('.app-error')).not.toBeNull();
  });
});

describe('#1509 — 作答失败只挂对应动作位，横幅不重复播报', () => {
  it('ask_user 作答失败 → 卡片 alert 位有文案，顶部横幅不再显示同一条', async () => {
    H.conv = askUserConv();
    H.error = null;
    H.answerFailure = { sessionId: SID, requestId: 'ask_user-1', message: '恢复失败：HTTP 502' };
    await mount();
    const cardError = document.querySelector('.ask-user-card .ask-user-error');
    expect(cardError).not.toBeNull();
    expect(cardError!.textContent).toContain('恢复失败：HTTP 502');
    // 原缺陷：同一条错误既进卡片又进顶部横幅（双播报）。
    expect(document.querySelector('.app-error')).toBeNull();
  });

  it('ask_user 请求身份不匹配（另一张卡的失败）→ 卡片不挂旧错误', async () => {
    H.conv = askUserConv();
    H.answerFailure = { sessionId: SID, requestId: 'other-request', message: '恢复失败：HTTP 502' };
    await mount();
    expect(document.querySelector('.ask-user-card .ask-user-error')).toBeNull();
  });

  it('plan_approval 裁决失败 → 计划卡 alert 位有文案，顶部横幅不重复', async () => {
    H.conv = planApprovalConv();
    H.answerFailure = { sessionId: SID, requestId: 'plan-1', message: '恢复失败：HTTP 500' };
    await mount();
    const cardError = document.querySelector('.plan-approval-card .plan-approval-error');
    expect(cardError).not.toBeNull();
    expect(cardError!.textContent).toContain('恢复失败：HTTP 500');
    expect(document.querySelector('.app-error')).toBeNull();
  });
});

describe('#1509 — 取消排队失败只挂队列区动作位', () => {
  it('同会话失败 + 队列非空 → Composer 队列区渲染 role=alert 通知，横幅不重复', async () => {
    H.conv = settledConv();
    H.conv.undelivered = [
      { kind: 'queue', id: 'q1', content: '稍后发这条', seq: 5, created_at: T },
    ];
    H.queueCancelError = { sessionId: SID, message: '取消排队消息失败：network 502' };
    await mount();
    const notice = document.querySelector('.composer-wrap .approval-error');
    expect(notice).not.toBeNull();
    expect(notice!.getAttribute('role')).toBe('alert');
    expect(notice!.textContent).toContain('取消排队消息失败：network 502');
    // 原缺陷回归钉：这条失败不得再打到顶部横幅。
    expect(document.querySelector('.app-error')).toBeNull();
  });

  it('会话身份不匹配（另一会话的失败）→ 当前会话队列区不挂', async () => {
    H.conv = settledConv();
    H.conv.undelivered = [
      { kind: 'queue', id: 'q1', content: '稍后发这条', seq: 5, created_at: T },
    ];
    H.queueCancelError = { sessionId: 's2', message: '取消排队消息失败：network 502' };
    await mount();
    expect(document.querySelector('.composer-wrap .approval-error')).toBeNull();
  });

  it('队列已空（动作位不在场）→ 不渲染孤儿通知', async () => {
    H.conv = settledConv();
    H.queueCancelError = { sessionId: SID, message: '取消排队消息失败：network 502' };
    await mount();
    expect(document.querySelector('.composer-wrap .approval-error')).toBeNull();
  });
});
