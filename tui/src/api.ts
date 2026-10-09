/**
 * REST 客户端（fetch）：后端 API 面以 src/agent_harness/web/app.py 为准。
 * TUI 是纯客户端：所有真相向服务端查询，本地不写任何会话状态。
 */
import { asNumber, asString, isRecord, type EventEnvelope } from "./events.ts";
import type { SessionSummaryView } from "./views/sessionselect.ts";

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
   */
  sendMessage(sessionId: string, content: string): Promise<unknown> {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
      method: "POST",
      body: JSON.stringify({ content, mode: "queue" }),
    });
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
