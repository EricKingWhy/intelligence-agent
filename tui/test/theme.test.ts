/**
 * 主题断言：accent 恒为 Pale Blossom #f1b3ca（One Voice Rule），glyph 白名单常量，
 * Windows loader 用 ● ○ 脉冲不用 braille。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { ACCENT, ACCENT_COLOR, GLYPHS, loaderFrames } from "../src/theme.ts";
import { colorToHex } from "@earendil-works/pi-tui";

test("accent 是 #f1b3ca 且解析一致", () => {
  assert.equal(ACCENT, "#f1b3ca");
  assert.equal(colorToHex(ACCENT_COLOR).toLowerCase(), "#f1b3ca");
});

test("GLYPHS 只含票面白名单字符", () => {
  const allowed = new Set(["●", "○", "└", "─", "│", "┌", "┐", "┘", "├", "┤"]);
  for (const value of Object.values(GLYPHS)) {
    assert.ok(allowed.has(value), `glyph ${value} 不在白名单`);
  }
});

test("Windows loader 用 ● 脉冲，非 Windows 交给 Pi 默认 braille（不在本包源码写字符）", () => {
  const win = loaderFrames("win32");
  assert.deepEqual(win, ["●", "○"]);
  assert.equal(loaderFrames("linux"), undefined, "非 Windows 不注入帧，用 Pi Loader 默认");
});
