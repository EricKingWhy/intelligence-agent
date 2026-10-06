/**
 * GBK glyph 白名单（票面铁律）：src/ 下所有 .ts 的非 ASCII 字符必须落在
 * 白名单或中文安全区（CJK 汉字 / CJK 标点 / 全角形式）内。
 * 禁用 ⎿ ❯ ✔ ✘ 与 braille spinner（Windows 中文终端实测会炸或宽度错乱）。
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const SRC_ROOT = join(fileURLToPath(new URL(".", import.meta.url)), "..", "src");

/** 票面白名单：● ○ └ ─ │ ┌ ┐ ┘ ├ ┤，外加 GB2312 内的中点。 */
const GLYPH_WHITELIST = new Set(
  ["●", "○", "└", "─", "│", "┌", "┐", "┘", "├", "┤", "·"],
);

/** 硬禁用集：即便未来有人误扩白名单，这些也必须保持拒绝。 */
const FORBIDDEN = ["⎿", "❯", "✔", "✘", "❮", "»", "⏺", "⏻", "⏸", "✓", "✗", "⚡", "➜"];

function isAllowedNonAscii(ch: string): boolean {
  const code = ch.codePointAt(0);
  if (code === undefined) return false;
  if (code >= 0x4e00 && code <= 0x9fff) return true; // CJK 汉字
  if (code >= 0x3000 && code <= 0x303f) return true; // CJK 标点
  if (code >= 0xff00 && code <= 0xffef) return true; // 全角形式
  if (code >= 0x3400 && code <= 0x4dbf) return true; // 扩展 A
  return GLYPH_WHITELIST.has(ch);
}

function listTsFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, name.name);
    if (name.isDirectory()) out.push(...listTsFiles(full));
    else if (name.name.endsWith(".ts")) out.push(full);
  }
  return out;
}

test("src 下所有非 ASCII 字符都在 GBK 白名单或中文安全区", () => {
  const offenders: string[] = [];
  for (const file of listTsFiles(SRC_ROOT)) {
    const text = readFileSync(file, "utf8");
    const seen = new Set<string>();
    for (const ch of text) {
      const code = ch.codePointAt(0) ?? 0;
      if (code >= 128 && !isAllowedNonAscii(ch) && !seen.has(ch)) {
        seen.add(ch);
        offenders.push(`${file}: U+${code.toString(16).padStart(4, "0")} '${ch}'`);
      }
    }
  }
  assert.deepEqual(offenders, []);
});

test("硬禁用字符与 braille spinner 绝不出现在 src", () => {
  for (const file of listTsFiles(SRC_ROOT)) {
    const text = readFileSync(file, "utf8");
    for (const bad of FORBIDDEN) {
      assert.ok(!text.includes(bad), `${file} 含禁用字符 ${JSON.stringify(bad)}`);
    }
    // braille 区（U+2800-U+28FF）整体拒绝
    assert.ok(!/[\u2800-\u28ff]/.test(text), `${file} 含 braille spinner 字符`);
  }
});
