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
import { mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
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
    { role: "user", text: "hello", tools: [], attachments: [], seq: 1 },
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

/**
 * 三种字符串写法都要看：只认双引号时，单引号或模板串写的 rgb(...) 会静默漏过
 * （2026-10-08 两轴审查各自独立报出；B 轴变异实测：加一行单引号 rgb(40, 36, 40)
 * 后本文件仍全绿）。
 *
 * 三次独立 `matchAll` **串联**，不是一条 `"..."|'...'|\`...\`` 交替正则：交替在 `'`
 * 处先命中单引号分支，把串里内嵌的双引号一并吞掉，`'a"rgb(40, 36, 40)"'` 于是漏检
 * （#848 R1）。
 */
const LITERAL_PATTERNS = [/"([^"\n]*)"/g, /'([^'\n]*)'/g, /`([^`\n]*)`/g];

function literalValues(text: string): string[] {
  return LITERAL_PATTERNS.flatMap((pattern) =>
    [...text.matchAll(pattern)].map((match) => match[1] ?? ""),
  );
}

/**
 * 颜色 token：`#` 后 3/4/6/8 位 hex，或颜色函数 `rgb(...)` 一类。
 *
 * 判据是**在字符串值里搜 token**，不是要求整个值就是一个颜色：`"1px solid rgb(40, 36, 40)"`
 * 这种把颜色嵌进更长字符串的写法同样是漏检口（#848 R7）。落在 token 上断言 parseColor，
 * 「整串是颜色」与「串里含颜色」于是走同一条判据。
 *
 * 4 位 hex（`#ffff`）也收进来：parseColor 只认 `#rgb` / `#rrggbb`，4 位会抛，属于必须报的形状。
 * 长分支在前，免得把 `#11223344` 截成 `#1122` 报一个对不上的 token。
 *
 * 已知边界（登记不修）：注释里被引号包住的颜色也会被扫到 ⇒ 假红。方向是 fail-closed
 * （假红响亮、当场可改；假绿才会把崩溃放进安装件），要消掉它得写 TS 注释剥离器，代价远大于收益。
 */
const COLOR_TOKEN =
  /#[0-9a-f]{8}|#[0-9a-f]{6}|#[0-9a-f]{4}|#[0-9a-f]{3}|(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|okhsl|color)\([^)\n]*\)/gi;

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...sourceFiles(path));
    else if (entry.name.endsWith(".ts")) out.push(path);
  }
  return out;
}

/**
 * 扫源：目录下所有 `.ts` 里的颜色 token（`[文件, token]`）。
 *
 * 探针与真实扫描走**同一个**函数（#848 R2）：只断言 `literalValues` 认得三种引号时，
 * 扫源循环被改回内联的双引号匹配，探针照样全绿（B 轴变异 M2 实测 4/4 绿）。
 */
function scanColorLiterals(dir: string): Array<[string, string]> {
  const found: Array<[string, string]> = [];
  for (const file of sourceFiles(dir)) {
    for (const value of literalValues(readFileSync(file, "utf8"))) {
      for (const token of value.matchAll(COLOR_TOKEN)) found.push([file, token[0]]);
    }
  }
  return found;
}

test("扫源认得三种引号 / 单引号内嵌双引号 / 串里嵌颜色 / 4 位 hex（走真实扫描函数）", () => {
  const dir = mkdtempSync(join(tmpdir(), "ia-tint-probe-"));
  writeFileSync(
    join(dir, "probe.ts"),
    [
      'const a = "rgb(1, 2, 3)";',
      "const b = 'rgb(4, 5, 6)';",
      "const c = `rgb(7, 8, 9)`;",
      `const d = 'a"rgb(10, 11, 12)"';`,
      'const e = "1px solid rgb(13, 14, 15)";',
      'const f = "#ffff";',
    ].join("\n"),
  );
  const found = [...new Set(scanColorLiterals(dir).map(([, token]) => token))];
  assert.deepEqual(found.sort(), [
    "rgb(1, 2, 3)",
    "rgb(4, 5, 6)",
    "rgb(7, 8, 9)",
    "rgb(10, 11, 12)",
    "rgb(13, 14, 15)",
    "#ffff",
  ].sort());
});

test("src/ 下所有颜色字面量都能被 parseColor 解析", () => {
  const found = scanColorLiterals(join(import.meta.dirname, "..", "src"));
  assert.ok(found.length > 0, "没扫到任何颜色字面量，扫描器本身失效了");
  for (const [file, value] of found) {
    assert.doesNotThrow(
      () => parseColor(value),
      `${file} 的字面量 "${value}" 不能被 parseColor 解析`,
    );
  }
});
