/** T8（#101）E2E fixture 助手——mock SSE 帧形状 = docs/BACKEND_CONTRACT_STREAMING_UI.md：
 *  text/delta durable 承接文本流、reasoning 族 envelope block_id、tool/output_delta
 *  按 channel、seq 每 session 单调。page.route 拦截 API，核心矩阵不依赖真后端。 */

import type { Page, Route } from '@playwright/test';

export const SID = 'e2e-session-0001';
export const RUN = 'e2e-run-0001';
export const T = '2026-09-07T00:00:00Z';

export interface FrameSpec {
  type: string;
  data?: Record<string, unknown>;
  seq?: number | null;
  run_id?: string | null;
  step_id?: number | null;
  session_id?: string | null;
  block_id?: string | null;
  time?: string;
}

export function sseFrame(f: FrameSpec): string {
  return `data: ${JSON.stringify(f)}\n\n`;
}

/** SSE 帧 → route.fulfill（字符串形态——Playwright fulfill({ response }) 的
 *  Response 包装在本环境不可靠（帧未送达），f-history 证明字符串形态可用）。
 *  帧一次性到达：投影瞬时完成，骨架只断言终态；流式中间态（caret/呼吸图标/
 *  跟随浮标）留待联调车道（真后端或本地 SSE fixture server，见 map 文档）。 */
export async function fulfillSse(route: Route, frames: FrameSpec[]): Promise<void> {
  await route.fulfill({ status: 200, contentType: 'text/event-stream', body: frames.map(sseFrame).join('') });
}

export interface ApiMock {
  /** GET /api/sessions（会话列表） */
  sessions?: unknown[];
  /** GET /api/sessions/{id}/events（历史重放） */
  events?: FrameSpec[];
  /** GET /api/models（模型目录；缺省 = 空目录 → 选择器降级隐藏） */
  models?: unknown[];
  /** POST /api/sessions（live 流） */
  onSessionPost?: (route: Route) => Promise<void> | void;
  /** GET /api/sessions/{id}/stream?after_seq=N（重连续传） */
  onStreamGet?: (route: Route) => Promise<void> | void;
}

export function routeApi(page: Page, mock: ApiMock): void {
  void page.route('**/api/**', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (path === '/api/health') {
      return route.fulfill({ status: 200, body: '{"status":"ok"}', contentType: 'application/json' });
    }
    if (path === '/api/sessions' && req.method() === 'GET') {
      return route.fulfill({ status: 200, body: JSON.stringify(mock.sessions ?? []), contentType: 'application/json' });
    }
    if (path === '/api/sessions' && req.method() === 'POST') {
      if (mock.onSessionPost) return mock.onSessionPost(route);
      return route.abort('aborted');
    }
    if (/^\/api\/sessions\/[^/]+\/events$/.test(path)) {
      return route.fulfill({ status: 200, body: JSON.stringify(mock.events ?? []), contentType: 'application/json' });
    }
    if (/^\/api\/sessions\/[^/]+\/stream$/.test(path)) {
      if (mock.onStreamGet) return mock.onStreamGet(route);
      return route.abort('aborted');
    }
    if (path === '/api/models') {
      return route.fulfill({ status: 200, body: JSON.stringify({ models: mock.models ?? [] }), contentType: 'application/json' });
    }
    return route.fulfill({ status: 404, body: '{"detail":"not mocked in e2e"}', contentType: 'application/json' });
  });
}

/** 提交一个任务（Composer 填写 + 发送）。 */
export async function submitTask(page: Page, task: string): Promise<void> {
  await page.getByLabel('Agent 任务').fill(task);
  await page.getByLabel('发送').click();
}
