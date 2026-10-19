// @vitest-environment jsdom
/** #1367 数据层契约：`sendFailure` 的 `retryable` / `onRetry` 必须按**能否证明
 *  未受理**分流（审查 P1-1 / C-P2-1 / P2-5）。
 *
 *  - 短窗 4xx、或基线读不到的降级路径 POST 失败 ⇒ 可重试（带动作）；
 *  - 认领超时、认领成功后的迟到失败、续聊窗内失败 ⇒ 服务端可能已受理 ⇒ 只呈现。
 *
 *  手法同 `useSession.detachedFailure.test.tsx`：只 mock 传输层。
 */

import { act, createElement, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentEvent } from '../types';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    listSessions: vi.fn(async () => []),
    getSessionEvents: vi.fn(async () => []),
    listSessionQueue: vi.fn(async () => ({ items: [], steers: [] })),
    sendMessage: vi.fn(),
    startSession: vi.fn(),
  };
});

// ⚠ `sessionIdBaseline` / `discoverNewSessionId` 在 `lib/wsStream`，**不在** `lib/api`。
vi.mock('../lib/wsStream', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/wsStream')>();
  return {
    ...actual,
    sessionIdBaseline: vi.fn(async () => new Set<string>()),
    discoverNewSessionId: vi.fn(),
  };
});

import { getSessionEvents, sendMessage as apiSendMessage, startSession } from '../lib/api';
import { SessionClaimTimeoutError, discoverNewSessionId, sessionIdBaseline } from '../lib/wsStream';
import { EARLY_RESPONSE_WINDOW_MS, useSession, type SessionApi } from './useSession';

const SID = 's-1367';

const idleHistory = (sid: string): AgentEvent[] =>
  [
    { type: 'session/started', seq: 1, session_id: sid, data: {}, run_id: null, step_id: null },
    { type: 'run/completed', seq: 2, session_id: sid, data: {}, run_id: 'r-1', step_id: null },
  ] as AgentEvent[];

const error422 = (): Response =>
  new Response(JSON.stringify({ detail: '模型不可用' }), {
    status: 422, headers: { 'content-type': 'application/json' },
  });

const captured = { hook: null as SessionApi | null };
let container: HTMLDivElement | null = null;
let root: Root | null = null;

function Harness() {
  const api = useSession();
  useEffect(() => { captured.hook = api; });
  return null;
}

async function mount(): Promise<void> {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => { root!.render(createElement(Harness)); });
  await act(async () => {});
  if (!captured.hook) throw new Error('harness 未捕获到 hook');
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  vi.useFakeTimers();
  vi.mocked(getSessionEvents).mockReset();
  vi.mocked(getSessionEvents).mockResolvedValue(idleHistory(SID));
  vi.mocked(apiSendMessage).mockReset();
  vi.mocked(startSession).mockReset();
  vi.mocked(sessionIdBaseline).mockReset();
  vi.mocked(sessionIdBaseline).mockResolvedValue(new Set<string>());
  vi.mocked(discoverNewSessionId).mockReset();
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  container?.remove();
  container = null;
  root = null;
  captured.hook = null;
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('#1367 P1-1 — 能证明未受理的失败才给重试动作', () => {
  it('短窗 422（POST 明确拒绝）⇒ retryable:true，且原样携带调用方的 onRetry', async () => {
    vi.mocked(startSession).mockResolvedValue(error422() as never);
    const retry = vi.fn(async () => true);
    await mount();

    let result: string | null = null;
    await act(async () => {
      result = await captured.hook!.submitTask({ task: '会撞 422 的任务' }, { onRetry: retry });
    });

    expect(result).toContain('提交失败');
    expect(captured.hook!.sendFailure?.retryable).toBe(true);
    expect(captured.hook!.sendFailure?.onRetry).toBe(retry);
  });

  it('调用方没给动作 ⇒ 不给按钮（续聊等入口的失败只呈现）', async () => {
    vi.mocked(startSession).mockResolvedValue(error422() as never);
    await mount();

    await act(async () => {
      await captured.hook!.submitTask({ task: '无动作的提交' });
    });

    expect(captured.hook!.sendFailure?.retryable).toBe(true);
    expect(captured.hook!.sendFailure?.onRetry).toBeUndefined();
  });

  it('降级路径（会话列表读不到）的 POST 失败 ⇒ retryable:true + 动作保留', async () => {
    vi.mocked(sessionIdBaseline).mockResolvedValue(null);
    vi.mocked(startSession).mockRejectedValue(new Error('网络中断：POST /api/sessions'));
    const retry = vi.fn(async () => true);
    await mount();

    let result: string | null = null;
    await act(async () => {
      result = await captured.hook!.submitTask({ task: '降级态也要能重试' }, { onRetry: retry });
    });

    expect(result).toContain('网络中断');
    expect(captured.hook!.sendFailure?.retryable).toBe(true);
    expect(captured.hook!.sendFailure?.onRetry).toBe(retry);
  });
});

describe('#1367 P1-1 — 结局不明的失败一律不可重试', () => {
  it('认领超时 ⇒ retryable:false，且不携带动作', async () => {
    vi.mocked(startSession).mockReturnValue(new Promise<Response>(() => {}) as never);
    vi.mocked(discoverNewSessionId).mockRejectedValue(new SessionClaimTimeoutError());
    const retry = vi.fn(async () => true);
    await mount();

    await act(async () => {
      const p = captured.hook!.submitTask({ task: '认领超时的提交' }, { onRetry: retry });
      await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
      await p;
    });

    expect(captured.hook!.sendFailure?.retryable).toBe(false);
    expect(captured.hook!.sendFailure?.onRetry).toBeUndefined();
    expect(captured.hook!.sendFailure?.message).toContain('可能已成功');
  });

  it('认领成功后 POST 迟到失败 ⇒ retryable:false（重发会造出第二个会话）', async () => {
    let rejectPost!: (e: Error) => void;
    vi.mocked(startSession).mockReturnValue(
      new Promise<Response>((_r, reject) => { rejectPost = reject; }) as never,
    );
    vi.mocked(discoverNewSessionId).mockResolvedValue('s-new');
    await mount();

    await act(async () => {
      const p = captured.hook!.submitTask({ task: '认领后断网' }, { onRetry: () => {} });
      await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
      await p;
    });
    expect(captured.hook!.sendFailure).toBeNull();

    await act(async () => {
      rejectPost(new Error('网络中断'));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(captured.hook!.sendFailure?.retryable).toBe(false);
    expect(captured.hook!.sendFailure?.onRetry).toBeUndefined();
    expect(captured.hook!.failedDraft?.text).toBe('认领后断网');
  });
});

describe('#1367 C-P2-1 — 续聊窗内失败不给重试', () => {
  it('窗内 POST reject ⇒ retryable:false（无法证明消息未被受理）', async () => {
    vi.mocked(apiSendMessage).mockRejectedValue(new Error('network down'));
    await mount();
    await act(async () => { captured.hook!.selectSession(SID); });
    await act(async () => {});

    let accepted: boolean | null = null;
    await act(async () => {
      accepted = await captured.hook!.sendMessage(SID, '续聊失败');
    });

    expect(accepted).toBe(false);
    expect(captured.hook!.sendFailure?.retryable).toBe(false);
    expect(captured.hook!.sendFailure?.onRetry).toBeUndefined();
    // 窗内失败由 Composer 自己保留原文（返回值 false），不走 failedDraft 回填。
    expect(captured.hook!.failedDraft).toBeNull();
    expect(captured.hook!.error).toContain('续聊失败');
  });
});
