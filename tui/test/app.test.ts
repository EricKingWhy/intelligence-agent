/**
 * W-28（#382）：app 层进度清单接线集成测试。
 *
 * 覆盖两处此前无测试的接线（审查 P3/P4）：
 * ① `interceptKeys` 的 `ctrl+t` 到 `renderPlan` 的折叠切换（喂 `\x14` 与 CSI-u
 *    `\x1b[116;5u` 两种编码，证明扩展键编码下同样成立）；
 * ② `renderIncremental` 在**无 turn**时仍渲染进度清单（消除「无 turn 早退早于
 *    renderPlan」的漏画风险）；
 * ③ `renderAll` 重置 `planExpanded`（跨会话/重建不泄漏上一会话的展开态）。
 *
 * TuiApp 在非 TTY 下可构造（本文件 `isTTY=false`）；TUI 画面写 stdout 由
 * `node --test` 捕获，不污染 reporter。私有成员经受控 cast 访问（测试专用）。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { TuiApp } from "../src/app.ts";
import type { ConversationState, PlanItem } from "../src/adapter.ts";

/** 测试用受控访问面：只暴露本文件断言所需的私有成员。 */
interface AppInternals {
  state: ConversationState;
  planExpanded: boolean;
  planText: { render(width: number): string[] } | null;
  interceptKeys(data: string): { consume: boolean } | undefined;
  renderAll(): void;
  renderIncremental(): void;
}

function makeApp(): AppInternals {
  return new TuiApp(
    { baseUrl: "http://127.0.0.1:0", sessionId: "s1" },
    false,
  ) as unknown as AppInternals;
}

function planText(app: AppInternals): string {
  return app.planText?.render(200).join("\n") ?? "";
}

const PLAN: PlanItem[] = [
  { id: "a", content: "读代码", activeForm: "正在读代码", status: "completed", source: "agent" },
  { id: "b", content: "写测试", activeForm: "正在写测试", status: "in_progress", source: "agent" },
  { id: "c", content: "实现", activeForm: "正在实现", status: "pending", source: "agent" },
];

test("app ctrl+t 集成：\\x14 经 interceptKeys 切换完成组折叠（默认折叠转展开）", () => {
  const app = makeApp();
  app.state.plan = PLAN;
  app.renderAll();
  assert.ok(!planText(app).includes("读代码"), "默认折叠：完成项不显示");
  assert.ok(planText(app).includes("正在写测试"), "进行中项恒显示");

  assert.deepEqual(app.interceptKeys("\x14"), { consume: true });
  assert.ok(planText(app).includes("读代码"), "ctrl+t 后完成项展开");
  assert.equal(app.planExpanded, true);
});

test("app ctrl+t 集成：CSI-u 扩展编码 \\x1b[116;5u（tmux/kitty）同样切换", () => {
  const app = makeApp();
  app.state.plan = PLAN;
  app.renderAll();
  app.interceptKeys("\x1b[116;5u");
  assert.equal(app.planExpanded, true, "CSI-u ctrl+t 须被识别");
  assert.ok(planText(app).includes("读代码"), "CSI-u ctrl+t 后完成项展开");
  // 再喂一次 legacy 编码应折叠回来（两种编码都落在同一开关上）。
  app.interceptKeys("\x14");
  assert.equal(app.planExpanded, false);
  assert.ok(!planText(app).includes("读代码"));
});

test("app ctrl+t 集成：非 ctrl+t 按键不切换（不误吞）", () => {
  const app = makeApp();
  app.state.plan = PLAN;
  app.renderAll();
  assert.equal(app.interceptKeys("t"), undefined, "裸 t 不应被消费");
  assert.equal(app.planExpanded, false);
});

test("renderIncremental 无 turn 时仍渲染进度清单（无 turn 早退风险已消除）", () => {
  const app = makeApp();
  app.state.plan = PLAN;
  // 刻意不先 renderAll：turns 与 renderedTurns 皆空，旧实现在此早退、漏画清单。
  app.renderIncremental();
  assert.notEqual(app.planText, null, "无 turn 时清单仍应挂载");
  assert.ok(planText(app).includes("进度清单"), "无 turn 时清单内容仍应渲染");
});

test("renderAll 重置 planExpanded：跨会话/重建不泄漏展开态", () => {
  const app = makeApp();
  app.state.plan = PLAN;
  app.renderAll();
  app.interceptKeys("\x14");
  assert.equal(app.planExpanded, true);
  // 模拟切会话/truncated 重建：renderAll 后折叠开关回到默认。
  app.renderAll();
  assert.equal(app.planExpanded, false, "renderAll 须重置 planExpanded");
  assert.ok(!planText(app).includes("读代码"), "重置后完成组重新折叠");
});
