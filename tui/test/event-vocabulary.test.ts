/**
 * 事件词汇守门（#1498 / G-1）。
 *
 * 事实：TUI 曾手抄 19 条事件名（`docs/EVENT_VOCABULARY.md` 的子集），而仓里已有
 * 机器单源 `src/agent_harness/session/event.py` 经 `scripts/gen_event_types.py`
 * 生成的 `web/src/generated/event-types.ts`。手抄的取值当时**全对**，问题是
 * 「第四份副本」本身：没有断言把它钉到生成物上，改一端不会红。
 *
 * 现在 TUI 吃的是同源产物 `tui/src/generated/event-types.ts`，本测试守三件事：
 *   ① 产物不是手写改出来的（文件头 + 再生成命令）；
 *   ② `EVENT` 就是生成物的 re-export（不是本地又写一份）；
 *   ③ `src/` 里（生成物之外）不得再出现任何事件值字面量——有人手抄回来就红。
 *
 * ③ 是漂移负控的 TUI 侧判据：把任意一条事件值抄回 `adapter.ts` 会立刻变红。
 * Python 侧的逐字节守卫（`tests/test_event_types_generated.py`）负责另一头：改了
 * `event.py` 却忘记重新生成时变红。
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import { EventType as GeneratedEventType } from "../src/generated/event-types.ts";
import { EVENT } from "../src/events.ts";

const SRC_ROOT = join(import.meta.dirname, "..", "src");
const ARTIFACT = join(SRC_ROOT, "generated", "event-types.ts");

function listTsFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, name.name);
    if (name.isDirectory()) out.push(...listTsFiles(full));
    else if (name.name.endsWith(".ts")) out.push(full);
  }
  return out;
}

/**
 * 把注释替换成等长空白（保留换行与偏移），字符串/模板/正则内容原样保留。
 * 守卫钉的是「活代码手抄」；注释里提事件名是合法文档（复核 A1 的 8 处全在注释里）。
 * 边界不是「正则里的 `/`」而是「正则里的引号」：`/^['"]+|['"]+$/g` 会让只认字符串态
 * 的解析器误入引号态、吞掉后续活代码（增量复核 F-1 实证）。故 `/` 按「上一个非空白
 * 字符属于 `(,=:[!&|?{};*%~^<>` 或行首」判为正则开头，正则内再分字符类两态。
 * `+`/`-` 不在前置集里：`i++ / 2`、`i-- / 2` 是除法，留着会把后续注释吞成正则，
 * 注释里的事件名就假红（#1549 P3-1）。代价是 `s + /re/.source` 这类合法正则也被
 * 当除法——文本层无法与 `n + / 2` 区分，只能靠语法消歧；`tui/src` 现无此形态。
 * 前面是标识符时不可能是正则，只有关键字后可以：`return /[//]/` 这类正则体内的裸
 * `//` 曾被当行注释吞掉后继活代码（#1549 P3-2），故补 `REGEX_KEYWORDS`。
 * 注释判定保持在正则判定之前：`x = 1; // 注释` 的 `//` 前是 `;`（preceder），
 * 若先判正则就会把整行注释吞成正则。
 */
const REGEX_PRECEDERS = "(,=:[!&|?{};*%~^<>";
const REGEX_KEYWORDS = new Set([
  "return", "typeof", "instanceof", "case", "delete", "void", "yield", "await",
  "throw", "new", "do", "else", "in", "of",
]);

function stripComments(text: string): string {
  type Mode = "code" | "line" | "block" | "double" | "single" | "template" | "regex";
  let mode: Mode = "code";
  let inClass = false;
  // 上一个非空白字符；行首为 ""。只用来判 `/` 是正则开头还是除号。
  let prev = "";
  // 上一个标识符 / 关键字（可跨空白）；`return /re/` 这类只认关键字后的 `/`。
  let lastWord = "";
  // 该标识符前的字符；`.` 之后是属性名（`obj.in / 2` 是除法），不算关键字。
  let lastWordPrev = "";
  let word = "";
  let wordPrev = "";
  let out = "";
  for (let i = 0; i < text.length; i++) {
    const ch = text[i] ?? "";
    const next = text[i + 1];
    if (mode === "code") {
      // 先结算标识符，`return/re/`（无空格）也认得出关键字。
      if (/[A-Za-z0-9_$]/.test(ch)) {
        if (word === "") wordPrev = prev;
        word += ch;
      } else if (word !== "") {
        lastWord = word;
        lastWordPrev = wordPrev;
        word = "";
      }
      // 注释判定必须在正则判定之前：`x = 1; // 注释` 的 `/` 前是 `;`（preceder），
      // 先判正则会把整行注释吞成正则。`return /[//]/` 的正则自第一个 `/` 起就不以
      // 裸 `//` 开头（第二字符是 `[`），故不受这条顺序影响。
      if (ch === "/" && next === "/") {
        mode = "line";
        out += "  ";
        i++;
        continue;
      }
      if (ch === "/" && next === "*") {
        mode = "block";
        out += "  ";
        i++;
        continue;
      }
      if (
        ch === "/" &&
        (prev === "" ||
          REGEX_PRECEDERS.includes(prev) ||
          (REGEX_KEYWORDS.has(lastWord) && lastWordPrev !== "."))
      ) {
        mode = "regex";
        inClass = false;
        out += ch;
        continue;
      }
      if (ch === '"') mode = "double";
      else if (ch === "'") mode = "single";
      else if (ch === "`") mode = "template";
      // 换行即不可能是关键字/自增的尾巴；关键字不跨行生效（`return` 后会 ASI）。
      if (ch === "\n") {
        prev = "";
        lastWord = "";
      } else if (!/\s/.test(ch)) prev = ch;      out += ch;
      continue;
    }
    if (mode === "line") {
      if (ch === "\n") {
        mode = "code";
        prev = "";
        out += ch;
      } else out += " ";
      continue;
    }
    if (mode === "block") {
      if (ch === "*" && next === "/") {
        mode = "code";
        out += "  ";
        i++;
        continue;
      }
      if (ch === "\n") {
        prev = "";
        out += ch;
        continue;
      }
      out += " ";
      continue;
    }
    if (mode === "regex") {
      // 正则内容原样保留；字符类里的 `/` 不是结束符，转义对整体跳过
      if (ch === "\\") {
        out += ch + (next ?? "");
        i++;
        continue;
      }
      if (inClass) {
        if (ch === "]") inClass = false;
        out += ch;
        continue;
      }
      if (ch === "[") {
        inClass = true;
        out += ch;
        continue;
      }
      if (ch === "/") {
        mode = "code";
        prev = "/";
      }
      out += ch;
      continue;
    }
    // 字符串/模板：内容必须原样保留（事件值字面量就在里面），只跳过转义对
    if (ch === "\\") {
      out += ch + (next ?? "");
      i++;
      continue;
    }
    if (
      (mode === "double" && ch === '"') ||
      (mode === "single" && ch === "'") ||
      (mode === "template" && ch === "`")
    ) {
      mode = "code";
      prev = ch;
    }
    out += ch;
  }
  return out;
}

test("生成物带 GENERATED 头与再生成命令（不是手写文件）", () => {
  const text = readFileSync(ARTIFACT, "utf8");
  const head = text.split("\n", 3).join("\n");
  assert.ok(
    head.includes("GENERATED by scripts/gen_event_types.py"),
    `产物首行缺 GENERATED 声明：${head}`,
  );
  assert.ok(
    head.includes("Regenerate: uv run python scripts/gen_event_types.py"),
    `产物缺再生成命令：${head}`,
  );
});

test("EVENT 是生成物的 re-export，取值对象相同", () => {
  assert.equal(EVENT, GeneratedEventType, "EVENT 必须直接是生成物，不得本地另写一份");
  assert.deepEqual({ ...EVENT }, { ...GeneratedEventType });
});

test("stripComments 认正则字面量里的引号（增量复核 F-1）", () => {
  // 正则是活代码：里面的引号不能开字符串态，否则会吞掉其后的活代码、又留下其前的注释。
  const src = `const a = x.replace(/^['"]+|['"]+$/g, "");\nconst b = "run/paused"; // 注释 "x/y"\n`;
  const out = stripComments(src);
  assert.equal(out.length, src.length, "只许等长空白化，不许增删字符");
  assert.ok(out.includes(`"run/paused"`), "正则后面的活代码字面量必须保留");
  assert.ok(!out.includes(`"x/y"`), "注释里的字面量必须被剥除");
  // 真实夹具：`src/lib/image-paste.ts` 的 `/^['"]+|['"]+$/g` 曾让 `"file://"` 被空白化。
  const real = stripComments(readFileSync(join(SRC_ROOT, "lib", "image-paste.ts"), "utf8"));
  assert.ok(real.includes(`"file://"`), "正则同行之后的字符串必须原样保留");
});

test("stripComments 不把 ++/-- 与二元 + 后的除法判成正则（#1549 P3-1）", () => {
  // `n++ / 2`、`n + / 2` 是除法。`+`/`-` 若留在正则前置字符集里，会吞到下一个 `/`，
  // 同行注释不再剥除，注释里的事件名就假红（增量复核 §7.3）。
  const src = `const a = n++ / 2; // 文档提及 "run/paused"\nconst b = n-- / 2; // "x/y"\nconst c = n + / 2; // "tool/call"\nconst d = cfg.in / 2; // "step/started"\n`;
  const out = stripComments(src);
  assert.equal(out.length, src.length, "只许等长空白化，不许增删字符");
  assert.ok(!out.includes(`"run/paused"`), "++ 后除法所在行的注释必须被剥除");
  assert.ok(!out.includes(`"x/y"`), "-- 后除法所在行的注释必须被剥除");
  assert.ok(!out.includes(`"tool/call"`), "二元 + 后除法所在行的注释必须被剥除");
  assert.ok(!out.includes(`"step/started"`), "`.in` 是属性名不是关键字，其后除法不得判成正则");
  // 前置字符是 `(` 的合法正则不许被误伤：`src/app.ts` 的 `split(/\s+/)`。
  const app = stripComments(readFileSync(join(SRC_ROOT, "app.ts"), "utf8"));
  assert.ok(app.includes("split(/\\s+/)"), "合法正则字面量必须原样保留");
});

test("stripComments 认 return 后正则里的裸 //（#1549 P3-2）", () => {
  // `return /[//]/`、`/[/]/`：正则内的裸 `//` 曾被当行注释，吞掉后继活代码（增量复核 §7.4）。
  const src = `function f(x) { return /[//]/.test(x); }\nfunction g(x) { return /[/]/.test(x); }\nconst keep = "run/paused"; // 注释 "x/y"\n`;
  const out = stripComments(src);
  assert.equal(out.length, src.length, "只许等长空白化，不许增删字符");
  assert.ok(out.includes("return /[//]/.test(x);"), "正则内裸 // 不得脱轨");
  assert.ok(out.includes("return /[/]/.test(x);"), "字符类里的 / 不得脱轨");
  assert.ok(out.includes(`"run/paused"`), "活代码字面量必须保留");
  assert.ok(!out.includes(`"x/y"`), "行注释里的字面量必须被剥除");
});

test("src/ 下（生成物之外）不再出现事件值字面量", () => {
  // 只查带 `/` 的事件值（`域/子域` 形状）：裸词事件值目前只有 `error`，
  // 而 "error" 在本包里是**工具卡状态**（`ToolCard.status`）与日志级别的合法字面量，
  // 不是词汇副本。带斜杠的 74 条里 18 条正是当年手抄的那批，抄回来即红。
  const eventValues = new Set<string>(
    Object.values(EVENT).filter((value) => value.includes("/")),
  );
  const offenders: string[] = [];
  for (const file of listTsFiles(SRC_ROOT)) {
    if (file === ARTIFACT) continue;
    // 三种引号都要查：生成物自己用单引号，单/双引号手抄同样得红（复核 F-1）。
    const text = stripComments(readFileSync(file, "utf8"));
    for (const match of text.matchAll(/(["'`])([a-z]+(?:\/[a-z_-]+)+)\1/g)) {
      const value = match[2];
      if (value !== undefined && eventValues.has(value)) {
        offenders.push(`${file}: ${match[1]}${value}${match[1]}`);
      }
    }
  }
  assert.deepEqual(offenders, [], "手抄事件字面量必须改回 EVENT 常量（词汇唯一来源是 event.py）");
});
