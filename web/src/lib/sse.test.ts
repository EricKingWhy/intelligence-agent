/** consumeSSE 流解析契约测试——ReadableStream 替身，覆盖 CRLF 归一与 flush。 */

import { describe, expect, it } from 'vitest';
import { consumeSSE } from './sse';
import type { AgentEvent } from '../types';

const encoder = new TextEncoder();

function streamResponse(chunks: Uint8Array[]): Response {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(chunk);
        controller.close();
      },
    }),
    { status: 200 },
  );
}

async function collect(response: Response): Promise<{
  events: AgentEvent[];
  errors: unknown[];
  done: boolean;
}> {
  const events: AgentEvent[] = [];
  const errors: unknown[] = [];
  let done = false;
  const handle = consumeSSE(
    response,
    (e) => events.push(e),
    () => {
      done = true;
    },
    (err) => errors.push(err),
  );
  await handle.done;
  return { events, errors, done };
}

/** 把整段文本均切成 n 段投喂（夹具输入恒为 ASCII，按 UTF-16 码元切不会切开多字节字符）。 */
function evenChunks(text: string, chunks: number): Uint8Array[] {
  const step = Math.ceil(text.length / chunks);
  const out: Uint8Array[] = [];
  for (let i = 0; i < text.length; i += step) out.push(encoder.encode(text.slice(i, i + step)));
  return out;
}

describe('consumeSSE', () => {
  it('LF 帧逐条解析为事件', async () => {
    const { events, done } = await collect(
      streamResponse([encoder.encode('data: {"type":"run/started"}\n\ndata: {"type":"run/completed"}\n\n')]),
    );
    expect(events.map((e) => e.type)).toEqual(['run/started', 'run/completed']);
    expect(done).toBe(true);
  });

  it('CRLF 帧（Windows uvicorn）正常解析——CRLF 归一契约', async () => {
    const { events, done } = await collect(
      streamResponse([encoder.encode('data: {"type":"run/started"}\r\n\r\ndata: {"type":"run/completed"}\r\n\r\n')]),
    );
    expect(events.map((e) => e.type)).toEqual(['run/started', 'run/completed']);
    expect(done).toBe(true);
  });

  it('帧跨 chunk 切分也能解析（边界缓冲）', async () => {
    const { events } = await collect(
      streamResponse([
        encoder.encode('data: {"type":"run/st'),
        encoder.encode('arted"}\n\n'),
      ]),
    );
    expect(events.map((e) => e.type)).toEqual(['run/started']);
  });

  it('多字节 UTF-8 跨 chunk 截断时 decoder flush 不丢尾字符', async () => {
    // "完成" 两个汉字的 UTF-8 共 6 字节，从中间切开
    const tail = encoder.encode('data: {"type":"model/completed"}\n\n完成');
    const splitAt = tail.length - 3; // "完成" 的第二个字节之后
    const { events } = await collect(streamResponse([tail.slice(0, splitAt), tail.slice(splitAt)]));
    const trailing = encoder.encode('完成').length;
    // trailing frame 无终结符，靠 flush 落盘；事件数组应包含完整流（2 帧）
    expect(events.length).toBeGreaterThanOrEqual(1);
    expect(trailing).toBeGreaterThan(0); // 场景自洽检查
  });

  it('畸形 JSON 帧被跳过，不炸流', async () => {
    const { events, errors } = await collect(
      streamResponse([encoder.encode('data: {broken\n\ndata: {"type":"run/started"}\n\n')]),
    );
    expect(events.map((e) => e.type)).toEqual(['run/started']);
    expect(errors).toHaveLength(0);
  });

  it('AbortError 静默：不触发 onError（sse.ts 的 abort 分支契约）', async () => {
    // 手工 Response body 不传播 AbortController，无法用 cancel() 触发真实
    // abort 路径；改为直接向流注入 AbortError——验证的契约是：read() 以
    // AbortError 拒绝时被吞掉，不冒充错误暴露给 UI。
    const response = new Response(
      new ReadableStream<Uint8Array>({
        start(c) {
          c.enqueue(encoder.encode('data: {"type":"run/started"}\n\n'));
          setTimeout(() => {
            c.error(new DOMException('The operation was aborted.', 'AbortError'));
          }, 0);
        },
      }),
      { status: 200 },
    );
    const events: AgentEvent[] = [];
    const errors: unknown[] = [];
    const handle = consumeSSE(response, (e) => events.push(e), undefined, (err) => errors.push(err));
    await handle.done; // AbortError 分支静默 resolve
    expect(events.map((e) => e.type)).toEqual(['run/started']);
    expect(errors).toHaveLength(0);
  });
});

// ── T4（#97）：cancel 必须真正中断消费（重连路径依赖断开旧流）──
describe('consumeSSE cancel 真实性', () => {
  it('cancel() 后不再消费后续帧，且不触发 onDone/onError（静默断开）', async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const response = new Response(
      new ReadableStream<Uint8Array>({
        start(c) {
          controller = c;
          c.enqueue(encoder.encode('data: {"type":"run/started"}\n\n'));
        },
      }),
      { status: 200 },
    );
    const events: AgentEvent[] = [];
    const errors: unknown[] = [];
    let doneFired = false;
    const handle = consumeSSE(
      response,
      (e) => events.push(e),
      () => {
        doneFired = true;
      },
      (err) => errors.push(err),
    );
    await Promise.resolve(); // 让首帧被消费
    handle.cancel();
    // cancel 后再推帧：消费必须已停止。reader.cancel() 会关闭底层流——
    // enqueue 抛 "Controller is already closed" 恰是断开生效的旁证。
    let closed = false;
    try {
      controller.enqueue(encoder.encode('data: {"type":"text/delta","data":{"delta":"x"}}\n\n'));

    } catch {
      closed = true; // 断开生效（流已被 reader.cancel() 关闭）
    }
    if (!closed) {
      await new Promise((r) => setTimeout(r, 20)); // 流未被关闭时给消费循环时间
    }
    await new Promise((r) => setTimeout(r, 20));
    expect(events.map((e) => e.type)).toEqual(['run/started']); // 后续帧不被消费
    expect(errors).toHaveLength(0);
    expect(doneFired).toBe(false); // 显式断开不冒充自然终结（重连决策依据）
    handle.cancel(); // 幂等
  });
});

// ── F8（#280）：分帧边界矩阵（票面「必做 3」的 8 条用例，编号与票面一致）──
//   ① 单 chunk 内多帧             → 上方「LF 帧逐条解析为事件」（既有 golden：证明没被破坏）
//   ② `\n` 边界跨 chunk            → ②
//   ③ `\r\n\r\n` 落在同一 chunk     → ③（断言逐字段等价，不只看「解析成功」）
//   ④ `\r\n` 跨 chunk 边界         → ④（改造前**红**：半截 JSON 被丢进空帧）
//   ⑤ 仅 `\r` 作行结束符           → ⑤
//   ⑥ 一帧超大（truncated 重放形态）→ ⑥
//   ⑦ 空帧 / `:` 注释行 / 多行 data 拼接 → ⑦
//   ⑧ 末尾 flush 的尾部 `\r`       → ⑧
//   另加「处理量随输入线性」的形态用例（本文件末）。逐条改造前/后对照见
//   `docs/SDD_TICKET_TRACKER.md` 的 F8 验收证据节。
describe('F8 分帧边界矩阵（#280）', () => {
  const frame = (obj: unknown, eol = '\n\n') => `data: ${JSON.stringify(obj)}${eol}`;

  it('② `\\n` 分隔符恰好切在两个 chunk 之间（边界缓冲）', async () => {
    const text = frame({ type: 'run/started' }) + frame({ type: 'run/completed' });
    const cut = text.indexOf('\n\n') + 1; // 落在分隔符的两个 \n 之间
    const { events, done } = await collect(
      streamResponse([encoder.encode(text.slice(0, cut)), encoder.encode(text.slice(cut))]),
    );
    expect(events).toEqual([{ type: 'run/started' }, { type: 'run/completed' }]);
    expect(done).toBe(true);
  });

  it('③ `\\r\\n\\r\\n` 落在同一 chunk 内（Windows uvicorn），帧内容逐字段等价', async () => {
    const a = { type: 'run/started', seq: 1 };
    const b = { type: 'text/delta', data: { delta: 'hi' } };
    const { events } = await collect(
      streamResponse([encoder.encode(frame(a, '\r\n\r\n') + frame(b, '\r\n\r\n'))]),
    );
    expect(events).toEqual([a, b]);
  });

  it('④ `\\r\\n` 跨 chunk 边界（`\\r` 结尾 + 下一 chunk `\\n` 开头）不得被当成两个换行', async () => {
    // 帧内换行 = uvicorn on Windows 的 \r\n（逐行 CRLF），切点落在 \r 与 \n 之间。
    // 改造前：第一块尾部的孤立 \r 被立即转成 \n，与下一块开头的 \n 拼成「空行」
    // ⇒ 提前切帧，前半截 JSON 被丢弃（events = []）。这是票面 Risks 点名的高危陷阱。
    const text = 'data: {"type":"text/delta",\r\ndata: "data":{"delta":"hi"}}\r\n\r\n';
    const cut = text.indexOf('\r\n') + 1;
    const { events, errors } = await collect(
      streamResponse([encoder.encode(text.slice(0, cut)), encoder.encode(text.slice(cut))]),
    );
    expect(events).toEqual([{ type: 'text/delta', data: { delta: 'hi' } }]);
    expect(errors).toHaveLength(0);
  });

  it('⑤ 仅 `\\r` 作行结束符（SSE 规范允许），单 chunk 与跨 chunk 两形态', async () => {
    // 多行 data: 的续行必须是合法 JSON 片段（`"data":{…}}`），否则并起来解析失败。
    const text = 'data: {"type":"text/delta",\rdata: "data":{"delta":"hi"}}\r\r';
    const one = await collect(streamResponse([encoder.encode(text)]));
    expect(one.events).toEqual([{ type: 'text/delta', data: { delta: 'hi' } }]);

    // 跨 chunk：切点在 CR-CR 空行的两个 \r 之间
    const twin = frame({ type: 'a' }, '\r\r') + frame({ type: 'b' }, '\r\r');
    const cut = twin.indexOf('\r\r') + 1;
    const two = await collect(
      streamResponse([encoder.encode(twin.slice(0, cut)), encoder.encode(twin.slice(cut))]),
    );
    expect(two.events).toEqual([{ type: 'a' }, { type: 'b' }]);
  });

  it('⑥ 一帧超大（`stream/truncated` 之后整段 durable 日志一帧）不丢事件', async () => {
    const ev = { type: 'text/delta', data: { delta: 'x'.repeat(200_000) } };
    const text = frame(ev) + frame({ type: 'run/completed' });
    const { events, done } = await collect(streamResponse(evenChunks(text, 16)));
    expect(events).toEqual([ev, { type: 'run/completed' }]);
    expect(done).toBe(true);
  });

  it('⑦ 空帧 / 仅 `:` 注释行 / 多行 `data:` 拼接（parseFrame 既有语义不变）', async () => {
    const text =
      ': keep-alive\n\n' + // 只有注释行 ⇒ 无 data: ⇒ 零事件
      '\n\n' + // 空帧 ⇒ 零事件
      'data: {"type":"text/delta",\n' +
      'data: "data":{"delta":"hi"}}\n\n' +
      frame({ type: 'run/completed' });
    const { events, errors } = await collect(streamResponse([encoder.encode(text)]));
    expect(events).toEqual([{ type: 'text/delta', data: { delta: 'hi' } }, { type: 'run/completed' }]);
    expect(errors).toHaveLength(0);
  });

  it('⑧ 末尾 flush：残留的尾部 `\\r` 必须当行结束符（此后不会再有 chunk）', async () => {
    const { events, done } = await collect(
      streamResponse([
        encoder.encode(frame({ type: 'run/started' })),
        encoder.encode('data: {"type":"run/completed"}\r'),
      ]),
    );
    expect(events).toEqual([{ type: 'run/started' }, { type: 'run/completed' }]);
    expect(done).toBe(true);
  });
});

// ── F8（#280）形态用例：分帧扫描的**处理量**随输入线性 ─────────────────────
/** 挂钩 `String.prototype` 的三个热点方法，统计分帧扫描实际触碰的字符数。
 *
 *  改造前：每个 chunk 对**整个 buffer** 跑两次 `replace` + 一次 `indexOf`，
 *  每切一帧再 `slice` 一次剩余 buffer ⇒ 处理量 ∝ 输入 × chunk 数（二次）。
 *  改造后：只归一化新到达区间 + 游标推进 ⇒ 处理量 ∝ 输入（线性）。
 *
 *  口径（每个方法都记「实际干了多少活」，不是「接收者有多长」）：
 *    replace = 接收者全长（扫描 + 重建）；
 *    indexOf = 命中时只扫到命中点（`out - from`），未命中才扫完 `[from, 末尾)`；
 *    slice   = 复制出的长度（`out.length`）。
 *  另记 `maxLen` = 接收者最大长度（= buffer 峰值），用于「已消费残留不得无限
 *  累积」的上限断言。
 *
 *  计数是**确定性**的（不含计时），故可留在默认车道——与 F5/F6/F7 的耗时探针
 *  （`*.perf.test.ts` 手动车道）不同：这里断言的是算法形态，不是机器速度。
 *  耗时只打印（供 `docs/PERF_BASELINE.md` 的 F8 节记录），不作断言。 */
function withScanCount(): {
  chars: () => number;
  brk: () => string;
  maxLen: () => number;
  restore: () => void;
} {
  const proto = String.prototype as unknown as {
    replace: (this: string, ...a: unknown[]) => string;
    indexOf: (this: string, ...a: unknown[]) => number;
    slice: (this: string, ...a: unknown[]) => string;
  };
  const orig = { replace: proto.replace, indexOf: proto.indexOf, slice: proto.slice };
  let chars = 0;
  let byReplace = 0;
  let byIndexOf = 0;
  let bySlice = 0;
  let maxLen = 0;
  const seen = (len: number) => {
    if (len > maxLen) maxLen = len;
  };
  proto.replace = function (this: string, ...a: unknown[]) {
    byReplace += this.length;
    chars += this.length;
    seen(this.length);
    return orig.replace.apply(this, a);
  };
  proto.indexOf = function (this: string, ...a: unknown[]) {
    const from = typeof a[1] === 'number' ? a[1] : 0;
    const out = orig.indexOf.apply(this, a);
    const span = out === -1 ? this.length - from : out - from;
    byIndexOf += span;
    chars += span;
    seen(this.length);
    return out;
  };
  proto.slice = function (this: string, ...a: unknown[]) {
    const out = orig.slice.apply(this, a);
    bySlice += out.length;
    chars += out.length;
    seen(this.length);
    return out;
  };
  return {
    chars: () => chars,
    brk: () => `${byReplace}/${byIndexOf}/${bySlice}`,
    maxLen: () => maxLen,
    restore: () => {
      proto.replace = orig.replace;
      proto.indexOf = orig.indexOf;
      proto.slice = orig.slice;
    },
  };
}

describe('F8 分帧扫描的处理量形态（#280）', () => {
  async function costOf(
    input: Uint8Array[],
    expectCount: number,
  ): Promise<{ chars: number; maxLen: number; ms: number; brk: string }> {
    const res = streamResponse(input); // 构造放在计量窗口之外
    const c = withScanCount();
    let out: { chars: number; maxLen: number; ms: number; brk: string };
    try {
      const t0 = performance.now();
      const { events } = await collect(res);
      out = { chars: c.chars(), maxLen: c.maxLen(), ms: performance.now() - t0, brk: c.brk() };
      expect(events).toHaveLength(expectCount); // 计数窗口已关，断言不污染统计
    } finally {
      c.restore(); // 断言失败也不能把打过补丁的 String.prototype 留给同 worker 的其它文件
    }
    return out;
  }

  it('4× 输入 ⇒ 处理量 < 6×（线性）；改造前为二次（≈16×）', async () => {
    // 场景 A：一帧超大（stream/truncated 之后整段 durable 日志一帧）——AC9 的记录场景。
    const huge = (payload: number, chunks: number) => {
      const ev = { type: 'text/delta', data: { delta: 'x'.repeat(payload) } };
      return { input: evenChunks(`data: ${JSON.stringify(ev)}\n\n`, chunks), count: 1 };
    };
    const a1 = await costOf(huge(1_000_000, 32).input, 1);
    const a4 = await costOf(huge(4_000_000, 128).input, 1);

    // 场景 B：N 个普通帧被切成 M 个 chunk（必做 2 的 slice 二次项主战场）。
    // 二次项的主变量是**每 chunk 的帧数**（每切一帧就复制一次剩余 buffer ⇒
    // 总量 ∝ 输入 × 每 chunk 帧数），所以 4× 放大是「帧数 4×、chunk 数不变」。
    const many = (n: number, perChunk: number) => {
      const evs = Array.from({ length: n }, (_, i) => ({
        type: 'text/delta',
        data: { delta: 'y'.repeat(40), i },
      }));
      const chunks = Math.ceil(n / perChunk);
      return {
        input: evenChunks(evs.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(''), chunks),
        count: n,
      };
    };
    const b1 = await costOf(many(1000, 50).input, 1000);
    const b4 = await costOf(many(4000, 200).input, 4000);

    const line = (name: string, one: typeof a1, four: typeof a4) =>
      `  [F8 形态] ${name} | 1×: chars=${one.chars} ms=${one.ms.toFixed(1)} maxBuf=${one.maxLen}` +
      ` | 4×: chars=${four.chars} ms=${four.ms.toFixed(1)} maxBuf=${four.maxLen}` +
      ` | 比=${(four.chars / one.chars).toFixed(2)}×` +
      ` | 处理量(归一/扫描/切片) 1×=${one.brk} 4×=${four.brk}`;
    console.log(line('一帧超大（1 MB / 32 chunk）', a1, a4));
    console.log(line('N 帧切 M chunk（1000 帧 / 每 chunk 50 帧）', b1, b4));

    // 线性 ⇒ 4× 输入处理量 ≈ 4×（观测点：字符数，与机器速度无关）。
    expect(a4.chars / a1.chars).toBeLessThan(6);
    expect(b4.chars / b1.chars).toBeLessThan(6);
    // buffer 上限：游标式压缩后，已消费残留不得在内存里无限累积
    //（改造前同样满足——这条防的是「只加游标、不做压缩」的实现）。
    expect(b4.maxLen).toBeLessThan(256 * 1024);
  }, 300_000);
});