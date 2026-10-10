/**
 * M-21：单条「数量 / 总字节」客户端预检的接线（app 装配层）。
 *
 * `addPendingImage` 是全部图片入场（剪贴板 / 粘贴路径 / `@path` 参数 / 提交窗口保留）
 * 的唯一汇聚点，预检装在这里 ⇒ 四个入口自动同守。判定纯函数在 image-paste.ts
 * （checkMessageImageLimits），本文件只钉接线行为：被拒的图不进数组、不插标记、
 * chat 区留一行明确原因（不静默）；未超限的既有行为逐字不变。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { TuiApp } from "../src/app.ts";
import { PNG_BYTES } from "./fixtures.ts";
import type { PendingImage } from "../src/lib/pending-images.ts";

function image(name: string): PendingImage {
  return { bytes: PNG_BYTES, mimeType: "image/png", name, path: null };
}

interface AppInternals {
  pendingImages: PendingImage[];
  editor: { getText(): string; setText(text: string): void };
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

test("M-21 修回（F1）：删光标记后预检与提交同一谓词，仍被引用的图才计数", () => {
  const app = makeApp();
  for (let i = 0; i < 20; i++) app.addPendingImage(image(`ok-${String(i)}.png`));
  // 用户在正文里删掉全部 [Image #N] 标记：这 20 张已撤销（提交路径不会发送它们，
  // 服务端按实际 refs 计数本会接受新图）。预检若仍按数组全量计数就是误拒。
  app.editor.setText("这些图都不要了");
  app.addPendingImage(image("fresh.png"));
  assert.equal(app.pendingImages.length, 21, "被删标记的图不计入上限，新图应进数组");
  assert.ok(app.editor.getText().includes("[Image #21]"), "新图照常插标记");
});
