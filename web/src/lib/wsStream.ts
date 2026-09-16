/** WS 传输层的 live 流：把 `/api/ws` 多路复用通道包成「形如 SSE 的 Response」。
 *
 * ## 为什么必须走 WS（实测结论，非推测）
 *
 * 部署交付层（`Server: CloudStudio Gateway`，前置腾讯 EdgeOne）会把**整个 HTTP
 * 响应攒到流结束才下发**，GET / POST 一视同仁：
 *
 * | 通道 | 响应头到达 | 帧到达跨度 |
 * | --- | --- | --- |
 * | `POST /api/sessions` | 发出后 **44.158s**（≈ run 全长） | 326 帧全在 0.094s 内 |
 * | `GET /sessions/{id}/stream` | 发出后 **41.551s** | 326 帧全在 0.082s 内 |
 *
 * 后果在 `submitTask` 里非常具体：`await startSession(payload)` 在响应头到达前
 * 不会 resolve，而响应头要等 run 跑完——于是前端**在答案已经答完之后**才拿到第一帧，
 * 打字机效果不可能存在。
 *
 * WebSocket 是帧协议，交付层无法这样攒帧。同一交付链路上的实测：
 * - 服务端 2s 心跳准点到（3.91 / 5.91 / 7.91 / 9.91 / 11.91s）；
 * - 同一轮 run 的 `text/delta` 在 **28.69s 内逐帧到达**（1.65s → 30.35s），
 *   而并行的 POST SSE 仍是 32.9s 后一次性到齐。
 *
 * ## 设计边界（不变量守护）
 *
 * - 本模块**只是传输层**：事件真相仍来自 RunManager 订阅（PRD §5.1 / 不变量 #22），
 *   这里不解释、不聚合、不丢事件。
 * - 刻意把 WS 帧**重新编码成 SSE 文本**喂给既有的 `consumeSSE`，因此
 *   `attachLiveStream` / 合帧器 / seenSeqs 去重门 / 重连调度全部零改动——
 *   换掉的只有字节从哪来。
 * - seq 幂等：快照与 live 窗口会重叠（服务端先订阅后取游标），按 `seq` 去重
 *   （契约「重放帧与 live 帧做同一 seq 幂等投影」）。`model/started` /
 *   `model/delta` 是 stream-only（seq=null、从不落库），不会出现在快照里，
 *   故 null-seq 帧无需去重也不会重复。
 *
 * ## WS 不可用时：自动降级到 HTTP SSE
 *
 * WS 不是所有部署都能用（前置代理可能拒掉 Upgrade 握手）。**建连阶段一个服务帧
 * 都没收到**就失败 = 传输不可用 → 用同一个 `after_seq` 改走
 * `GET /sessions/{id}/stream`，把 SSE 响应体逐字节搬进本流。
 *
 * 两种失败**不**降级，因为它们恰恰证明 WS 是通的：
 * - 已经收到过服务帧（快照/事件/心跳）后断流 —— 那是流本身的问题，交给上层的
 *   重连状态机（换一条新 WS 更可能成功）；
 * - 服务端 `error` 帧（如会话不存在 / 快照失败）—— 同上。
 *
 * ⚠ **降级不是「能用」，是「不静默」**：交付层攒包时，降级流的帧同样要到 run 结束
 * 才到，而 `useSession` 的停摆检查（`RECONNECT_STALL_MS` = 10s 无帧 → 断开重连）
 * 会先把它判成停摆 → 重连 3 次 → 如实报「连接中断」。即：用户看到的是**明确失败**，
 * 不是永远转圈。（要让它「慢但能用」，得让停摆检查认识「已降级的传输」——那是
 * 传输策略问题，见 #208，不在本模块单方面改。）
 *
 * 反向的落差（#208，**已修**）：WS 快照曾**没有** backlog 上限、也不发
 * `stream/truncated`（那条保护只在 SSE `GET /stream` 里）——超大会话的一次重连
 * 会把整段 durable 日志塞进一个 WS 帧。后端现在按与 SSE **同一判据**处理，
 * 前提是本模块把游标上行（见 `onopen` 的 `after_seq`）；不带游标的客户端退回
 * 「全发 + 客户端过滤」的旧行为，不会因协议变更而坏。
 *
 * 握手成功但服务端**永不吐帧也不断开**（半死连接）不在本模块加 deadline：那会给
 * 慢链路引入假降级，而它已经被上层的停摆检查覆盖（10s → 重连 3 次 → 明确报错）。
 */

import type { AgentEvent } from '../types';
import { listSessions, streamSession } from './api';
// 终态集合**不另抄一份**：runState.ts 是「这个 run 收口了吗」的唯一判断点
// （T8 加 run/interrupted 时三处各自枚举集体漂移，见那里的注释）。
import { RUN_TERMINAL_TYPES } from './runState';

/** 抖动窗口：`done` 未及时到达时，收到终态事件后兜底收流。 */
const TERMINAL_FALLBACK_MS = 2500;

function wsEndpoint(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/api/ws`;
}

/**
 * 订阅一个 session 的 live 流，返回 SSE 形状的 `Response`。
 *
 * @param sessionId 目标会话
 * @param afterSeq  游标：≤ 该 seq 的事件不再下发给上层（-1 = 从头发）。用于接流/续传，
 *                  让快照只补「本地还没有的」那一截，避免历史被重复投影。
 */
export function wsStreamResponse(sessionId: string, afterSeq = -1): Response {
  const encoder = new TextEncoder();
  let sock: WebSocket | null = null;
  let closed = false;
  let maxSeq = afterSeq;
  let terminalTimer: ReturnType<typeof setTimeout> | null = null;
  /** 收到的**服务帧**数（快照/事件/心跳/错误都算）。0 = 建连就没成功过。 */
  let serverFrames = 0;
  /** 已切到 SSE 降级：旧 socket 的后续事件（error/close 会成对来）一律忽略。 */
  let fallbackStarted = false;
  /** 降级流的读句柄——`cancel()` 必须真正中断它（否则那条 HTTP 请求会吊到
   *  交付层放行，服务端的 run 订阅也跟着多活一整轮）。 */
  let sseReader: ReadableStreamDefaultReader<Uint8Array> | null = null;

  const teardown = () => {
    if (terminalTimer !== null) {
      clearTimeout(terminalTimer);
      terminalTimer = null;
    }
    const s = sock;
    sock = null;
    if (s && s.readyState !== WebSocket.CLOSED) {
      try {
        s.close();
      } catch {
        /* 关闭失败无意义 */
      }
    }
  };

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const settle = (err?: Error) => {
        if (closed) return;
        closed = true;
        teardown();
        try {
          if (err) controller.error(err);
          else controller.close();
        } catch {
          /* 上层已取消：controller 已失效，静默 */
        }
      };
      const emitRaw = (chunk: Uint8Array) => {
        if (closed) return;
        try {
          controller.enqueue(chunk);
        } catch {
          /* controller 已关闭 */
        }
      };
      const emit = (event: unknown) => {
        emitRaw(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
      };

      /** WS 建连阶段就失败（零服务帧）→ 降级到 HTTP SSE，字节原样搬运。
       *  只降级一次：失败后 `serverFrames` 置非零，再失败就走 settle。 */
      const fallbackToSse = async (cause: string) => {
        let res: Response;
        try {
          res = await streamSession(sessionId, afterSeq);
        } catch (e) {
          settle(new Error(`${cause}；SSE 降级请求也失败：${(e as Error).message}`));
          return;
        }
        if (!res.ok || !res.body) {
          settle(new Error(`${cause}；SSE 降级 ${res.status}`));
          return;
        }
        const reader = res.body.getReader();
        sseReader = reader;
        try {
          for (;;) {
            const { done, value } = await reader.read();
            // closed = 上层已 cancel：主动断掉 HTTP 请求（只 break 会把它留在
            // 后台读到交付层放行，白占一个服务端订阅）
            if (closed) {
              void reader.cancel().catch(() => {});
              return;
            }
            if (done) break;
            if (value) emitRaw(value);
          }
          settle();
        } catch (e) {
          settle(e instanceof Error ? e : new Error(String(e)));
        } finally {
          sseReader = null;
        }
      };

      /** 失败收口：零服务帧 = WS 传输不可用 → 降级；否则按流失败上报。 */
      const fail = (err: Error | null) => {
        if (closed || fallbackStarted) return;
        if (serverFrames > 0) {
          settle(err ?? undefined);
          return;
        }
        fallbackStarted = true;
        teardown();
        void fallbackToSse(err ? err.message : 'WebSocket 不可用');
      };

      try {
        sock = new WebSocket(wsEndpoint());
      } catch (e) {
        // 构造就抛（CSP / 非 WS 环境）：没有任何 socket 可关，直接降级
        fallbackStarted = true;
        void fallbackToSse((e as Error).message);
        return;
      }
      const s = sock;

      s.onopen = () => {
        // 游标必须上行（#208）：服务端据此只补 (after_seq, replay_upto] 那一截，
        // 并在 backlog 超阈值时改发 stream/truncated（与 SSE 同一判据）。
        // 不带游标时服务端只能按"总事件数"判定 —— 那会把"我已有全部历史、只差
        // 尾部"也判成超限，客户端重建后重新订阅仍然超限 ⇒ 重建-订阅死循环。
        s.send(JSON.stringify({ type: 'subscribe', session_id: sessionId, after_seq: afterSeq }));
      };

      s.onmessage = (msg: MessageEvent) => {
        if (closed) return;
        let frame: { type?: string; [k: string]: unknown };
        try {
          frame = JSON.parse(String(msg.data)) as typeof frame;
        } catch {
          return; // 非 JSON 帧（理论上不存在）：忽略，不污染流
        }
        serverFrames += 1;

        switch (frame.type) {
          case 'server_ping':
            // 应用层心跳（WS_PING_INTERVAL=2s；30s 无上行即被判死）
            try {
              s.send(JSON.stringify({ type: 'pong' }));
            } catch {
              /* 发送失败由 onclose 兜底 */
            }
            return;
          case 'snapshot': {
            const events = (frame.events as AgentEvent[] | undefined) ?? [];
            for (const ev of events) {
              if (typeof ev.seq === 'number') {
                if (ev.seq <= maxSeq) continue;
                maxSeq = ev.seq;
              }
              emit(ev);
            }
            // 服务端无在途 run：不会有 done（没有 relay task 在跑）——快照即全量
            if (frame.has_active_run === false) settle();
            return;
          }
          case 'event': {
            const ev = frame.event as AgentEvent | undefined;
            if (!ev) return;
            if (typeof ev.seq === 'number') {
              if (ev.seq <= maxSeq) return;
              maxSeq = ev.seq;
            }
            emit(ev);
            // 终态已投影；正常路径由服务端 done 收流，这里只做兜底
            if (RUN_TERMINAL_TYPES.has(ev.type) && terminalTimer === null) {
              terminalTimer = setTimeout(() => settle(), TERMINAL_FALLBACK_MS);
            }
            return;
          }
          case 'done':
            settle();
            return;
          case 'error':
            settle(new Error(String(frame.message ?? 'ws error')));
            return;
          default:
            return; // 未知下行（如 launched/queued/steered）：非本订阅关心
        }
      };

      s.onerror = () => fail(new Error('ws error'));
      // 异常关闭：不冒充自然终结——让 onStreamEnd 依 terminalSeen 决定收尾还是重连。
      // 建连阶段就关（握手被拒）走 fail → 降级；已在流的关闭按原语义收口。
      s.onclose = () => fail(null);
    },
    cancel() {
      // 上层 cancel()（导航离开 / 显式断开 / 重连前静默断开）
      closed = true;
      teardown();
      // 降级流另有一条 HTTP 请求在飞：一并中断，别留下孤儿订阅
      // （`consumeSSE.cancel()` 对 SSE 响应体就是这么做的，此处同契约）
      const r = sseReader;
      sseReader = null;
      if (r) void r.cancel().catch(() => {});
    },
  });

  return new Response(stream, {
    status: 200,
    headers: { 'content-type': 'text/event-stream' },
  });
}

/** 轮询会话列表，找出本轮新建的那个 session_id。
 *
 * ## 为什么需要它
 *
 * 交付层攒包导致 `POST /api/sessions` 的响应头要等 run 结束才到，**响应体里的
 * session_id 因此不可用于「立刻接流」**。而会话一旦创建就落进 durable 存储，
 * `GET /api/sessions`（普通 JSON、立即返回）能马上看到它——用「发起前基线 +
 * 差分」认出新建的那条，既不改动后端，也不影响 POST 上原有的全部字段语义
 * （模型 / 权限 / 校验一律照旧生效）。
 *
 * @param known     发起 POST **之前**已存在的会话 id 集合（基线）
 * @param timeoutMs 认领超时
 */
export async function discoverNewSessionId(
  known: ReadonlySet<string>,
  timeoutMs = 6000,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  let lastErr: unknown = null;
  while (Date.now() < deadline) {
    try {
      const rows = await listSessions({ includeArchived: true });
      // rows 通常已按时间倒序；仍全表扫以容忍排序变化
      const fresh = rows.find((r) => !known.has(r.session_id));
      if (fresh) return fresh.session_id;
    } catch (e) {
      lastErr = e;
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error(
    lastErr instanceof Error ? `未认领到新会话：${lastErr.message}` : '未认领到新会话（超时）',
  );
}

/** 取当前会话 id 基线（给 discoverNewSessionId 用）。
 *
 *  失败返回 `null`（**不**退化成空集）：空集在 `discoverNewSessionId` 眼里等于
 *  「库里本来一条会话都没有」，于是差分会把**任意一条既有会话**当成刚新建的那条
 *  ——接流接错会话，而且错得很自然（列表按活动时间倒序，最新那条正好是用户上次
 *  在用的）。读不到就说读不到，由调用方决定失败文案。
 *
 *  空集只有一种合法来源：基线确实为空（首次使用、库里还没有任何会话）。 */
export async function sessionIdBaseline(): Promise<Set<string> | null> {
  try {
    const rows = await listSessions({ includeArchived: true });
    return new Set(rows.map((r) => r.session_id));
  } catch {
    return null;
  }
}

/** 会话是否还在。重连接流失败时用来分辨两种失败：
 *
 *  - **会话已不存在** —— 重试多少次都是同样的结局（旧语义：HTTP 404 → 停止重连
 *    + 「会话不存在」文案，不空转）；
 *  - **瞬时故障** —— 退避重试有意义。
 *
 *  为什么需要它：WS 的错误帧只有一句 message（`snapshot failed` / id 非法），
 *  不含状态码；HTTP SSE 的 404 又会被降级层抹平成流错误。**存在性**是唯一
 *  在两套传输下口径一致的判据。
 *
 *  列表读不到时返回 `true`（当作还在）：把「读不到」误报成「已删」，会让一次
 *  列表抖动直接掐断一条本来能自愈的重连链。
 *
 *  判据口径与 `SessionService.list_sessions` 一致：不带过滤地全量列（含**已归档**，
 *  后端无分页/上限），所以「不在列表里」等价于「已删」。 */
export async function sessionExists(sessionId: string): Promise<boolean> {
  try {
    const rows = await listSessions({ includeArchived: true });
    return rows.some((r) => r.session_id === sessionId);
  } catch {
    return true;
  }
}
