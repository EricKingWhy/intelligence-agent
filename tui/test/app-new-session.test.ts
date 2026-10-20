/**
 * #1418：TUI 只有 `/new` 这一条路径携带 `previous_session_id`。
 *
 * 服务端在创建新会话的同一请求里结束前任会话（归档行 end_reason=new_session）。
 * `/sessions` 选择器与 `/quit` 不走这条路径：前者只是切订阅，后者只发
 * client-exit。判据 = 是否 POST `/api/sessions` 且 body 带 previous_session_id。
 *
 * 全部经注入 fetch 驱动（不碰真网络、不碰真 TTY），与 app-reconnect.test.ts
 * 同一套受控 cast 访问面。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { TuiApp } from "../src/app.ts";

interface AppInternals {
  running: boolean;
  reconnectTimer: ReturnType<typeof setTimeout> | null;
  runCommand(command: string): Promise<void>;
  showSessionPicker(): Promise<void>;
  quit(): Promise<void>;
  tui: { showOverlay(component: unknown, options?: unknown): { hide(): void }; stop(): void };
}

interface Call {
  method: string;
  path: string;
  body: unknown;
}

const JSON_HEADERS = { "content-type": "application/json" };

function makeHarness(): { app: AppInternals; calls: Call[] } {
  const calls: Call[] = [];
  const fetchFn = (async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    const url = new URL(String(input));
    calls.push({
      method: init?.method ?? "GET",
      path: url.pathname,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    });
    if (url.pathname === "/api/sessions") {
      if (init?.method === "POST") {
        return new Response(JSON.stringify({ session_id: "s2" }), {
          status: 200,
          headers: JSON_HEADERS,
        });
      }
      return new Response(
        JSON.stringify([
          {
            session_id: "s1",
            event_count: 1,
            first_user_message: "old",
            last_event_time: null,
            archived: false,
          },
        ]),
        { status: 200, headers: JSON_HEADERS },
      );
    }
    if (url.pathname.endsWith("/events")) {
      return new Response("[]", { status: 200, headers: JSON_HEADERS });
    }
    if (url.pathname.endsWith("/client-exit")) {
      return new Response(JSON.stringify({ ok: true }), { status: 200, headers: JSON_HEADERS });
    }
    // /stream：立刻干净收束（running=false 后循环退出，不留定时器）。
    return new Response('data: {"type":"run/completed","data":{},"seq":1}\n\n', {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as typeof fetch;
  const app = new TuiApp(
    { baseUrl: "http://127.0.0.1:0", sessionId: "s1", fetchImpl: fetchFn },
    false,
  ) as unknown as AppInternals;
  return { app, calls };
}

function halt(app: AppInternals): void {
  app.running = false;
  if (app.reconnectTimer !== null) clearTimeout(app.reconnectTimer);
}

test("/new 创建请求携带 previous_session_id=当前会话", async () => {
  const { app, calls } = makeHarness();
  try {
    await app.runCommand("/new");
    const create = calls.filter((c) => c.method === "POST" && c.path === "/api/sessions");
    assert.equal(create.length, 1);
    assert.deepEqual(create[0]?.body, { previous_session_id: "s1" });
  } finally {
    halt(app);
  }
});

test("/sessions 选择器切会话不发创建请求（不结束任何会话）", async () => {
  const { app, calls } = makeHarness();
  let list: { onSelect?: (item: { value: string }) => void } | undefined;
  app.tui.showOverlay = (component) => {
    list = component as typeof list;
    return { hide() {} };
  };
  try {
    await app.showSessionPicker();
    list?.onSelect?.({ value: "s2" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    assert.equal(
      calls.filter((c) => c.method === "POST" && c.path === "/api/sessions").length,
      0,
      "切会话不是新建，不得触发 previous_session_id 结束写入",
    );
  } finally {
    halt(app);
  }
});

test("/quit 只发 client-exit，不发创建请求", async () => {
  const { app, calls } = makeHarness();
  const realExit = process.exit;
  const realStop = app.tui.stop;
  let exited = false;
  process.exit = ((code?: number) => {
    exited = true;
    throw new Error(`exit ${String(code)}`);
  }) as typeof process.exit;
  app.tui.stop = () => {};
  try {
    await app.quit().catch(() => undefined);
    assert.equal(exited, true, "quit 必须走 process.exit");
    assert.equal(
      calls.filter((c) => c.method === "POST" && c.path === "/api/sessions").length,
      0,
    );
    assert.equal(
      calls.filter((c) => c.path.endsWith("/client-exit")).length,
      1,
      "quit 只发 client-exit",
    );
  } finally {
    process.exit = realExit;
    app.tui.stop = realStop;
    halt(app);
  }
});
