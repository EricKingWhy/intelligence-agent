/**
 * 格式化移植（src/agent_harness/cli.py：_collapse_args / _format_tokens 同一语义，
 * 两个入口说同一件事）。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { collapseArgs, formatTokens, decimalRemaining } from "../src/format.ts";

test("collapseArgs：key=value，含空格字符串加引号，嵌套压 JSON", () => {
  assert.equal(collapseArgs({ command: "ls" }), "command=ls");
  assert.equal(collapseArgs({ path: "my file.txt" }), 'path="my file.txt"');
  assert.equal(collapseArgs({ opts: { deep: true } }), 'opts={"deep":true}');
  assert.equal(collapseArgs({}), "");
});

test("collapseArgs：超限截断", () => {
  const long = collapseArgs({ text: "x".repeat(300) });
  assert.ok(long.length < 300);
  assert.ok(long.endsWith("..."));
});

test("formatTokens：K/M 压缩；非法读数 = ?（不编数）", () => {
  assert.equal(formatTokens(500), "500");
  assert.equal(formatTokens(1500), "1.5k");
  assert.equal(formatTokens(2_500_000), "2.5M");
  assert.equal(formatTokens(null), "?");
  assert.equal(formatTokens(-1), "?");
});

test("decimalRemaining：十进制精确差并夹到 0", () => {
  assert.equal(decimalRemaining("1.50", "2.25"), "0.75");
  assert.equal(decimalRemaining(5, 10), "5");
  assert.equal(decimalRemaining("9.99", "1.00"), "0");
});

test("decimalRemaining：任一侧不可得返回 unavailable", () => {
  assert.equal(decimalRemaining(null, "2.25"), "unavailable");
  assert.equal(decimalRemaining("1.0", undefined), "unavailable");
  assert.equal(decimalRemaining("abc", "1.0"), "unavailable");
  assert.equal(decimalRemaining(true, "1.0"), "unavailable");
});
