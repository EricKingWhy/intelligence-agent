/**
 * W-28（#382）：进度清单 TUI 渲染。
 *
 * 覆盖两件事：
 * ① adapter 投影——`task/plan_updated` 整表覆盖 + 行级容错（语义与 W-27
 *    `web/src/lib/projection.ts` 同源，前端不维护第二套清单真相，不变量 #22）；
 * ② 纯函数 `renderPlanList(plan, theme)` 的三态快照——完成=删除线+dim、
 *    in_progress=activeForm+accent+bold、pending=dim，glyph 全走 GBK 白名单。
 *
 * 主题用「记录型」替身（把着色换成可读标记），快照断言语义而不是具体色值；
 * 删除线 `\x1b[9m` / 粗体 `\x1b[1m` 由渲染函数内联产生，仍逐字钉进快照。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { applyEvent, createState, type EventEnvelope, type PlanItem } from "../src/adapter.ts";
import type { IaTheme } from "../src/theme.ts";
import { renderPlanList } from "../src/views/plan.ts";

function env(seq: number, type: string, data: Record<string, unknown>): EventEnvelope {
  return {
    type,
    data,
    seq,
    run_id: "r1",
    step_id: seq,
    session_id: "s1",
    time: "2026-10-07T00:00:00Z",
    durability: "durable",
  };
}

/** 记录型主题：每个着色函数换成可读标记，快照断言语义而非色值。 */
function testTheme(): IaTheme {
  return {
    mode: "truecolor",
    accent: (text) => `<a>${text}</a>`,
    muted: (text) => `<m>${text}</m>`,
    dim: (text) => `<d>${text}</d>`,
    text: (text) => `<t>${text}</t>`,
    success: (text) => `<s>${text}</s>`,
    error: (text) => `<e>${text}</e>`,
    warning: (text) => `<w>${text}</w>`,
  };
}

const item = (
  id: string,
  status: string,
  content: string,
  activeForm: string,
): PlanItem => ({ id, content, activeForm, status, source: "agent" });

/** 三态混合清单：完成 → 进行中 → 待办（覆盖全部三种样式分支）。 */
const PLAN: PlanItem[] = [
  item("a", "completed", "读代码", "正在读代码"),
  item("b", "in_progress", "写测试", "正在写测试"),
  item("c", "pending", "实现", "正在实现"),
];

// ── ① adapter 投影 ──────────────────────────────────────────────────────────

test("plan 初始为 null（未出现过清单 ≠ 清空）", () => {
  assert.equal(createState().plan, null);
});

test("task/plan_updated 首帧写入：五字段逐字进 state.plan", () => {
  const state = createState();
  applyEvent(state, env(1, "task/plan_updated", { items: [item("a", "in_progress", "任务 a", "正在执行 a")] }));
  assert.deepEqual(state.plan, [item("a", "in_progress", "任务 a", "正在执行 a")]);
});

test("task/plan_updated 整表覆盖 last-wins：第二帧整体替换第一帧", () => {
  const state = createState();
  applyEvent(state, env(1, "task/plan_updated", { items: [item("a", "completed", "A", "A")] }));
  applyEvent(state, env(2, "task/plan_updated", { items: [item("x", "pending", "X", "X")] }));
  assert.deepEqual(state.plan, [item("x", "pending", "X", "X")]);
});

test("空 items = 合法清空：plan 变 []（渲染面归组件，不留空壳）", () => {
  const state = createState();
  applyEvent(state, env(1, "task/plan_updated", { items: [item("a", "pending", "A", "A")] }));
  applyEvent(state, env(2, "task/plan_updated", { items: [] }));
  assert.deepEqual(state.plan, []);
});

test("行级容错：非对象行、缺 id 行、空 id 行丢弃，好行保留", () => {
  const state = createState();
  applyEvent(state, env(1, "task/plan_updated", {
    items: [null, "garbage", { content: "没 id" }, { id: "", content: "空 id" }, item("ok", "pending", "OK", "OK")],
  }));
  assert.deepEqual(state.plan, [item("ok", "pending", "OK", "OK")]);
});

test("重复 id：首个胜（key 唯一性；服务端本不该发，防手写 JSONL）", () => {
  const state = createState();
  applyEvent(state, env(1, "task/plan_updated", {
    items: [item("a", "pending", "先", "先"), item("a", "completed", "后", "后")],
  }));
  assert.deepEqual(state.plan, [item("a", "pending", "先", "先")]);
});

test("字段归一不猜测：content 非字符串取空串、activeForm 缺失回落 content、status 非字符串记空", () => {
  const state = createState();
  applyEvent(state, env(1, "task/plan_updated", {
    items: [
      { id: "a", content: 42, status: 7 },
      { id: "b", content: "任务 b", activeForm: "", status: "pending" },
    ],
  }));
  assert.deepEqual(state.plan, [
    { id: "a", content: "", activeForm: "", status: "", source: "" },
    { id: "b", content: "任务 b", activeForm: "任务 b", status: "pending", source: "" },
  ]);
});

test("状态机违规原样保留：双 in_progress 不被投影改写（渲染端容错归组件）", () => {
  const state = createState();
  applyEvent(state, env(1, "task/plan_updated", {
    items: [item("a", "in_progress", "A", "A"), item("b", "in_progress", "B", "B")],
  }));
  assert.deepEqual(state.plan, [item("a", "in_progress", "A", "A"), item("b", "in_progress", "B", "B")]);
});

test("items 非数组 = 坏帧整个忽略：plan 保持原值", () => {
  const state = createState();
  applyEvent(state, env(1, "task/plan_updated", { items: [item("a", "pending", "A", "A")] }));
  applyEvent(state, env(2, "task/plan_updated", { items: "garbage" }));
  assert.deepEqual(state.plan, [item("a", "pending", "A", "A")]);
});

// ── ② renderPlanList 三态快照 ───────────────────────────────────────────────

test("空清单 / null 不渲染（无清单会话不留空壳）", () => {
  const theme = testTheme();
  assert.deepEqual(renderPlanList(null, theme), []);
  assert.deepEqual(renderPlanList([], theme), []);
});

test("三态快照（展开）：计数行 + 完成删除线+dim + 进行中 activeForm+accent+bold + 待办 dim", () => {
  const lines = renderPlanList(PLAN, testTheme(), { expanded: true });
  assert.deepEqual(lines, [
    "<d>└ </d><t>进度清单</t><d> · 1/3 complete</d>",
    "<d>└ </d>\x1b[9m<d>  · 读代码</d>\x1b[29m",
    "  \x1b[1m<a>> ● 正在写测试</a>\x1b[22m",
    "  <d>  ○ 实现</d>",
  ]);
});

test("三态快照（折叠默认）：完成组整组收起 + N more below 披露行", () => {
  const lines = renderPlanList(PLAN, testTheme());
  assert.deepEqual(lines, [
    "<d>└ </d><t>进度清单</t><d> · 1/3 complete</d>",
    "<d>└ </d>\x1b[1m<a>> ● 正在写测试</a>\x1b[22m",
    "  <d>  ○ 实现</d>",
    "  <d>· 1 more below (ctrl+t)</d>",
  ]);
});

test("ASCII 兜底表：glyph 换成 [ ] / [-]，结构不变（Claude Code 双表策略）", () => {
  const lines = renderPlanList(PLAN, testTheme(), { expanded: true, ascii: true });
  assert.deepEqual(lines, [
    "<d>└ </d><t>进度清单</t><d> · 1/3 complete</d>",
    "<d>└ </d>\x1b[9m<d>  [-] 读代码</d>\x1b[29m",
    "  \x1b[1m<a>> [ ] 正在写测试</a>\x1b[22m",
    "  <d>  [ ] 实现</d>",
  ]);
});

test("未知 status 按待办渲染（有序优先级表兜底，不崩）", () => {
  const lines = renderPlanList([item("a", "weird", "未知态", "未知态")], testTheme(), { expanded: true });
  assert.deepEqual(lines, [
    "<d>└ </d><t>进度清单</t><d> · 0/1 complete</d>",
    "<d>└ </d><d>  ○ 未知态</d>",
  ]);
});

test("in_progress 文案取 activeForm；activeForm 为空回落 content", () => {
  const lines = renderPlanList([item("a", "in_progress", "落地文案", "")], testTheme(), { expanded: true });
  assert.ok(lines[1]?.includes("落地文案"), "activeForm 为空时回落 content");
  assert.ok(!lines[1]?.includes("undefined"));
});

test("行数超上限时截断并给 N more below（Codex 折叠披露触发）", () => {
  const many: PlanItem[] = Array.from({ length: 12 }, (_, i) =>
    item(`p${i}`, "pending", `步骤 ${i}`, `步骤 ${i}`),
  );
  const lines = renderPlanList(many, testTheme(), { expanded: true });
  // 头 + 上限内行 + 披露行
  assert.equal(lines.length, 1 + 8 + 1);
  assert.ok(lines[lines.length - 1]?.includes("4 more below"), `末行应为披露行，实际 ${lines[lines.length - 1]}`);
});

test("真实主题：完成项确实带 \\x1b[9m 删除线与 \\x1b[29m 复位（硬约束）", async () => {
  const { createTheme } = await import("../src/theme.ts");
  const theme = createTheme("truecolor");
  const lines = renderPlanList(PLAN, theme, { expanded: true });
  const completedLine = lines[1] ?? "";
  assert.ok(completedLine.includes("\x1b[9m"), "完成项须带删除线开启序列");
  assert.ok(completedLine.includes("\x1b[29m"), "完成项须带删除线复位序列");
});

test("glyph 全走白名单：渲染输出无非白名单字符（GBK 安全）", () => {
  const WHITELIST = new Set(["●", "○", "└", "─", "│", "┌", "┐", "┘", "├", "┤", "·"]);
  // 与 test/glyphs.test.ts 同一判据：白名单 glyph + CJK 汉字/标点安全区。
  const allowed = (ch: string): boolean => {
    const code = ch.codePointAt(0) ?? 0;
    if (code >= 0x4e00 && code <= 0x9fff) return true;
    if (code >= 0x3000 && code <= 0x303f) return true;
    if (code >= 0xff00 && code <= 0xffef) return true;
    if (code >= 0x3400 && code <= 0x4dbf) return true;
    return WHITELIST.has(ch);
  };
  const rendered = renderPlanList(PLAN, testTheme(), { expanded: true, ascii: true }).join("");
  for (const ch of rendered) {
    const code = ch.codePointAt(0) ?? 0;
    if (code < 128) continue;
    assert.ok(allowed(ch), `输出含非白名单字符 ${JSON.stringify(ch)}`);
  }
});
