/**
 * M-21：单条「数量 / 总字节」客户端预检的接线（app 装配层）。
 *
 * `addPendingImage` 是全部图片入场（剪贴板 / 粘贴路径 / `@path` 参数 / 提交窗口保留）
 * 的唯一汇聚点，预检装在这里 ⇒ 三个入口自动同守。判定纯函数在 image-paste.ts
 * （checkMessageImageLimits），本文件只钉接线行为：被拒的图不进数组、不插标记、
 * chat 区留一行明确原因（不静默）；未超限的既有行为逐字不变。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { TuiApp } from "../src/app.ts";
import type { PendingImage } from "../src/lib/pending-images.ts";

/** 1x1 PNG（与 app-images.test.ts 同一枚；本文件不读盘、不碰剪贴板）。 */
const PNG_BYTES = Uint8Array.from([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
  0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
  0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00,
  0x1f, 0x15, 0xc4, 0x89,
  0x00, 0x00, 0x00, 0x00, 0x49, 0x44, 0x41, 0x54, 0xae, 0x42, 0x60, 0x82,
]);

function image(name: string): PendingImage {
  return { bytes: PNG_BYTES, mimeType: "image/png", name, path: null };
}

interface AppInternals {
  pendingImages: PendingImage[];
  editor: { getText(): string };
  chatContainer: { children: { render(width: number): string[] }[] };
  addPendingImage(image: PendingImage): void;
}

function makeApp(): AppInternals {
  const fetchFn = (async () =>
    new Response(JSON.stringify({}), {
      status: 200,
      headers: { "content-type": "application/json" },
    })) as unknown as typeof fetch;
  return new TuiApp(
    {
      baseUrl: "http://127.0.0.1:0",
      sessionId: "s1",
      fetchImpl: fetchFn,
      platform: process.platform,
      env: {},
    },
    false,
  ) as unknown as AppInternals;
}

function chatText(app: AppInternals): string {
  return app.chatContainer.children
    .map((child) => child.render(80).join("\n"))
    .join("\n");
}

test("M-21：第 21 张被拒（数组停在 20、正文标记不插、chat 区留原因）", () => {
  const app = makeApp();
  for (let i = 0; i < 20; i++) app.addPendingImage(image(`ok-${String(i)}.png`));
  assert.equal(app.pendingImages.length, 20);
  app.addPendingImage(image("over.png"));
  assert.equal(app.pendingImages.length, 20, "超限的图不进数组");
  assert.ok(!app.editor.getText().includes("[Image #21]"), "被拒不插标记");
  const notes = chatText(app);
  assert.ok(notes.includes("最多 20 张"), "拒绝理由要说明上限");
});

test("M-21：未超限时既有行为逐字不变（数组 + 标记同步增长）", () => {
  const app = makeApp();
  app.addPendingImage(image("a.png"));
  app.addPendingImage(image("b.png"));
  assert.equal(app.pendingImages.length, 2);
  assert.ok(app.editor.getText().includes("[Image #2]"));
});
