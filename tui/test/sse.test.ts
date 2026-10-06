/**
 * seq 幂等去重核心（票面：断网/重启后 seq 回放幂等、无重复卡）。
 * 纯函数层，不触网；IO 壳在 src/sse.ts。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { SeqCursor } from "../src/sse.ts";

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
