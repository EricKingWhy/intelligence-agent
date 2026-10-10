/**
 * seq 幂等去重核心（票面：断网/重启后 seq 回放幂等、无重复卡）。
 * 纯函数层，不触网；IO 壳在 src/sse.ts。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { SeqCursor, consumeSseBody } from "../src/sse.ts";
import type { EventEnvelope } from "../src/events.ts";

function frame(seq: number | null, type = "text/delta") {
  return {
    type,
    data: {},
    seq,
    run_id: null,
    step_id: null,
    session_id: "s1",
    time: "2026-10-06T00:00:00Z",
    durability: seq === null ? "transient" : "durable",
  };
}

test("首帧 seq 应用并推进游标", () => {
  const cursor = new SeqCursor();
  assert.equal(cursor.next(frame(1)), "apply");
  assert.equal(cursor.lastSeq, 1);
});

test("重复 seq 跳过（幂等，无重复卡）", () => {
  const cursor = new SeqCursor();
  cursor.next(frame(3));
  assert.equal(cursor.next(frame(3)), "skip");
  assert.equal(cursor.next(frame(2)), "skip");
  assert.equal(cursor.lastSeq, 3);
});

test("seq 回放窗口内的帧全部去重，新帧继续应用", () => {
  const cursor = new SeqCursor();
  for (const seq of [1, 2, 3]) cursor.next(frame(seq));
  // 断线重连后服务端重放 after_seq 附近窗口，可能交叠
  assert.equal(cursor.next(frame(2)), "skip");
  assert.equal(cursor.next(frame(3)), "skip");
  assert.equal(cursor.next(frame(4)), "apply");
  assert.equal(cursor.next(frame(9)), "apply");
  assert.equal(cursor.lastSeq, 9);
});

test("无 seq 的 transient 帧不投影也不动游标", () => {
  const cursor = new SeqCursor();
  cursor.next(frame(5));
  assert.equal(cursor.next(frame(null, "model/started")), "skip");
  assert.equal(cursor.lastSeq, 5);
});

test("stream/truncated 控制帧触发全量重建", () => {
  const cursor = new SeqCursor();
  cursor.next(frame(5));
  assert.equal(cursor.next(frame(null, "stream/truncated")), "rebuild");
  assert.equal(cursor.lastSeq, 5, "控制帧不是运行事实，不参与游标");
});

test("markRebuilt：全量重建后游标取真实 max seq", () => {
  const cursor = new SeqCursor();
  cursor.markRebuilt(42);
  assert.equal(cursor.lastSeq, 42);
  assert.equal(cursor.next(frame(42)), "skip", "重建覆盖的 seq 不重复应用");
  assert.equal(cursor.next(frame(43)), "apply");
});


// ---------------------------------------------------------------------------
// 帧切分（IO 壳 consumeSseBody）。
//
// 夹具是**真实服务抓下来的原始字节**，不是照客户端假设手写的形状：服务端
// sse-starlette 的帧分隔符是 CRLF（`ServerSentEvent.DEFAULT_SEPARATOR = "\r\n"`，
// `_sse_response` 没有传 `sep`）。只认裸 LF 就一帧都切不出来——字节照收、流照常
// 收尾并重连，但永远没有帧，也不报错（#854：附着后收不到任何 run 的直播帧）。
// ---------------------------------------------------------------------------

/** 真实 `POST /api/sessions` 响应的前两帧（2026-10-08 抓取，原样）。 */
const REAL_FRAME_USER_MESSAGE =
  'data: {"type": "user/message", "data": {"content": "抓原始字节"}, "seq": 2, "run_id": null, "step_id": null, "session_id": "cad0044f-6d09-4419-a64f-584b09dc0937", "time": "2026-10-08T08:18:21.259+00:00", "schema_version": "runtime_event/v1", "durability": "durable"}';
const REAL_FRAME_RUN_STARTED =
  'data: {"type": "run/started", "data": {"turn_index": 1, "agent_profile": "main", "model": "deepseek-chat"}, "seq": 3, "run_id": "70add1d1-9c8e-4f13-b574-d4acc89942d5", "step_id": null, "session_id": "cad0044f-6d09-4419-a64f-584b09dc0937", "time": "2026-10-08T08:18:21.300+00:00", "schema_version": "runtime_event/v1", "durability": "durable"}';

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder();
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function drainStream(
  chunks: string[],
): Promise<{ frames: EventEnvelope[]; truncated: number }> {
  const frames: EventEnvelope[] = [];
  let truncated = 0;
  await consumeSseBody(streamOf(chunks), {
    onFrame: (frame) => frames.push(frame),
    onTruncated: () => {
      truncated += 1;
    },
  });
  return { frames, truncated };
}

test("CRLF 分隔的真实服务字节必须切出全部帧（#854）", async () => {
  const { frames } = await drainStream([
    `${REAL_FRAME_USER_MESSAGE}\r\n\r\n${REAL_FRAME_RUN_STARTED}\r\n\r\n`,
  ]);
  assert.deepEqual(
    frames.map((f) => f.type),
    ["user/message", "run/started"],
  );
  assert.deepEqual(
    frames.map((f) => f.seq),
    [2, 3],
  );
  assert.equal(frames[0]?.session_id, "cad0044f-6d09-4419-a64f-584b09dc0937");
});

test("帧分隔符跨 chunk 边界（\r 与 \n 分属两段）仍能切帧", async () => {
  const { frames } = await drainStream([
    `${REAL_FRAME_USER_MESSAGE}\r\n\r`,
    `\n${REAL_FRAME_RUN_STARTED}\r\n\r\n`,
  ]);
  assert.deepEqual(
    frames.map((f) => f.seq),
    [2, 3],
  );
});

test("裸 LF 分隔仍可用（回归：不能改成只认 CRLF）", async () => {
  const { frames } = await drainStream([
    `${REAL_FRAME_USER_MESSAGE}\n\n${REAL_FRAME_RUN_STARTED}\n\n`,
  ]);
  assert.deepEqual(
    frames.map((f) => f.seq),
    [2, 3],
  );
});

test("ping 注释行不产生帧（CRLF 与 LF 两种形状）", async () => {
  const crlf = await drainStream([
    ": ping - 2026-10-08T08:18:21Z\r\n\r\n",
    `${REAL_FRAME_USER_MESSAGE}\r\n\r\n`,
  ]);
  assert.deepEqual(
    crlf.frames.map((f) => f.seq),
    [2],
  );
  const lf = await drainStream([
    ": ping - 2026-10-08T08:18:21Z\n\n",
    `${REAL_FRAME_USER_MESSAGE}\n\n`,
  ]);
  assert.deepEqual(
    lf.frames.map((f) => f.seq),
    [2],
  );
});

// ---------------------------------------------------------------------------
// stream/truncated 控制帧（#859）。
//
// 夹具是**真实服务抓下来的原始字节**（#859 票面探针：`STREAM_REPLAY_MAX_EVENTS=5`
// 后 `GET /stream?after_seq=0`）。服务端 `build_truncated_control` 按设计**不带 `time`**
// ——它不是运行事实（不变量 #4 边界）。旧夹具照客户端假设给它补了 `time`，
// 于是 `parseEnvelope` 丢帧、`onTruncated` 永不触发这件事一直测不出来。
// ---------------------------------------------------------------------------

/** 真实 backlog 超限响应的唯一一帧（#859 票面，原样；keys 无 `time`）。 */
const REAL_FRAME_TRUNCATED =
  'data: {"type": "stream/truncated", "data": {"after_seq": 0, "latest_seq": 17}, "seq": null, "run_id": null, "step_id": null, "session_id": "18ed3001-aa67-4ab6-b889-d3935349a320", "schema_version": "runtime_event/v1", "durability": "transient"}';

test("不带 time 的真实 stream/truncated 帧必须触发 onTruncated（#859）", async () => {
  const hints: (number | null)[] = [];
  const frames: EventEnvelope[] = [];
  await consumeSseBody(streamOf([`${REAL_FRAME_TRUNCATED}\r\n\r\n`]), {
    onFrame: (frame) => frames.push(frame),
    onTruncated: (hint) => hints.push(hint),
  });
  assert.deepEqual(hints, [17], "控制帧必须到达重建分支，并带回 latest_seq 提示");
  assert.equal(frames.length, 0, "控制帧不是运行事实，不得投影");
});

test("运行事实仍须带 time：缺 time 的普通事件照旧按坏帧丢弃（#859 不放宽）", async () => {
  const noTime = REAL_FRAME_USER_MESSAGE.replace(/, "time": "[^"]*"/, "");
  assert.notEqual(noTime, REAL_FRAME_USER_MESSAGE, "夹具必须真的去掉了 time");
  const { frames, truncated } = await drainStream([
    `${noTime}\r\n\r\n${REAL_FRAME_RUN_STARTED}\r\n\r\n`,
  ]);
  assert.deepEqual(
    frames.map((f) => f.seq),
    [3],
    "缺 time 的运行事实丢弃，带 time 的照常投影",
  );
  assert.equal(truncated, 0);
});
