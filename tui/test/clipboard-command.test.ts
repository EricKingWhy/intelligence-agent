/**
 * 剪贴板子进程读取（#827 MM-06 / AC1）。
 *
 * COPY 自 Pi `packages/coding-agent/src/utils/clipboard-command.ts` @ `28dcce2b`。
 * 用 `process.execPath` 起真子进程断言三条外部行为：成功回字节、超时/非零退出
 * 回 undefined、超过上限回 undefined。不断言内部实现（spawn 参数等）。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { runClipboardCommand } from "../src/lib/clipboard-command.ts";

const NODE = process.execPath;

test("成功：标准输出原样回字节", async () => {
  const out = await runClipboardCommand(NODE, ["-e", "process.stdout.write('abc')"]);
  assert.equal(out?.toString("utf8"), "abc");
});

test("成功但无输出：回空 Buffer（空 ≠ 失败）", async () => {
  const out = await runClipboardCommand(NODE, ["-e", ""]);
  assert.notEqual(out, undefined);
  assert.equal(out?.length, 0);
});

test("非零退出：回 undefined（命令失败）", async () => {
  const out = await runClipboardCommand(NODE, ["-e", "process.exit(3)"]);
  assert.equal(out, undefined);
});

test("命令不存在：回 undefined（不抛）", async () => {
  const out = await runClipboardCommand("ia-tui-definitely-missing-cmd", []);
  assert.equal(out, undefined);
});

// 这两条用真子进程验超时/上限：被测代码的收口走 `execFile` 的 `timeout`/`maxBuffer`，
// 决定时长的是**操作系统进程**而不是本进程的时钟，没有可注入的假计时器可用
// （确定性时间控制在这里不成立）。子进程活 30s，超时 100ms ⇒ 断言 <3s 是粗上界
// 而非精确等待，不引入固定 sleep。
test("超时：按 timeout 收口并回 undefined", { timeout: 5000 }, async () => {
  const started = Date.now();
  const out = await runClipboardCommand(NODE, ["-e", "setTimeout(() => {}, 30000)"], {
    timeoutMs: 100,
  });
  assert.equal(out, undefined);
  assert.ok(Date.now() - started < 3000, "必须在超时点附近返回，而不是等子进程自己结束");
});

test("输出超过上限：放弃并回 undefined", async () => {
  const out = await runClipboardCommand(
    NODE,
    ["-e", "process.stdout.write('x'.repeat(4096))"],
    { maxBufferBytes: 64 },
  );
  assert.equal(out, undefined);
});
