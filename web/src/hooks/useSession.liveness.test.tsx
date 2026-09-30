// @vitest-environment jsdom
/** #440 AC1 活性回调覆盖度——hook 级红绿测试。
 *
 * ── 缺陷 ─────────────────────────────────────────────────────────────────
 * `markStreamFrame`（lastFrameAtRef 的唯一停摆基准刷新口，#420 AC1 引入）此前只
 * 被 4/8 个接流点带上：submitTask / 重连 / truncated 重建 / resumeLiveStream 传了，
 * sendFollowUp 的 ack（排队/steer 受理回执）与 launched、resumePausedRun 的
 * launched、flushQueue 的 launched 四处没传 ⇒ 那些流在审批等待期（后端零事件、
 * 唯一下行是 2s 心跳）没有任何活性信号，10s 后被停摆看门狗误判成僵死 → 假重连
 * （最好情况：一次多余断连+重放；最坏情况：额度耗尽 give-up 假报「连接中断」）。
 *
 * ── 手法 ─────────────────────────────────────────────────────────────────
 * mock `../lib/wsStream` 捕获 (sessionId, afterSeq, onLiveness) 三参并回一条**永不
 * 自动出帧**的流——帧的出现时机由测试驱动：「心跳」= 调捕获到的 onLiveness（与真
 * WS 传输的契约逐字一致，wsStream.test.ts 的 #420 AC1 用例锁过）。api 层只换被
 * 驱动的端点；useSession 本体、consumeSSE、投影、重连状态机全走真实代码路径。
 * 假时钟推进 RECONNECT_STALL_MS 的数倍：有活性喂入不得停摆；对照组（无心跳）
 * 证明本夹具对停摆**敏感**（不是怎么跑都绿的 vacuous 断言）。
 */

import { act, useEffect } from 'react';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EARLY_RESPONSE_WINDOW_MS, useSession } from './useSession';

// ── wsStream mock：捕获三参；流不出帧、不收尾（帧时机归测试驱动）────────────
const wsMock = vi.hoisted(() => ({
  calls: [] as { sessionId: string; afterSeq: number; onLiveness?: () => void }[],
}));

vi.mock('../lib/wsStream', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/wsStream')>();
  return {
    ...actual,
    wsStreamResponse: (sessionId: string, afterSeq?: number, onLiveness?: () => void) => {
      wsMock.calls.push({ sessionId, afterSeq: afterSeq ?? -1, onLiveness });
      return new Response(new ReadableStream<Uint8Array>({ start() {} }), {
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
      });
    },
  };
});

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    listSessions: vi.fn(async () => []),
    getSessionEvents: vi.fn(async () => []),
    listSessionQueue: vi.fn(async () => ({ items: [], steers: [] })),
    sendMessage: vi.fn(),
    resumeSession: vi.fn(),
    flushSessionQueue: vi.fn(),
  };
});

import {
  flushSessionQueue,
  getSessionEvents,
  listSessionQueue,
  resumeSession,
  sendMessage,
} from '../lib/api';

// ── 夹具 ─────────────────────────────────────────────────────────────────
const SID = 's-1';

/** 排队回执（ack 分支判别依据：2xx + application/json ⇒ 消息已受理、run 仍在跑）。 */
const ackResponse = () =>
  new Response(JSON.stringify({ status: 'queued', queue_id: 'q-1' }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });

/** 每 2s 给「当前流」喂一次心跳：调最新一次捕获到的 onLiveness。
 *  真传输里这就是 server_ping（2s 一次）经 onLiveness 侧信道的到达。 */
function startHeartbeats(ms = 2000): ReturnType<typeof setInterval> {
  return setInterval(() => {
    wsMock.calls.at(-1)?.onLiveness?.();
  }, ms);
}

type SessionApi = ReturnType<typeof useSession>;
/** 属性写入而非变量重赋值：Harness 渲染期捕获 hook API（oxlint react(globals) 只拦变量重赋值）。 */
const captured = { hook: null as SessionApi | null };
let container: HTMLDivElement | null = null;
let root: Root | null = null;

function Harness() {
  const api = useSession();
  // effect 里捕获而非渲染期直写：react-compiler 的 immutability lint 拦渲染期外写。
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

/** 走完「判别窗口」驱动一次不落地的投递（悬挂 = 判 launched ⇒ 直驱 WS 接流）。 */
async function driveLaunched(action: () => Promise<void>): Promise<void> {
  let pending: Promise<void> | undefined;
  await act(async () => {
    pending = action();
    await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
    await pending;
  });
}

beforeEach(() => {
  // createRoot 的测试必须显式声明 act 环境，否则 act() 退化成“只警告不刷新”。
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  // BUG-005 的首屏恢复读 localStorage——jsdom 同文件跨用例共享，上一用例 give-up
  // 落下的 selectedId 会把下一用例的挂载直接拉回 viewing（历史装载被意外触发）。
  window.localStorage.clear();
  vi.useFakeTimers();
  wsMock.calls.length = 0;
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

describe('#440 AC1 — 审批等待期只有心跳帧（无事件）不得触发停摆重连', () => {
  it('对照（无任何帧）：停摆看门狗必须触发——3 次重连耗尽后 give-up（夹具敏感性证明）', async () => {
    // 无心跳 = lastFrameAt 停在挂载时刻：看门狗必须把它判成僵死。这条**不**随
    // 修复变红变绿——它证明上面的绿不是「怎么跑都绿」，断言对停摆真的敏感。
    vi.mocked(sendMessage).mockResolvedValue(ackResponse());
    await mount();
    await act(async () => { await captured.hook!.sendMessage(SID, 'hi'); });
    expect(captured.hook!.streaming).toBe(true);

    await act(async () => { await vi.advanceTimersByTimeAsync(70_000); });

    expect(captured.hook!.streaming).toBe(false); // give-up → viewing
    // 1 次初接 + 3 次重连 = 4；give-up 后不得继续空转
    expect(wsMock.calls).toHaveLength(4);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(wsMock.calls).toHaveLength(4);
  });

  it('sendFollowUp 排队回执（ack）路径：心跳不断供 ⇒ 不停摆、不重连、第三参到位', async () => {
    vi.mocked(sendMessage).mockResolvedValue(ackResponse());
    await mount();
    await act(async () => { await captured.hook!.sendMessage(SID, '排队消息'); });

    // 未接 4 处的最小回归：第三参（活性观察者）必须传到位——行为断言的前置。
    expect(wsMock.calls).toHaveLength(1);
    expect(wsMock.calls[0].sessionId).toBe(SID);
    expect(wsMock.calls[0].onLiveness).toBeTypeOf('function');

    const hb = startHeartbeats();
    await act(async () => { await vi.advanceTimersByTimeAsync(70_000); });
    clearInterval(hb);

    // 人在决策（审批等待）期间心跳每 2s 到达 = 链路活着：不得假停摆。
    // 红（修复前）：初接流没带 onLiveness ⇒ 10s 无刷新 ⇒ 假停摆 ⇒ 重连一次
    // （重连路径 #420 时已接）⇒ calls 变 2；绿（修复后）：恒 1。
    expect(captured.hook!.error).toBeNull();
    expect(captured.hook!.streaming).toBe(true);
    expect(wsMock.calls).toHaveLength(1);
  });

  it('sendFollowUp launched 分支：心跳不断供 ⇒ 不停摆、不重连、第三参到位', async () => {
    // 悬挂的 POST = 判别窗外未落定 = 判 launched ⇒ 直驱 WS 接流。
    vi.mocked(sendMessage).mockImplementation(() => new Promise<Response>(() => {}));
    await mount();
    await driveLaunched(() => captured.hook!.sendMessage(SID, 'hi'));

    expect(wsMock.calls).toHaveLength(1);
    expect(wsMock.calls[0].sessionId).toBe(SID);
    expect(wsMock.calls[0].onLiveness).toBeTypeOf('function');

    const hb = startHeartbeats();
    await act(async () => { await vi.advanceTimersByTimeAsync(70_000); });
    clearInterval(hb);

    expect(captured.hook!.error).toBeNull();
    expect(captured.hook!.streaming).toBe(true);
    expect(wsMock.calls).toHaveLength(1);
  });
});

describe('#440 — 未接 4 处的逐点回归：wsStreamResponse 第三参必须到位', () => {
  it('resumePausedRun launched 分支', async () => {
    vi.mocked(resumeSession).mockImplementation(() => new Promise<Response>(() => {}));
    await mount();
    await driveLaunched(() =>
      captured.hook!.resumePausedRun(SID, { runId: 'r-1', expectedVersion: 1, kind: 'tool', tool: 'bash', value: 10 }),
    );

    expect(wsMock.calls).toHaveLength(1);
    expect(wsMock.calls[0].sessionId).toBe(SID);
    expect(wsMock.calls[0].onLiveness).toBeTypeOf('function');
    expect(captured.hook!.streaming).toBe(true);
  });

  it('flushQueue launched 分支', async () => {
    vi.mocked(flushSessionQueue).mockImplementation(() => new Promise<Response>(() => {}));
    await mount();
    await driveLaunched(() => captured.hook!.flushQueue(SID));

    expect(wsMock.calls).toHaveLength(1);
    expect(wsMock.calls[0].sessionId).toBe(SID);
    expect(wsMock.calls[0].onLiveness).toBeTypeOf('function');
    expect(captured.hook!.streaming).toBe(true);
  });

  it('历史装载（viewing）不被以上改动波及：getSessionEvents 仍只按需调用', async () => {
    // 守护夹具本身：listSessions/getSessionEvents 的 mock 不掩盖意外请求——
    // 上面各用例若在 live 期间触发了历史重读，这里的第一性断言会暴露。
    vi.mocked(sendMessage).mockResolvedValue(ackResponse());
    await mount();
    await act(async () => { await captured.hook!.sendMessage(SID, 'hi'); });
    expect(getSessionEvents).not.toHaveBeenCalled();
    expect(listSessionQueue).not.toHaveBeenCalled();
  });
});
