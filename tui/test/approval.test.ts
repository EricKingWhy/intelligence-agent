/**
 * 批准决策纯函数（票面：TTY 内 y/N，非 TTY 默认拒绝——审批拒绝不可绕过）。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { approvalDecision } from "../src/views/approval.ts";

test("TTY：y 批准 / n 拒绝", () => {
  assert.equal(approvalDecision(true, "y"), "approved");
  assert.equal(approvalDecision(true, "n"), "denied");
});

test("TTY：未按键继续等待", () => {
  assert.equal(approvalDecision(true, null), "wait");
});

test("非 TTY：默认拒绝，不留悬空审批", () => {
  assert.equal(approvalDecision(false, null), "denied");
  // 即便用户在管道里敲了别的字符也不放行
  assert.equal(approvalDecision(false, "other"), "denied");
});
