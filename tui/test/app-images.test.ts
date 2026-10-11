/**
 * TUI 图片输入接线（#827 MM-06）：AC1（Alt+V 剪贴板）、AC2（粘贴路径识别）、
 * AC3（标记 ↔ 待发数组 / 删标记即撤销）、AC4（`@path` 命令行参数）、
 * AC5（上传 + 请求体引用，经注入 fetch 的 adapter 接缝）、AC8（视觉能力提交前拒绝）、
 * AC9（第三方声明），以及独立审查修复项（P1 预检只认"仍被引用的图"、P3 悬空标记/窗口内
 * 新图/footer 投影/切会话清场、P4 单图上限提示）。
 *
 * 全部经**注入的 fetch**（`AppOptions.fetchImpl`）与**注入的剪贴板读取器**
 * （`AppOptions.readClipboardImage`）驱动：不碰真网络、不碰真剪贴板、不用真计时器。
 * `TuiApp` 非 TTY 可构造（与 `app.test.ts` 同一套受控 cast 访问面）。
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { TuiApp } from "../src/app.ts";
import { PNG_BYTES } from "./fixtures.ts";
import type { ConversationState } from "../src/adapter.ts";
import type { PendingImage } from "../src/lib/pending-images.ts";
import type { ClipboardImage } from "../src/lib/clipboard-image.ts";

/** 仓库内的真图片 fixture（AC2/AC4 要真读盘）。 */
// `URL.pathname` 在 Windows 上给 `/D:/...`（前导斜杠 + 正斜杠），`statSync` 解不出来
// => 图被判"读不到"（#830 D3）。`fileURLToPath` 是本平台正确的转换。
const FIXTURE_PNG = fileURLToPath(new URL("./fixtures/shot.png", import.meta.url));
const MISSING_PNG = fileURLToPath(new URL("./fixtures/not-there.png", import.meta.url));

/** 让挂起的异步剪贴板读取跑完（纯微任务，不涉真计时器）。 */
async function settle(): Promise<void> {
  for (let tick = 0; tick < 5; tick++) await Promise.resolve();
}

interface RecordedCall {
  url: string;
  /** JSON 请求体的原文（上传的字节体不解析）。 */
  body: string;
}

interface AppInternals {
  state: ConversationState;
  pendingImages: PendingImage[];
  editor: { getText(): string; setText(text: string): void };
  imagesContainer: { children: { render(width: number): string[] }[] };
  /** chat 区（提示语断言：#2 拒绝理由、#3 窗口保留提示都在这里）。 */
  chatContainer: { children: { render(width: number): string[] }[] };
  interceptKeys(data: string): { consume: boolean } | undefined;
  handleSubmit(text: string): Promise<void>;
  /** 切会话（#6）：会 `rebuildFromHistory()`（走 GET /events）+ `subscribeLoop()`。 */
  switchSession(sessionId: string): Promise<void>;
  /** 订阅循环（#6 测试要把它换成空实现，否则真 SSE 循环 + 1s 重连定时器会吊住 node:test）。 */
  subscribeLoop(): void;
}

interface Harness {
  app: AppInternals;
  calls: RecordedCall[];
  /** 置 true 后上传一律 500（AC5 失败路径断言）。 */
  failUploads: { value: boolean };
  /** 剪贴板读取次数（证明"一次按键只读一次剪贴板"）。 */
  clipboardReads: { count: number };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function makeHarness(options?: {
  models?: unknown;
  modelsFail?: boolean;
  /** 视觉预检（GET /api/models）在途时先挂起（复审 N1：模拟"预检窗口里用户又贴了一张"）。 */
  modelsGate?: Promise<void>;
  clipboard?: ClipboardImage | null;
  clipboardThrows?: boolean;
  initialImages?: string[];
  platform?: NodeJS.Platform;
  env?: NodeJS.ProcessEnv;
  /** 上传在途时执行（#3：模拟"上传窗口里用户又贴了一张"）。 */
  onUpload?: (app: AppInternals) => void;
}): Harness {
  const calls: RecordedCall[] = [];
  const failUploads = { value: false };
  const clipboardReads = { count: 0 };
  let uploadCount = 0;
  const fetchFn = (async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, body: typeof init?.body === "string" ? init.body : "" });
    if (url.includes("/attachments")) {
      if (failUploads.value) return new Response("boom", { status: 500 });
      options?.onUpload?.(app);
      uploadCount += 1;
      return jsonResponse({
        attachment_id: `sha256:img${String(uploadCount)}`,
        media_type: "image/png",
        bytes: PNG_BYTES.length,
        width: 1,
        height: 1,
        name: "clipboard.png",
      });
    }
    if (url.includes("/api/models")) {
      if (options?.modelsFail === true) return new Response("nope", { status: 503 });
      if (options?.modelsGate) await options.modelsGate;
      return jsonResponse({ models: options?.models ?? [] });
    }
    // GET /events（切会话/启动时的全量重建）：空历史。
    if (url.includes("/events")) return jsonResponse([]);
    return jsonResponse({ status: "queued" });
  }) as unknown as typeof fetch;

  const app = new TuiApp(
    {
      baseUrl: "http://127.0.0.1:0",
      sessionId: "s1",
      fetchImpl: fetchFn,
      // 默认取**宿主**平台：本文件里 AC2/AC4/P3/N1 会把仓库内的真 fixture 路径喂进去
      // （`FIXTURE_PNG` 由 `fileURLToPath` 产出），而路径语义由注入的 platform 决定
      // （`resolvePastedImagePath` 在非 win32 上做 `\<char>` 反转义）——写死 "linux"
      // 时 Windows 上的 `D:\...` 会被反转义毁掉 ⇒ 图读不回来（#830 D3 余下的 3 例）。
      platform: options?.platform ?? process.platform,
      env: options?.env ?? {},
      initialImages: options?.initialImages,
      readClipboardImage: async () => {
        clipboardReads.count += 1;
        if (options?.clipboardThrows === true) throw new Error("剪贴板坏了");
        if (options?.clipboard === null) return null;
        return options?.clipboard ?? { bytes: PNG_BYTES, mimeType: "image/png" };
      },
    },
    false,
  ) as unknown as AppInternals;
  return { app, calls, failUploads, clipboardReads };
}

/** 消息 POST 的请求体（找不到就返回空数组，由断言暴露）。 */
function messageBodies(calls: RecordedCall[]): unknown[] {
  return calls
    .filter((call) => call.url.includes("/messages"))
    .map((call) => JSON.parse(call.body));
}

/** 视觉能力可用的模型目录（走"允许附图"的分支用）。 */
const VISION_MODELS = [{ id: "m", model: "m", is_default: true, supports_vision: true }];

test("AC1：Alt+V（legacy \\x1bv）取剪贴板图 ⇒ 待发数组 1 张 + 正文插 [Image #1]", async () => {
  const harness = makeHarness();
  const outcome = harness.app.interceptKeys("\x1bv");
  assert.deepEqual(outcome, { consume: true });
  await settle();
  assert.equal(harness.clipboardReads.count, 1);
  assert.equal(harness.app.pendingImages.length, 1);
  assert.equal(harness.app.pendingImages[0]?.mimeType, "image/png");
  assert.equal(harness.app.pendingImages[0]?.name, "clipboard.png");
  assert.equal(harness.app.pendingImages[0]?.path, null, "剪贴板字节没有本地路径，不编造");
  assert.equal(harness.app.editor.getText(), "[Image #1]");
});

test("AC1：CSI-u 扩展编码 \\x1b[118;3u（kitty/tmux 上报的 Alt+V）同样触发", async () => {
  const harness = makeHarness();
  assert.deepEqual(harness.app.interceptKeys("\x1b[118;3u"), { consume: true });
  await settle();
  assert.equal(harness.app.pendingImages.length, 1);
});

test("AC1：Ctrl+V 任何平台都不拦截（那是终端自己的粘贴文本键；图片只认 Alt+V）", () => {
  const wsl = makeHarness({ env: { WSL_DISTRO_NAME: "Ubuntu" } });
  assert.equal(wsl.app.interceptKeys("\x16"), undefined, "WSL 的 Ctrl+V 交给终端/编辑器");
  assert.equal(wsl.app.pendingImages.length, 0);

  const plain = makeHarness({ env: {} });
  assert.equal(plain.app.interceptKeys("\x16"), undefined, "非 WSL 的 Ctrl+V 同样不拦截");
  assert.equal(plain.app.pendingImages.length, 0);
});

test("AC1：Windows 上 Alt+V 生效（Ctrl+V 交给终端截获）", async () => {
  const win = makeHarness({ platform: "win32" });
  assert.deepEqual(win.app.interceptKeys("\x1bv"), { consume: true });
  assert.equal(win.app.interceptKeys("\x16"), undefined);
  await settle();
  assert.equal(win.app.pendingImages.length, 1);
});

test("AC1：剪贴板没有图 ⇒ 明确提示，不静默、不插标记", async () => {
  const harness = makeHarness({ clipboard: null });
  harness.app.interceptKeys("\x1bv");
  await settle();
  assert.equal(harness.app.pendingImages.length, 0);
  assert.equal(harness.app.editor.getText(), "");
});

test("AC1：剪贴板读取抛异常 ⇒ catch 并提示（不炸掉 TUI、不插标记）", async () => {
  const harness = makeHarness({ clipboardThrows: true });
  harness.app.interceptKeys("\x1bv");
  await settle();
  assert.equal(harness.app.pendingImages.length, 0);
  assert.equal(harness.app.editor.getText(), "");
});

test("AC2：整块括号粘贴的图片路径 ⇒ 识别为附图并吞掉那段文本", () => {
  const harness = makeHarness();
  const outcome = harness.app.interceptKeys(`\x1b[200~${FIXTURE_PNG}\x1b[201~`);
  assert.deepEqual(outcome, { consume: true });
  assert.equal(harness.app.pendingImages.length, 1);
  assert.equal(harness.app.pendingImages[0]?.path, FIXTURE_PNG);
  assert.equal(harness.app.pendingImages[0]?.name, "shot.png");
  assert.equal(harness.app.editor.getText(), "[Image #1]", "路径文本不落进正文");
});

test("AC2：非图片的粘贴不拦截（多行文本、普通路径照旧交给编辑器）", () => {
  const harness = makeHarness();
  assert.equal(harness.app.interceptKeys("\x1b[200~第一行\n第二行\x1b[201~"), undefined);
  assert.equal(harness.app.interceptKeys("\x1b[200~/tmp/notes.txt\x1b[201~"), undefined);
  assert.equal(harness.app.pendingImages.length, 0);
});

test("AC2：粘的图片路径不存在 ⇒ 报错且不落附图（文本已被当附图处理）", () => {
  const harness = makeHarness();
  const outcome = harness.app.interceptKeys(`\x1b[200~${MISSING_PNG}\x1b[201~`);
  assert.deepEqual(outcome, { consume: true });
  assert.equal(harness.app.pendingImages.length, 0);
  assert.equal(harness.app.editor.getText(), "");
});

test("AC4：@path 命令行参数在启动时进待发数组并插标记（随首轮消息带入）", () => {
  const harness = makeHarness({ initialImages: [FIXTURE_PNG, FIXTURE_PNG] });
  assert.equal(harness.app.pendingImages.length, 2);
  assert.equal(harness.app.editor.getText(), "[Image #1][Image #2]");
});

test("AC4：非图片扩展名的参数不进数组（明确错误，不静默忽略）", () => {
  const harness = makeHarness({ initialImages: ["/tmp/notes.txt"] });
  assert.equal(harness.app.pendingImages.length, 0);
  assert.equal(harness.app.editor.getText(), "");
});

test("AC3+AC5：删掉 1 号标记后提交 ⇒ 只上传被引用的那张，正文重编号", async () => {
  const harness = makeHarness({ models: VISION_MODELS });
  const app = harness.app;
  app.state.modelName = "m";
  app.interceptKeys("\x1bv");
  await settle();
  app.interceptKeys("\x1bv");
  await settle();
  assert.equal(app.pendingImages.length, 2);
  assert.equal(app.editor.getText(), "[Image #1][Image #2]");

  // 用户删掉 1 号标记，只留 2 号（提交时重编号成 1 号）。
  await app.handleSubmit("只发这一张 [Image #2]");

  assert.equal(
    harness.calls.filter((call) => call.url.includes("/attachments")).length,
    1,
    "未被引用的那张不上传",
  );
  assert.deepEqual(messageBodies(harness.calls), [
    { content: "只发这一张 [Image #1]", mode: "queue", attachments: ["sha256:img1"] },
  ]);
  assert.equal(app.pendingImages.length, 0, "提交后待发数组清空");
  assert.equal(app.editor.getText(), "");
});

test("AC3+AC5：删光全部标记 ⇒ 附件零上传、请求体不带 attachments 键", async () => {
  const harness = makeHarness({ models: VISION_MODELS });
  const app = harness.app;
  app.state.modelName = "m";
  app.interceptKeys("\x1bv");
  await settle();

  await app.handleSubmit("图片标记都删了，只发文字");

  assert.equal(harness.calls.filter((call) => call.url.includes("/attachments")).length, 0);
  assert.deepEqual(messageBodies(harness.calls), [
    { content: "图片标记都删了，只发文字", mode: "queue" },
  ]);
  assert.equal(app.pendingImages.length, 0);
});

test("AC5：两张图都引用 ⇒ 各上传一次，请求体按正文引用序给 id", async () => {
  const harness = makeHarness({ models: VISION_MODELS });
  const app = harness.app;
  app.state.modelName = "m";
  app.interceptKeys("\x1bv");
  await settle();
  app.interceptKeys("\x1bv");
  await settle();

  await app.handleSubmit("[Image #1] 和 [Image #2]");

  assert.equal(harness.calls.filter((call) => call.url.includes("/attachments")).length, 2);
  assert.deepEqual(messageBodies(harness.calls), [
    {
      content: "[Image #1] 和 [Image #2]",
      mode: "queue",
      attachments: ["sha256:img1", "sha256:img2"],
    },
  ]);
});

test("AC5：上传走真字节 + 声明类型与名字（服务端按它比对字节判定）", async () => {
  const harness = makeHarness({ models: VISION_MODELS });
  const app = harness.app;
  app.state.modelName = "m";
  app.interceptKeys("\x1bv");
  await settle();

  await app.handleSubmit("[Image #1]");

  const upload = harness.calls.find((call) => call.url.includes("/attachments"));
  assert.ok(upload?.url.includes("?name=clipboard.png"), "剪贴板图的声明名带正确扩展名");
});

test("AC8：模型 supports_vision=false ⇒ 提交前拒绝，草稿与图片都保留", async () => {
  const harness = makeHarness({
    models: [{ id: "text-only", model: "text-only", is_default: true, supports_vision: false }],
  });
  const app = harness.app;
  app.state.modelName = "text-only";
  app.interceptKeys("\x1bv");
  await settle();

  await app.handleSubmit("[Image #1] 看看这个");

  assert.equal(harness.calls.length, 1, "只查了 /api/models：没上传、也没发消息");
  assert.ok(harness.calls[0]?.url.includes("/api/models"));
  assert.equal(app.pendingImages.length, 1, "拒绝后图片保留");
  assert.equal(app.editor.getText(), "[Image #1]", "拒绝后正文保留");
});

test("AC8：能力位缺席（未知）同样拒绝（与服务端同一失败关闭口径）", async () => {
  const harness = makeHarness({ models: [{ id: "m", model: "m", is_default: true }] });
  const app = harness.app;
  app.state.modelName = "m";
  app.interceptKeys("\x1bv");
  await settle();

  await app.handleSubmit("[Image #1] 看看这个");

  assert.equal(messageBodies(harness.calls).length, 0);
  assert.equal(app.pendingImages.length, 1);
});

test("AC8：目录查不到（网络失败）⇒ 不预检，让服务端 422 做权威判定", async () => {
  const harness = makeHarness({ modelsFail: true });
  const app = harness.app;
  app.interceptKeys("\x1bv");
  await settle();

  await app.handleSubmit("[Image #1] 看看这个");

  assert.deepEqual(messageBodies(harness.calls), [
    { content: "[Image #1] 看看这个", mode: "queue", attachments: ["sha256:img1"] },
  ]);
});

test("AC5：上传失败 ⇒ 明确提示且草稿保留（不吞图、不静默）", async () => {
  const harness = makeHarness({ models: VISION_MODELS });
  const app = harness.app;
  app.state.modelName = "m";
  app.interceptKeys("\x1bv");
  await settle();
  harness.failUploads.value = true;

  await app.handleSubmit("[Image #1] 看看这个");

  assert.equal(messageBodies(harness.calls).length, 0, "上传失败就不投消息");
  assert.equal(app.pendingImages.length, 1, "图片保留待重发");
  assert.equal(app.editor.getText(), "[Image #1]", "正文保留");
});

test("AC6/AC7 接线：待发图片区挂进 footer（说明行 + 每张图一个渲染件）", async () => {
  const harness = makeHarness();
  harness.app.interceptKeys("\x1bv");
  await settle();

  const children = harness.app.imagesContainer.children;
  assert.equal(children.length, 2, "说明行 + 1 张图");
  const header = children[0]?.render(120).join("\n") ?? "";
  assert.ok(header.includes("待发图片 1 张"), "footer 说明行如实给张数");
  const view = children[1]?.render(120).join("\n") ?? "";
  assert.ok(view.includes("clipboard.png"), "图片件给出文件名/路径");
});

test("AC6/AC7 接线：提交后待发图片区清空（不留空壳）", async () => {
  const harness = makeHarness({ models: VISION_MODELS });
  const app = harness.app;
  app.state.modelName = "m";
  app.interceptKeys("\x1bv");
  await settle();
  assert.equal(app.imagesContainer.children.length, 2);

  await app.handleSubmit("[Image #1]");

  assert.equal(app.imagesContainer.children.length, 0);
});

test("独立审查 P1：正文里没有 [Image #N] 标记 ⇒ 不做视觉预检（只发文字不许被永久拒）", async () => {
  const harness = makeHarness({
    models: [{ id: "text-only", model: "text-only", is_default: true, supports_vision: false }],
  });
  const app = harness.app;
  app.state.modelName = "text-only";
  app.interceptKeys("\x1bv");
  await settle();
  assert.equal(app.pendingImages.length, 1);

  // 用户把正文里的标记删了只发文字（handleSubmit 收到的 = 提交那一刻的正文，与真实一致）。
  await app.handleSubmit("图片标记都删了，只发文字");

  assert.equal(
    harness.calls.filter((call) => call.url.includes("/api/models")).length,
    0,
    "没有引用任何图 ⇒ 连目录都不查",
  );
  assert.equal(harness.calls.filter((call) => call.url.includes("/attachments")).length, 0);
  assert.deepEqual(messageBodies(harness.calls), [
    { content: "图片标记都删了，只发文字", mode: "queue" },
  ]);
  assert.equal(app.pendingImages.length, 0, "删标记即撤销：待发数组清空");
  assert.equal(app.editor.getText(), "");
});

test("独立审查 P1：拒绝提示点明判定依据 + 给两条出路", async () => {
  const harness = makeHarness({
    models: [{ id: "text-only", model: "text-only", is_default: true, supports_vision: false }],
  });
  const app = harness.app;
  app.state.modelName = "text-only";
  app.interceptKeys("\x1bv");
  await settle();

  await app.handleSubmit("[Image #1] 看看这个");

  const notes = app.chatContainer.children.map((child) => child.render(500).join("\n")).join("\n");
  assert.ok(notes.includes("supports_vision=false"), "点明判定依据");
  assert.ok(notes.includes("只发文字"), "出路一：删标记只发文字");
  assert.ok(notes.includes("换用支持视觉的模型"), "出路二：换模型");
  assert.equal(app.pendingImages.length, 1, "拒绝后图片保留");
  assert.equal(app.editor.getText(), "[Image #1]", "拒绝后正文保留");
});

test("独立审查 P4：提交时正文里的悬空标记（手打 [Image #9]）被删掉，不发给模型", async () => {
  const harness = makeHarness({ models: VISION_MODELS });
  const app = harness.app;
  app.state.modelName = "m";
  app.interceptKeys("\x1bv");
  await settle();
  app.editor.setText("[Image #1] 和 [Image #9] 都看看");

  await app.handleSubmit("[Image #1] 和 [Image #9] 都看看");

  assert.deepEqual(messageBodies(harness.calls), [
    { content: "[Image #1] 和 都看看", mode: "queue", attachments: ["sha256:img1"] },
  ]);
});

test("独立审查 P3：上传窗口里新贴的图不被吞掉（收尾只清本次快照内的图）", async () => {
  const harness = makeHarness({
    models: VISION_MODELS,
    // 上传在途时用户又贴了一张（走 AC2 的路径粘贴 => 产物可辨识：path/名字与剪贴板图不同）。
    onUpload: (app) => {
      app.interceptKeys(`\x1b[200~${FIXTURE_PNG}\x1b[201~`);
    },
  });
  const app = harness.app;
  app.state.modelName = "m";
  app.interceptKeys("\x1bv");
  await settle();
  assert.equal(app.pendingImages.length, 1);
  // pi-tui 的 submitValue() 先清编辑器再 onSubmit(text)：按真实时序建模（否则本用例不成立）。
  app.editor.setText("");

  await app.handleSubmit("[Image #1] 第一张");

  assert.equal(app.pendingImages.length, 1, "窗口内的新图还在");
  assert.equal(app.pendingImages[0]?.path, FIXTURE_PNG, "留下的正是窗口内新贴的那张");
  assert.equal(app.editor.getText(), "[Image #1]", "新图标记重编号为 1");
  assert.deepEqual(messageBodies(harness.calls), [
    { content: "[Image #1] 第一张", mode: "queue", attachments: ["sha256:img1"] },
  ]);
});

test("复审 N1：视觉预检 listModels 的 await 窗口里新贴的图不被吞掉（快照覆盖全部 await）", async () => {
  // 预检（GET /api/models）挂在一个可控的未决 promise 上（不用真计时器）：提交停在这个
  // 窗口内，此时经 AC2 的路径粘贴注入一张新图。旧代码把快照取在 listModels 之后 =>
  // 新图下标 < 快照 => 收尾被静默清掉；新代码快照前移 => 新图被保留。
  let releaseModels!: () => void;
  const modelsGate = new Promise<void>((resolve) => {
    releaseModels = resolve;
  });
  const harness = makeHarness({ models: VISION_MODELS, modelsGate });
  const app = harness.app;
  app.state.modelName = "m";
  app.interceptKeys("\x1bv");
  await settle();
  assert.equal(app.pendingImages.length, 1);
  // pi-tui 的 submitValue() 先清编辑器再 onSubmit(text)：按真实时序建模。
  app.editor.setText("");

  const submitting = app.handleSubmit("[Image #1] 第一张");
  app.interceptKeys(`\x1b[200~${FIXTURE_PNG}\x1b[201~`);
  assert.equal(app.pendingImages.length, 2, "预检窗口内新贴的图已入列");

  releaseModels();
  await submitting;

  assert.equal(app.pendingImages.length, 1, "窗口内新贴的图不被吞掉");
  assert.equal(app.pendingImages[0]?.path, FIXTURE_PNG, "留下的正是窗口内新贴的那张");
  assert.equal(app.editor.getText(), "[Image #1]", "新图标记重编号为 1");
  const notes = app.chatContainer.children.map((child) => child.render(500).join("\n")).join("\n");
  assert.ok(notes.includes("提交期间新粘贴"), "给出保留提示");
  assert.deepEqual(messageBodies(harness.calls), [
    { content: "[Image #1] 第一张", mode: "queue", attachments: ["sha256:img1"] },
  ]);
});

test("独立审查 P3：footer 图片区跟着正文标记走（删掉标记的那张立刻不再显示）", async () => {
  const harness = makeHarness();
  const app = harness.app;
  app.interceptKeys("\x1bv");
  await settle();
  app.interceptKeys("\x1bv");
  await settle();
  assert.equal(app.imagesContainer.children.length, 3, "说明行 + 2 张");

  app.editor.setText("[Image #2]");

  assert.equal(app.imagesContainer.children.length, 2, "说明行 + 仅剩的 1 张");
  const header = app.imagesContainer.children[0]?.render(120).join("\n") ?? "";
  assert.ok(header.includes("待发图片 1 张"), "计数如实反映可见张数");
});

test("独立审查 P3：切会话清空待发图片并抹掉正文标记（旧会话的图不跟着走）", async () => {
  const harness = makeHarness();
  const app = harness.app;
  app.interceptKeys("\x1bv");
  await settle();
  app.editor.setText("[Image #1] 草稿");
  assert.equal(app.pendingImages.length, 1);
  assert.equal(app.imagesContainer.children.length, 2);
  // 真订阅循环会连 SSE + 1s 重连定时器，node:test 进程会被吊住 => 换空实现。
  app.subscribeLoop = () => {};

  await app.switchSession("s2");

  assert.equal(app.pendingImages.length, 0);
  assert.equal(app.editor.getText(), " 草稿", "标记抹掉，其余草稿原样");
  assert.equal(app.imagesContainer.children.length, 0);
});

test("独立审查 P3/P4：超过 20 MiB 的 @path 参数给出上限提示，不进待发数组", () => {
  const dir = mkdtempSync(join(tmpdir(), "ia827-app-big-"));
  try {
    const big = join(dir, "big.png");
    // 稀疏文件（Node 原生 `truncateSync`，先建空文件——外部 `truncate -s` 才自带创建；那个
    // 二进制在 Windows 上靠 runner 镜像的 Git for Windows coreutils，属外部依赖——复审 P3）。
    writeFileSync(big, "");
    truncateSync(big, 21 * 1024 * 1024);
    const harness = makeHarness({ initialImages: [big] });

    assert.equal(harness.app.pendingImages.length, 0);
    assert.equal(harness.app.editor.getText(), "");
    const notes = harness.app.chatContainer.children
      .map((child) => child.render(500).join("\n"))
      .join("\n");
    assert.ok(notes.includes("20 MiB"), "提示里给出与服务端同口径的上限");
    assert.ok(notes.includes(big), "提示里带上路径");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
