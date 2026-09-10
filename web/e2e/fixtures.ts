/** T8（#101）E2E fixture 助手——mock SSE 帧形状 = docs/BACKEND_CONTRACT_STREAMING_UI.md：
 *  text/delta durable 承接文本流、reasoning 族 envelope block_id、tool/output_delta
 *  按 channel、seq 每 session 单调。page.route 拦截 API，核心矩阵不依赖真后端。 */

import { expect, type Page, type Route } from '@playwright/test';

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
  // ── Phase 2b Composer control row（Ticket F1/B1）──
  /** GET /api/permission-modes（权限模式清单） */
  permissionModes?: unknown[];
  /** GET /api/agent-profiles（Agent Profile 清单） */
  agentProfiles?: unknown[];
  /** GET /api/reasoning-efforts（Reasoning Effort 档位清单） */
  reasoningEfforts?: unknown[];
  /** GET /api/context-providers（Context Provider 清单；当前诚实返空） */
  contextProviders?: unknown[];
  /** POST /api/sessions/{id}/messages（续聊入口；空闲会话 → 同形 SSE） */
  onMessagesPost?: (route: Route) => Promise<void> | void;
  /** POST /api/sessions/{id}/forks（T7 #137 分叉；缺省 200 → 派生 child）。
   *  注入此回调即可断言请求体（BUG-001 回归锁：from_seq 必须是 user/message 的
   *  seq，不是 turn.step_id）或伪造 422。 */
  onForkPost?: (route: Route) => Promise<void> | void;
  /** POST /api/sessions/{id}/recover（T8 #138 恢复；缺省 200 → 返回 mock.events）。
   *  真实语义：响应是与 GET events 同构的全量事件数组。 */
  onRecoverPost?: (route: Route) => Promise<void> | void;
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
    // ── Phase 2b Composer control row（Ticket F1/B1）──
    if (path === '/api/permission-modes') {
      return route.fulfill({ status: 200, body: JSON.stringify({ modes: mock.permissionModes ?? [] }), contentType: 'application/json' });
    }
    if (path === '/api/agent-profiles') {
      return route.fulfill({ status: 200, body: JSON.stringify({ profiles: mock.agentProfiles ?? [] }), contentType: 'application/json' });
    }
    if (path === '/api/reasoning-efforts') {
      return route.fulfill({ status: 200, body: JSON.stringify({ efforts: mock.reasoningEfforts ?? [] }), contentType: 'application/json' });
    }
    if (path === '/api/context-providers') {
      return route.fulfill({ status: 200, body: JSON.stringify({ providers: mock.contextProviders ?? [] }), contentType: 'application/json' });
    }
    if (/^\/api\/sessions\/[^/]+\/messages$/.test(path) && req.method() === 'POST') {
      if (mock.onMessagesPost) return mock.onMessagesPost(route);
      return route.abort('aborted');
    }
    if (/^\/api\/sessions\/[^/]+\/forks$/.test(path) && req.method() === 'POST') {
      if (mock.onForkPost) return mock.onForkPost(route);
      const body = (req.postDataJSON() ?? {}) as { from_seq?: number };
      return route.fulfill({
        status: 200,
        body: JSON.stringify({ session_id: `${SID}-fork-${body.from_seq ?? 0}`, from_seq: body.from_seq ?? 0 }),
        contentType: 'application/json',
      });
    }
    if (/^\/api\/sessions\/[^/]+\/recover$/.test(path) && req.method() === 'POST') {
      if (mock.onRecoverPost) return mock.onRecoverPost(route);
      return route.fulfill({ status: 200, body: JSON.stringify(mock.events ?? []), contentType: 'application/json' });
    }
    return route.fulfill({ status: 404, body: '{"detail":"not mocked in e2e"}', contentType: 'application/json' });
  });
}

/** 提交一个任务（Composer 填写 + 发送）。 */
export async function submitTask(page: Page, task: string): Promise<void> {
  await page.getByLabel('Agent 任务').fill(task);
  await page.getByLabel('发送').click();
}

// ── 控制目录 fixture（/api/models + 四个清单端点）──
// 多个 spec 共用同一份，避免各自复制后静默漂移（code-review：catalog drift）。

export const MODELS = [
  { name: 'deepseek-v4-flash-0731', provider: 'senseaudio', model: 'deepseek-v4-flash-0731', default: true },
  { name: 'qwen-max', provider: 'senseaudio', model: 'qwen3.8-max-0902', default: false },
  { name: 'claude-sonnet-4', provider: 'anthropic', model: 'claude-sonnet-4-20250514', default: false },
];

export const PERMISSION_MODES = [
  { id: 'auto', display_name: 'Auto Approve', description: '自动批准工具调用' },
  { id: 'ask', display_name: 'Ask Each Time', description: '每次工具调用都询问' },
  { id: 'deny', display_name: 'Deny All', description: '拒绝所有工具调用' },
];

export const AGENT_PROFILES = [
  { id: 'main', display_name: 'Main', description: '通用编排代理（默认）' },
  { id: 'coding', display_name: 'Coding', description: '代码编辑、调试和构建任务专用' },
  { id: 'research_review', display_name: 'Research & Review', description: '研究、检索和审查任务专用' },
];

export const REASONING_EFFORTS = [
  { id: 'minimal', display_name: 'Minimal', description: '最少推理开销；最快但最不彻底。' },
  { id: 'standard', display_name: 'Standard', description: '典型任务的平衡推理深度（默认）。' },
  { id: 'deep', display_name: 'Deep', description: '最多推理开销；较慢但最彻底。' },
];

export const CONTEXT_PROVIDERS = [
  { id: 'memory', display_name: 'Memory', description: 'Inject relevant recalled memories scoped to the user into the model context.' },
  { id: 'skills', display_name: 'Skills', description: 'Inject the catalog of available skills (name + description) into the model context.' },
];

// ── 长目录 fixture（F-DEFER-1：搜索框显示阈值 >5 条）──
// 阈值速查（源码）：ModelPicker 用 `models.length + 1 > 5`（默认链算 1 条）；
// ControlPicker / ContextProviderPicker 用 `entries.length > 5`。
// 三处 spec 曾各自内联长目录 → 阈值/条数一改就静默漂移，故统一在此构造。

/** 造一个 id/display_name 结构的长目录（映射 ControlPicker / ContextProviderPicker 端点形状）。
 *
 * `highlight` 指定某一项的 display_name（供"键入过滤"类用例断言），默认 `前缀 N`。 */
export function longCatalog(prefix: string, n: number, highlight?: { index: number; label: string }) {
  return Array.from({ length: n }, (_, i) => ({
    id: `${prefix}-${i}`,
    display_name: highlight?.index === i ? highlight.label : `${prefix} ${i}`,
    description: `${prefix} 档位 ${i}`,
  }));
}

/** 长模型目录：MODELS（3）+ 2 条 = 5 条，+1 默认链 = 6 > 5 → 搜索框显示。 */
export const SEARCHABLE_MODELS = [
  ...MODELS,
  { name: 'gpt-5-mini', provider: 'openai', model: 'gpt-5-mini', default: false },
  { name: 'gemini-3-pro', provider: 'google', model: 'gemini-3-pro', default: false },
];

/** 长模型目录 PLUS：MODELS（3）+ 4 条 = 7 条，+1 = 8 > 5（更强的长目录信号，供可见性用例）。 */
export const LONG_MODELS = [
  ...SEARCHABLE_MODELS,
  { name: 'llama-5-70b', provider: 'meta', model: 'llama-5-70b', default: false },
  { name: 'mistral-large-3', provider: 'mistral', model: 'mistral-large-3', default: false },
];

// ── Composer 控制行交互 helper（跨 spec 共用）──

/** 键盘在 ControlPicker 里选第 N+1 项：打开 → ↓×N → Enter，断言 trigger 文本。
 *
 * F-DEFER-1（cmdk 焦点真相，2026-09-10 探针实测）：
 * - 「浮层已开」只看 `[role="listbox"]`（CommandList）。`role="combobox"`
 *   （CommandInput 本身）在短目录（≤5 条）时被 `.hidden` 的 wrap 包住，
 *   其 rect 为 0×0 → Playwright 判为不可见。
 * - **不要对搜索框调 fill()**：短目录下 rect 为 0，fill 会永久等待可交互
 *   状态直至超时；长目录下 aria-label 并不落在 input 上，
 *   `getByRole('combobox', { name })` 命中 0 个。
 * - 打开后焦点在 `DIV[role="dialog"]`（popover 容器），键盘事件需落到
 *   **listbox 自身**（`tabIndex=-1`，cmdk 在此承接方向键）才能驱动选中。
 *   故统一 `listbox.focus()`——长短目录同一路径，无分支。 */
export async function pickControl(
  page: Page,
  label: string,
  downPresses: number,
  expected: string,
): Promise<void> {
  const trigger = page.locator(`.composer-control[aria-label="${label}"]`);
  // `:visible` 限定当前打开的浮层：关闭动画期间上一层 listbox 仍是 DOM 节点，
  // 不加过滤会 strict mode violation（探针实测命中 2 个）。
  const listbox = page.locator('[role="listbox"]:visible').last();
  await trigger.focus();
  await page.keyboard.press('Enter');
  await expect(listbox).toBeVisible();
  // 焦点落到 listbox（cmdk 的方向键承接者）；短目录搜索框 0×0，不能 fill。
  await listbox.focus();
  for (let i = 0; i < downPresses; i += 1) await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(trigger).toContainText(expected);
  await page.keyboard.press('Escape');
}

/** 键盘在 ModelPicker 里选目录第一行（「默认链」之后第一项 = MODELS[0]），断言 trigger 文本。
 *
 * F-DEFER-1：同 pickControl——焦点显式落到 listbox，不碰 0×0 的搜索框。 */
export async function pickFirstModel(page: Page): Promise<void> {
  const trigger = page.locator('.composer-model[aria-label="模型选择"]');
  const listbox = page.locator('[role="listbox"]:visible').last();
  await trigger.focus();
  await page.keyboard.press('Enter');
  await expect(listbox).toBeVisible();
  await listbox.focus();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(trigger).toContainText(MODELS[0].name);
  await page.keyboard.press('Escape');
}
