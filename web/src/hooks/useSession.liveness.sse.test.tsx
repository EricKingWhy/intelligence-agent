// @vitest-environment jsdom
/** #440 AC1 — 审批等待期走 **SSE 降级流** 不得假停摆（端到端集成）。
 *
 * ── 缺陷 ─────────────────────────────────────────────────────────────────
 * WS 不可用（代理拒 Upgrade）时 `wsStreamResponse` 降级搬运 HTTP SSE 字节，但
 * 降级读循环只 `emitRaw`、从不调 `onLiveness`（#440 的 SSE 半边）。后端 SSE
 * keepalive 是**注释帧**（`: ping - <ts>`，app.py `SSE_PING_INTERVAL_SECONDS`），
 * 客户端 `parseFrame` 只取 `data:` 行 ⇒ 注释帧永远不会成为事件 ⇒ 审批等待期
 * （后端零事件）该路径没有任何活性信号，10s 后假停摆 → 3 次重连耗尽 →
 * give-up 假报「连接中断（connection stalled）」。
 *
 * ── 手法 ─────────────────────────────────────────────────────────────────
 * **不** mock wsStream 模块（要测的就是它）：只 stub 全局 WebSocket（建连即断、
 * 零服务帧 ⇒ 触发降级）与 fetch（返回可逐步投喂的 SSE 流）。useSession 本体、
 * wsStreamResponse、consumeSSE、停摆看门狗、重连状态机全走真实代码路径——
 * 「注释帧字节 → 降级读循环 → onLiveness → markStreamFrame → 停摆基准」整条
 * 链只要一环没接上，假时钟推进 70s 后就会 give-up。对照 wsStream.test.ts 的
 * 同名单测（只锁降级循环本身），这里锁的是接进 hook 之后的整体行为。
 */

import { act, useEffect } from 'react';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSession } from './useSession';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    listSessions: vi.fn(async () => []),
    getSessionEvents: vi.fn(async () => []),
    listSessionQueue: vi.fn(async () => ({ items: [], steers: [] })),
    // 排队回执（2xx + JSON）⇒ ack 分支接流——审批等待期的入口场景。
    sendMessage: vi.fn(async () =>
      new Response(JSON.stringify({ status: 'queued', queue_id: 'q-1' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    ),
  };
});

/** 建连即死（零服务帧）的假 WebSocket：构造成功、随后被测试 fireClose 断开
 *  ⇒ wsStreamResponse 按契约降级（「已收到服务帧后断流」不降级，故必须零帧）。 */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly CLOSED = 3;

  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((msg: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(_url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = FakeWebSocket.CLOSED;
  }

  fireClose(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }
}

type SessionApi = ReturnType<typeof useSession>;
/** 属性写入而非变量重赋值：Harness 渲染期捕获 hook API（oxlint react(globals) 只拦变量重赋值）。 */
const captured = { hook: null as SessionApi | null };
let container: HTMLDivElement | null = null;
let root: Root | null = null;
/** 降级 SSE 流的投喂口：keepalive 注释帧从这里按 2s 一拍进去。 */
let sseController: ReadableStreamDefaultController<Uint8Array> | null = null;

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

/** 走完真实降级链：ack 接流 → WS 零服务帧断开 → 降级 fetch → 读循环在飞。 */
async function driveAckAndFallback(sid: string): Promise<void> {
  await act(async () => { await captured.hook!.sendMessage(sid, '排队消息'); });
  expect(FakeWebSocket.instances).toHaveLength(1);
  await act(async () => {
    FakeWebSocket.instances[0].fireClose(); // 零服务帧 → 降级
    await vi.advanceTimersByTimeAsync(10); // 放行降级 fetch + 读循环挂起
  });
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
  FakeWebSocket.instances = [];
  sseController = null;
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(c) {
            sseController = c;
          },
        }),
        { status: 200, headers: { 'content-type': 'text/event-stream' } },
      ),
    ),
  );
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  container?.remove();
  root = null;
  container = null;
  captured.hook = null;
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('#440 — 审批等待期走 SSE 降级流：keepalive 注释帧计入活性，不得假停摆', () => {
  it('注释帧每 2s 到达 ⇒ 停摆看门狗不触发（不重连、不 give-up）', async () => {
    const SID = 's-1';
    await mount();
    await driveAckAndFallback(SID);
    const fetchMock = vi.mocked(fetch);
    expect(fetchMock).toHaveBeenCalledTimes(1); // 降级流已建立

    // 后端 keepalive：2s 一拍的注释帧（`: ping - <ts>`）。
    const enc = new TextEncoder();
    const hb = setInterval(() => {
      try {
        sseController?.enqueue(enc.encode(`: ping - ${new Date().toISOString()}\n\n`));
      } catch {
        /* 流已被停摆重连 cancel（只发生在被测缺陷在场时）：停止投喂 */
      }
    }, 2000);
    await act(async () => { await vi.advanceTimersByTimeAsync(70_000); });
    clearInterval(hb);

    // 红（修复前）：降级读循环不喂活性 ⇒ T+20s 假停摆 ⇒ 3 次重连（新 WS）⇒
    //   T+50s give-up：streaming=false、WS 实例 4 个。
    // 绿（修复后）：字节到达即活性 ⇒ 全程一条降级流、一个 WS 实例。
    expect(captured.hook!.error).toBeNull();
    expect(captured.hook!.streaming).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1); // 没有重连（重连必先建新 WS）
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('字节也断供的降级流仍必须被看门狗判停（活性口径没有被放宽成「永不超时」）', async () => {
    // 部署交付层把 SSE 整个攒到流结束才下发时，连注释帧都到不了——那**没有**
    // 任何活性证据，看门狗照旧要把它判成僵死走重连（wsStream.ts 头注的「不静默」
    // 契约）。守这条边界：#440 的修复不得把停摆检测焊死成永触发。
    const SID = 's-2';
    await mount();
    await driveAckAndFallback(SID);

    await act(async () => { await vi.advanceTimersByTimeAsync(70_000); });

    expect(captured.hook!.streaming).toBe(false); // give-up
    expect(FakeWebSocket.instances).toHaveLength(4); // 1 初接 + 3 重连
  });
});
