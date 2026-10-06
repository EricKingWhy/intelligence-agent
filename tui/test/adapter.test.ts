/**
 * 事件 -> 视图模型映射（adapter）：turns 累积、工具卡状态机 1:1 服务端事件、
 * 审批队列、artifact 入口、run 四态可区分。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { applyEvent, createState, type ConversationState, type EventEnvelope } from "../src/adapter.ts";

function env(seq: number, type: string, data: Record<string, unknown>): EventEnvelope {
  return {
    type,
    data,
    seq,
    run_id: "r1",
    step_id: seq,
    session_id: "s1",
    time: "2026-10-06T00:00:00Z",
    durability: "durable",
  };
}

function applyAll(state: ConversationState, events: EventEnvelope[]): ConversationState {
  for (const e of events) applyEvent(state, e);
  return state;
}

test("user/message 建用户轮；text/delta 累积流式文本；model/completed 定稿", () => {
  const state = createState();
  applyAll(state, [
    env(1, "user/message", { content: "帮我看看日志" }),
    env(2, "text/delta", { delta: "正在" }),
    env(3, "text/delta", { delta: "分析" }),
    env(4, "model/completed", { content: "正在分析完成" }),
  ]);
  assert.equal(state.turns.length, 2);
  assert.equal(state.turns[0]?.role, "user");
  assert.equal(state.turns[0]?.text, "帮我看看日志");
  assert.equal(state.turns[1]?.role, "assistant");
  assert.equal(state.turns[1]?.text, "正在分析完成", "completed.content 定稿覆盖累积 delta");
});

test("无 completed 时流式文本保留累积值（真实数据，不伪造终稿）", () => {
  const state = createState();
  applyAll(state, [
    env(1, "user/message", { content: "hi" }),
    env(2, "text/delta", { delta: "部分回答" }),
  ]);
  assert.equal(state.turns[1]?.text, "部分回答");
});

test("tool/call -> running 卡；tool/result ok -> success 并解析 message", () => {
  const state = createState();
  applyAll(state, [
    env(1, "user/message", { content: "ls" }),
    env(2, "tool/call", { tool_call_id: "t1", tool_name: "bash", args: { command: "ls -l" } }),
    env(3, "tool/result", {
      tool_call_id: "t1",
      content: JSON.stringify({ ok: true, message: "file-a\nfile-b", metadata: { duration_ms: 1500 } }),
    }),
  ]);
  const turn = state.turns[1];
  assert.equal(turn?.tools.length, 1);
  const tool = turn?.tools[0];
  assert.equal(tool?.name, "bash");
  assert.equal(tool?.status, "success");
  assert.equal(tool?.ok, true);
  assert.ok(tool?.message.includes("file-a"));
  assert.equal(tool?.durationMs, 1.5);
});

test("tool/result 非 ok -> error；unparseable 内容原样保留不编造", () => {
  const state = createState();
  applyAll(state, [
    env(1, "tool/call", { tool_call_id: "t2", tool_name: "bash", args: {} }),
    env(2, "tool/result", { tool_call_id: "t2", content: "not json" }),
  ]);
  const tool = state.turns[0]?.tools[0];
  assert.equal(tool?.status, "error");
  assert.equal(tool?.ok, null);
  assert.equal(tool?.message, "not json");
});

test("tool/output_delta 追加到对应卡（channel 保留）", () => {
  const state = createState();
  applyAll(state, [
    env(1, "tool/call", { tool_call_id: "t3", tool_name: "bash", args: {} }),
    env(2, "tool/output_delta", { tool_call_id: "t3", delta: "chunk-1", channel: "stdout" }),
    env(3, "tool/output_delta", { tool_call_id: "t3", delta: "chunk-2", channel: "stdout" }),
  ]);
  const tool = state.turns[0]?.tools[0];
  assert.equal(tool?.output, "chunk-1chunk-2");
  assert.equal(tool?.status, "running");
});

test("approval 队列：requested 入队、resolved 幂等出队", () => {
  const state = createState();
  applyAll(state, [
    env(1, "tool/approval-requested", {
      approval_id: "a1", tool_name: "bash", tool_call_id: "t1",
      action_type: "execute", title: "运行命令", description: "rm -rf build",
      allowed_decisions: ["approve", "deny"],
    }),
    env(2, "tool/approval-requested", { approval_id: "a1", tool_name: "bash" }),
  ]);
  assert.equal(state.pendingApprovals.length, 1, "重放同 id 不重复入队");
  assert.equal(state.pendingApprovals[0]?.title, "运行命令");
  applyEvent(state, env(3, "permission/resolved", { approval_id: "a1", decision: "approve" }));
  assert.equal(state.pendingApprovals.length, 0);
  applyEvent(state, env(4, "permission/resolved", { approval_id: "a1", decision: "approve" }));
  assert.equal(state.pendingApprovals.length, 0, "重复决议不留痕不报错");
});

test("artifact/created 挂到宿主工具卡；找不到宿主不伪造", () => {
  const state = createState();
  applyAll(state, [
    env(1, "tool/call", { tool_call_id: "t9", tool_name: "export", args: {} }),
    env(2, "artifact/created", { artifact_id: "art-1", tool_call_id: "t9", size: 1234, mime_type: "text/plain" }),
    env(3, "artifact/created", { artifact_id: "art-2", tool_call_id: "ghost", size: 1, mime_type: null }),
  ]);
  const tool = state.turns[0]?.tools[0];
  assert.deepEqual(tool?.artifact, {
    artifact_id: "art-1", size: 1234, mime_type: "text/plain", source_tool: null,
  });
  assert.equal(state.orphanArtifacts.length, 1, "无宿主的产物以入口占位保留真实 id");
  assert.equal(state.orphanArtifacts[0]?.artifact_id, "art-2");
});

test("run 四态可区分：running / paused / completed / failed", () => {
  const paused = createState();
  applyEvent(paused, env(1, "run/paused", { reason: "client_absent", budget_version: 1 }));
  assert.equal(paused.runStatus, "paused");
  assert.equal(paused.pauseInfo?.reason, "client_absent");

  const resumed = createState();
  applyEvent(resumed, env(1, "run/paused", { reason: "client_absent", budget_version: 1 }));
  applyEvent(resumed, env(2, "run/resumed", { resume_basis: "client_return", budget_version: 2 }));
  assert.equal(resumed.runStatus, "running");
  assert.equal(resumed.pauseInfo, null);

  const done = createState();
  applyEvent(done, env(1, "run/completed", { usage_total: { prompt_tokens: 10, completion_tokens: 5 } }));
  assert.equal(done.runStatus, "completed");
  assert.deepEqual(done.usageTotal, { prompt_tokens: 10, completion_tokens: 5 });

  const failed = createState();
  applyEvent(failed, env(1, "run/failed", { reason: "cancelled" }));
  assert.equal(failed.runStatus, "failed");
  assert.equal(failed.failReason, "cancelled");
});

test("run/resumed 更新恢复快照；来源 seq 留痕（在场协议重连展示）", () => {
  const state = createState();
  applyEvent(state, env(7, "run/paused", { reason: "client_absent", budget_version: 4 }));
  applyEvent(state, env(9, "run/resumed", { resume_basis: "client_return", from_pause_seq: 7, budget_version: 5 }));
  assert.equal(state.lastResume?.fromPauseSeq, 7);
  assert.equal(state.lastResume?.basis, "client_return");
});
