/**
 * 包内颜色字面量守门（#842 / D12）。
 *
 * 契约方 @earendil-works/pi-tui 的 parseColor 只接受 number / #rgb / #rrggbb /
 * oklch(...) / okhsl(...)，其余一律 throw "Invalid color value"。写 rgb(...) 会在
 * 构造组件那一刻抛，附着已有用户消息的会话时直接带崩 TUI（安装件实测）。
 * 这里守两条：
 *   1) 行为：用户轮与三种状态的工具卡都能装配（attach 崩溃的最小复现）；
 *   2) 字面量：src/ 下所有形似颜色的字符串都必须能被 parseColor 解析。
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import { colorToHex, parseColor } from "@earendil-works/pi-tui";

import type { ToolCard } from "../src/adapter.ts";
import { CARD_TINTS, createTheme, type IaTheme } from "../src/theme.ts";
import { turnComponents } from "../src/views/chat.ts";
import { toolCardComponent } from "../src/views/toolcard.ts";

const theme: IaTheme = createTheme("truecolor");

test("用户轮与三种状态的工具卡都能装配（rgb() 字面量会在这里抛）", () => {
  const components = turnComponents(
    { role: "user", text: "hello", tools: [], seq: 1 },
    theme,
  );
  assert.equal(components.length, 1);

  for (const status of ["running", "success", "error"] as const) {
    const card: ToolCard = {
      toolCallId: "call_1",
      name: "bash",
      title: "",
      args: {},
      status,
      ok: null,
      message: "",
      durationMs: null,
      output: "",
      artifact: null,
    };
    assert.doesNotThrow(
      () => toolCardComponent(card, theme),
      `工具卡 status=${status} 装配失败`,
    );
  }
});

test("CARD_TINTS 是可解析的 hex，且与原 rgb() 字面量逐位等值", () => {
  assert.deepEqual(CARD_TINTS, {
    running: "#26282e",
    success: "#1e2821",
    error: "#2e1e21",
  });
  for (const [name, value] of Object.entries(CARD_TINTS)) {
    assert.equal(
      colorToHex(parseColor(value)).toLowerCase(),
      value,
      `${name} 解析结果与字面量不一致`,
    );
  }
});

/** 依赖的 parseColor 认得的形态：number / #rgb / #rrggbb / oklch(...) / okhsl(...)。 */
const HEX = /^#(?:[\da-f]{3}|[\da-f]{6})$/i;
const COLOR_FN = /^(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|okhsl|color)\(/i;

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...sourceFiles(path));
    else if (entry.name.endsWith(".ts")) out.push(path);
  }
  return out;
}

test("src/ 下所有颜色字面量都能被 parseColor 解析", () => {
  const found: Array<[string, string]> = [];
  for (const file of sourceFiles(join(import.meta.dirname, "..", "src"))) {
    const text = readFileSync(file, "utf8");
    for (const match of text.matchAll(/"([^"\n]*)"/g)) {
      const value = match[1] ?? "";
      if (HEX.test(value) || COLOR_FN.test(value)) found.push([file, value]);
    }
  }
  assert.ok(found.length > 0, "没扫到任何颜色字面量，扫描器本身失效了");
  for (const [file, value] of found) {
    assert.doesNotThrow(
      () => parseColor(value),
      `${file} 的字面量 "${value}" 不能被 parseColor 解析`,
    );
  }
});
