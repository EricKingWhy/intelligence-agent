/** SSE client using fetch + ReadableStream — no EventSource polyfill needed.
 *
 * Why not EventSource: EventSource is GET-only, doesn't support POST body.
 * Our backend streams from POST /api/sessions, so we parse SSE manually.
 *
 * Spec compliance (spec 11 §4): on disconnect we MUST stop consuming and
 * let the producer cancel — call abort() to break the stream.
 */

import type { AgentEvent } from '../types';

export interface SSEHandle {
  /** Resolves when stream ends (run/completed, run/failed, or error). */
  done: Promise<void>;
  /** Abort the stream — used on unmount or manual cancel. */
  cancel: () => void;
}

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
  const controller = new AbortController();
  const reader = response.body?.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  if (!reader) {
    throw new Error('Response has no body — SSE requires a ReadableStream body.');
  }

  const done = (async () => {
    try {
      while (true) {
        const { value, done: streamDone } = await reader.read();
        if (streamDone) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line. The spec allows \n, \r\n, or \r
        // line endings, so normalize CRLF → LF before searching for '\n\n'.
        // Without this, a server emitting \r\n\r\n (uvicorn on Windows does)
        // never matches a hard-coded '\n\n' boundary and every frame hangs.
        buffer = buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n');

        // Process every complete frame currently in the buffer.
        let sep: number;
        while ((sep = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          parseFrame(frame).forEach(onEvent);
        }
      }
      // Flush any trailing frame without a terminator.
      if (buffer.trim()) {
        parseFrame(buffer).forEach(onEvent);
      }
      onDone?.();
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        onError?.(err);
      }
    }
  })();

  return {
    done,
    cancel: () => controller.abort(),
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
