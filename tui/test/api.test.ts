/**
 * ApiClient 的响应体分流（#853）。
 *
 * 症状：`POST /api/sessions/{id}/messages` 在「会话空闲」这条正常路径上返回的是
 * **SSE**（服务端 launched 分支，app.py:3358-3363），而 request() 无条件
 * `JSON.parse(text)` ⇒ 必然抛 SyntaxError ⇒ TUI 落一行假 `send failed:`，
 * 而消息其实已被接受、run 已经跑完。
 *
 * 次生：`await response.text()` 要等整轮 run 收尾才返回 ⇒ UI 表现为
 * 「发送挂住整轮 run，然后报失败」。
 *
 * 服务端契约（app.py:1600-1601）：run 是 **detached** 的，「断连只 unsubscribe，
 * run 不受影响」；TUI 自己另有一条按 seq 续传的 `/stream` 订阅在投递事件
 * ⇒ POST 的 SSE 体对 TUI 无用，放弃它既正确也不丢事件。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiClient, ApiError } from "../src/api.ts";

const SSE_FRAME = 'data: {"type":"user/message","seq":2}\n\n';

function sseResponse(
  body: ReadableStream<Uint8Array>,
): Response {
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

/** 有限 SSE 体：给出首帧后收束（模拟 run 很快结束）。 */
function closedSseBody(): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(SSE_FRAME));
      controller.close();
    },
  });
}

/** 永不收束的 SSE 体：模拟「run 还在跑」，并在被取消时打点。 */
function openSseBody(onCancel: () => void): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let sent = false;
  return new ReadableStream({
    pull(controller) {
      if (!sent) {
        sent = true;
        controller.enqueue(encoder.encode(SSE_FRAME));
      }
      // 之后既不 enqueue 也不 close：读到底就会永久挂住
    },
    cancel() {
      onCancel();
    },
  });
}

function clientWith(fetchFn: typeof fetch): ApiClient {
  return new ApiClient("http://127.0.0.1:1", fetchFn);
}

test("空闲会话发消息：SSE 响应不再抛假失败，按「已受理」返回 null", async () => {
  const fetchFn = (async () => sseResponse(closedSseBody())) as unknown as typeof fetch;

  const result = await clientWith(fetchFn).sendMessage("s1", "hi");

  assert.equal(result, null);
});

test("SSE 响应不被读到底：不挂住整轮 run，并主动放弃响应体", { timeout: 3000 }, async () => {
  let cancelled = false;
  const fetchFn = (async () =>
    sseResponse(openSseBody(() => {
      cancelled = true;
    }))) as unknown as typeof fetch;

  const result = await clientWith(fetchFn).sendMessage("s1", "hi");

  assert.equal(result, null);
  assert.equal(cancelled, true, "必须 cancel 响应体（run detached，事件另有 /stream 订阅）");
});

test("content-type 带 charset 参数的 SSE 同样分流", async () => {
  const fetchFn = (async () =>
    new Response(closedSseBody(), {
      status: 200,
      headers: { "content-type": "text/event-stream; charset=utf-8" },
    })) as unknown as typeof fetch;

  const result = await clientWith(fetchFn).sendMessage("s1", "hi");

  assert.equal(result, null);
});

test("JSON 响应仍照常解析（不回归）", async () => {
  const fetchFn = (async () =>
    new Response(JSON.stringify([{ session_id: "s1" }]), {
      status: 200,
      headers: { "content-type": "application/json" },
    })) as unknown as typeof fetch;

  const sessions = await clientWith(fetchFn).listSessions();

  assert.deepEqual(sessions, [{ session_id: "s1" }]);
});

test("空体 2xx 仍返回 null（不回归）", async () => {
  const fetchFn = (async () =>
    new Response("", { status: 200 })) as unknown as typeof fetch;

  const result = await clientWith(fetchFn).sendMessage("s1", "hi");

  assert.equal(result, null);
});

test("非 2xx 仍抛 ApiError 且带 detail（不回归）", async () => {
  const fetchFn = (async () =>
    new Response(JSON.stringify({ detail: "boom" }), {
      status: 500,
      headers: { "content-type": "application/json" },
    })) as unknown as typeof fetch;

  await assert.rejects(
    () => clientWith(fetchFn).sendMessage("s1", "hi"),
    (error: unknown) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 500);
      assert.equal(error.detail, "boom");
      return true;
    },
  );
});

/** 抓一次请求（URL + init + 原始字节体）的探针。 */
function captureFetch(
  respond: (url: string, init: RequestInit | undefined) => Response,
): { calls: { url: string; init: RequestInit | undefined }[]; fetchFn: typeof fetch } {
  const calls: { url: string; init: RequestInit | undefined }[] = [];
  const fetchFn = (async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    return respond(url, init);
  }) as unknown as typeof fetch;
  return { calls, fetchFn };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

test("AC5：附图提交 ⇒ 请求体带 attachments 引用列表（顺序即正文引用序）", async () => {
  const probe = captureFetch(() => new Response("", { status: 200 }));

  await clientWith(probe.fetchFn).sendMessage("s1", "看这两张 [Image #1] [Image #2]", [
    "sha256:aaa",
    "sha256:bbb",
  ]);

  assert.equal(probe.calls.length, 1);
  assert.equal(probe.calls[0]?.url, "http://127.0.0.1:1/api/sessions/s1/messages");
  assert.deepEqual(JSON.parse(String(probe.calls[0]?.init?.body)), {
    content: "看这两张 [Image #1] [Image #2]",
    mode: "queue",
    attachments: ["sha256:aaa", "sha256:bbb"],
  });
});

test("AC5：无附图 ⇒ 请求体不带 attachments 键（纯文本行为逐字不变）", async () => {
  const probe = captureFetch(() => new Response("", { status: 200 }));

  await clientWith(probe.fetchFn).sendMessage("s1", "纯文字");

  assert.deepEqual(JSON.parse(String(probe.calls[0]?.init?.body)), {
    content: "纯文字",
    mode: "queue",
  });
});

test("AC5：uploadAttachment 发原始字节 + 声明 Content-Type，?name= 编码进查询串", async () => {
  const bytes = Uint8Array.from([0x89, 0x50, 0x4e, 0x47]);
  const probe = captureFetch(() =>
    jsonResponse({
      attachment_id: "sha256:deadbeef",
      media_type: "image/png",
      bytes: 4,
      width: 1,
      height: 1,
      name: "我的 截图.png",
    }),
  );

  const receipt = await clientWith(probe.fetchFn).uploadAttachment("s1", bytes, {
    name: "我的 截图.png",
    mediaType: "image/png",
  });

  assert.equal(probe.calls[0]?.url, "http://127.0.0.1:1/api/sessions/s1/attachments?name=%E6%88%91%E7%9A%84%20%E6%88%AA%E5%9B%BE.png");
  const init = probe.calls[0]?.init;
  assert.equal(init?.method, "POST");
  assert.equal(
    (init?.headers as Record<string, string>)["content-type"],
    "image/png",
    "声明类型必须盖过 request() 的 JSON 缺省头（服务端按它比对字节判定）",
  );
  assert.deepEqual([...Uint8Array.from(init?.body as Uint8Array)], [...bytes]);
  assert.equal(receipt.attachment_id, "sha256:deadbeef");
});

test("AC5：省略声明名时不带 ?name=（服务端 name 可缺省）", async () => {
  const probe = captureFetch(() => jsonResponse({ attachment_id: "sha256:1", media_type: "image/png", bytes: 4, width: 1, height: 1 }));

  await clientWith(probe.fetchFn).uploadAttachment("s1", Uint8Array.from([0x89]), {
    mediaType: "image/png",
  });

  assert.equal(probe.calls[0]?.url, "http://127.0.0.1:1/api/sessions/s1/attachments");
});

test("AC8：listModels 解包 {models:[...]}，只收 TUI 需要的字段、丢弃形状不对的条目", async () => {
  const probe = captureFetch(() =>
    jsonResponse({
      models: [
        { id: "vision-a", model: "vision-a", is_default: true, supports_vision: true, provider: "p" },
        { id: "text-b", model: "text-b", is_default: false, supports_vision: false },
        { id: "no-model", is_default: false },
        { model: "id 缺失" },
        "不是对象",
      ],
    }),
  );

  const models = await clientWith(probe.fetchFn).listModels();

  assert.equal(probe.calls[0]?.url, "http://127.0.0.1:1/api/models");
  assert.deepEqual(models, [
    { id: "vision-a", model: "vision-a", is_default: true, supports_vision: true },
    { id: "text-b", model: "text-b", is_default: false, supports_vision: false },
    { id: "no-model", is_default: false },
  ]);
});

test("AC8：能力位缺席的条目不带 supports_vision（未知 ≠ false 的「假」）", async () => {
  const probe = captureFetch(() => jsonResponse({ models: [{ id: "unknown", is_default: false }] }));

  const models = await clientWith(probe.fetchFn).listModels();

  assert.equal("supports_vision" in (models[0] ?? {}), false);
});
