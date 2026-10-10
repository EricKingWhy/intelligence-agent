/**
 * SSE 重连熔断（#843，W-21 D13）：Host 被 kill 后桌面以**新端口**重开服务，
 * 已附着的 TUI 不重解析端点文件，旧 base 永久 `fetch failed`；修复前表现为
 * 无声的永久重连风暴（每秒一行 `stream reconnect: TypeError: fetch failed`）。
 *
 * 本票裁决（用户 2026-10-10）：只在**连续失败达阈值后停止重试 + 给出可见提示**；
 * 重解析端点文件并迁移 base 明确不做（scope 锁死）。
 *
 * 覆盖：
 * ① 连续 N 次失败即熔断，且**不再发起第 N+1 次**；
 * ② 熔断后 chat 区与 footer 各有一条可见提示（不是无声滚动）；
 * ③ 干净收束（`ended` = Host 活着）重置连续计数，阈值内照常重连；
 * ④ `truncated` 是重建路径、不计入失败；
 * ⑤ 切会话重置熔断（新会话重新给满阈值，不被上一会话的耗尽状态锁死）。
 *
 * 全部经**注入 fetch**（`AppOptions.fetchImpl`）驱动：不碰真网络、不碰真 TTY。
 * `TuiApp` 非 TTY 可构造（与 `app.test.ts` / `app-images.test.ts` 同一套受控 cast 访问面）。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { TuiApp } from "../src/app.ts";
import type { ConversationState } from "../src/adapter.ts";

/** 一次 /stream 订阅的行为脚本。 */
type Step = "error" | "ended" | "truncated";

interface AppInternals {
  state: ConversationState;
  running: boolean;
  reconnectFailures: number;
  streamStopped: boolean;
  reconnectTimer: ReturnType<typeof setTimeout> | null;
  streamHintText: { render(width: number): string[] } | null;
  chatContainer: { children: { render(width: number): string[] }[] };
  subscribeLoop(): void;
  switchSession(sessionId: string): Promise<void>;
}

interface Harness {
  app: AppInternals;
  /** /stream 被调用的次数（证明熔断后不再重试）。 */
  streamCalls: () => number;
}

/** 轮询等待条件成立（真计时器路径：重连退避是 1s，测试不假设固定 sleep）。 */
async function waitFor(pred: () => boolean, timeoutMs = 20000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (pred()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error("waitFor 超时：条件始终不成立");
}

/** 让本测试起的订阅循环停下来（否则 1s 退避定时器会一直挂住 node:test）。 */
function halt(app: AppInternals): void {
  app.running = false;
  if (app.reconnectTimer !== null) clearTimeout(app.reconnectTimer);
}

function sseResponse(body: string): Response {
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

/** 第 n 次 /stream 订阅按 `steps[n]` 行为；超出脚本一律 "error"。 */
function makeHarness(steps: Step[]): Harness {
  let streamCalls = 0;
  const fetchFn = (async (input: Parameters<typeof fetch>[0]) => {
    const url = String(input);
    if (!url.includes("/stream")) {
      // GET /events（truncated 后的全量重建）：空历史即可。
      return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
    }
    const step = steps[streamCalls] ?? "error";
    streamCalls += 1;
    if (step === "error") throw new TypeError("fetch failed");
    if (step === "ended") return sseResponse("");
    // truncated 控制帧（无 seq，键序无关；parseEnvelope 只认字段名）。
    return sseResponse(
      `data: ${JSON.stringify({
        type: "stream/truncated",
        data: { latest_seq: 0 },
        seq: null,
        run_id: null,
        step_id: null,
        session_id: "s1",
        time: "2026-10-10T00:00:00Z",
        durability: "transient",
      })}\r\n\r\n`,
    );
  }) as typeof fetch;
  const app = new TuiApp(
    { baseUrl: "http://127.0.0.1:0", sessionId: "s1", fetchImpl: fetchFn },
    false,
  ) as unknown as AppInternals;
  return { app, streamCalls: () => streamCalls };
}

function chatText(app: AppInternals): string {
  return app.chatContainer.children.map((c) => c.render(200).join("\n")).join("\n");
}

function bannerText(app: AppInternals): string {
  return app.streamHintText?.render(200).join("\n") ?? "";
}

test("#843 连续重连失败达阈值即熔断，且不再发起下一次订阅", async () => {
  const { app, streamCalls } = makeHarness(["error", "error", "error", "error", "error", "error"]);
  app.subscribeLoop();
  try {
    await waitFor(() => app.streamStopped);
    assert.equal(streamCalls(), 5, "恰好在第 5 次失败后停止，第 6 次不得再发");
    assert.equal(app.reconnectFailures, 5);
    // 熔断后再等一个退避周期：仍不得有新订阅（不是「少重试一次」而是停）。
    await new Promise((resolve) => setTimeout(resolve, 1200));
    assert.equal(streamCalls(), 5, "熔断后不得继续重试");
  } finally {
    halt(app);
  }
});

test("#843 熔断给出可见提示：chat 一行 + footer 常驻横幅", async () => {
  const { app } = makeHarness(["error", "error", "error", "error", "error"]);
  app.subscribeLoop();
  try {
    await waitFor(() => app.streamStopped);
    const chat = chatText(app);
    assert.ok(chat.includes("Host 连接已断开"), `chat 区应有明确提示，实际：${chat}`);
    assert.ok(chat.includes("请重开 TUI"), "提示须给出动作，而不是只说连接失败");
    const banner = bannerText(app);
    assert.ok(banner.includes("Host 连接已断开"), `footer 常驻横幅须在场，实际：${banner}`);
    assert.ok(banner.includes("ia-tui --session s1"), "横幅须给出可直接照抄的重开命令");
  } finally {
    halt(app);
  }
});

test("#843 干净收束重置连续计数：阈值内仍正常重连", async () => {
  // 前 4 次失败，第 5 次干净收束（Host 活着）-> 计数归零，不熔断。
  const { app, streamCalls } = makeHarness(["error", "error", "error", "error", "ended", "error"]);
  app.subscribeLoop();
  try {
    await waitFor(() => streamCalls() >= 5);
    assert.equal(app.streamStopped, false, "流干净收束证明 Host 活着，不得熔断");
    assert.equal(app.reconnectFailures, 0, "ended 须把连续失败计数归零");
    assert.equal(bannerText(app), "", "未熔断不得挂提示横幅");
  } finally {
    halt(app);
  }
});

test("#843 truncated 是重建路径、不计入失败（阈值按纯 error 计）", async () => {
  // 1 次 truncated + 5 次 error：若 truncated 被计入失败，第 5 次调用就会熔断（少一次）。
  const { app, streamCalls } = makeHarness([
    "truncated",
    "error",
    "error",
    "error",
    "error",
    "error",
  ]);
  app.subscribeLoop();
  try {
    await waitFor(() => app.streamStopped);
    assert.equal(streamCalls(), 6, "truncated 不占失败额度：恰在第 5 次 error 后熔断");
    assert.equal(app.reconnectFailures, 5);
  } finally {
    halt(app);
  }
});

test("#843 切会话重置熔断：新会话重新给满阈值，不被上一会话锁死", async () => {
  const { app } = makeHarness(["error", "error", "error", "error", "error"]);
  app.subscribeLoop();
  try {
    await waitFor(() => app.streamStopped);
    // 切会话会自己起新订阅循环：本测试只验证熔断状态被重置，循环用空实现顶掉。
    app.subscribeLoop = () => {};
    await app.switchSession("s2");
    assert.equal(app.streamStopped, false, "新会话不得继承上一会话的熔断状态");
    assert.equal(app.reconnectFailures, 0);
    assert.equal(bannerText(app), "", "切会话后提示横幅须撤下");
  } finally {
    halt(app);
  }
});
