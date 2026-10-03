// @vitest-environment jsdom
/** #561（UI-01）外部启动 run 的接管——hook 级红绿测试。
 *
 * ── 缺陷 ─────────────────────────────────────────────────────────────────
 * Inspector 只在「选中瞬间」检查一次 hasUnterminatedRun（历史装载 effect）；
 * 选中**之后**经外部（POST /messages / CLI / 另一标签页）启动的 run 没有任何
 * 订阅者——实时流、审批弹窗、错误态全不出现（原始报告：后端 6 个 delta +
 * run/completed 完整，对话区 45s 纹丝不动，页面零 /stream 订阅）。
 *
 * ── 修复契约（票面方案依据：ADAPT，复用既有事件 cursor + resumeLiveStream）──
 * viewing 态常驻轻量轮询（EXTERNAL_RUN_POLL_INTERVAL_MS）：读既有 GET /events，
 * 发现新事实后二选一——
 *   - 在途 run（hasUnterminatedRun）→ resumeLiveStream 接管（BUG-006 同一条路；
 *     resumeAttemptedRef 同游标去重 + 代际自增 ⇒ 订阅数 ≤1）；
 *   - 新事实全部已收口（「快速完成」）→ applyEvent 增量追赶，不开流、不迁 live。
 *
 * ── 手法 ─────────────────────────────────────────────────────────────────
 * 与 useSession.liveness.test.tsx 同款夹具：mock api 层（getSessionEvents 读
 * 可换装的 holder =「durable log 长出新事实」）与 wsStream 层（捕获接流三元组、
 * 流不出帧，帧时机归测试驱动）；useSession 本体、consumeSSE、投影、重连状态机
 * 全走真实代码路径。票面加测的路径逐条落在这里：选中后 API launch、快速完成、
 * 切换 Session（不错接）、重复 run/started（单次接管）、live 期间休眠（订阅数
 * ≤1）、审批/失败经接管流到达、卸载清理。重连 gap 的流机器本身由既有
 * reconnect 契约测试与 e2e 覆盖——轮询对它的唯一义务是 live 期间休眠（本文
 * 「live 期间轮询休眠」用例）。
 */

import { act, useEffect } from 'react';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentEvent } from '../types';
import { EXTERNAL_RUN_POLL_INTERVAL_MS, useSession } from './useSession';

// ── api mock：getSessionEvents 读可换装的 holder ─────────────────────────────
const apiState = vi.hoisted(() => ({ events: [] as AgentEvent[] }));

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    listSessions: vi.fn(async () => []),
    getSessionEvents: vi.fn(async () => apiState.events),
    listSessionQueue: vi.fn(async () => ({ items: [], steers: [] })),
    sendMessage: vi.fn(),
    resumeSession: vi.fn(),
    flushSessionQueue: vi.fn(),
  };
});

// ── wsStream mock：捕获接流三元组；流不出帧、不收尾（帧时机归测试驱动）────────
const wsMock = vi.hoisted(() => ({
  calls: [] as { sessionId: string; afterSeq: number; onLiveness?: () => void }[],
  controllers: [] as ReadableStreamDefaultController<Uint8Array>[],
}));

vi.mock('../lib/wsStream', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/wsStream')>();
  return {
    ...actual,
    wsStreamResponse: (sessionId: string, afterSeq?: number, onLiveness?: () => void) => {
      wsMock.calls.push({ sessionId, afterSeq: afterSeq ?? -1, onLiveness });
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

import { getSessionEvents } from '../lib/api';

// ── 帧构造（形状对齐 e2e fixtures / 后端事件词表，validateEvent 可过）──────────
const SID = 's-1';
const SID2 = 's-2';
const RUN = 'r-1';
const T = '2026-10-03T00:00:00Z';

function frame(
  sid: string,
  seq: number,
  type: string,
  data?: Record<string, unknown>,
  stepId?: number,
): AgentEvent {
  return {
    type,
    seq,
    session_id: sid,
    run_id: RUN,
    ...(stepId !== undefined ? { step_id: stepId } : {}),
    ...(data !== undefined ? { data } : {}),
    time: T,
  } as AgentEvent;
}

/** 空闲会话的 durable 历史：一个已收口的 run（游标 = 2）。 */
const idleHistory = (): AgentEvent[] => [
  frame(SID, 1, 'session/started'),
  frame(SID, 2, 'run/completed', {}),
];
const idleHistory2 = (): AgentEvent[] => [
  frame(SID2, 1, 'session/started'),
  frame(SID2, 2, 'run/completed', {}),
];

/** 外部启动且仍在途的 run（审批等待 / 长回答的中间态）。 */
const inFlightRun = (): AgentEvent[] => [
  ...idleHistory(),
  frame(SID, 3, 'run/started'),
  frame(SID, 4, 'user/message', { content: '外部启动' }, 1),
];

/** 外部 run 在两次轮询之间整个跑完（「快速完成」）。 */
const fastCompletedRun = (): AgentEvent[] => [
  ...idleHistory(),
  frame(SID, 3, 'run/started'),
  frame(SID, 4, 'user/message', { content: '快速完成' }, 1),
  frame(SID, 5, 'text/delta', { delta: '外部回答' }, 1),
  frame(SID, 6, 'model/completed', { content: '外部回答' }, 1),
  frame(SID, 7, 'run/completed', {}),
];

// ── 流投喂 ──────────────────────────────────────────────────────────────────
const enc = new TextEncoder();
function feedStream(...frames: unknown[]): void {
  const controller = wsMock.controllers.at(-1);
  for (const f of frames) controller?.enqueue(enc.encode(`data: ${JSON.stringify(f)}\n\n`));
}
function closeStream(): void {
  wsMock.controllers.at(-1)?.close();
}

// ── 夹具（与 useSession.liveness.test.tsx 同款）──────────────────────────────
type SessionApi = ReturnType<typeof useSession>;
/** 属性写入而非变量重赋值：oxlint react(globals) 只拦变量重赋值。 */
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

/** 选中会话并等历史装载落地（conversation 物化 = 轮询的起跑线）。 */
async function enterViewing(sid = SID): Promise<void> {
  await act(async () => {
    captured.hook!.selectSession(sid);
  });
  await act(async () => {});
  expect(captured.hook!.conversation?.session_id).toBe(sid);
  expect(captured.hook!.streaming).toBe(false);
}

/** 推进一个轮询间隔（恰好触发一次 tick）。 */
async function advanceOnePoll(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(EXTERNAL_RUN_POLL_INTERVAL_MS);
  });
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  vi.useFakeTimers();
  wsMock.calls.length = 0;
  wsMock.controllers.length = 0;
  apiState.events = idleHistory();
  // 个别用例会换装 mockImplementation（如「切换 Session」的在途扣留）；
  // clearAllMocks 不还原实现 ⇒ 每个用例开工前重装「读 holder」的缺省实现。
  vi.mocked(getSessionEvents).mockImplementation(async () => apiState.events);
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

describe('#561 — viewing 态外部 run 发现', () => {
  it('核心红：选中后外部启动在途 run → 一个轮询间隔内接管（live 迁移 + 游标正确 + 单订阅）', async () => {
    await mount();
    await enterViewing(SID);

    // 「外部」启动：durable log 长出一个仍在途的 run（POST /messages 走的正是这条）
    apiState.events = inFlightRun();
    await advanceOnePoll();

    expect(captured.hook!.streaming).toBe(true);
    expect(captured.hook!.error).toBeNull();
    // 接管 = 复用既有恢复路径：WS 接流、游标带上刚读到的最大 seq、单次订阅
    expect(wsMock.calls).toHaveLength(1);
    expect(wsMock.calls[0].sessionId).toBe(SID);
    expect(wsMock.calls[0].afterSeq).toBe(4);
    // 接管瞬间视图即服务器真相（initialConv = 刚读到的事件全量重建）
    expect(captured.hook!.conversation?.events.some((e) => e.seq === 4)).toBe(true);
  });

  it('快速完成（两次轮询之间跑完）→ 增量追赶渲染：不开流、不迁 live', async () => {
    await mount();
    await enterViewing(SID);

    apiState.events = fastCompletedRun();
    await advanceOnePoll();

    expect(wsMock.calls).toHaveLength(0); // 没有可接的流：不为已收口的 run 开订阅
    expect(captured.hook!.streaming).toBe(false);
    // 视图已追上 durable 真相（t6b 同机制：外部 run 的终态/失败必须出现在对话区）
    const conv = captured.hook!.conversation;
    expect(conv?.events.some((e) => e.seq === 7 && e.type === 'run/completed')).toBe(true);
    expect(conv?.events.some((e) => e.seq === 5 && e.type === 'text/delta')).toBe(true);
  });

  it('切换 Session 后，在途轮询读数不得接管旧会话（不错接别的 Session）', async () => {
    let armed = false;
    let release: (events: AgentEvent[]) => void = () => {};
    vi.mocked(getSessionEvents).mockImplementation(async (sid: string) => {
      if (sid === SID && armed) {
        return new Promise<AgentEvent[]>((resolve) => {
          release = resolve;
        });
      }
      return sid === SID ? idleHistory() : idleHistory2();
    });

    await mount();
    await enterViewing(SID);

    // 外部事实在途、读数被扣住（in-flight 的轮询）⇒ 此刻切走
    armed = true;
    await advanceOnePoll();
    await act(async () => {
      captured.hook!.selectSession(SID2);
    });
    await act(async () => {});
    expect(captured.hook!.selectedId).toBe(SID2);

    // 读数放行：带的是 SID 的新事实，但当下已不在查看 SID ⇒ 不得接管
    release(inFlightRun());
    await act(async () => {});
    await advanceOnePoll();

    expect(wsMock.calls).toHaveLength(0);
    expect(captured.hook!.streaming).toBe(false);
    expect(captured.hook!.selectedId).toBe(SID2);
    expect(captured.hook!.conversation?.session_id).toBe(SID2);
  });

  it('live 期间轮询休眠：期间新事实也不加第二条流、不再读事件（订阅数 ≤1）', async () => {
    await mount();
    await enterViewing(SID);

    apiState.events = inFlightRun();
    await advanceOnePoll();
    expect(captured.hook!.streaming).toBe(true);
    expect(wsMock.calls).toHaveLength(1);

    // live 期间 durable log 继续长（run 的正常产出）——轮询必须装聋
    apiState.events = [...inFlightRun(), frame(SID, 5, 'text/delta', { delta: '继续' }, 1)];
    const readsAtTakeover = vi.mocked(getSessionEvents).mock.calls.length;
    const hb = setInterval(() => wsMock.calls.at(-1)?.onLiveness?.(), 2000); // 心跳防停摆重连
    await act(async () => {
      await vi.advanceTimersByTimeAsync(EXTERNAL_RUN_POLL_INTERVAL_MS * 3);
    });
    clearInterval(hb);

    expect(wsMock.calls).toHaveLength(1);
    expect(vi.mocked(getSessionEvents).mock.calls.length).toBe(readsAtTakeover);
    expect(captured.hook!.streaming).toBe(true);
  });

  it('重复 run/started（同一轮询窗内两次启动）→ 单次接管、游标取最大', async () => {
    await mount();
    await enterViewing(SID);

    apiState.events = [
      ...idleHistory(),
      frame(SID, 3, 'run/started'),
      frame(SID, 4, 'user/message', { content: '外部一' }, 1),
      frame(SID, 5, 'run/started'),
      frame(SID, 6, 'user/message', { content: '外部二' }, 1),
    ];
    await advanceOnePoll();

    expect(wsMock.calls).toHaveLength(1);
    expect(wsMock.calls[0]).toMatchObject({ sessionId: SID, afterSeq: 6 });
    expect(captured.hook!.streaming).toBe(true);
  });

  it('审批路径：接管流上到达的 tool/approval-requested 进 pending_approvals（弹窗数据可达）', async () => {
    await mount();
    await enterViewing(SID);

    apiState.events = inFlightRun();
    await advanceOnePoll();
    expect(captured.hook!.streaming).toBe(true);

    // 后端审批等待期经在途流广播的请求帧（形状 = session/approval.py 写入口；
    // 同一事实也是 durable 的——审批请求先落盘再等决策）。
    const approvalFrame = {
      type: 'tool/approval-requested',
      data: {
        approval_id: 'ap-1',
        tool_name: 'write',
        tool_call_id: 'tc-1',
        action_type: 'workspace-write',
        title: 'write (workspace-write)',
        description: '需要审批',
        arguments_preview: { path: 'demo.txt', content: 'HELLO' },
        permission: 'workspace-write',
        policy: 'read-only',
        reason: '需要审批',
        allowed_decisions: ['deny', 'approve_once'],
      },
      seq: 5,
      session_id: SID,
      run_id: RUN,
      step_id: 1,
      time: T,
    };
    apiState.events = [...inFlightRun(), approvalFrame as AgentEvent];
    feedStream(approvalFrame);
    // 非终态帧经 24ms 合帧窗口提交（P1-3）——假时钟要放过这一拍
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });

    expect(captured.hook!.conversation?.pending_approvals).toHaveLength(1);
    expect(captured.hook!.conversation?.pending_approvals[0]?.approval_id).toBe('ap-1');
  });

  it('失败路径：接管流上到达的 run/failed 驱动终态收尾（streaming 落定 + 失败归因投影）', async () => {
    await mount();
    await enterViewing(SID);

    apiState.events = inFlightRun();
    await advanceOnePoll();
    expect(captured.hook!.streaming).toBe(true);

    // 终态事件与真后端一致：先落 durable log，再经在途流广播
    const failedFrame = {
      type: 'run/failed',
      data: { reason: 'provider_5xx', message: '模型 500' },
      seq: 5,
      session_id: SID,
      run_id: RUN,
      time: T,
    };
    apiState.events = [...inFlightRun(), failedFrame as AgentEvent];
    feedStream(failedFrame);
    closeStream(); // 服务端终态后收流（真后端行为）
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });

    expect(captured.hook!.streaming).toBe(false);
    expect(captured.hook!.conversation?.run_failure?.reason).toBe('provider_5xx');
  });

  it('idle 态不轮询：没有会话可发现，零事件读取', async () => {
    await mount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(EXTERNAL_RUN_POLL_INTERVAL_MS * 3);
    });
    expect(getSessionEvents).not.toHaveBeenCalled();
  });

  it('卸载清理：unmount 后轮询停止（不再读事件、不再有定时器副作用）', async () => {
    await mount();
    await enterViewing(SID);
    const readsBeforeUnmount = vi.mocked(getSessionEvents).mock.calls.length;
    expect(readsBeforeUnmount).toBeGreaterThan(0); // 历史装载读过一次

    await act(async () => {
      root!.unmount();
    });
    root = null;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(EXTERNAL_RUN_POLL_INTERVAL_MS * 6);
    });

    expect(vi.mocked(getSessionEvents).mock.calls.length).toBe(readsBeforeUnmount);
    expect(wsMock.calls).toHaveLength(0);
  });
});
