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
 * ⑥ truncated（#859）：先等 GET /events 重建完成、再按重建游标续订；重建失败与连接失败同一退避/额度。
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
  cursor: { lastSeq: number };
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

/**
 * 真实形状的 truncated 控制帧：与服务端
 * `json.dumps(build_truncated_control("s1", after_seq=-1, latest_seq=N), ensure_ascii=False)`
 * 逐字节相同（`src/agent_harness/web/serialization.py`）——**不带 `time`**（#859）。
 */
function truncatedFrame(latestSeq: number): string {
  return `data: {"type": "stream/truncated", "data": {"after_seq": -1, "latest_seq": ${latestSeq}}, "seq": null, "run_id": null, "step_id": null, "session_id": "s1", "schema_version": "runtime_event/v1", "durability": "transient"}`;
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
    return sseResponse(`${truncatedFrame(0)}\r\n\r\n`);
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

// ---------------------------------------------------------------------------
// truncated 重建与续订的先后（#859 范围扩展，用户 2026-10-11 00:38 批准）。
//
// ADR-0016 §2.3：收到 `stream/truncated` ⇒ 先 GET /events 全量重建，**再**带
// `after_seq=<重建后真实 max seq>` 重连。对齐 web 端 `useSession.doTruncatedRebuild`
// （hold → 等重建 → 以 maxSeq 续传；重建失败走 scheduleReconnect 同一退避/额度）。
// 全部走 app 层：真实无 `time` 的控制帧经 consumeSseBody → onTruncated → 重建。
// ---------------------------------------------------------------------------

/** 一条真实形状的 durable 事件行（GET /events 与 /stream 同形；带 time）。 */
function userRow(seq: number, content: string): Record<string, unknown> {
  return {
    type: "user/message",
    data: { content },
    seq,
    run_id: null,
    step_id: null,
    session_id: "s1",
    time: "Sun 2026-10-11 0:38 AM CST (UTC+08:00)",
    schema_version: "runtime_event/v1",
    durability: "durable",
  };
}

interface RebuildHarness {
  app: AppInternals;
  /** 按发生顺序记录的请求：`stream:<after_seq>` 或 `events`。 */
  log: string[];
  /** 每次 /stream 请求发起的时刻（ms），用于证明失败路径有退避、不空转。 */
  streamTimes: number[];
  /** 放行当前挂起的 GET /events（只对 `holdEvents` 模式有效）。 */
  releaseEvents: () => void;
}

/**
 * - `/stream` 第 n 次按 `streams[n]` 返回响应体（超出脚本：返回空体 = 干净收束）；
 * - `/events`：`"hold"` = 挂起直到 `releaseEvents()`，然后返回 `history`；`"fail"` = 恒 HTTP 500。
 */
function makeRebuildHarness(
  streams: string[],
  events: "hold" | "fail",
  history: Record<string, unknown>[] = [],
): RebuildHarness {
  const log: string[] = [];
  const streamTimes: number[] = [];
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let streamCalls = 0;
  const fetchFn = (async (input: Parameters<typeof fetch>[0]) => {
    const url = new URL(String(input));
    if (url.pathname.endsWith("/events")) {
      log.push("events");
      if (events === "fail") {
        return new Response('{"detail":"boom"}', {
          status: 500,
          headers: { "content-type": "application/json" },
        });
      }
      await gate;
      return new Response(JSON.stringify(history), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    log.push(`stream:${url.searchParams.get("after_seq")}`);
    streamTimes.push(Date.now());
    const body = streams[streamCalls] ?? "";
    streamCalls += 1;
    return sseResponse(body);
  }) as typeof fetch;
  const app = new TuiApp(
    { baseUrl: "http://127.0.0.1:0", sessionId: "s1", fetchImpl: fetchFn },
    false,
  ) as unknown as AppInternals;
  return { app, log, streamTimes, releaseEvents: () => release() };
}

const HISTORY_0_TO_20 = Array.from({ length: 21 }, (_, seq) => userRow(seq, `history-${seq}`));

test("#859 truncated：重建期间不重连，重建只发一次 GET /events，完成后带重建游标续订", async () => {
  const { app, log, releaseEvents } = makeRebuildHarness(
    [`${truncatedFrame(20)}\r\n\r\n`],
    "hold",
    HISTORY_0_TO_20,
  );
  app.subscribeLoop();
  try {
    await waitFor(() => log.includes("events"));
    // 重建挂起期间给足时间：此时任何 /stream 都是「带旧游标抢跑」。
    await new Promise((resolve) => setTimeout(resolve, 200));
    assert.deepEqual(log, ["stream:-1", "events"], "重建完成前不得重连 /stream");
    releaseEvents();
    await waitFor(() => log.length >= 3);
    // ADR-0016 §2.3 的先后：全量重建 → 再以重建后真实 max seq 续订。
    assert.deepEqual(log.slice(0, 3), ["stream:-1", "events", "stream:20"]);
    assert.equal(log.filter((x) => x === "events").length, 1, "一次 truncated 只重建一次");
    assert.equal(app.cursor.lastSeq, 20);
    assert.equal(app.reconnectFailures, 0, "成功重建不计失败");
  } finally {
    halt(app);
  }
});

test("#859 truncated：重建得到的新 state 不得覆盖续订流已投影的帧（帧不丢）", async () => {
  const live21 = `data: ${JSON.stringify(userRow(21, "live-21"))}\r\n\r\n`;
  const { app, log, releaseEvents } = makeRebuildHarness(
    // 第 2 次订阅（无论何时发起）都投递 seq 21 这一条新事实。
    [`${truncatedFrame(20)}\r\n\r\n`, live21],
    "hold",
    HISTORY_0_TO_20,
  );
  app.subscribeLoop();
  try {
    await waitFor(() => log.includes("events"));
    await new Promise((resolve) => setTimeout(resolve, 200));
    releaseEvents();
    await waitFor(() => log.filter((x) => x.startsWith("stream:")).length >= 2);
    await new Promise((resolve) => setTimeout(resolve, 200));
    const texts = app.state.turns.map((t) => t.text).join("\n");
    assert.ok(texts.includes("history-20"), "重建结果在场");
    assert.ok(texts.includes("live-21"), `续订流投影的 seq 21 不得被重建覆盖丢失，实际：${texts}`);
    assert.equal(app.cursor.lastSeq, 21);
  } finally {
    halt(app);
  }
});

test("#859 truncated 后 GET /events 持续失败：按退避计失败并熔断，不空转", async () => {
  const frame = `${truncatedFrame(20)}\r\n\r\n`;
  const { app, log, streamTimes } = makeRebuildHarness(
    Array.from({ length: 50 }, () => frame),
    "fail",
  );
  app.subscribeLoop();
  try {
    await waitFor(() => app.streamStopped);
    assert.equal(streamTimes.length, 5, "重建失败与连接失败同一额度：恰 5 次后熔断");
    assert.equal(log.filter((x) => x === "events").length, 5, "每次 truncated 只重建一次");
    assert.equal(app.reconnectFailures, 5);
    const gaps = streamTimes.slice(1).map((t, i) => t - (streamTimes[i] ?? t));
    assert.ok(
      gaps.every((gap) => gap >= 900),
      `重建失败后须走 1s 退避，实际间隔（ms）：${gaps.join(", ")}`,
    );
    const chat = chatText(app);
    assert.ok(chat.includes("boom"), `重建失败须留可见原因，实际：${chat}`);
    assert.ok(chat.includes("Host 连接已断开"), "熔断提示照常出现");
  } finally {
    halt(app);
  }
});

// ---------------------------------------------------------------------------
// 切会话期间旧会话的 GET /events 迟到（#958）：重建结果只在发起时的 generation
// 仍是当前时才落地（与 subscribeLoop 的 `gen !== this.generation` 同一守卫）；
// 过期结果丢弃：不改 state、不改 cursor。
// ---------------------------------------------------------------------------

test("#958 切会话后旧会话迟到的 GET /events 结果被丢弃：新会话 state 与游标不受影响", async () => {
  const s1History = HISTORY_0_TO_20;
  const s2History = [0, 1, 2].map((seq) => ({ ...userRow(seq, `s2-${seq}`), session_id: "s2" }));
  const log: string[] = [];
  let releaseS1: () => void = () => {};
  const s1Gate = new Promise<void>((resolve) => {
    releaseS1 = resolve;
  });
  const fetchFn = (async (input: Parameters<typeof fetch>[0]) => {
    const url = new URL(String(input));
    if (url.pathname.endsWith("/events")) {
      const sid = url.pathname.includes("/s2/") ? "s2" : "s1";
      log.push(`events:${sid}`);
      if (sid === "s1") await s1Gate; // 旧会话的重建挂起，直到切会话完成后才放行
      return new Response(JSON.stringify(sid === "s1" ? s1History : s2History), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    log.push(`stream:${url.searchParams.get("after_seq")}`);
    return sseResponse(`${truncatedFrame(20)}\r\n\r\n`);
  }) as typeof fetch;
  const app = new TuiApp(
    { baseUrl: "http://127.0.0.1:0", sessionId: "s1", fetchImpl: fetchFn },
    false,
  ) as unknown as AppInternals;
  app.subscribeLoop();
  try {
    await waitFor(() => log.includes("events:s1"));
    // 新会话的订阅循环与本例无关（其 truncated 重建路径由 #859 例覆盖）：空实现顶掉。
    app.subscribeLoop = () => {};
    await app.switchSession("s2");
    const s2Texts = () => app.state.turns.map((t) => t.text).join(",");
    assert.equal(s2Texts(), "s2-0,s2-1,s2-2", "切会话完成后应是新会话的重建结果");
    assert.equal(app.cursor.lastSeq, 2);
    releaseS1();
    await waitFor(() => log.filter((x) => x.startsWith("events:")).length >= 2);
    await new Promise((resolve) => setTimeout(resolve, 200));
    assert.equal(s2Texts(), "s2-0,s2-1,s2-2", "旧会话迟到的重建结果不得写入新会话 state");
    assert.equal(app.cursor.lastSeq, 2, "旧会话的 maxSeq 不得回填到新会话游标");
  } finally {
    halt(app);
  }
});
