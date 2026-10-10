/**
 * M-09：历史附图在 transcript 里的端到端投影（app 装配层）。
 *
 * - 协议终端（kitty）：`GET /events` 的 user/message 带 attachments 引用 =>
 *   拉取受控端点字节（GET .../attachments/{id}/content）=> 用户轮下方渲染 pi-tui 缩略图；
 * - 无协议终端（images: null）：**一个字节都不取**，文本占位定格（终态）。
 *
 * 能力位是 pi-tui 的进程级缓存（`setCapabilities` 为测试导出），用例内显式设置并重置。
 * 全部经注入的 fetch 驱动，不碰真网络；`TuiApp` 非 TTY 可构造（与 app.test.ts 同一套
 * 受控 cast 访问面）。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { resetCapabilitiesCache, setCapabilities } from "@earendil-works/pi-tui";

import { TuiApp } from "../src/app.ts";

/** 1x1 PNG（合法签名 + IHDR），与 app-images.test.ts 同一枚。 */
const PNG_BYTES = Uint8Array.from([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
  0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
  0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00,
  0x1f, 0x15, 0xc4, 0x89,
  0x00, 0x00, 0x00, 0x00, 0x49, 0x44, 0x41, 0x54, 0xae, 0x42, 0x60, 0x82,
]);

const ATTACHMENT_ID = "sha256:" + "a".repeat(64);

/** 让挂起的字节取回与渲染跑完：宏任务轮询（fetch 的 arrayBuffer 不保证在纯微任务内落定）。 */
async function until(condition: () => boolean): Promise<void> {
  for (let tick = 0; tick < 100 && !condition(); tick++) {
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
}

interface AppInternals {
  state: { turns: { role: string; attachments: unknown[] }[] };
  chatContainer: { children: { render(width: number): string[] }[] };
  rebuildFromHistory(): Promise<void>;
  switchSession(sessionId: string): Promise<void>;
  subscribeLoop(): void;
}

function makeHarness(options?: { contentFails?: boolean }): {
  app: AppInternals;
  contentCalls: { count: number };
} {
  const contentCalls = { count: 0 };
  const events = [
    {
      type: "user/message",
      data: {
        content: "看这张历史图",
        attachments: [
          {
            kind: "image",
            attachment_id: ATTACHMENT_ID,
            media_type: "image/png",
            bytes: PNG_BYTES.length,
            width: 1,
            height: 1,
          },
        ],
      },
      seq: 1,
      run_id: null,
      step_id: 1,
      session_id: "s1",
      time: "2026-10-11T00:00:00Z",
      durability: "durable",
    },
  ];
  const fetchFn = (async (input: Parameters<typeof fetch>[0]) => {
    const url = String(input);
    if (url.includes("/events")) {
      return new Response(JSON.stringify(events), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/content")) {
      contentCalls.count += 1;
      if (options?.contentFails === true) {
        return new Response(JSON.stringify({ detail: "404: 未被引用" }), { status: 404 });
      }
      return new Response(new Blob([PNG_BYTES]), {
        status: 200,
        headers: { "content-type": "image/png" },
      });
    }
    return new Response(JSON.stringify({ status: "queued" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as unknown as typeof fetch;

  const app = new TuiApp(
    {
      baseUrl: "http://127.0.0.1:0",
      sessionId: "s1",
      fetchImpl: fetchFn,
      platform: process.platform,
      env: {},
    },
    false,
  ) as unknown as AppInternals;
  return { app, contentCalls };
}

function chatText(app: AppInternals): string {
  return app.chatContainer.children
    .map((child) => child.render(80).join("\n"))
    .join("\n");
}

test("M-09：协议终端（kitty）里历史附图经受控端点取字节并渲染缩略图", async () => {
  setCapabilities({ images: "kitty", trueColor: true, hyperlinks: true });
  try {
    const { app, contentCalls } = makeHarness();
    await app.rebuildFromHistory();
    await until(() => chatText(app).includes("\x1b_G"));
    assert.equal(contentCalls.count, 1, "字节应从受控端点取回一次");
    const output = chatText(app);
    assert.ok(
      output.includes("\x1b_G"),
      "用户轮下方应出现 pi-tui kitty 缩略图（与待发图同一分派）",
    );
  } finally {
    resetCapabilitiesCache();
  }
});

test("M-09：无协议终端（images: null）文本占位定格，且一个字节都不取", async () => {
  setCapabilities({ images: null, trueColor: true, hyperlinks: true });
  try {
    const { app, contentCalls } = makeHarness();
    await app.rebuildFromHistory();
    await until(() => chatText(app).includes("sha256:"));
    assert.equal(contentCalls.count, 0, "无协议终端不应发起字节取回");
    const output = chatText(app);
    assert.ok(!output.includes("\x1b_G"), "不得出现任何图片协议转义序列");
    assert.ok(output.includes("sha256:"), "占位用真实 attachment_id 兜底（name 结构性缺省）");
    assert.ok(output.includes("image/png"));
    assert.ok(output.includes("1x1"), "占位用引用里的宽高");
  } finally {
    resetCapabilitiesCache();
  }
});

test("M-09：字节取回失败（404）=> 占位定格 + 一行明确提示，不静默不重试风暴", async () => {
  setCapabilities({ images: "kitty", trueColor: true, hyperlinks: true });
  try {
    const { app, contentCalls } = makeHarness({ contentFails: true });
    await app.rebuildFromHistory();
    await until(() => contentCalls.count === 1);
    await until(() => chatText(app).includes("sha256:"));
    assert.equal(contentCalls.count, 1, "失败不重试（started 标记挡住重复发起）");
    const output = chatText(app);
    assert.ok(!output.includes("\x1b_G"), "字节不在手绝不渲染缩略图");
  } finally {
    resetCapabilitiesCache();
  }
});
