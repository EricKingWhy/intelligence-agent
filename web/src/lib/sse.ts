/** SSE client using fetch + ReadableStream — no EventSource polyfill needed.
 *
 * Why not EventSource: EventSource is GET-only, doesn't support POST body.
 * Our backend streams from POST /api/sessions, so we parse SSE manually.
 *
 * ADR-0016 detached-run 修订（T5 #98）：cancel() 只断传输层（unsubscribe）——
 * run 服务端继续跑到终态，显式取消走 POST /cancel（useSession cancelStream）。
 * abort 保留给导航离开/卸载时的订阅清理。
 */

import type { AgentEvent } from '../types';

export interface SSEHandle {
  /** Resolves when stream ends (run/completed, run/failed, or error). */
  done: Promise<void>;
  /** Abort the stream — used on unmount or manual cancel. */
  cancel: () => void;
}

/** 已消费前缀超过该长度就整体压缩一次（`slice` 的唯一复制点，见 `drain`）。 */
const COMPACT_THRESHOLD = 64 * 1024;

/**
 * Parse SSE chunks from a fetch Response body, invoke onEvent for each parsed
 * data: frame (JSON-decoded). Returns a handle to cancel.
 */
export function consumeSSE(
  response: Response,
  onEvent: (event: AgentEvent) => void,
  onDone?: () => void,
  onError?: (err: unknown) => void,
): SSEHandle {
  const reader = response.body?.getReader();
  const decoder = new TextDecoder();
  let buffer = ''; // 已归一化、尚未切帧消费的文本
  let readPos = 0; // 下一个待切帧的起点（消费游标）
  let scanFrom = 0; // 下一个 `\n\n` 可能的起点（≥ readPos，跨 chunk 只需回看 1 个字符）
  let carry = ''; // 尚未归一化的原始尾部（只可能是孤立的 '\r'）
  // T4（#97）：cancel 必须真正中断消费——重连路径（seq gap / stream/truncated /
  // 停摆）依赖断开旧流。此前 AbortController 从未接入 fetch，cancel 是空操作，
  // 靠上游 modeRef 守卫兜住 UI 才未暴露。reader.cancel() 使挂起 read() 立即
  // 落定，cancelled 标记保证静默断开（不冒充 onDone 自然终结——那是重连决策依据）。
  let cancelled = false;

  if (!reader) {
    throw new Error('Response has no body — SSE requires a ReadableStream body.');
  }

  /** 切出 buffer 里所有完整帧（空行分隔），游标推进；只在这里压缩一次 buffer。
   *
   *  F8（#280）：原实现每帧 `buffer = buffer.slice(sep + 2)` ⇒ 每个 chunk 里
   *  F 帧要复制 F 次剩余 buffer，总量 ∝ 输入 × 每 chunk 帧数。改成读位置指针后，
   *  复制只发生在已消费前缀 ≥ 阈值时——两次压缩之间必然已消费 ≥ 阈值字节，
   *  故压缩次数 ≤ 输入/阈值；而「未消费尾巴很大」只可能是一帧未结束（后续帧都
   *  排在它后面，游标不会前进），此时不会触发压缩，不存在反复搬大尾巴的路径。 */
  const drain = () => {
    let sep: number;
    while ((sep = buffer.indexOf('\n\n', scanFrom)) !== -1) {
      parseFrame(buffer.slice(readPos, sep)).forEach(onEvent);
      readPos = sep + 2;
      scanFrom = readPos;
    }
    // 本轮没找到分隔符：下一个可能的起点是「尾部最后一个字符」——它可能是跨界
    // `\n\n` 的前半个。不这样记账，一帧超大时每个 chunk 都要重扫整个 buffer。
    scanFrom = buffer.length > readPos ? buffer.length - 1 : readPos;
    if (readPos >= COMPACT_THRESHOLD) {
      buffer = buffer.slice(readPos);
      scanFrom -= readPos;
      readPos = 0;
    }
  };

  const done = (async () => {
    try {
      while (true) {
        const { value, done: streamDone } = await reader.read();
        if (streamDone || cancelled) break;
        const text = carry + decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line. The spec allows \n, \r\n, or \r
        // line endings, so normalize CRLF → LF before searching for '\n\n'.
        // Without this, a server emitting \r\n\r\n (uvicorn on Windows does)
        // never matches a hard-coded '\n\n' boundary and every frame hangs.
        //
        // F8（#280）：归一化改为**只处理新到达区间**（上面的 `carry` + 本次解码
        // 结果），不再每个 chunk 对整个 buffer 跑两遍正则。尾部孤立的 '\r' 必须
        // 留给下一个 chunk 判定——否则「chunk 以 \r 结尾 + 下一 chunk 以 \n 开头」
        // 会被当成一个空行，提前切帧、丢事件（④）。
        carry = text.endsWith('\r') ? '\r' : '';
        const region = carry ? text.slice(0, -1) : text;
        buffer += region.replace(/\r\n/g, '\n').replace(/\r/g, '\n');

        drain();
      }
      if (cancelled) return;
      // Flush the decoder's pending multi-byte sequence, then any trailing frame.
      // 流已结束 ⇒ 残留的尾部 '\r' 就是行结束符（不会再有 chunk 来配对）。
      buffer += (carry + decoder.decode()).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
      carry = '';
      drain();
      const tail = buffer.slice(readPos);
      if (tail.trim()) {
        parseFrame(tail).forEach(onEvent);
      }
      onDone?.();
    } catch (err) {
      if (!cancelled && (err as Error).name !== 'AbortError') {
        onError?.(err);
      }
    }
  })();

  return {
    done,
    cancel: () => {
      if (cancelled) return;
      cancelled = true;
      void reader.cancel().catch(() => {});
    },
  };
}

/** Parse a single SSE frame (multi-line "data:" fields) into AgentEvents. */
function parseFrame(frame: string): AgentEvent[] {
  const events: AgentEvent[] = [];
  const lines = frame.split('\n');
  const dataLines: string[] = [];

  for (const line of lines) {
    if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trim());
    }
    // event:/id:/retry: ignored for our use case
  }

  if (dataLines.length === 0) return events;
  const payload = dataLines.join('\n');

  try {
    const parsed = JSON.parse(payload);
    if (parsed && typeof parsed === 'object' && 'type' in parsed) {
      events.push(parsed as AgentEvent);
    }
  } catch {
    // Malformed JSON — skip frame, don't crash the stream.
  }
  return events;
}
