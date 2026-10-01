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
import {
  EARLY_RESPONSE_WINDOW_MS,
  MAX_RECONNECT_ATTEMPTS,
  RECONNECT_STALL_MS,
  useSession,
} from './useSession';

// ── wsStream mock：捕获三参；流不出帧、不收尾（帧时机归测试驱动）────────────
const wsMock = vi.hoisted(() => ({
  calls: [] as { sessionId: string; afterSeq: number; onLiveness?: () => void }[],
  // #478：每条流的控制器——帧出现时机由测试经 feedStream/closeStream 显式驱动，
  // **不在构造时灌帧**：modeRef 是渲染期同步的，构造期灌帧会在 sendMessage 的
  // act 内、setMode 提交之前就被微任务消费掉（生产上帧走网络，无此竞态）。
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
        {
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
        },
      );
    },
  };
});

/** 向当前（最新一条）流喂 SSE 帧。必须在 mode 提交后调用（见 wsMock 注释）。 */
function feedStream(...chunks: string[]): void {
  const controller = wsMock.controllers.at(-1);
  const encoder = new TextEncoder();
  for (const chunk of chunks) controller?.enqueue(encoder.encode(chunk));
}

/** 收掉当前流（干净 close ⇒ consumeSSE 走 onDone）。 */
function closeStream(): void {
  wsMock.controllers.at(-1)?.close();
}

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
  wsMock.controllers.length = 0;
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
    // （窗口取 90s：#456 修复后每条新流有各自完整的 10s 宽限，give-up 从
    // ~T+50s 推迟到 ~T+80s——判定语义不变，只是节奏被正确地放慢。）
    vi.mocked(sendMessage).mockResolvedValue(ackResponse());
    await mount();
    await act(async () => { await captured.hook!.sendMessage(SID, 'hi'); });
    expect(captured.hook!.streaming).toBe(true);

    await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });

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

describe('#456 — 挂新流必须重置停摆宽限起点（lastFrameAtRef）', () => {
  it('停摆触发的重连挂上新流后，新流一个宽限窗内无帧不得立即再判停；持续无帧最终仍 give-up', async () => {
    vi.mocked(sendMessage).mockResolvedValue(ackResponse());
    await mount();
    await act(async () => { await captured.hook!.sendMessage(SID, 'hi'); });
    expect(captured.hook!.streaming).toBe(true);
    expect(wsMock.calls).toHaveLength(1);

    // 推进到第一次停摆判定（首个心跳 tick 的 elapsed 恰等于阈值不判停，第二个
    // tick 必判）⇒ 重连挂上新流（第 2 条流，~T+20.5s）。
    await act(async () => { await vi.advanceTimersByTimeAsync(RECONNECT_STALL_MS * 2 + 1_000); });
    expect(wsMock.calls).toHaveLength(2);

    // 新流挂上后的一个宽限窗内（阈值 + 一个心跳 tick 余量，止于 ~T+32s）无帧：
    // 不得再判停。红（#456 在场）：基准仍是旧流最后一帧 ⇒ T+30s 的心跳 tick
    // 按**旧基准**算 elapsed ⇒ 立即再判停 ⇒ 挂上第 3 条流（新流自己才活了 9.5s）。
    await act(async () => { await vi.advanceTimersByTimeAsync(RECONNECT_STALL_MS + 1_000); });
    expect(wsMock.calls).toHaveLength(2);

    // 负对照（证明重置的是宽限**起点**、不是取消了判定）：持续无帧 ⇒ 新流的
    // 宽限照常耗尽 ⇒ 重连链走完额度 give-up（1 初接 + 3 重连 = 4 条流）。
    await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
    expect(captured.hook!.streaming).toBe(false);
    expect(wsMock.calls).toHaveLength(MAX_RECONNECT_ATTEMPTS + 1);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(wsMock.calls).toHaveLength(MAX_RECONNECT_ATTEMPTS + 1);
  });

  it('初接流同样重置宽限起点：挂载后闲置再提交，首条流获得完整 10s 宽限', async () => {
    vi.mocked(sendMessage).mockResolvedValue(ackResponse());
    await mount();
    // 页面挂载后用户思考了 15s 才提交：初接流不得继承挂载时刻的旧基准，
    // 否则提交后第一个心跳 tick 就会按旧基准误判停摆。
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    await act(async () => { await captured.hook!.sendMessage(SID, 'hi'); });
    expect(captured.hook!.streaming).toBe(true);
    expect(wsMock.calls).toHaveLength(1);

    // 新流一个宽限窗内无帧（推进止于 ~T+26s，旧基准下 T+20s tick 就会误判）：
    // 不得判停重连。
    await act(async () => { await vi.advanceTimersByTimeAsync(RECONNECT_STALL_MS + 1_000); });
    expect(wsMock.calls).toHaveLength(1);
  });
});

describe('#478 — durable pause 的干净收流不得触发重连链', () => {
  /** 真实形状的 run/paused 帧（载荷样板取自 projection.pause.test.ts 的 pausedEvent；
   *  seq 由用例按接流游标连续填充）。 */
  const pausedEventWire = {
    type: 'run/paused',
    session_id: SID,
    run_id: 'run-1',
    step_id: 2,
    time: '2026-09-25T00:00:00.000Z',
    data: {
      reason: 'budget_exhausted',
      trigger_dimension: 'run.max_agent_turns_total',
      budget_version: 1,
      consumed: { agent_turns: 3 },
      limits: {
        local: { max_agent_turns: 500, source: 'deployment' },
        run: { max_agent_turns_total: 4 },
      },
      continuation: {
        completed: ['本逻辑 run 已消耗 3 个 agent turn'],
        remaining: ['暂停发生在下一轮模型决策之前'],
        blockers: ['run.max_agent_turns_total 到顶：consumed=3, ceiling=4'],
        next_safe_action: '提高绝对 ceiling 后以同一 run_id 恢复',
      },
      closeout_source: 'model',
      resume_requirements: [],
      trace_id: 'trace-abc',
    },
  };

  it('收到 run/paused 后服务端关流：零重连、零横幅，直接落到可续跑的 paused 态', async () => {
    // durable log 与流帧同一份事件：finishLive 进 viewing 后的历史重载会从
    // **权威源**（GET /events）重投影 paused 事实（不变量 #22），因此 mock 返回
    // 同一序列。#312：run/paused 算收口 ⇒ hasUnterminatedRun=false，重载不再
    // 触发 resume 接流——流计数恒 1 是断言的一部分。
    const startedEvent = {
      type: 'run/started',
      seq: 0,
      session_id: SID,
      run_id: 'run-1',
      time: '2026-09-25T00:00:00.000Z',
      data: { turn_index: 1, model: 'glm-5.3' },
    };
    const pausedEvent = { ...pausedEventWire, seq: 1 };
    vi.mocked(getSessionEvents).mockResolvedValue([
      startedEvent as never,
      pausedEvent as never,
    ]);
    vi.mocked(sendMessage).mockResolvedValue(ackResponse());
    await mount();
    await act(async () => { await captured.hook!.sendMessage(SID, 'hi'); });
    expect(captured.hook!.streaming).toBe(true);
    expect(wsMock.calls).toHaveLength(1);

    // mode 已提交（streaming=true 断言已过）再喂帧：ack 分支的接流游标是 -1
    //（空会话无历史）⇒ 帧必须从 seq 0 连续起，否则 isSeqGap 会把首帧判成 gap。
    await act(async () => {
      feedStream(
        `data: ${JSON.stringify(startedEvent)}\n\n`,
        `data: ${JSON.stringify(pausedEvent)}\n\n`,
      );
      closeStream();
      await vi.advanceTimersByTimeAsync(1_000);
    });

    // 干净迁移：不进重连链（恒 1 条流）、不落「连接中断」、退出 streaming，
    // paused 事实经权威重载投影在案 ⇒ PausedPanel 渲染条件成立。
    expect(wsMock.calls).toHaveLength(1);
    expect(captured.hook!.streaming).toBe(false);
    expect(captured.hook!.error).toBeNull();
    expect(captured.hook!.reconnecting).toBe(false);
    expect(captured.hook!.conversation?.run_paused).not.toBeNull();
    expect(captured.hook!.conversation?.run_paused?.reason).toBe('budget_exhausted');
  });
});
