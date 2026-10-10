/**
 * REST 客户端（fetch）：后端 API 面以 src/agent_harness/web/app.py 为准。
 * TUI 是纯客户端：所有真相向服务端查询，本地不写任何会话状态。
 */
import { asNumber, asString, isRecord, type EventEnvelope } from "./events.ts";
import type { ModelOptionView } from "./lib/vision.ts";
import type { SessionSummaryView } from "./views/sessionselect.ts";

/** `POST /api/sessions/{id}/attachments` 的回执（`web/attachments.py:AttachmentUploadResponse`）。 */
export interface AttachmentUploadResponse {
  attachment_id: string;
  media_type: string;
  bytes: number;
  width: number;
  height: number;
  name?: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`HTTP ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export interface ResumeRequest {
  run_id?: string;
  resume_basis?: string;
  budget?: { expected_version?: number };
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly fetchFn: typeof fetch;

  constructor(baseUrl: string, fetchFn: typeof fetch = fetch) {
    this.baseUrl = baseUrl;
    this.fetchFn = fetchFn;
  }

  private url(path: string): string {
    return `${this.baseUrl.replace(/\/$/, "")}${path}`;
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await this.fetchFn(this.url(path), {
      ...init,
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    });
    // #853：`POST /messages` 的 launched 分支（会话空闲）返回 text/event-stream，
    // 而 request() 原先无条件 `JSON.parse(text)`，必然抛 SyntaxError，TUI 落一行假
    // `send failed`（消息其实已被接受、run 已跑完）。且 `await response.text()` 要等
    // 整轮 run 收尾才返回，UI 表现为「发送挂住整轮 run，然后报失败」。
    // 服务端的 run 是 detached 的（app.py `_run_stream_response`：断连只
    // unsubscribe、不影响 run），本客户端另有一条按 seq 续传的 /stream 订阅在投递
    // 事件，该响应体对 TUI 无用，直接放弃：既不抛错，也不阻塞。
    if (response.ok && isEventStream(response)) {
      await discardBody(response);
      return null as T;
    }
    const text = await response.text();
    if (!response.ok) {
      let detail = text;
      try {
        const parsed: unknown = JSON.parse(text);
        if (typeof parsed === "object" && parsed !== null && "detail" in parsed) {
          detail = String((parsed as { detail: unknown }).detail);
        }
      } catch {
        // 非 JSON 错误体：原样上抛
      }
      throw new ApiError(response.status, detail);
    }
    return (text ? JSON.parse(text) : null) as T;
  }

  listSessions(): Promise<SessionSummaryView[]> {
    return this.request("/api/sessions");
  }

  async getEvents(sessionId: string): Promise<EventEnvelope[]> {
    const raw = await this.request<unknown[]>(
      `/api/sessions/${encodeURIComponent(sessionId)}/events`,
    );
    return raw.map(parseEventRow);
  }

  /**
   * 只建会话（launch=false，无 task）-> 返回会话 JSON；任务随后经
   * /messages 投递（launch=true 与 task 组合的 SSE 响应对 TUI 无用，
   * 事件统一走 GET /stream 订阅消费）。
   */
  createSession(): Promise<{ session_id?: string }> {
    return this.request("/api/sessions?launch=false", {
      method: "POST",
      body: JSON.stringify({}),
    });
  }

  /**
   * 投递消息。会话空闲时服务端返回 launched 的 SSE 流（#853）：这里放弃该响应体、
   * 按「已受理」返回 null，事件由 /stream 订阅投递（见 request()）。
   *
   * `attachments`（#827 MM-06 / AC4）：MM-02 的上传回执 `attachment_id` 列表。
   * 空/未给 => **不带该键**（纯文本提交的请求体逐字节不变）。
   */
  sendMessage(sessionId: string, content: string, attachments?: string[]): Promise<unknown> {
    const body: Record<string, unknown> = { content, mode: "queue" };
    if (attachments && attachments.length > 0) body["attachments"] = attachments;
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /**
   * 上传一张图片（#827 MM-06 / AC5 的 adapter 接缝）。
   *
   * 契约（`web/attachments.py:169-207`）：请求体是**原始字节**（不套 JSON、不 base64），
   * `Content-Type` 声明图片类型，`?name=` 是展示名。服务端按 magic bytes 判型，并且
   * **声明（Content-Type / 文件名扩展名）与字节判定不符即拒**（`IMAGE_TYPE_MISMATCH`），
   * 所以调用方必须保证三者一致（本仓 TUI 由 `uploadDeclaredName` 保证）。
   */
  uploadAttachment(
    sessionId: string,
    bytes: Uint8Array,
    options: { name?: string; mediaType: string },
  ): Promise<AttachmentUploadResponse> {
    const query = options.name ? `?name=${encodeURIComponent(options.name)}` : "";
    return this.request(
      `/api/sessions/${encodeURIComponent(sessionId)}/attachments${query}`,
      {
        method: "POST",
        headers: { "content-type": options.mediaType },
        body: bytes as unknown as RequestInit["body"],
      },
    );
  }

  /**
   * M-09：受控读回一张历史附图的原始字节（`web/src/lib/api.ts::getAttachmentBytes`
   * 同款通道、同一条端点契约：只认本会话事件流引用过的 id，其余 404）。
   * `request()` 会强制 JSON.parse，这里必须走原始 fetch（响应是字节流不是 JSON）。
   */
  async getAttachmentBytes(sessionId: string, attachmentId: string): Promise<Uint8Array> {
    const response = await this.fetchFn(
      this.url(
        `/api/sessions/${encodeURIComponent(sessionId)}/attachments/` +
          `${encodeURIComponent(attachmentId)}/content`,
      ),
    );
    if (!response.ok) {
      const text = await response.text();
      let detail = text;
      try {
        const parsed: unknown = JSON.parse(text);
        if (typeof parsed === "object" && parsed !== null && "detail" in parsed) {
          detail = String((parsed as { detail: unknown }).detail);
        }
      } catch {
        // 非 JSON 错误体：原样上抛
      }
      throw new ApiError(response.status, detail);
    }
    return new Uint8Array(await response.arrayBuffer());
  }

  /**
   * 模型目录（`GET /api/models`，AC8 的视觉能力来源）。响应外层是 `{"models": [...]}`；
   * 这里只收出 TUI 真正要的字段（id / model / is_default / supports_vision），
   * 其余字段（provider、display_name、不可用原因...）不引入本客户端。形状不对的条目
   * **丢弃**而不是猜。
   */
  async listModels(): Promise<ModelOptionView[]> {
    const raw = await this.request<{ models?: unknown }>("/api/models");
    const rows = Array.isArray(raw?.models) ? raw.models : [];
    const options: ModelOptionView[] = [];
    for (const row of rows) {
      if (!isRecord(row)) continue;
      const id = asString(row["id"]);
      if (!id) continue;
      const option: ModelOptionView = { id };
      const model = asString(row["model"]);
      if (model) option.model = model;
      if (typeof row["is_default"] === "boolean") option.is_default = row["is_default"];
      if (typeof row["supports_vision"] === "boolean") {
        option.supports_vision = row["supports_vision"];
      }
      options.push(option);
    }
    return options;
  }

  approve(
    sessionId: string,
    approvalId: string,
    approved: boolean,
    decision?: string,
  ): Promise<unknown> {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/approve`, {
      method: "POST",
      body: JSON.stringify({
        approval_id: approvalId,
        approved,
        decision: decision ?? (approved ? "approve" : "deny"),
      }),
    });
  }

  cancel(sessionId: string): Promise<{ status: string }> {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/cancel`, {
      method: "POST",
      body: JSON.stringify({}),
    });
  }

  compact(sessionId: string): Promise<unknown> {
    return this.request(
      `/api/sessions/${encodeURIComponent(sessionId)}/context/compact`,
      { method: "POST" },
    );
  }

  budget(sessionId: string): Promise<Record<string, unknown>> {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/budget`);
  }

  progress(sessionId: string): Promise<unknown> {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/progress`);
  }

  resume(sessionId: string, req: ResumeRequest): Promise<unknown> {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/resume`, {
      method: "POST",
      body: JSON.stringify(req),
    });
  }

  /**
   * 客户端明确退出信号（ADR-0046 选项 B 写侧）。
   * 服务端 `POST /api/sessions/{id}/client-exit`（#360 W-17 补的 web 接缝，
   * 接 RunManager.signal_client_exit）：200 即送达；404/405 时如实上报，
   * 调用方降级为断线宽限基线，不伪造暂停成功。
   */
  async signalClientExit(
    sessionId: string,
    clientId: string,
  ): Promise<{ delivered: boolean; status: number }> {
    const response = await this.fetchFn(
      this.url(`/api/sessions/${encodeURIComponent(sessionId)}/client-exit`),
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ client_id: clientId }),
      },
    ).catch(() => null);
    if (response === null) return { delivered: false, status: 0 };
    return { delivered: response.ok, status: response.status };
  }
}

/** 响应体是否为 SSE（`text/event-stream`，可能带 `; charset=` 参数）。 */
function isEventStream(response: Response): boolean {
  return (response.headers.get("content-type") ?? "")
    .toLowerCase()
    .includes("text/event-stream");
}

/**
 * 放弃响应体（#853）：launched 分支的 SSE 体只表示「已受理」。服务端的 run 是
 * detached 的，断连只 unsubscribe、不影响 run（app.py `_run_stream_response`）；
 * 不读到底，发消息不再挂住整轮 run。
 */
async function discardBody(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // 连接已断/已取消：无副作用
  }
}

/** GET /events 返回的是 to_dict() 行（含 event_id 等），收敛成信封形状。 */
function parseEventRow(row: unknown): EventEnvelope {
  const obj = (typeof row === "object" && row !== null ? row : {}) as Record<string, unknown>;
  return {
    type: asString(obj.type),
    data: isRecord(obj.data) ? obj.data : {},
    seq: asNumber(obj.seq),
    run_id: typeof obj.run_id === "string" ? obj.run_id : null,
    step_id: asNumber(obj.step_id),
    session_id: asString(obj.session_id),
    time: asString(obj.time),
    durability: "durable",
  };
}
