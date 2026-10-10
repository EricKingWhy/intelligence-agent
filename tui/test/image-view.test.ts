/**
 * AC6 / AC7：终端降级与缩略图渲染。
 *
 * - AC7：终端支持图片协议（kitty / iTerm2）时按 **pi-tui 既有能力**渲染缩略图
 *   （REUSE `Image` / `renderImage`，不自造）；
 * - AC6：`WT_SESSION`（Windows Terminal）下 pi-tui 自报 `images: null` ⇒ 渲染为
 *   文本占位（文件名 + 尺寸 + 可打开的原图路径），**且绝不出现任何图片协议转义序列**
 *   （负向断言）。
 *
 * 能力位是 pi-tui 的**进程级缓存**（`setCapabilities` 就是为这种测试导出的），故本文件
 * 显式设置并在每个用例后重置——不用 `WT_SESSION` 环境变量，避免测试机自身跑在 tmux/
 * kitty 里时被上游探测抢先（那样断言会变成对环境的断言）。
 */
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { resetCapabilitiesCache, setCapabilities } from "@earendil-works/pi-tui";

import { renderDraftImage, renderHistoryImage } from "../src/lib/image-view.ts";
import type { TurnImageRef } from "../src/adapter.ts";
import type { PendingImage } from "../src/lib/pending-images.ts";

/** 1x1 PNG（IHDR width=height=1），足够让 dimensity 探测出 1x1。 */
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
const PNG_BYTES = Uint8Array.from(Buffer.from(PNG_BASE64, "base64"));

const IMAGE_PATH = "/home/u/shots/shot.png";

const IMAGE: PendingImage = {
  bytes: PNG_BYTES,
  mimeType: "image/png",
  name: "shot.png",
  path: IMAGE_PATH,
};

const theme = { fallbackColor: (text: string): string => text };

/** M-09：历史附图引用（服务端 ImageRef 镜像；宽高 2x3 与字节里的 1x1 PNG 不同步是刻意的：
 *  引用里的宽高是上传时服务端判定的，占位渲染只信引用，不要求与字节一致）。 */
const REF: TurnImageRef = {
  attachment_id: "sha256:" + "d".repeat(64),
  media_type: "image/png",
  bytes: 95,
  width: 2,
  height: 3,
  name: "shot.png",
};

const ANON_REF: TurnImageRef = {
  attachment_id: "sha256:" + "e".repeat(64),
  media_type: "image/png",
  bytes: 95,
  width: 2,
  height: 3,
  name: null,
};

/** 图片协议转义序列（kitty APC / iTerm2 OSC 1337）——AC6 的负向断言集。 */
const IMAGE_PROTOCOL_ESCAPES = ["\x1b_G", "\x1b]1337;File=", "\x1bP"];

function withCapabilities<T>(images: "kitty" | "iterm2" | null, hyperlinks: boolean, fn: () => T): T {
  setCapabilities({ images, trueColor: true, hyperlinks });
  try {
    return fn();
  } finally {
    resetCapabilitiesCache();
  }
}

test("AC7：图片协议终端（kitty）⇒ 交给 pi-tui Image 组件渲染缩略图", () => {
  withCapabilities("kitty", true, () => {
    const rendered = renderDraftImage(IMAGE, theme);
    assert.equal(rendered.kind, "image");
    assert.ok(rendered.kind === "image");
    const output = rendered.component.render(80).join("\n");
    assert.ok(output.includes("\x1b_G"), "kitty 图形协议序列应由 pi-tui 生成");
  });
});

test("AC7：iTerm2 终端同样走组件渲染（协议不同，能力位同一判定）", () => {
  withCapabilities("iterm2", true, () => {
    const rendered = renderDraftImage(IMAGE, theme);
    assert.equal(rendered.kind, "image");
    assert.ok(rendered.kind === "image");
    assert.ok(
      rendered.component.render(80).join("\n").includes("\x1b]1337;File="),
      "iTerm2 内联图片序列应由 pi-tui 生成",
    );
  });
});

test("AC6：WT_SESSION（images: null）⇒ 文本占位，不含任何图片协议转义序列", () => {
  withCapabilities(null, true, () => {
    const rendered = renderDraftImage(IMAGE, theme);
    assert.equal(rendered.kind, "text");
    assert.ok(rendered.kind === "text");
    const output = rendered.lines.join("\n");
    for (const escape of IMAGE_PROTOCOL_ESCAPES) {
      assert.ok(!output.includes(escape), `降级输出不得含 ${JSON.stringify(escape)}`);
    }
    assert.ok(!output.includes(PNG_BASE64.slice(0, 24)), "降级输出不得内嵌 base64 图数据");
    assert.ok(output.includes("shot.png"), "占位必须给出文件名");
    assert.ok(output.includes("1x1"), "占位必须给出尺寸");
    assert.ok(output.includes("image/png"), "占位必须给出 media type");
    // 不写死 `file:///home/u/...`：`imageFallback`（上游 pi-tui）用 `pathToFileURL` 造链接，
    // 同一条 `/home/u/...` 在 Windows 上按**当前盘**解析成 `file:///C:/home/u/...` ⇒ 期望值
    // 随平台变、盘符不可写死（#830 D3）。这里也不与同一个原语的输出比字面量（那会把两侧
    // 绑死、形状回归看不见），而是断言**语义**：OSC 8 链接必须存在，且解码回来就是同一份
    // 文件 —— 斜杠数、盘符、百分号编码任一出错都会在这里红。
    // 终止符两类都排除：`\x1b`（ST，上游 hyperlink 用 `\x1b\\`）与 `\x07`（BEL，部分终端
    // 用 BEL 收尾）—— 否则终止符被吃进 href、`fileURLToPath` 假红。
    const linked = /file:\/\/[^\s\x1b\x07]+/.exec(output);
    assert.ok(linked, "支持 OSC 8 的终端上路径应是可点击的原图链接");
    assert.equal(
      fileURLToPath(linked[0]),
      resolve(IMAGE_PATH),
      "OSC 8 链接必须指回同一份原图",
    );
  });
});

test("AC6：终端不支持 OSC 8 时，占位仍给出可读的原图路径（缩短为 ~/...）", () => {
  withCapabilities(null, false, () => {
    const rendered = renderDraftImage(IMAGE, theme);
    assert.ok(rendered.kind === "text");
    const output = rendered.lines.join("\n");
    assert.ok(!output.includes("\x1b]8;"), "无超链接能力时不得输出 OSC 8 序列");
    assert.ok(output.includes("shots/shot.png"), "路径必须以文本形式可见");
  });
});

test("剪贴板图（无本地路径）也降级成文本占位（文件名兜底，不猜路径）", () => {
  withCapabilities(null, true, () => {
    const clipboard: PendingImage = {
      bytes: PNG_BYTES,
      mimeType: "image/png",
      name: "clipboard.png",
      path: null,
    };
    const rendered = renderDraftImage(clipboard, theme);
    assert.ok(rendered.kind === "text");
    const output = rendered.lines.join("\n");
    assert.ok(output.includes("clipboard.png"));
    for (const escape of IMAGE_PROTOCOL_ESCAPES) assert.ok(!output.includes(escape));
  });
});

test("损坏字节（尺寸未知）也照常降级，不抛", () => {
  withCapabilities(null, true, () => {
    const broken: PendingImage = {
      bytes: new TextEncoder().encode("not an image"),
      mimeType: "image/png",
      name: "broken.png",
      path: "/tmp/broken.png",
    };
    const rendered = renderDraftImage(broken, theme);
    assert.ok(rendered.kind === "text");
    assert.ok(rendered.lines.join("\n").includes("broken.png"));
  });
});

test("M-09：历史附图（字节已取回）走与待发图同一个分派：协议终端 => 缩略图", () => {
  withCapabilities("kitty", true, () => {
    const rendered = renderHistoryImage(REF, PNG_BYTES, theme);
    assert.ok(rendered.kind === "image", "字节在手 + 协议可用 => 与 renderDraftImage 同一分支");
    assert.ok(
      rendered.component.render(80).join("\n").includes("\x1b_G"),
      "kitty 图形协议序列应由 pi-tui 生成（复用而非第二套）",
    );
  });
});

test("M-09：历史附图无协议 => 文本占位（media type + 尺寸 + 展示名）", () => {
  withCapabilities(null, true, () => {
    const rendered = renderHistoryImage(REF, PNG_BYTES, theme);
    assert.ok(rendered.kind === "text");
    const output = rendered.lines.join("\n");
    for (const escape of IMAGE_PROTOCOL_ESCAPES) assert.ok(!output.includes(escape));
    assert.ok(output.includes("shot.png"), "有展示名给展示名");
    // 字节在手时尺寸以**真实字节**的探测为准（1x1 PNG），引用里的宽高只是字节缺席时的兜底。
    assert.ok(output.includes("1x1"));
    assert.ok(output.includes("image/png"));
  });
});

test("M-09：字节未取回（协议终端上也先给占位，字节到了再换缩略图）", () => {
  withCapabilities("kitty", true, () => {
    const rendered = renderHistoryImage(REF, null, theme);
    assert.ok(rendered.kind === "text", "字节不在手绝不渲染假缩略图");
    const output = rendered.lines.join("\n");
    for (const escape of IMAGE_PROTOCOL_ESCAPES) assert.ok(!output.includes(escape));
    assert.ok(output.includes("2x3"), "字节缺席时占位用引用里的宽高兜底");
  });
});

test("M-09：无展示名（服务端结构性缺省）=> 用 attachment_id 兜底，不编造文件名", () => {
  withCapabilities(null, true, () => {
    const rendered = renderHistoryImage(ANON_REF, null, theme);
    assert.ok(rendered.kind === "text");
    assert.ok(
      rendered.lines.join("\n").includes("sha256:" + "e".repeat(8)),
      "占位里应出现 attachment_id 前缀（真实 id，不伪造）",
    );
  });
});
