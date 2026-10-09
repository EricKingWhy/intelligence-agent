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
import { test } from "node:test";
import { pathToFileURL } from "node:url";

import { resetCapabilitiesCache, setCapabilities } from "@earendil-works/pi-tui";

import { renderDraftImage } from "../src/lib/image-view.ts";
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
    // 期望值按**运行时平台**生成：`imageFallback` 用 `pathToFileURL` 造链接，同一条
    // `/home/u/...` 在 Windows 上会得到 `file:///D:/home/u/...`（#830 D3：写死
    // `file:///home/u/...` 只在 POSIX 上成立）。
    assert.ok(
      output.includes(pathToFileURL(IMAGE_PATH).href),
      "支持 OSC 8 的终端上路径应是可点击的原图链接",
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
