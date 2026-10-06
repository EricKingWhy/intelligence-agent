/**
 * REST 客户端（fetch）：后端 API 面以 src/agent_harness/web/app.py 为准。
 * TUI 是纯客户端：所有真相向服务端查询，本地不写任何会话状态。
 */
import type { EventEnvelope } from "./events.ts";
import type { SessionSummaryView } from "./views/sessionselect.ts";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(`HTTP ${status}: ${detail}`);
    this.name = "ApiError";
  }
}

export interface ResumeRequest {
  resume_basis?: string;
  expected_version?: number;
  budget?: unknown;
}

export class ApiClient {
  constructor(
    private readonly baseUrl: string,
    private readonly fetchFn: typeof fetch = fetch,
  ) {}

  private url(path: string): string {
    return `${this.baseUrl.replace(/\/$/, "")}${path}`;
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await this.fetchFn(this.url(path), {
      ...init,
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    });
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

  createSession(task: string): Promise<unknown> {
    return this.request("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ task }),
    });
  }

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
   * 客户端明确退出信号（ADR-0046 选项 B 写侧）。服务端接缝当前只有
   * RunManager.signal_client_exit（进程内），web 层端点未开: 404/405 时
   * 如实上报"接缝未开"，调用方降级为断线宽限基线，不伪造暂停成功。
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

/** GET /events 返回的是 to_dict() 行（含 event_id 等），收敛成信封形状。 */
function parseEventRow(row: unknown): EventEnvelope {
  const obj = (typeof row === "object" && row !== null ? row : {}) as Record<string, unknown>;
  return {
    type: typeof obj.type === "string" ? obj.type : "",
    data: (typeof obj.data === "object" && obj.data !== null ? obj.data : {}) as Record<string, unknown>,
    seq: typeof obj.seq === "number" ? obj.seq : null,
    run_id: typeof obj.run_id === "string" ? obj.run_id : null,
    step_id: typeof obj.step_id === "number" ? obj.step_id : null,
    session_id: typeof obj.session_id === "string" ? obj.session_id : "",
    time: typeof obj.time === "string" ? obj.time : "",
    durability: "durable",
  };
}
