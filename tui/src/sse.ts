/**
 * SSE 订阅（重连续传，ADR-0016 第 2.3 节 / 票面 seq 幂等铁律）：
 * - SeqCursor 是纯函数核心（去重 / 重建信号），可单测；
 * - StreamClient 是 IO 壳：fetch GET /api/sessions/{id}/stream?after_seq=N，
 *   断开由调用方按游标重连；网络层不维护第二份会话状态。
 */
import { parseEnvelope, parseTruncatedControl, type EventEnvelope } from "./events.ts";
/**
 * SSE 帧分隔符（空行）与行分隔符。
 *
 * 服务端 sse-starlette 的默认分隔符是 CRLF（`ServerSentEvent.DEFAULT_SEPARATOR
 * = "\r\n"`，`web/app.py` 的 `_sse_response` 没传 `sep`），线形是
 * `data: {...}\r\n\r\n`。只认裸 LF 的 `"\n\n"` 永远匹配不上：字节照收、流照常
 * 收尾重连，却一帧都切不出来，客户端静默空转（#854）。web 端 `web/src/lib/sse.ts`
 * 早有同一条结论（注释原话 "uvicorn on Windows does"），这里按同一线形事实匹配。
 *
 * 分隔符可能跨 chunk（`\r` 落在上一段、`\n` 落在下一段），所以只在**整个缓冲区**
 * 上匹配：不预先把 `\r\n` 归一化成 `\n`，也就不必为「尾部孤立 `\r`」再养一份 carry。
 */
const FRAME_SEPARATOR = /\r?\n\r?\n|\r\r/;
const LINE_BREAK = /\r\n|\r|\n/;

export type CursorDecision = "apply" | "skip" | "rebuild";

/**
 * seq 幂等游标：同一事实只投影一次。
 * - 重复 / 回放窗口内的 seq 跳过（无重复卡）；
 * - 无 seq 的 transient 帧不投影、不动游标；
 * - stream/truncated 控制帧不是运行事实（不变量 #4 边界），只触发全量重建。
 */
export class SeqCursor {
  private last = -1;

  next(frame: Pick<EventEnvelope, "seq" | "type">): CursorDecision {
    if (frame.seq === null) {
      return frame.type === "stream/truncated" ? "rebuild" : "skip";
    }
    if (frame.seq <= this.last) return "skip";
    this.last = frame.seq;
    return "apply";
  }

  get lastSeq(): number {
    return this.last;
  }

  /** 全量重建（GET /events）完成后回填真实 max seq。 */
  markRebuilt(maxSeq: number): void {
    if (maxSeq > this.last) this.last = maxSeq;
  }
}

export interface StreamHandlers {
  onFrame(frame: EventEnvelope): void;
  /** truncated 控制帧：调用方应 GET /events 重建，然后 cursor.markRebuilt(maxSeq)。 */
  onTruncated(latestSeqServerHint: number | null): void;
  /** 流干净收束（run 终态）或网络断开；调用方决定是否重连。 */
  onClosed(reason: "ended" | "error" | "truncated", error?: unknown): void;
}

export interface StreamOptions {
  signal?: AbortSignal;
  /** 注入 fetch（带 Bearer 的通道，见 host.ts）；缺省全局 fetch = 既有行为。 */
  fetchImpl?: typeof fetch;
}

/** 从 SSE 响应体逐帧解析（data: 行以空行分隔；只认 data: 前缀，帧内 JSON 单行）。 */
export async function consumeSseBody(
  body: ReadableStream<Uint8Array>,
  handlers: Pick<StreamHandlers, "onFrame" | "onTruncated">,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep: RegExpExecArray | null;
      while ((sep = FRAME_SEPARATOR.exec(buffer)) !== null) {
        const chunk = buffer.slice(0, sep.index);
        buffer = buffer.slice(sep.index + sep[0].length);
        for (const line of chunk.split(LINE_BREAK)) {
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          // 控制帧先于信封校验识别：它不带 time，过 parseEnvelope 会被当坏帧丢掉（#859）。
          const truncated = parseTruncatedControl(payload);
          if (truncated !== null) {
            handlers.onTruncated(truncated.latestSeq);
            continue;
          }
          const frame = parseEnvelope(payload);
          if (frame === null) continue;
          handlers.onFrame(frame);
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/** 打开一条重连续传流；resolve 于流收束（终态 / 断开 / truncated 后）。 */
export async function openStream(
  baseUrl: string,
  sessionId: string,
  cursor: SeqCursor,
  handlers: StreamHandlers,
  options: StreamOptions = {},
): Promise<void> {
  const url = `${baseUrl.replace(/\/$/, "")}/api/sessions/${encodeURIComponent(sessionId)}/stream?after_seq=${cursor.lastSeq}`;
  let response: Response;
  try {
    response = await (options.fetchImpl ?? fetch)(url, {
      headers: { accept: "text/event-stream" },
      signal: options.signal,
    });
  } catch (error) {
    handlers.onClosed("error", error);
    return;
  }
  if (!response.ok || response.body === null) {
    handlers.onClosed("error", new Error(`stream HTTP ${response.status}`));
    return;
  }
  try {
    await consumeSseBody(response.body, {
      onFrame: (frame) => {
        if (cursor.next(frame) === "apply") handlers.onFrame(frame);
      },
      onTruncated: (hint) => {
        handlers.onTruncated(hint);
        handlers.onClosed("truncated");
      },
    });
    handlers.onClosed("ended");
  } catch (error) {
    handlers.onClosed("error", error);
  }
}
