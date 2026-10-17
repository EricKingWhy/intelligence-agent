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
 * ── 修复契约（票面 schema + 独立审查 F2/F3）───────────────────────────────
 * 后端异常臂落盘 `error` 事件（**durable**）：`data.code=run-failed-before-plan`
 * + 可读 message + `hint`（可手动退出 plan 模式）。前端：
 *   1. 收到该帧当**终态**收口（terminalSeen）⇒ 后端随后收流走干净迁移，
 *      不进重连链（重试永远不会成功）；
 *   2. 横幅 `runFailedBeforePlan` = **事件流投影**（`deriveRunFailedBeforePlan`：
 *      最新生命周期标记 = 该 error）——迟到接流的 WS 快照与刷新后的
 *      GET /events 走同一份事件列表，因此横幅在刷新/切回后照样在场
 *      （stream-only 的瞬时帧做不到这件事：致命启动失败发生在 run 起点，
 *      前端接流总晚一步，帧发出时没有订阅者，必丢）；
 *   3. 退出 = `POST /workflow-mode {mode:'normal'}`（显式、幂等）→ 重读事件
 *      重投影（档位真相在事件流，不用回执写本地状态）→ 新的 mode-changed
 *      取代失败标记，横幅随投影自行消失。
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
import { EARLY_RESPONSE_WINDOW_MS, useSession } from './useSession';
import { deriveRunFailedBeforePlan } from '../lib/projection';

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
    // 创建路径（F2 的真实入口）：POST 永不 resolve = 交付层攒包（响应头等 run 结束）。
    startSession: vi.fn(() => new Promise<Response>(() => {})),
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
    // 创建路径的「基线 + 差分」认领：基线给空集、差分直接给 SID（#1148 同款夹具）。
    sessionIdBaseline: vi.fn(async () => new Set<string>()),
    discoverNewSessionId: vi.fn(async () => SID),
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
const SID2 = 's-2';
const RUN = 'r-1';
const T = '2026-10-17T00:00:00Z';

function frame(
  seq: number,
  type: string,
  data?: Record<string, unknown>,
  sid = SID,
): AgentEvent {
  return {
    type,
    seq,
    session_id: sid,
    run_id: RUN,
    ...(data !== undefined ? { data } : {}),
    time: T,
  } as AgentEvent;
}

/** 后端异常臂的 error 事件（#1193 票面 schema）：**durable**——落盘后迟到接流
 *  的 WS 快照与刷新后的 GET /events 都读得到（审查 F2/F3 的判据）。 */
function planStuckFrame(seq = 5): AgentEvent {
  return {
    type: 'error',
    seq,
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

/** 失败已落盘的 plan 会话（plan 档 + durable error；没有 run/started）。 */
const stuckHistory = (): AgentEvent[] => [
  frame(1, 'session/started'),
  frame(2, 'workflow/mode-changed', { mode: 'plan' }),
  planStuckFrame(3),
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

/** 经创建路径进入 live（F2 的真实入口）：submitTask → 认领 SID → WS 接流。
 *  POST 永不 resolve = 交付层攒包；帧时机归测试驱动。 */
async function enterLiveViaSubmit(): Promise<void> {
  await mount();
  let pending: Promise<unknown> | undefined;
  await act(async () => {
    pending = captured.hook!.submitTask({ task: '先出计划' });
    await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
    await pending;
  });
  expect(captured.hook!.streaming).toBe(true);
  expect(wsMock.calls).toHaveLength(1);
  expect(wsMock.calls[0]).toMatchObject({ sessionId: SID, afterSeq: -1 });
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
  it('刷新/迟到接流：durable 历史里就有该事件 → 横幅在场（不依赖当次流里的瞬时帧）', async () => {
    await mount();
    // 打开一个「失败已落盘」的会话：没有实时流、没有 error 帧经过 onEvent——
    // 横幅只能来自事件流投影（修复前这里恒为 null：横幅只活在流里）。
    apiState.events = stuckHistory();
    await enterViewing(SID);

    expect(captured.hook!.runFailedBeforePlan).toMatchObject({
      sessionId: SID,
      hint: expect.stringContaining('手动退出 plan 模式'),
    });
    expect(captured.hook!.runFailedBeforePlan?.message).toContain('运行失败');
    expect(wsMock.calls).toHaveLength(0); // 没有在途 run：不开流
  });

  it('收到 error 帧：终态收口，收流不进重连链（无「连接中断」静默重试）', async () => {
    await enterLiveViaSubmit();

    await act(async () => {
      // durable 的同一事实：先落盘（GET /events 读面）再广播（帧）。此例的形态
      // 正是票面场景——run 在 begin_run 前就死了，日志里没有 run/started。
      apiState.events = stuckHistory();
      feedStream(planStuckFrame(3));
    });
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
    // 迁移到 viewing 会重读 durable 历史重建对话——横幅仍在（这正是 durable
    // 的意义：瞬时帧在这一次重读里就没了）。
    expect(captured.hook!.runFailedBeforePlan?.sessionId).toBe(SID);
  });

  it('退出 plan 模式：显式调用 workflow-mode(normal) → 重读事件 → 横幅随投影消失', async () => {
    await mount();
    apiState.events = stuckHistory();
    await enterViewing(SID);
    expect(captured.hook!.runFailedBeforePlan).not.toBeNull();
    const readsBefore = vi.mocked(getSessionEvents).mock.calls.length;

    // 退出的 durable 效果：事件流里多一条 mode-changed(normal)。
    vi.mocked(changeWorkflowMode).mockImplementationOnce(
      async (sessionId: string, mode: string) => {
        apiState.workflowModeCalls.push({ sessionId, mode });
        apiState.events = [...stuckHistory(), frame(4, 'workflow/mode-changed', { mode: 'normal' })];
        return { id: sessionId, mode };
      },
    );
    await act(async () => {
      await captured.hook!.exitPlanMode(SID);
    });

    expect(apiState.workflowModeCalls).toEqual([{ sessionId: SID, mode: 'normal' }]);
    // 档位真相在事件流：退出成功后必须重读重投影（不用回执写本地状态）。
    expect(vi.mocked(getSessionEvents).mock.calls.length).toBeGreaterThan(readsBefore);
    expect(captured.hook!.runFailedBeforePlan).toBeNull();
  });

  it('退出失败：横幅保持在场、错误向上抛（由横幅就地回显，不静默吞）', async () => {
    await mount();
    apiState.events = stuckHistory();
    await enterViewing(SID);

    vi.mocked(changeWorkflowMode).mockRejectedValueOnce(
      Object.assign(new Error('存储写锁耗尽'), { status: 503 }),
    );
    await act(async () => {
      await expect(captured.hook!.exitPlanMode(SID)).rejects.toThrow('存储写锁耗尽');
    });

    // 横幅保持——恢复入口不因一次失败消失。
    expect(captured.hook!.runFailedBeforePlan?.sessionId).toBe(SID);
  });

  it('切到别的会话再切回：横幅不跨会话存活，但本会话的记录仍在（投影语义）', async () => {
    await mount();
    apiState.events = stuckHistory();
    await enterViewing(SID);
    expect(captured.hook!.runFailedBeforePlan).not.toBeNull();

    apiState.events = [frame(1, 'session/started', undefined, SID2), frame(2, 'run/completed', {}, SID2)];
    await act(async () => {
      captured.hook!.selectSession(SID2);
    });
    await act(async () => {});
    expect(captured.hook!.runFailedBeforePlan).toBeNull();

    apiState.events = stuckHistory();
    await act(async () => {
      captured.hook!.selectSession(SID);
    });
    await act(async () => {});
    expect(captured.hook!.runFailedBeforePlan?.sessionId).toBe(SID);
  });
});

describe('#1193 — 横幅派生规则表（deriveRunFailedBeforePlan）', () => {
  const err = planStuckFrame(3);
  const modePlan = frame(1, 'workflow/mode-changed', { mode: 'plan' });
  const modeNormal = frame(4, 'workflow/mode-changed', { mode: 'normal' });
  const started = frame(5, 'run/started');

  it('空流 / 只有 plan 档：无横幅（新 plan 会话不是失败证据，不猜）', () => {
    expect(deriveRunFailedBeforePlan([])).toBeNull();
    expect(deriveRunFailedBeforePlan([modePlan])).toBeNull();
  });

  it('error 是最新标记 → 横幅；message/hint 原样取出', () => {
    const hit = deriveRunFailedBeforePlan([modePlan, err]);
    expect(hit?.message).toContain('运行失败');
    expect(hit?.hint).toContain('手动退出 plan 模式');
  });

  it('其后有 mode-changed（用户退出/改档）→ 无横幅', () => {
    expect(deriveRunFailedBeforePlan([err, modeNormal])).toBeNull();
  });

  it('其后有 run/started（用户重试且已启动）→ 无横幅', () => {
    expect(deriveRunFailedBeforePlan([err, started])).toBeNull();
  });

  it('重试再次失败（error 又出现）→ 横幅重现', () => {
    const again = { ...planStuckFrame(7), data: { ...err.data, message: '第二次失败' } } as AgentEvent;
    expect(deriveRunFailedBeforePlan([err, started, again])?.message).toBe('第二次失败');
  });

  it('message 缺失时给可读兜底（不显示空白横幅）', () => {
    const bare = { ...planStuckFrame(9), data: { code: 'run-failed-before-plan' } } as AgentEvent;
    expect(deriveRunFailedBeforePlan([bare])?.message).toContain('运行失败');
    expect(deriveRunFailedBeforePlan([bare])?.hint).toBe('');
  });
});
