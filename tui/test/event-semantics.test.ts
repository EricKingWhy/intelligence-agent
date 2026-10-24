/**
 * #1498 第二步：跨端纯事件语义合并后的 TUI 侧验收（F7-2 / F7-3）。
 *
 * 夹具取自票面「卡内事实 2」的实测：
 *   terminal fixture `{"tuiRun":"interrupted","tuiTool":"running","webTool":"stopped"}`
 *     —— 中断终态下未配对工具，TUI 曾经永远显示「运行中」，Web 显示 stopped；
 *   supersede fixture `{"tui":["old","old answer","corrected"],"webShadowed":[true,false]}`
 *     —— TUI 曾经忽略 `message/superseded`，被修订掉的输入与旧回答仍作现行显示。
 *
 * 两个 bug 都是「一个客户端修了、另一个不知道」的产物，所以修复必须落在那份
 * 共享的纯规则上（`src/shared/event-semantics.ts`），而不是再各写一遍。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { applyEvent, createState, visibleTurns, type EventEnvelope } from "../src/adapter.ts";
import { RUN_TERMINAL_TYPES } from "../src/shared/event-semantics.ts";

function env(seq: number, type: string, data: Record<string, unknown> = {}): EventEnvelope {
  return {
    type,
    data,
    seq,
    run_id: "r1",
    step_id: seq,
    session_id: "s1",
    time: "2026-10-22T00:00:00Z",
    durability: "durable",
  };
}

function applyAll(events: EventEnvelope[]) {
  const state = createState();
  for (const e of events) applyEvent(state, e);
  return state;
}

// ── F7-2：终态清扫（未配对工具必须离开 running）────────────────────────────

test("F7-2 票面夹具：run/interrupted 后未配对工具是 stopped（不再显示运行中）", () => {
  const state = applyAll([
    env(1, "user/message", { content: "跑一下" }),
    env(2, "tool/call", { tool_call_id: "t1", tool_name: "bash", args: {} }),
    env(3, "run/interrupted", { interrupted_seq: 2, reason: "process_restart" }),
  ]);
  assert.equal(state.runStatus, "interrupted", "tuiRun");
  const tool = state.turns[1]?.tools[0];
  assert.equal(tool?.status, "stopped", "tuiTool 必须与 webTool=stopped 一致（中断不等于失败）");
});

test("F7-2 反控：已配对的工具在终态后保持自己的结局（success / error 不被改成 stopped）", () => {
  const state = applyAll([
    env(1, "tool/call", { tool_call_id: "ok", tool_name: "bash", args: {} }),
    env(2, "tool/result", { tool_call_id: "ok", content: JSON.stringify({ ok: true }) }),
    env(3, "tool/call", { tool_call_id: "bad", tool_name: "bash", args: {} }),
    env(4, "tool/result", { tool_call_id: "bad", content: JSON.stringify({ ok: false }) }),
    env(5, "run/completed", {}),
  ]);
  const tools = state.turns[0]?.tools ?? [];
  assert.deepEqual(tools.map((t) => t.status), ["success", "error"]);
});

test("F7-2：三个终态与暂停都收口当前执行区间（暂停不是终态但区间确实结束）", () => {
  for (const type of ["run/completed", "run/failed", "run/interrupted", "run/paused"]) {
    const state = applyAll([
      env(1, "tool/call", { tool_call_id: "t1", tool_name: "bash", args: {} }),
      env(2, type, {}),
    ]);
    assert.equal(
      state.turns[0]?.tools[0]?.status,
      "stopped",
      `${type} 后仍在 running 的工具必须标 stopped`,
    );
  }
});

test("F7-2：run/resumed 之后新工具照常从 running 起（清扫不粘住状态机）", () => {
  const state = applyAll([
    env(1, "tool/call", { tool_call_id: "old", tool_name: "bash", args: {} }),
    env(2, "run/paused", { reason: "client_absent" }),
    env(3, "run/resumed", { resume_basis: "client_return" }),
    env(4, "tool/call", { tool_call_id: "new", tool_name: "bash", args: {} }),
  ]);
  const tools = state.turns[0]?.tools ?? [];
  assert.equal(tools[0]?.status, "stopped");
  assert.equal(tools[1]?.status, "running");
});

// ── F7-3：superseded 的旧输入与旧回答退场 ──────────────────────────────────

test("F7-3 票面夹具：旧输入与旧回答不再作现行对话显示，修订后的轮照常显示", () => {
  const state = applyAll([
    env(1, "user/message", { content: "old" }),
    env(2, "text/delta", { delta: "old answer" }),
    // 用户把第 1 条输入改写成第 3 条（后端落 message/superseded 指向被改写的那条）
    env(3, "user/message", { content: "corrected" }),
    env(4, "message/superseded", { superseded_seq: 1 }),
  ]);
  const visible = visibleTurns(state);
  assert.deepEqual(
    visible.map((turn) => turn.text),
    ["corrected"],
    "tui 可见轮次 = corrected（旧输入与旧回答都退场，Web webShadowed=[true,false] 同口径）",
  );
  // 状态里不删元素（重放确定性、append-only 语义）：只影响渲染。
  assert.equal(state.turns.length, 3);
});

test("F7-3：未被取代的轮不受影响（区间只覆盖被取代的那一轮）", () => {
  const state = applyAll([
    env(1, "user/message", { content: "keep-1" }),
    env(2, "text/delta", { delta: "answer-1" }),
    env(3, "user/message", { content: "drop" }),
    env(4, "text/delta", { delta: "answer-drop" }),
    env(5, "user/message", { content: "keep-2" }),
    env(6, "message/superseded", { superseded_seq: 3 }),
  ]);
  assert.deepEqual(
    visibleTurns(state).map((turn) => turn.text),
    ["keep-1", "answer-1", "keep-2"],
  );
});

test("F7-3：坏 superseded_seq（非数字 / 缺键）不误伤任何轮", () => {
  const state = applyAll([
    env(1, "user/message", { content: "a" }),
    env(2, "message/superseded", { superseded_seq: "1" }),
    env(3, "message/superseded", {}),
  ]);
  assert.deepEqual(visibleTurns(state).map((turn) => turn.text), ["a"]);
});

// ── 共享规则：终态集合只有一份（两端都锁同一份字面集合）─────────────────────

test("共享 RUN_TERMINAL_TYPES 锁字面集合（run/paused 非终态，不许混入）", () => {
  assert.deepEqual([...RUN_TERMINAL_TYPES].sort(), [
    "run/completed",
    "run/failed",
    "run/interrupted",
  ]);
});
