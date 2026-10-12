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
  handleSubmit(text: string): Promise<void>;
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

test("B2-01（#984 D6）：贴 20 删光再贴 20 ⇒ 数组有界（40），held bytes 不随循环线性增长", () => {
  const app = makeApp();
  for (let i = 0; i < 20; i++) app.addPendingImage(image(`a-${String(i)}.png`));
  app.editor.setText("第一批不要了");
  // 第二批每张贴入时积压恰为 20（未超过上限）：全部放行，数组到 40。
  for (let i = 0; i < 20; i++) app.addPendingImage(image(`b-${String(i)}.png`));
  assert.equal(app.pendingImages.length, 40, "第二批 20 张全部入列");
  app.editor.setText("第二批也不要了");
  app.addPendingImage(image("c.png"));
  assert.equal(app.pendingImages.length, 40, "积压超过 20 张后再贴被拒：数组停在 40");
  const held = app.pendingImages.reduce((sum, item) => sum + item.bytes.length, 0);
  assert.equal(held, 40 * PNG_BYTES.length, "held bytes 有界");
});

test("B2-01（#984）：积压超过 20 张后再贴 ⇒ 明确拒绝（不进数组、不插标记、留原因与出路）", () => {
  const app = makeApp();
  for (let i = 0; i < 20; i++) app.addPendingImage(image(`ok-${String(i)}.png`));
  app.editor.setText("");
  app.addPendingImage(image("fresh.png"));
  assert.equal(app.pendingImages.length, 21, "积压恰 20 张时仍放行（与 F1 同一边界）");
  app.editor.setText("");
  app.addPendingImage(image("over.png"));
  assert.equal(app.pendingImages.length, 21, "积压 21 张后再贴被拒");
  assert.ok(!app.editor.getText().includes("[Image #22]"), "被拒不插标记");
  // 提示较长：用宽渲染取全文（80 列会折行截断短语，与 app-images P1 用例同形）。
  const notes = app.chatContainer.children
    .map((child) => child.render(500).join("\n"))
    .join("\n");
  assert.ok(notes.includes("未被正文引用"), "点明计数口径");
  assert.ok(notes.includes("20 张"), "给出上限值（与单条消息图片上限同口径）");
  assert.ok(notes.includes("提交当前草稿"), "给出路：提交即清场");
  assert.ok(notes.includes("草稿为空时先输入任意内容"), "出路覆盖空草稿边界（审查 F1）");
});

test("B2-01（#984 审查 F2）：按 M-21 指引删标记后撞上积压闸门，拒绝文案的出路仍然可用", async () => {
  // 场景（审查 Correctness F2）：20 引用 + 20 死图时贴图被 M-21 拒；用户照指引删
  // 1 个标记（19 引用 + 21 死）再贴，撞上积压闸门。断言这次的文案出路（提交清场）
  // 真实可走：提交后队列清空、可继续贴图，指引环有终态。
  const app = makeApp();
  for (let i = 0; i < 20; i++) app.addPendingImage(image(`a-${String(i)}.png`));
  app.editor.setText("");
  for (let i = 0; i < 20; i++) app.addPendingImage(image(`b-${String(i)}.png`));
  assert.equal(app.pendingImages.length, 40);
  app.editor.setText("[Image #1] 引用一张");
  app.addPendingImage(image("over.png"));
  assert.equal(app.pendingImages.length, 40, "19 引用 + 21 未引用：积压闸门拒绝");
  const notes = app.chatContainer.children
    .map((child) => child.render(500).join("\n"))
    .join("\n");
  assert.ok(notes.includes("先提交当前草稿"), "给出可用的出路");
  // 提交前删掉标记（无引用：无视觉预检、零上传），只验证出路本身可走。
  app.editor.setText("");
  await app.handleSubmit("按出路清场");
  assert.equal(app.pendingImages.length, 0, "出路真实可用：提交即清场");
  app.addPendingImage(image("new.png"));
  assert.equal(app.pendingImages.length, 1, "清场后恢复可贴");
});

test("B2-01（#984）：提交草稿即清场，之后可继续贴图（拒绝提示给出的出路真实可用）", async () => {
  const app = makeApp();
  for (let i = 0; i < 20; i++) app.addPendingImage(image(`ok-${String(i)}.png`));
  app.editor.setText("");
  app.addPendingImage(image("fresh.png"));
  app.editor.setText("");
  app.addPendingImage(image("over.png"));
  assert.equal(app.pendingImages.length, 21);
  // 提交纯文字草稿（无标记：无视觉预检、零上传），队列清空。
  await app.handleSubmit("清空队列");
  assert.equal(app.pendingImages.length, 0, "提交后待发数组清空");
  app.addPendingImage(image("new.png"));
  assert.equal(app.pendingImages.length, 1, "清场后恢复可贴");
});
