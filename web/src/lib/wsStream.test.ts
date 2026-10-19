/** wsStream 契约测试——WS 帧 → SSE 文本的重编码，以及会话认领 / 基线。
 *
 * 这一层是**传输层**（不变量 #22：事件真相仍来自 RunManager 订阅），它唯一的
 * 对外承诺是「产出与既有 SSE 通道同形的字节」。所以断言分三类：
 *   1. 重编码形状（既有 consumeSSE 能原样消费——这是整个替换方案的成立前提）；
 *   2. 游标语义（afterSeq 过滤 + 终态兜底收流）；
 *   3. 会话认领（基线的失败态必须是 null，不能退化成空集）。
 *
 * node 环境没有 window / WebSocket：两者都由本文件 stub（同 density.test.ts 的
 * localStorage/document 手法）。node 22 自带全局 WebSocket，stubGlobal 覆盖它。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { consumeSSE } from './sse';
import {
  CLAIM_BASE_DELAY_MS,
  CLAIM_MAX_DELAY_MS,
  DEFAULT_SESSION_CLAIM_TIMEOUT_MS,
  SessionClaimAmbiguousError,
  SessionClaimTimeoutError,
  claimBackoffMs,
  discoverNewSessionId,
  sessionExists,
  sessionIdBaseline,
  sessionIdBaselineDetailed,
  wsStreamResponse,
} from './wsStream';
import type { AgentEvent } from '../types';

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 3;

  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  readonly url: string;
  /** 构造时带的子协议列表（#890 凭据通道就挂在它上面）。 */
  readonly protocols: string[] | undefined;
  onopen: (() => void) | null = null;
  onmessage: ((msg: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string, protocols?: string[] | string) {
    this.url = url;
    this.protocols = typeof protocols === 'string' ? [protocols] : protocols;
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = FakeWebSocket.CLOSED;
  }

  // ── 测试驱动 ──
  fireOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  fire(obj: unknown): void {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  fireRaw(data: string): void {
    this.onmessage?.({ data });
  }
  fireError(): void {
    this.onerror?.();
  }
  fireClose(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }
}

const sid = 's-1';
const endpoint = 'ws://localhost:5173/api/ws';

/** 读出下一个 SSE 帧携带的事件；流已收尾返回 null。 */
async function readEvent(
  reader: ReadableStreamDefaultReader<Uint8Array>,
): Promise<AgentEvent | null> {
  const { value, done } = await reader.read();
  if (done) return null;
  const text = new TextDecoder().decode(value);
  const line = text.split('\n').find((l) => l.startsWith('data: '));
  return line ? (JSON.parse(line.slice(6)) as AgentEvent) : null;
}

let socket: FakeWebSocket;

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.stubGlobal('window', { location: { protocol: 'http:', host: 'localhost:5173' } });
  socket = null as unknown as FakeWebSocket;
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** 起一条流并完成握手，返回 reader 与建好的假 socket。 */
function openStream(afterSeq?: number) {
  const res = wsStreamResponse(sid, afterSeq);
  const reader = res.body!.getReader();
  socket = FakeWebSocket.instances[0];
  socket.fireOpen();
  return reader;
}

describe('wsStreamResponse — WS 帧重编码为 SSE 文本', () => {
  it('连上后按 session_id 订阅 + 带上本地游标（WS 是多路复用通道，订阅是必需的握手）', () => {
    openStream();
    expect(socket.url).toBe(endpoint);
    // 游标必须是**默认值 -1**而不是省略：服务端按 `after_seq` 只补
    // `(after_seq, replay_upto]` 那一截，并据此判 backlog 超限（#208）。缺了这个
    // 字段，服务端只能拿"总事件数"当 backlog ⇒ 长会话每次订阅都被判超限。
    expect(socket.sent).toEqual([
      JSON.stringify({ type: 'subscribe', session_id: sid, after_seq: -1 }),
    ]);
  });

  it('订阅带上调用方给的游标（重建后重新订阅不能退回从头补）', () => {
    openStream(4);
    expect(socket.sent).toEqual([
      JSON.stringify({ type: 'subscribe', session_id: sid, after_seq: 4 }),
    ]);
  });

  it('event 帧原样重编码成 SSE data 帧——既有 consumeSSE 直接可消费', async () => {
    const reader = openStream();
    const event = { type: 'text/delta', data: { text: 'hi' }, seq: 7 };
    socket.fire({ type: 'event', event });

    const raw = await reader.read();
    expect(new TextDecoder().decode(raw.value)).toBe(`data: ${JSON.stringify(event)}\n\n`);
  });

  it('server_ping 回 pong，且**不**产出任何事件帧（应用层心跳不进事件流）', async () => {
    const reader = openStream();
    socket.fire({ type: 'server_ping' });
    expect(socket.sent[1]).toBe(JSON.stringify({ type: 'pong' }));

    // 紧跟一条真实事件：它必须是流里的**第一**帧——证明 ping 没有污染流。
    socket.fire({ type: 'event', event: { type: 'run/started', seq: 1 } });
    expect((await readEvent(reader))?.type).toBe('run/started');
  });

  it('#420 AC1：每条服务帧（含 server_ping）都通知活性观察者——心跳计入活性', () => {
    // 审批等待期后端零事件、唯一下行是 2s 一次的 server_ping；观察者（useSession
    // 的 lastFrameAtRef）据此让停摆看门狗知道「链路活着，是人（模型/审批）在安静」，
    // 不再把 10s 无事件误判成连接僵死而掐断重连。Slack RTM 心跳语义同款。
    const liveness = vi.fn();
    const res = wsStreamResponse(sid, -1, liveness);
    const reader = res.body!.getReader();
    socket = FakeWebSocket.instances[0];
    socket.fireOpen();

    socket.fire({ type: 'server_ping' });
    expect(liveness).toHaveBeenCalledTimes(1);

    socket.fire({ type: 'event', event: { type: 'run/started', seq: 1 } });
    expect(liveness).toHaveBeenCalledTimes(2);

    // 非 JSON 帧不算：它没有证明任何东西（连"服务端还活着"都证不了）
    socket.fireRaw('<<not json>>');
    expect(liveness).toHaveBeenCalledTimes(2);

    void reader.cancel().catch(() => {});
  });

  it('非 JSON 帧被忽略，不污染流也不抛错', async () => {
    const reader = openStream();
    socket.fireRaw('<<not json>>');
    socket.fire({ type: 'event', event: { type: 'run/started', seq: 1 } });
    expect((await readEvent(reader))?.type).toBe('run/started');
  });

  it('未知下行类型（launched / queued / steered 等）不下发', async () => {
    const reader = openStream();
    socket.fire({ type: 'queued', queue_id: 'q1' });
    socket.fire({ type: 'event', event: { type: 'run/started', seq: 1 } });
    expect((await readEvent(reader))?.type).toBe('run/started');
  });

  it('快照重编码为逐帧下发；has_active_run=false 时流随即收尾', async () => {
    const reader = openStream();
    socket.fire({
      type: 'snapshot',
      events: [{ type: 'session/started', seq: 1 }, { type: 'user/message', seq: 2 }],
      has_active_run: false,
    });

    expect((await readEvent(reader))?.seq).toBe(1);
    expect((await readEvent(reader))?.seq).toBe(2);
    // 无在途 run ⇒ 不会有 done 帧，快照即全量 ⇒ 收尾（否则流悬挂到天荒地老）
    expect(await readEvent(reader)).toBeNull();
  });
});

describe('wsStreamResponse — 游标与收尾', () => {
  it('afterSeq：快照里 ≤ 游标的 durable 事件不再下发（快照与 live 窗口重叠）', async () => {
    const reader = openStream(5);
    socket.fire({
      type: 'snapshot',
      events: [{ type: 'a', seq: 3 }, { type: 'b', seq: 5 }, { type: 'c', seq: 6 }],
      has_active_run: true,
    });

    expect((await readEvent(reader))?.seq).toBe(6); // 3 / 5 已被本地投影过
  });

  it('游标随快照推进：快照内已见过的 seq 在 live 帧里再出现也不重复下发', async () => {
    const reader = openStream(-1);
    socket.fire({ type: 'snapshot', events: [{ type: 'a', seq: 4 }], has_active_run: true });
    expect((await readEvent(reader))?.seq).toBe(4);

    socket.fire({ type: 'event', event: { type: 'a', seq: 4 } }); // 重放同一 seq
    socket.fire({ type: 'event', event: { type: 'b', seq: 5 } });
    expect((await readEvent(reader))?.seq).toBe(5);
  });

  it('null seq 的 ephemeral 帧（model/delta）不做游标过滤，永远下发', async () => {
    const reader = openStream(100);
    socket.fire({ type: 'event', event: { type: 'model/delta', seq: null } });
    expect((await readEvent(reader))?.type).toBe('model/delta');
  });

  it('done 帧 → 流干净收尾', async () => {
    const reader = openStream();
    socket.fire({ type: 'done', session_id: sid });
    expect(await readEvent(reader)).toBeNull();
  });

  it('终态事件后 done 迟到 → 兜底计时器收流（不让流悬挂）', async () => {
    vi.useFakeTimers();
    const reader = openStream();
    socket.fire({ type: 'event', event: { type: 'run/completed', seq: 9 } });
    expect((await readEvent(reader))?.type).toBe('run/completed');

    await vi.advanceTimersByTimeAsync(3000);
    expect(await readEvent(reader)).toBeNull();
  });

  it('error 帧 → 消费端 onError（携带后端 message），而非静默收尾', async () => {
    const res = wsStreamResponse(sid);
    const onError = vi.fn();
    // consumeSSE 的 onDone/onError 才是 attachLiveStream 的真实接缝——用真回调断言。
    consumeSSE(res, () => {}, () => {}, onError);
    const sock = FakeWebSocket.instances[0];
    sock.fireOpen();

    sock.fire({ type: 'error', message: 'snapshot failed' });
    await vi.waitFor(() => expect(onError).toHaveBeenCalled());
    expect((onError.mock.calls[0][0] as Error).message).toBe('snapshot failed');
  });

  it('cancel()（切走 / 重连前静默断开）关闭底层 socket，不再消费帧', async () => {
    const res = wsStreamResponse(sid);
    const handle = consumeSSE(res, () => {}, () => {});
    const sock = FakeWebSocket.instances[0];
    sock.fireOpen();

    handle.cancel();
    await vi.waitFor(() => expect(sock.readyState).toBe(FakeWebSocket.CLOSED));
  });

  it('socket 异常关闭（已收到服务帧）→ 流收尾，由上层按 terminalSeen 决定收尾还是重连', async () => {
    const reader = openStream();
    // 先收到服务帧 = WS 这条链路是通的：此后断流是「流本身的事」，交给上层重连
    //（换一条新 WS 更可能成功），不是传输不可用。
    socket.fire({ type: 'server_ping' });
    socket.fireClose();
    expect(await readEvent(reader)).toBeNull();
  });
});

/** 降级路径：WS 建连阶段一个服务帧都没收到 = 传输不可用（代理拒 Upgrade / CSP）。 */
describe('wsStreamResponse — WS 不可用时降级到 HTTP SSE', () => {
  /** 假 SSE 响应（真 Response + 真 ReadableStream：降级是**字节级搬运**，得用真流证）。 */
  function sseResponse(text: string, status = 200): Response {
    return new Response(text, {
      status,
      headers: { 'content-type': 'text/event-stream' },
    });
  }

  it('零服务帧 + onerror → 改走 GET /stream（携带同一游标），响应体原样搬进流', async () => {
    const sse = 'data: {"type":"text/delta","data":{"text":"hi"},"seq":3}\n\n';
    const fetchMock = vi.fn(async () => sseResponse(sse));
    vi.stubGlobal('fetch', fetchMock);

    const reader = openStream(7);
    socket.fireError();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());

    // 游标必须一致：降级补的是同一截，不能从头发（会把历史重放一遍）
    const url = String((fetchMock.mock.calls[0] as unknown[])[0]);
    expect(url).toContain('/api/sessions/s-1/stream?after_seq=7');
    expect((await readEvent(reader))?.seq).toBe(3);
    expect(await readEvent(reader)).toBeNull(); // 搬完即收尾

    // error/close 成对到来是常态：不能二次降级（第二次降级会把流搅乱）
    socket.fireClose();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it('#440：降级流每读到一段字节都通知活性观察者——keepalive 注释帧也算（done 不算）', async () => {
    // 后端 SSE keepalive 是注释帧（`: ping - <ts>`），parseFrame 只取 data: 行 ⇒
    // 永远不会成为事件。降级读循环若不把「读到字节」喂给活性侧信道，审批等待期
    // 的降级流 10s 后会被停摆看门狗误判成僵死（#440 的 SSE 半边）。
    const liveness = vi.fn();
    const enc = new TextEncoder();
    let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(c) {
              controller = c;
              c.enqueue(enc.encode('data: {"type":"run/started","seq":1}\n\n'));
            },
          }),
          { status: 200, headers: { 'content-type': 'text/event-stream' } },
        ),
      ),
    );

    const res = wsStreamResponse(sid, -1, liveness);
    const reader = res.body!.getReader();
    socket = FakeWebSocket.instances[0];
    socket.fireClose(); // 零服务帧 → 降级

    // 事件字节到达 = 活性（与 WS 侧「每条服务帧算活性」同一口径）
    expect((await readEvent(reader))?.type).toBe('run/started');
    expect(liveness).toHaveBeenCalledTimes(1);

    // 2s 后的 keepalive 注释帧：不成事件，但字节到达 = 链路活着的证据
    controller!.enqueue(enc.encode(': ping - 2026-09-30T00:00:02Z\n\n'));
    const chunk = await reader.read();
    expect(chunk.done).toBe(false);
    expect(new TextDecoder().decode(chunk.value!)).toContain(': ping');
    expect(liveness).toHaveBeenCalledTimes(2);

    // 流正常收口（done = 空 read）：什么都没证明——不得再刷活性（结束不误报）
    controller!.close();
    expect(await readEvent(reader)).toBeNull();
    expect(liveness).toHaveBeenCalledTimes(2);
  });

  it('零服务帧 + 直接 close（Upgrade 握手被拒）→ 同样降级', async () => {
    const fetchMock = vi.fn(async () => sseResponse('data: {"type":"run/started","seq":1}\n\n'));
    vi.stubGlobal('fetch', fetchMock);

    const reader = openStream();
    socket.fireClose(); // 没有 onerror，只有 close（握手被代理拒掉就是这个形状）

    expect((await readEvent(reader))?.type).toBe('run/started');
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it('#890：降级必须打 console.warn（否则现场只剩「直播卡住」，排障无从下手）', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse('data: {"type":"a","seq":1}\n\n')));

    const reader = openStream();
    socket.fireClose(); // 凭据/部署有问题时就是这个形状

    await vi.waitFor(() => expect(warn).toHaveBeenCalledTimes(1));
    expect(String(warn.mock.calls[0][0])).toContain('降级到 HTTP SSE');
    warn.mockRestore();
    void reader.cancel().catch(() => {});
  });

  it('#890：已收到服务帧后断流**不**打降级 warn（那是上层重连的事，不刷屏）', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const reader = openStream();
    socket.fire({ type: 'server_ping' });
    socket.fireClose();

    expect(await readEvent(reader)).toBeNull();
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it('连 WebSocket 都构造不出来（CSP / 非 WS 环境）→ 直接降级', async () => {
    vi.stubGlobal('WebSocket', class {
      constructor() {
        throw new Error('blocked by CSP');
      }
    });
    const fetchMock = vi.fn(async () => sseResponse('data: {"type":"run/started","seq":2}\n\n'));
    vi.stubGlobal('fetch', fetchMock);

    const res = wsStreamResponse(sid);
    const reader = res.body!.getReader();
    expect((await readEvent(reader))?.seq).toBe(2);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('降级本身也失败（网络故障）→ 流以错误收尾，不静默悬挂', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('network down');
    }));
    const reader = openStream();
    socket.fireError();

    await expect(reader.read()).rejects.toThrow(/SSE 降级请求也失败：network down/);
  });

  it('降级拿到非 2xx（如会话被删的 404）→ 流以错误收尾', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse('', 404)));
    const reader = openStream();
    socket.fireClose();

    await expect(reader.read()).rejects.toThrow(/SSE 降级 404/);
  });

  it('已收到服务帧后断流 → **不**降级（WS 是通的，由上层重连换新连接）', async () => {
    const fetchMock = vi.fn(async () => sseResponse('data: {"type":"a","seq":1}\n\n'));
    vi.stubGlobal('fetch', fetchMock);

    const reader = openStream();
    socket.fire({ type: 'server_ping' }); // 服务帧 = 链路可用
    socket.fireClose();

    expect(await readEvent(reader)).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('降级流被 cancel() → 真正中断那条 HTTP 请求（不留孤儿订阅）', async () => {
    // 攒包交付层上这条请求可能吊到 run 结束（实测 41.6s）：只置 closed 不 cancel，
    // 服务端的 run 订阅就会跟着多活一整轮——与 consumeSSE.cancel() 的契约不一致。
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(new TextEncoder().encode('data: {"type":"run/started","seq":1}\n\n'));
      },
      cancel() {
        cancelled = true;
      },
    });
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } })));

    const res = wsStreamResponse(sid);
    const seen: string[] = [];
    const handle = consumeSSE(res, (e) => seen.push(e.type), () => {});
    const sock = FakeWebSocket.instances[0];
    sock.fireOpen();
    sock.fireError(); // 零服务帧 → 降级
    // 等到降级流真的吐了一帧：此刻它正挂在**下一个 read** 上（这才是"cancel 时有
    // 读在飞"的形态——若在首个 read 之前就 cancel，循环自己的 closed 检查就够，
    // 测不出 reader 是否被中断）。
    await vi.waitFor(() => expect(seen).toEqual(['run/started']));

    handle.cancel();
    await vi.waitFor(() => expect(cancelled).toBe(true));
  });

  it('cancel() 之后 socket 关闭 → 不降级（切走会话不该再发一条 SSE 请求）', async () => {
    const fetchMock = vi.fn(async () => sseResponse(''));
    vi.stubGlobal('fetch', fetchMock);

    const res = wsStreamResponse(sid);
    const handle = consumeSSE(res, () => {}, () => {});
    const sock = FakeWebSocket.instances[0];
    sock.fireOpen();

    handle.cancel();
    sock.fireClose();
    sock.fireError();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('wsStreamResponse — 凭据走子协议（#890 P1-1）', () => {
  // 浏览器 `WebSocket` 构造器不能设 `Authorization` 头，子协议是唯一通道。
  // 后端前缀/业务子协议名见 `src/agent_harness/web/app.py` 的同名常量。
  const PREFIX = 'base64url.bearer.authorization.agent-harness.';
  const BUSINESS = 'agent-harness.v1';

  /** node 环境没有 localStorage（项目无 jsdom）：按 density.test.ts 的手法打桩。 */
  const store = new Map<string, string>();
  beforeEach(() => {
    store.clear();
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
  });

  /** 按后端口径（base64url 无 padding）算出期望的子协议值。
   *
   *  只用 DOM 全域（`TextEncoder` + `btoa`），与仓内既有做法一致（`auth.ts:74`、
   *  `wsStream.ts:110`）：`tsconfig.app.json` 的 `types` 只有 `vite/client`，
   *  没有 node 类型，`Buffer` 会直接把 gate0 的 `tsc -b` 车道打红。
   *  外部锚在下一行的字面量上（不是自己编自己解）。 */
  function expectBearerSubprotocol(token: string): string {
    const bytes = new TextEncoder().encode(token);
    let binary = '';
    for (const b of bytes) binary += String.fromCharCode(b);
    const encoded = btoa(binary)
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '');
    return PREFIX + encoded;
  }

  // 独立算出的锚（`base64.urlsafe_b64encode(...).rstrip('=')`，非本函数产出）：
  // 让「与后端逐字同形」这句声称有外部证据，而不是自己编自己解的空转。
  it('编码口径锚定在外部算出的值上', () => {
    expect(expectBearerSubprotocol('abc.def-ghi')).toBe(`${PREFIX}YWJjLmRlZi1naGk`);
    expect(expectBearerSubprotocol('令牌')).toBe(`${PREFIX}5Luk54mM`);
  });

  it('无 token（本地信任形态）→ 只发业务子协议，不造凭据通道', () => {
    wsStreamResponse(sid).body!.getReader();
    expect(FakeWebSocket.instances[0].protocols).toEqual([BUSINESS]);
  });

  it('有 token → 额外带上 base64url 编码的 bearer 子协议（与后端逐字同形）', () => {
    localStorage.setItem('ahi.apiToken', 'abc.def-ghi');
    wsStreamResponse(sid).body!.getReader();
    const protocols = FakeWebSocket.instances[0].protocols!;
    // 业务子协议必须**在**：后端永不回显 token 那个，浏览器请求了子协议却收不到
    // 回显会自己判握手失败。
    expect(protocols).toEqual([BUSINESS, expectBearerSubprotocol('abc.def-ghi')]);
  });

  it('base64url 无 padding：子协议值必须是合法 HTTP token 字符（不含 `=`/`+`/`/`）', () => {
    // padding 的 `=` 会被浏览器直接拒发这个子协议——编码方式不是风格问题。
    localStorage.setItem('ahi.apiToken', 'x'.repeat(10)); // 10 字节 ⇒ base64 有 padding
    wsStreamResponse(sid).body!.getReader();
    const bearer = FakeWebSocket.instances[0].protocols![1];
    expect(bearer).not.toMatch(/[=+/]/);
    expect(bearer.startsWith(PREFIX)).toBe(true);
  });

  it('非 ASCII token 也能编（btoa 只吃 Latin-1，故必须先过 TextEncoder）', () => {
    localStorage.setItem('ahi.apiToken', '令牌');
    wsStreamResponse(sid).body!.getReader();
    const bearer = FakeWebSocket.instances[0].protocols![1];
    expect(bearer).toBe(expectBearerSubprotocol('令牌'));
    expect(bearer).not.toMatch(/[=+/]/);
  });
});

describe('sessionIdBaseline / discoverNewSessionId', () => {
  function stubSessions(rows: unknown[]): void {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(rows), { status: 200 })),
    );
  }

  it('sessionIdBaseline 成功 → 现有会话 id 集合', async () => {
    stubSessions([{ session_id: 'a' }, { session_id: 'b' }]);
    expect(await sessionIdBaseline()).toEqual(new Set(['a', 'b']));
  });

  it('sessionIdBaseline 读不到 → **null**（不退化成空集）', async () => {
    // 空集在 discoverNewSessionId 眼里等于「库里本来什么都没有」，差分会把任意
    // 一条既有会话当成刚新建的那条——接流接错会话。读不到必须说读不到。
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('boom', { status: 500 })),
    );
    expect(await sessionIdBaseline()).toBeNull();
  });

  it('discoverNewSessionId 认领不在基线里的那一条（既有多条也不误认）', async () => {
    stubSessions([{ session_id: 'old-2' }, { session_id: 'old-1' }, { session_id: 'fresh' }]);
    expect(await discoverNewSessionId(new Set(['old-1', 'old-2']))).toBe('fresh');
  });

  it('discoverNewSessionId 基线为空 → 认领列表里最新的一条', async () => {
    stubSessions([{ session_id: 'only' }]);
    expect(await discoverNewSessionId(new Set())).toBe('only');
  });

  it('discoverNewSessionId 超时 → 抛 SessionClaimTimeoutError（不返回伪造的 id）', async () => {
    stubSessions([]);
    await expect(
      discoverNewSessionId(new Set(['x']), {
        timeoutMs: 300,
        baseDelayMs: 10,
        maxDelayMs: 40,
      }),
    ).rejects.toBeInstanceOf(SessionClaimTimeoutError);
  });

  it('慢响应 + 大列表：新会话晚 6 轮出现，仍在配置预算内认领（#1321）', async () => {
    // 每次列表往返 40ms、5000 条（含归档）——按比例缩小 1500ms 的量级，
    // 保持「预算内只跑得下若干轮」的形状；真实 1500ms 验收见票面记录。
    let calls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        calls += 1;
        await new Promise((r) => setTimeout(r, 40));
        const rows = Array.from({ length: 5000 }, (_, i) => ({ session_id: `old-${i}` }));
        if (calls >= 6) rows.push({ session_id: 'fresh' });
        return new Response(JSON.stringify(rows), { status: 200 });
      }),
    );

    await expect(
      discoverNewSessionId(
        new Set(Array.from({ length: 5000 }, (_, i) => `old-${i}`)),
        {
          timeoutMs: 2500,
          baseDelayMs: 20,
          maxDelayMs: 80,
        },
      ),
    ).resolves.toBe('fresh');
    expect(calls).toBeGreaterThanOrEqual(6);
  });

  it('默认预算与退避参数：不再是 6000ms 硬编码，上限可配置（#1321 AC1/AC2）', () => {
    expect(DEFAULT_SESSION_CLAIM_TIMEOUT_MS).toBeGreaterThan(6000);
    expect(CLAIM_MAX_DELAY_MS).toBeGreaterThan(CLAIM_BASE_DELAY_MS);
    // 指数增长：每轮上限翻倍（attempt 0/1/2 ⇒ 150/300/600）
    expect(claimBackoffMs(0, 150, 15_000, () => 0)).toBe(75);
    expect(claimBackoffMs(1, 150, 15_000, () => 0)).toBe(150);
    expect(claimBackoffMs(2, 150, 15_000, () => 0)).toBe(300);
    // equal jitter：同一轮的实际值落在 [cap/2, cap]
    expect(claimBackoffMs(2, 150, 15_000, () => 0.999)).toBeGreaterThan(449);
    expect(claimBackoffMs(2, 150, 15_000, () => 0.999)).toBeLessThanOrEqual(600);
    // 封顶
    expect(claimBackoffMs(20, 150, 1000, () => 1)).toBe(1000);
  });
});

/** `sessionExists`：重连接流失败时分辨「会话已删（404 语义）」与「瞬时故障」。
 *  它是 WS 传输下**唯一**口径一致的判据（WS 错误帧不带状态码、HTTP 404 被降级层
 *  抹平），判错的两个方向都真伤用户：误报已删 = 掐断一条能自愈的重连链 +
 *  说一句假话；漏报 = 对着已删会话空转重试。 */
describe('sessionExists — 404 语义的存在性判据', () => {
  function stubSessions(rows: unknown[] | 'fail'): void {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        rows === 'fail' ? new Response('boom', { status: 500 }) : new Response(JSON.stringify(rows)),
      ),
    );
  }

  it('在列表里（含**已归档**）→ true', async () => {
    stubSessions([{ session_id: 's-1', archived: true }, { session_id: 's-2' }]);
    expect(await sessionExists('s-1')).toBe(true);
    // 请求必须带 include_archived：漏掉它会把已归档会话误判成已删
    const call = (vi.mocked(fetch).mock.calls[0] as unknown[])[0] as string;
    expect(String(call)).toContain('include_archived=true');
  });

  it('不在列表里 → false（这才是「已删」）', async () => {
    stubSessions([{ session_id: 'other' }]);
    expect(await sessionExists('s-1')).toBe(false);
  });

  it('列表读不到 → **true**（保守：不把一次抖动当成已删）', async () => {
    stubSessions('fail');
    expect(await sessionExists('s-1')).toBe(true);
  });
});

/** #1333：会话认领差分误认——`first_event_time` 过滤 + 多候选 fail-closed。
 *
 * 红钉场景（票面复现思路的压缩版）：`GET /api/sessions` 每次往返有延迟、
 * 新会话延迟可见；在基线读取之后、认领轮询期间，让上一轮迟到的会话
 *（`other-0`，创建于本次 POST 之前）先于 `fresh-0` 可见。
 * 旧实现（纯集合差分）会返回 `other-0`；修复后必须过滤掉它、认领 `fresh-0`。 */
describe('discoverNewSessionId #1333 时间过滤与歧义 fail-closed', () => {
  const T0 = 1_750_000_000_000;
  const iso = (ms: number) => new Date(ms).toISOString();
  const fast = { timeoutMs: 8000, baseDelayMs: 20, maxDelayMs: 100, random: () => 0 };

  it('上一轮迟到的会话（first_event_time 早于本次提交）不得被认领', async () => {
    const start = Date.now();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        await new Promise((r) => setTimeout(r, 50));
        const rows = [
          { session_id: 'base-1', first_event_time: iso(T0 - 60_000) },
          // 迟到：创建于本次 POST 之前，但在基线读取之后才对列表可见
          { session_id: 'other-0', first_event_time: iso(T0 - 5_000) },
        ];
        // fresh-0 在提交 1200ms 后才可见（票面 12s 的压缩版）
        if (Date.now() - start >= 1200) {
          rows.push({ session_id: 'fresh-0', first_event_time: iso(T0 + 100) });
        }
        return new Response(JSON.stringify(rows), { status: 200 });
      }),
    );
    const sid = await discoverNewSessionId(new Set(['base-1']), {
      ...fast,
      submittedAtServerMs: T0,
    });
    expect(sid).toBe('fresh-0');
  });

  it('同轮出现多个候选 → 抛 SessionClaimAmbiguousError（不盲选第一条）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify([
            { session_id: 'base-1', first_event_time: iso(T0 - 60_000) },
            { session_id: 'other-0', first_event_time: iso(T0 + 50) },
            { session_id: 'fresh-0', first_event_time: iso(T0 + 100) },
          ]),
          { status: 200 },
        ),
      ),
    );
    let caught: unknown = null;
    try {
      await discoverNewSessionId(new Set(['base-1']), {
        ...fast,
        submittedAtServerMs: T0,
      });
      expect.unreachable('应抛 SessionClaimAmbiguousError');
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(SessionClaimAmbiguousError);
    // 票面要求"可识别的无法确认"：文案 + 候选 id 都在
    expect((caught as Error).message).toContain('无法确认');
    expect((caught as Error).message).toContain('other-0');
    expect((caught as Error).message).toContain('fresh-0');
    expect((caught as SessionClaimAmbiguousError).candidateIds).toEqual(
      expect.arrayContaining(['other-0', 'fresh-0']),
    );
  });

  it('first_event_time 恰等于提交时刻（>= 边界）→ 可认领', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify([
            { session_id: 'base-1', first_event_time: iso(T0 - 60_000) },
            { session_id: 'fresh-0', first_event_time: iso(T0) },
          ]),
          { status: 200 },
        ),
      ),
    );
    await expect(
      discoverNewSessionId(new Set(['base-1']), { ...fast, submittedAtServerMs: T0 }),
    ).resolves.toBe('fresh-0');
  });

  it('给了提交时刻但行没有 first_event_time → 不认领（等超时，不造假 id）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify([{ session_id: 'mystery' }]), { status: 200 }),
      ),
    );
    await expect(
      discoverNewSessionId(new Set(['base-1']), {
        timeoutMs: 400,
        baseDelayMs: 20,
        maxDelayMs: 60,
        random: () => 0,
        submittedAtServerMs: T0,
      }),
    ).rejects.toBeInstanceOf(SessionClaimTimeoutError);
  });

  it('不传 submittedAtServerMs → 旧行为（纯集合差分），#1321 旧调用不受影响', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify([{ session_id: 'base-1' }, { session_id: 'fresh' }]),
          { status: 200 },
        ),
      ),
    );
    // 旧签名：行没有 first_event_time 也照认（claimLoad 负载验收走这条路）
    await expect(discoverNewSessionId(new Set(['base-1']))).resolves.toBe('fresh');
  });
});

describe('sessionIdBaselineDetailed（#1333 服务端时钟锚点）', () => {
  const T0 = 1_750_000_000_000;

  it('从基线响应的 Date 头取服务端时刻', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify([{ session_id: 'a' }]), {
          status: 200,
          headers: { Date: new Date(T0).toUTCString() },
        }),
      ),
    );
    const d = await sessionIdBaselineDetailed();
    expect(d?.ids).toEqual(new Set(['a']));
    expect(d?.serverNowMs).toBe(T0);
  });

  it('响应没有 Date 头 → serverNowMs 为 null（调用方降级）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify([{ session_id: 'a' }]), { status: 200 })),
    );
    const d = await sessionIdBaselineDetailed();
    expect(d?.ids).toEqual(new Set(['a']));
    expect(d?.serverNowMs).toBeNull();
  });

  it('读不到 → null（与 sessionIdBaseline 同契约）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('boom', { status: 500 })),
    );
    expect(await sessionIdBaselineDetailed()).toBeNull();
  });
});
