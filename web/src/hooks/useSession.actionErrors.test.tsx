// @vitest-environment jsdom
/** #1509（INT-1）：动作错误按身份归属——hook 级回归锁。
 *
 * 组件级（`App.errorBanner.test.tsx`）锁的是 App 挂载/门控；真正有出错空间的是
 * hook 里的错误槽位归属：
 *
 *  - `cancelItem` 非 404 失败 → `queueCancelError`（队列区动作位），**不再**写通用
 *    `error` 槽（原缺陷：错误打到顶部横幅，离触发它的队列条目很远）；
 *  - 404 = 幂等取消语义，保持静默（既有行为，回归钉）；
 *  - 旧尝试的迟到失败不得覆盖新尝试的结果（代际守卫）；
 *  - `resumePausedRun` 的 `user_input` 支（问答卡 / 计划卡 / 约束模态共用的作答
 *    通道）失败 → `answerFailure`（卡/模态各自的 alert 位），**不再**写通用 `error`
 *    （原缺陷：同一条错误既进卡片又进顶部横幅，双播报）；
 *  - 非 user_input 支（预算恢复等，无内联位）保持走通用 `error` 横幅通道。
 *
 * mock 写法照抄 `useSession.loading.test.tsx`：只 mock 数据层，真 hook 真投影。
 */
import { act, useEffect } from 'react';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentEvent, ConversationState, SessionSummary } from '../types';
import { NotFoundError, type ResumePausedRunTarget, type UserInputAnswerPayload } from '../lib/api';
import { useSession } from './useSession';

const SID = 's-action';

const apiMock = vi.hoisted(() => ({
  listSessions: vi.fn(),
  getSessionEvents: vi.fn(),
  cancelQueueItem: vi.fn(),
  resumeSession: vi.fn(),
}));

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    listSessions: (...args: unknown[]) => apiMock.listSessions(...args),
    getSessionEvents: (...args: unknown[]) => apiMock.getSessionEvents(...args),
    cancelQueueItem: (...args: unknown[]) => apiMock.cancelQueueItem(...args),
    resumeSession: (...args: unknown[]) => apiMock.resumeSession(...args),
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

const settledHistory = (sid: string): AgentEvent[] => [
  { type: 'session/started', seq: 1, session_id: sid, run_id: 'r-1' } as AgentEvent,
  { type: 'run/completed', seq: 2, session_id: sid, run_id: 'r-1' } as AgentEvent,
];

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
  error: string | null;
  /** #1509：取消排队失败的动作位槽（会话身份 + 文案）。 */
  queueCancelError: { sessionId: string; message: string } | null;
  /** #1509：作答（user_input 恢复支）失败的动作位槽（会话 + 请求身份 + 文案）。 */
  answerFailure: { sessionId: string; requestId: string; message: string } | null;
  cancelItem: (sessionId: string, itemId: string) => Promise<void>;
  selectSession: (id: string | null) => void;
  resumePausedRun: (
    sessionId: string,
    request:
      | ({ runId: string; expectedVersion: number } & ResumePausedRunTarget)
      | { runId: string; expectedVersion: number; kind: 'user_input'; inputRequest: UserInputAnswerPayload },
  ) => Promise<void>;
}

const captured = { hook: null as HookView | null };

function Harness() {
  const api = useSession();
  useEffect(() => {
    captured.hook = api;
  });
  return null;
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

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
  apiMock.listSessions.mockReset();
  apiMock.getSessionEvents.mockReset();
  apiMock.cancelQueueItem.mockReset();
  apiMock.resumeSession.mockReset();
  apiMock.listSessions.mockResolvedValue([summary(SID)]);
  apiMock.getSessionEvents.mockResolvedValue(settledHistory(SID));
  apiMock.cancelQueueItem.mockResolvedValue(undefined);
  apiMock.resumeSession.mockResolvedValue(new Response(null, { status: 200 }) as unknown as Response);
});

afterEach(async () => {
  if (root) await act(async () => root!.unmount());
  container?.remove();
  root = null;
  container = null;
  captured.hook = null;
});

describe('#1509 cancelItem — 取消排队失败归队列区动作位', () => {
  it('非 404 失败 → queueCancelError（带会话身份），不再写通用 error 槽', async () => {
    apiMock.cancelQueueItem.mockRejectedValueOnce(new Error('network 502'));
    await mount();
    await act(async () => {
      await captured.hook!.cancelItem(SID, 'q1');
    });
    expect(captured.hook!.queueCancelError).toEqual({
      sessionId: SID,
      message: expect.stringContaining('取消排队消息失败') as unknown as string,
    });
    expect(captured.hook!.queueCancelError!.message).toContain('network 502');
    // 原缺陷回归钉：这条失败不得再打到顶部横幅的通用槽。
    expect(captured.hook!.error).toBeNull();
  });

  it('404 = 幂等取消语义：静默，两个槽都不写', async () => {
    apiMock.cancelQueueItem.mockRejectedValueOnce(new NotFoundError('already consumed'));
    await mount();
    await act(async () => {
      await captured.hook!.cancelItem(SID, 'q1');
    });
    expect(captured.hook!.error).toBeNull();
    expect(captured.hook!.queueCancelError).toBeNull();
  });

  it('旧尝试的迟到失败不得覆盖新尝试的成功（代际守卫）', async () => {
    const a = deferred<void>();
    const b = deferred<void>();
    apiMock.cancelQueueItem
      .mockImplementationOnce(() => a.promise)
      .mockImplementationOnce(() => b.promise);
    await mount();
    let p1: Promise<void> = Promise.resolve();
    let p2: Promise<void> = Promise.resolve();
    await act(async () => {
      p1 = captured.hook!.cancelItem(SID, 'q1');
      p2 = captured.hook!.cancelItem(SID, 'q2');
    });
    await act(async () => {
      b.resolve(undefined);
      await p2;
    });
    await act(async () => {
      a.reject(new Error('late 502'));
      await p1.catch(() => undefined);
    });
    expect(captured.hook!.queueCancelError).toBeNull();
    expect(captured.hook!.error).toBeNull();
  });

  it('失败后再取消成功 → 清掉未处理的失败（成功后清除）', async () => {
    apiMock.cancelQueueItem.mockRejectedValueOnce(new Error('boom'));
    await mount();
    await act(async () => {
      await captured.hook!.cancelItem(SID, 'q1');
    });
    expect(captured.hook!.queueCancelError).not.toBeNull();
    await act(async () => {
      await captured.hook!.cancelItem(SID, 'q1');
    });
    expect(captured.hook!.queueCancelError).toBeNull();
  });
});

/** 先把会话选中并等历史落地：真实使用中作答失败时 `conversation` 已是该会话的
 *  投影（卡片/面板在场），`fail()` 里的 `selectSession` 重读不会被 `switchingSession`
 *  判成"切会话"而清掉错误横幅。不先选中，失败信息会被装载 effect 抹掉，测的就是
 *  假症状。 */
async function selectAndSettle(): Promise<void> {
  await act(async () => {
    captured.hook!.selectSession(SID);
  });
}

describe('#1509 resumePausedRun — 作答失败归卡/模态动作位', () => {
  const askRequest = {
    runId: 'r1',
    expectedVersion: 3,
    kind: 'user_input' as const,
    inputRequest: {
      request_id: 'ask-1',
      questions: [{ question: '新版本什么时候发布？', answer: { label: '今晚低峰期' } }],
    } as UserInputAnswerPayload,
  };

  it('user_input 支失败 → answerFailure（会话 + 请求身份），不再写通用 error 槽', async () => {
    apiMock.resumeSession.mockRejectedValueOnce(new Error('HTTP 502'));
    await mount();
    await selectAndSettle();
    await act(async () => {
      await captured.hook!.resumePausedRun(SID, askRequest);
    });
    expect(captured.hook!.answerFailure).toEqual({
      sessionId: SID,
      requestId: 'ask-1',
      message: expect.stringContaining('恢复失败') as unknown as string,
    });
    expect(captured.hook!.answerFailure!.message).toContain('HTTP 502');
    // 原缺陷回归钉：作答失败只到卡/模态位，不再同时打到顶部横幅（双播报）。
    expect(captured.hook!.error).toBeNull();
  });

  it('非 user_input 支（预算恢复，无内联位）保持走通用 error 横幅通道', async () => {
    apiMock.resumeSession.mockRejectedValueOnce(new Error('HTTP 409'));
    await mount();
    await selectAndSettle();
    await act(async () => {
      await captured.hook!.resumePausedRun(SID, {
        runId: 'r1',
        expectedVersion: 3,
        kind: 'tool',
        tool: 'bash',
        value: 10,
      });
    });
    expect(captured.hook!.error).toContain('恢复失败');
    expect(captured.hook!.answerFailure).toBeNull();
  });

  it('重试（再次进入恢复）清掉上一次的作答失败：槽里只留最新一次', async () => {
    apiMock.resumeSession
      .mockRejectedValueOnce(new Error('first 502'))
      .mockRejectedValueOnce(new Error('second 503'));
    await mount();
    await selectAndSettle();
    await act(async () => {
      await captured.hook!.resumePausedRun(SID, askRequest);
    });
    expect(captured.hook!.answerFailure!.message).toContain('first 502');
    await act(async () => {
      await captured.hook!.resumePausedRun(SID, askRequest);
    });
    expect(captured.hook!.answerFailure!.message).toContain('second 503');
    expect(captured.hook!.answerFailure!.message).not.toContain('first 502');
  });
});
