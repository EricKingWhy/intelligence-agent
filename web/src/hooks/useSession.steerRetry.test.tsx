// @vitest-environment jsdom
/** #1324：steer 打空（409 `SteerTargetNotFound`）→ 改投 queue → 受理。
 *
 *  ADR-0030 §4.6/§13 决定了这个 409 是**设计内**的：空转会话上的 steer 没有可
 *  注入的目标，后端明说改用 queue。`sendFollowUp` 的 `retry-queue` 支因此去掉
 *  `queue_id` 重投一次。票面 AC2 选 B（显式化重试），AC3 要求这条路径：
 *
 *   1. 重投成功后**不留任何错误态**（error / recoverState 都不许亮）；
 *   2. 重试**不静默**——console 有一条可检索的归因记录（AC2(b)）；
 *   3. 用户可见层不出现误导性文案（AC2(c)，由 1 覆盖）。
 *
 *  手法同 `useSession.followUpRecovery.test.tsx`：只 mock 传输层，hook 本体 /
 *  分派表全走真实代码路径。
 */

import { act, createElement, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentEvent } from '../types';

const apiState = vi.hoisted(() => ({ events: [] as AgentEvent[] }));

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    listSessions: vi.fn(async () => []),
    getSessionEvents: vi.fn(async () => apiState.events),
    listSessionQueue: vi.fn(async () => ({ items: [], steers: [] })),
    sendMessage: vi.fn(),
    recoverSession: vi.fn(),
  };
});

import { sendMessage as apiSendMessage } from '../lib/api';
import { useSession } from './useSession';

const SID = 's-1324';
const STEER_MARKER = "steer requires an active run; use mode='queue' to enqueue";

const idleHistory = (sid: string): AgentEvent[] =>
  [
    { type: 'session/started', seq: 1, session_id: sid, data: {}, run_id: null, step_id: null },
    { type: 'run/completed', seq: 2, session_id: sid, data: {}, run_id: 'r-1', step_id: null },
  ] as AgentEvent[];

const steerTargetMissing409 = (): Response =>
  new Response(JSON.stringify({ detail: STEER_MARKER }), {
    status: 409,
    headers: { 'content-type': 'application/json' },
  });

const acceptedReceipt = (): Response =>
  new Response(JSON.stringify({ status: 'launched' }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });

type SessionApi = ReturnType<typeof useSession>;
const captured = { hook: null as SessionApi | null };
let container: HTMLDivElement | null = null;
let root: Root | null = null;

function Harness() {
  const api = useSession();
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

async function enterViewing(sid = SID): Promise<void> {
  await act(async () => {
    captured.hook!.selectSession(sid);
  });
  await act(async () => {});
  expect(captured.hook!.conversation?.session_id).toBe(sid);
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  apiState.events = idleHistory(SID);
  vi.mocked(apiSendMessage).mockReset();
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  container?.remove();
  root = null;
  container = null;
  captured.hook = null;
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe('#1324 steer 打空 → 改投 queue', () => {
  it('重投成功后不留任何错误态，且重试有显式 console 归因', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.mocked(apiSendMessage)
      .mockResolvedValueOnce(steerTargetMissing409())
      .mockResolvedValueOnce(acceptedReceipt());

    await mount();
    await enterViewing(SID);

    let accepted: boolean | undefined;
    await act(async () => {
      accepted = await captured.hook!.sendSteer(SID, '补一句引导');
    });

    expect(accepted).toBe(true);
    // ① 不留错误态：横幅与待裁决态都不许被这次设计内重试点亮。
    expect(captured.hook!.error).toBeNull();
    expect(captured.hook!.recoverState.conflict).toBe(false);
    expect(captured.hook!.recoverState.pendingDecisions).toBeNull();

    // ② 两次投递：第一次 steer，第二次去掉 queue_id、改投 queue（递归恒为 1）。
    expect(vi.mocked(apiSendMessage)).toHaveBeenCalledTimes(2);
    const secondPayload = vi.mocked(apiSendMessage).mock.calls[1][1];
    expect(secondPayload.mode).toBe('queue');
    expect(secondPayload.content).toBe('补一句引导');

    // ③ 不静默：一条可检索的归因记录，带会话与后端 detail。
    expect(warn).toHaveBeenCalledTimes(1);
    const line = String(warn.mock.calls[0][0]);
    expect(line).toContain('SteerTargetNotFound');
    expect(line).toContain(`session=${SID}`);
    expect(line).toContain(STEER_MARKER);
  });
});
