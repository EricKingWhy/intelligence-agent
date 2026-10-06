/**
 * 暂停横幅渲染语义（移植 src/agent_harness/cli.py render_pause_block 的合同）：
 * unavailable 纪律（缺键不编 0）、分维只渲染有事实的维度、
 * client_absent 的恢复提示只指向 client_return 且显式确认。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { renderPauseLines, renderResumeHint } from "../src/views/pause.ts";

const BUDGET_PAUSE = {
  reason: "budget_exhausted",
  trigger_dimension: "run.max_agent_turns_total",
  budget_version: 3,
  limits: { run: { max_agent_turns_total: 10 }, local: { max_agent_turns: 4 } },
  consumed: { agent_turns: 10 },
  continuation: { completed: ["step 1"], remaining: ["step 2", "step 3"], next_safe_action: "raise ceiling" },
  resume_requirements: ["budget_increase"],
  closeout_source: "deterministic",
};

test("暂停块头部：reason / dimension / version", () => {
  const lines = renderPauseLines(BUDGET_PAUSE);
  assert.ok(lines.some((l) => l.includes("[run paused]") && l.includes("reason=budget_exhausted")));
  assert.ok(lines.some((l) => l.includes("dimension=run.max_agent_turns_total") && l.includes("version=3")));
});

test("turns 行：consumed / limit / remaining 与 local fuse", () => {
  const lines = renderPauseLines(BUDGET_PAUSE);
  const turns = lines.find((l) => l.includes("turns:"));
  assert.ok(turns);
  assert.ok(turns.includes("consumed 10"));
  assert.ok(turns.includes("limit 10"));
  assert.ok(turns.includes("remaining 0"));
  assert.ok(turns.includes("local fuse 4"));
});

test("缺失 consumed 一律 unavailable，永不 0", () => {
  const lines = renderPauseLines({ reason: "client_absent", trigger_dimension: "client_presence", budget_version: 2 });
  const turns = lines.find((l) => l.includes("turns:"));
  assert.ok(turns);
  assert.ok(turns.includes("consumed unavailable"));
  assert.ok(turns.includes("limit unlimited"));
  assert.ok(turns.includes("remaining unavailable"));
  assert.ok(!lines.some((l) => l.includes("remaining 0")));
});

test("额外维度：只渲染有事实可说的维度", () => {
  const withDims = renderPauseLines({
    reason: "budget_exhausted",
    trigger_dimension: "run.max_total_tokens",
    budget_version: 1,
    limits: { run: { max_total_tokens: 5000 } },
    consumed: { agent_turns: 1, total_tokens: 5000 },
  });
  const tokens = withDims.find((l) => l.includes("total_tokens:"));
  assert.ok(tokens && tokens.includes("limit 5000") && tokens.includes("remaining 0"));
  // 没配 model_requests 的 ceiling、消耗快照也没这个键：零行
  assert.ok(!withDims.some((l) => l.includes("model_requests:")));
});

test("deadline 维：键缺席零行；None=unlimited；畸形=unavailable", () => {
  assert.ok(!renderPauseLines({}).some((l) => l.includes("deadline:")));
  const unlim = renderPauseLines({ limits: { run: { deadline_at: null } } });
  assert.ok(unlim.some((l) => l.includes("deadline: unlimited")));
  const bad = renderPauseLines({ limits: { run: { deadline_at: " 2026-01-01" } } });
  assert.ok(bad.some((l) => l.includes("deadline: unavailable")));
});

test("per-tool 维：calls 与 attempts 分列，不混同", () => {
  const lines = renderPauseLines({
    reason: "budget_exhausted",
    trigger_dimension: "run.tool_call_limits.bash",
    budget_version: 2,
    limits: { run: { tool_call_limits: { bash: 3 } } },
    consumed: { tool_calls_by_tool: { bash: 2 }, tool_attempts_by_tool: { bash: 4 } },
  });
  const tool = lines.find((l) => l.includes("tool bash:"));
  assert.ok(tool);
  assert.ok(tool.includes("consumed 2 calls"));
  assert.ok(tool.includes("4 attempts"));
  assert.ok(tool.includes("limit 3"));
});

test("continuation 与 resume requirements", () => {
  const lines = renderPauseLines(BUDGET_PAUSE);
  assert.ok(lines.some((l) => l.includes("completed: step 1")));
  assert.ok(lines.some((l) => l.includes("step 3")));
  assert.ok(lines.some((l) => l.includes("next: raise ceiling")));
  assert.ok(lines.some((l) => l.includes("resume requirements: budget_increase")));
});

test("client_absent 恢复提示：唯一依据 client_return，显式确认不自动续跑", () => {
  const hint = renderResumeHint("sess-1", {
    reason: "client_absent",
    trigger_dimension: "client_presence",
    budget_version: 5,
  });
  assert.ok(hint.includes("client_return"));
  assert.ok(hint.includes("5"));
  assert.ok(hint.includes("sess-1"));
  assert.ok(hint.includes("不自动续跑"));
});

test("cost 维 remaining 用十进制精确差，不走浮点", () => {
  const lines = renderPauseLines({
    reason: "budget_exhausted",
    trigger_dimension: "run.max_cost_usd",
    budget_version: 1,
    limits: { run: { max_cost_usd: "2.25" } },
    consumed: { cost_usd: "1.50" },
  });
  const cost = lines.find((l) => l.includes("cost_usd:"));
  assert.ok(cost && cost.includes("remaining 0.75"));
});

test("畸形读数（布尔/形状不合）报 unavailable，不印值", () => {
  const lines = renderPauseLines({
    reason: "budget_exhausted",
    trigger_dimension: "run.max_agent_turns_total",
    budget_version: 1,
    limits: { run: { max_agent_turns_total: true } },
    consumed: { agent_turns: "many" },
  });
  const turns = lines.find((l) => l.includes("turns:"));
  assert.ok(turns);
  assert.ok(turns.includes("consumed unavailable"));
  assert.ok(turns.includes("limit unavailable"));
});
