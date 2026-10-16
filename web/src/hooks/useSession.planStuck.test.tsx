// @vitest-environment jsdom
/** #1193（缺陷二前端半）：run 致命异常且计划从未生成——前端停止「连接中断」
 * 静默重试、亮出带「退出 plan 模式」动作的横幅，退出走**显式**操作。
 *
 * ── 缺陷 ─────────────────────────────────────────────────────────────────
 * run 启动期致命异常（plan 未生成）时后端刻意不回滚 mode（fail-closed）——
 * 会话停在 plan 档（只读、无计划）。修复前后端不推任何明确信号，前端只看到
 * 「连接中断（stream ended unexpectedly）：重试 3 次未成功」——重试永远不会
 * 成功（每轮新 run 都派生 PLAN、写工具全被拦），用户也没有恢复入口。
 *
 * ── 修复契约（票面 schema）───────────────────────────────────────────────
 * 后端异常臂推 `error` 事件（stream-only）：`data.code=run-failed-before-plan`
 * + 可读 message + `hint`（可手动退出 plan 模式）。前端收到即：
 *   1. 当**终态**收口（terminalSeen）⇒ 后端随后收流走干净迁移，不进重连链；
 *   2. 横幅 `runFailedBeforePlan`（可读原因 + hint）+ 退出动作；
 *   3. 退出 = `POST /workflow-mode {mode:'normal'}`（显式、幂等）→ 重读事件
 *      重投影（档位真相在事件流，不用回执写本地状态）→ 清横幅。
 *
 * ── 手法 ─────────────────────────────────────────────────────────────────
 * 与 useSession.externalRun.test.tsx 同款夹具：mock api 层与 wsStream 层
 * （捕获接流、帧时机归测试驱动）；useSession 本体、consumeSSE、投影、重连
 * 状态机全走真实代码路径。重连链的判别断言 = 收流后 wsMock.calls 不再增长
 * （修复前这里会多出重连尝试）。
 */

import { act, useEffect } from 'react';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentEvent } from '../types';
import { EXTERNAL_RUN_POLL_INTERVAL_MS, useSession } from './useSession';

// ── api mock：getSessionEvents 读可换装的 holder；workflow-mode 记录调用 ──────
const apiState = vi.hoisted(() => ({
  events: [] as AgentEvent[],
  workflowModeCalls: [] as { sessionId: string; mode: string }[],
}));

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    listSessions: vi.fn(async () => []),
    getSessionEvents: vi.fn(async () => apiState.events),
    getSessionRecovery: vi.fn(async () => []),
    listSessionQueue: vi.fn(async () => ({ items: [], steers: [] })),
    sendMessage: vi.fn(),
    resumeSession: vi.fn(),
    flushSessionQueue: vi.fn(),
    changeWorkflowMode: vi.fn(async (sessionId: string, mode: string) => {
      apiState.workflowModeCalls.push({ sessionId, mode });
      return { id: sessionId, mode };
    }),
  };
});

// ── wsStream mock：捕获接流三元组；流不出帧、不收尾（帧时机归测试驱动）────────
const wsMock = vi.hoisted(() => ({
  calls: [] as { sessionId: string; afterSeq: number }[],
  controllers: [] as ReadableStreamDefaultController<Uint8Array>[],
}));

vi.mock('../lib/wsStream', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/wsStream')>();
  return {
    ...actual,
    wsStreamResponse: (sessionId: string, afterSeq?: number) => {
      wsMock.calls.push({ sessionId, afterSeq: afterSeq ?? -1 });
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            wsMock.controllers.push(controller);
          },
        }),
        { status: 200, headers: { 'content-type': 'text/event-stream' } },
      );
    },
  };
});

import { changeWorkflowMode, getSessionEvents } from '../lib/api';

const SID = 's-1';
const RUN = 'r-1';
const T = '2026-10-17T00:00:00Z';

function frame(
  seq: number,
  type: string,
  data?: Record<string, unknown>,
): AgentEvent {
  return {
    type,
    seq,
    session_id: SID,
    run_id: RUN,
    ...(data !== undefined ? { data } : {}),
    time: T,
  } as AgentEvent;
}

/** 后端异常臂的 error 帧（#1193 票面 schema）：stream-only（seq=null）。 */
function planStuckFrame(): AgentEvent {
  return {
    type: 'error',
    seq: null,
    session_id: SID,
    run_id: null,
    data: {
      code: 'run-failed-before-plan',
      message: '运行失败（RuntimeError），未分类异常；完整原始信息见后端日志',
      workflow_mode: 'plan',
      hint: '会话仍处于 plan 档（只读），可手动退出 plan 模式后重试',
    },
    time: T,
  } as unknown as AgentEvent;
}

/** 空闲会话的 durable 历史：一个已收口的 run（游标 = 2）。 */
const idleHistory = (): AgentEvent[] => [
  frame(1, 'session/started'),
  frame(2, 'run/completed', {}),
];

/** 外部启动且仍在途的 run（进入 live 的入口，走既有轮询接管路径）。 */
const inFlightRun = (): AgentEvent[] => [
  ...idleHistory(),
  frame(3, 'run/started'),
  frame(4, 'user/message', { content: '先出计划' }),
];

const enc = new TextEncoder();
function feedStream(...frames: unknown[]): void {
  const controller = wsMock.controllers.at(-1);
  for (const f of frames) controller?.enqueue(enc.encode(`data: ${JSON.stringify(f)}\n\n`));
}
function closeStream(): void {
  wsMock.controllers.at(-1)?.close();
}

// ── 夹具（与 useSession.externalRun.test.tsx 同款）──────────────────────────
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

/** 经既有轮询接管路径进入 live（wsMock.calls[0] = 本条流）。 */
async function enterLive(): Promise<void> {
  await mount();
  await enterViewing(SID);
  apiState.events = inFlightRun();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(EXTERNAL_RUN_POLL_INTERVAL_MS);
  });
  expect(captured.hook!.streaming).toBe(true);
  expect(wsMock.calls).toHaveLength(1);
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  vi.useFakeTimers();
  wsMock.calls.length = 0;
  wsMock.controllers.length = 0;
  apiState.events = idleHistory();
  apiState.workflowModeCalls.length = 0;
  vi.mocked(getSessionEvents).mockImplementation(async () => apiState.events);
  vi.mocked(changeWorkflowMode).mockImplementation(
    async (sessionId: string, mode: string) => {
      apiState.workflowModeCalls.push({ sessionId, mode });
      return { id: sessionId, mode };
    },
  );
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  container?.remove();
  root = null;
  container = null;
  captured.hook = null;
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('#1193 — run 致命异常且计划从未生成（前端半）', () => {
  it('收到 error 帧：亮出 plan-stuck 横幅；收流不进重连链（终态收口，无「连接中断」）', async () => {
    await enterLive();

    await act(async () => {
      feedStream(planStuckFrame());
    });
    expect(captured.hook!.runFailedBeforePlan).toMatchObject({
      sessionId: SID,
      hint: expect.stringContaining('手动退出 plan 模式'),
    });
    expect(captured.hook!.runFailedBeforePlan?.message).toContain('运行失败');

    // 后端异常臂随后收流：终态已见 ⇒ 干净迁移 viewing，**不**调度重连。
    await act(async () => {
      closeStream();
      // 推进足够覆盖重连退避 + 停摆看门狗的全部定时器。
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(captured.hook!.streaming).toBe(false);
    expect(captured.hook!.reconnecting).toBe(false);
    // 判别断言：没有第二次接流（修复前这里会重连，最终 give-up 报「连接中断」）。
    expect(wsMock.calls).toHaveLength(1);
    expect(captured.hook!.error).toBeNull();
  });

  it('退出 plan 模式：显式调用 workflow-mode(normal) → 重读事件 → 清横幅', async () => {
    await enterLive();
    await act(async () => {
      feedStream(planStuckFrame());
    });
    await act(async () => {
      closeStream();
      await vi.advanceTimersByTimeAsync(60_000);
    });
    const readsBefore = vi.mocked(getSessionEvents).mock.calls.length;

    await act(async () => {
      await captured.hook!.exitPlanMode(SID);
    });

    expect(apiState.workflowModeCalls).toEqual([{ sessionId: SID, mode: 'normal' }]);
    // 档位真相在事件流：退出成功后必须重读重投影（不用回执写本地状态）。
    expect(vi.mocked(getSessionEvents).mock.calls.length).toBeGreaterThan(readsBefore);
    expect(captured.hook!.runFailedBeforePlan).toBeNull();
  });

  it('退出失败：横幅保持在场、错误向上抛（由横幅就地回显，不静默吞）', async () => {
    await enterLive();
    await act(async () => {
      feedStream(planStuckFrame());
    });
    await act(async () => {
      closeStream();
      await vi.advanceTimersByTimeAsync(60_000);
    });

    vi.mocked(changeWorkflowMode).mockRejectedValueOnce(
      Object.assign(new Error('存储写锁耗尽'), { status: 503 }),
    );
    await act(async () => {
      await expect(captured.hook!.exitPlanMode(SID)).rejects.toThrow('存储写锁耗尽');
    });

    // 横幅保持——恢复入口不因一次失败消失。
    expect(captured.hook!.runFailedBeforePlan?.sessionId).toBe(SID);
  });

  it('切到别的会话：横幅不跨会话存活', async () => {
    await enterLive();
    await act(async () => {
      feedStream(planStuckFrame());
    });
    await act(async () => {
      closeStream();
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(captured.hook!.runFailedBeforePlan).not.toBeNull();

    apiState.events = [frame(1, 'session/started'), frame(2, 'run/completed', {})].map(
      (e) => ({ ...e, session_id: 's-2' }) as AgentEvent,
    );
    await act(async () => {
      captured.hook!.selectSession('s-2');
    });
    await act(async () => {});

    expect(captured.hook!.runFailedBeforePlan).toBeNull();
  });
});
