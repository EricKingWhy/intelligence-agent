/** REST API client. All fetches go through here — single seam for base URL / auth headers.
 *
 * Auth seam (backend auth_seam fail-closed, df4f7d8): when the user has configured
 * a Bearer token (lib/auth), apiFetch injects `Authorization` on every request —
 * including the SSE POST. 401 responses are broadcast (auth.onUnauthorized) and
 * thrown as UnauthorizedError so callers surface the guidance path.
 */

import type {
  AgentEvent,
  HostDirsListing,
  MemoryDeleted,
  MemoryScope,
  MemorySummary,
  Project,
  ProjectDeleted,
  ProjectStatus,
  SessionSummary,
} from '../types';
import { emitUnauthorized, getToken } from './auth';

const BASE = ''; // relative — Vite proxy handles /api → :8000

/** Thrown for any 401 (after auth.onUnauthorized has broadcast the detail). */
export class UnauthorizedError extends Error {}

/** 404 = 目标资源不存在（会话已被删除 / 属于另一个后端实例）。
 *
 *  与「加载失败」区分开：调用方据此**清理记住的选中会话**并安静回到空态，
 *  而不是弹一条用户无法处理、每次刷新都会重演的错误横幅（BUG-005 的
 *  陈旧 id 分支）。 */
export class NotFoundError extends Error {}

/** 409 = 审批已决（幂等成功）——OBS-015 修复引入。
 *
 *  后端 `PendingApprovalQueue.resolve()` 对同一 approval_id 的第二次决策返回 409。
 *  这**不是**错误：用户的意图已经生效，卡片应翻到「已批准/已拒绝」。
 *  与网络失败 / 5xx 区分开：那些意味着决策**没有**到达后端，卡片必须保持 pending。 */
export class AlreadyResolvedError extends Error {}

/** FastAPI 错误体 `{detail}` 读取：形状不符或 JSON 解析失败返回 ''——
 *  错误处理路径自身不再产生新错误（多处 401/409/4xx 消费共享的单一实现）。
 *
 *  `detail` 有**两种合法形状**，两者都要认：
 *  - `string`：端点自己 `raise HTTPException(detail=…)` —— 后端的可行动中文原因；
 *  - `Array<{loc, msg, type}>`：Pydantic 请求体校验失败的固定形状（422）。不认它
 *    就会把"path 必须是绝对路径"降级成"注册项目失败（422）"，把最该看懂的一条
 *    提示扔在门外。只取 `msg` 并剥掉 Pydantic 自己的 `Value error, ` 前缀——
 *    用户要看的是规则的结论，不是校验器的转述层。 */
async function readErrorDetail(res: Response): Promise<string> {
  try {
    const j = (await res.json()) as { detail?: unknown } | null;
    const detail = j?.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail
        .flatMap((item) => {
          const msg = (item as { msg?: unknown } | null)?.msg;
          return typeof msg === 'string' && msg ? [msg.replace(/^Value error,\s*/, '')] : [];
        })
        .join('；');
    }
    return '';
  } catch {
    return '';
  }
}

/** Single request seam: auth header injection + 401 interception. */
async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (res.status === 401) {
    const detail = await readErrorDetail(res);
    emitUnauthorized(detail || 'Missing identity token');
    throw new UnauthorizedError(detail || '需要身份令牌（401）');
  }
  return res;
}

export async function getHealth(): Promise<{ status: string }> {
  const res = await apiFetch('/api/health');
  if (!res.ok) throw new Error(`health ${res.status}`);
  return res.json();
}

export async function listSessions(): Promise<SessionSummary[]> {
  const res = await apiFetch('/api/sessions');
  if (!res.ok) throw new Error(`list sessions ${res.status}`);
  return res.json();
}

export async function getSessionEvents(sessionId: string): Promise<AgentEvent[]> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/events`);
  if (res.status === 404) throw new NotFoundError(`会话不存在（${sessionId}）`);
  if (!res.ok) throw new Error(`get events ${res.status}`);
  return res.json();
}

export interface StartSessionPayload {
  task: string;
  workspace?: string;
  /** 目录根会话（WS-6 / #169，ADR-0027 D2）：会话直接在**这个已存在的绝对路径**
   *  下运行，它同时成为会话的 workspace root（工具的相对路径都相对它解析），
   *  并在后端自动注册为项目 + attach（幂等）。
   *
   *  与 `workspace`（workspaces_root 下的单段名字，ADR-0025 D8 的旧语义）
   *  **互斥**：两个都传 → 422 `workspace 与 cwd 只能二选一`。前端入口一次只用一种，
   *  这条互斥在后端兜底而不是在这里猜（谁先谁后是可观测契约，见 PRD §4.1）。 */
  cwd?: string;
  max_steps?: number;
  auto_approve?: boolean;
  /** 可选模型选择（T10 #103，契约 C6）：GET /api/models 的 name；不传 = 默认
   *  链；未知 → 422（调用方提示重新选择并刷新目录）。 */
  model?: string;
  /** 会话级控制字段——全部在运行时真实消费（不再是 staged no-op）：
   *  - permission_mode: GET /api/permission-modes 的 id；审批阈值（非硬墙），
   *    不传 = 后端默认。
   *  - agent_profile: GET /api/agent-profiles 的 id；注入 system_prompt +
   *    按 tool_scope 收窄工具集（ADR-0020a），不传 = 后端默认。
   *  - reasoning_effort: GET /api/reasoning-efforts 的 id；经 create_chat_model
   *    注入模型原生字段（reasoning_effort 批次 `79e2860`），不传 = 后端默认。
   *  - context_providers: GET /api/context-providers 的 id 列表；按 provider
   *    name 筛选已装配子集（ADR-0021），不传 = 全部已装配 provider。
   *
   *  API 边界校验：permission_mode / reasoning_effort / agent_profile 为封闭枚举
   *  （未知值 → 422）；context_providers 做形状校验并对照 wiring 真实 id
   *  （未知 → 422）。 */
  permission_mode?: string;
  agent_profile?: string;
  reasoning_effort?: string;
  context_providers?: string[];
}

/** B1 契约通用清单条目——{id, display_name, description}。
 *  四个清单端点（permission-modes / agent-profiles / reasoning-efforts /
 *  context-providers）共用此结构，与 /api/models 富化模式对齐。 */
export interface CatalogEntry {
  id: string;
  display_name: string;
  description: string;
}

/** GET /api/permission-modes —— 权限模式清单。
 *  返回 PermissionPolicy 全集 + 人类可读描述。 */
export async function getPermissionModes(): Promise<CatalogEntry[]> {
  const res = await apiFetch('/api/permission-modes');
  if (!res.ok) throw new Error(`permission-modes ${res.status}`);
  return parseCatalogEntries(await res.json(), 'modes');
}

/** GET /api/agent-profiles —— Agent Profile 清单。
 *  返回 AGENT_PROFILE_DESCRIPTIONS 全集。 */
export async function getAgentProfiles(): Promise<CatalogEntry[]> {
  const res = await apiFetch('/api/agent-profiles');
  if (!res.ok) throw new Error(`agent-profiles ${res.status}`);
  return parseCatalogEntries(await res.json(), 'profiles');
}

/** GET /api/reasoning-efforts —— Reasoning Effort 档位清单。
 *  返回 REASONING_EFFORT_DESCRIPTIONS 全集。 */
export async function getReasoningEfforts(): Promise<CatalogEntry[]> {
  const res = await apiFetch('/api/reasoning-efforts');
  if (!res.ok) throw new Error(`reasoning-efforts ${res.status}`);
  return parseCatalogEntries(await res.json(), 'efforts');
}

/** GET /api/context-providers —— Context Provider 清单。
 *  当前诚实返空数组（runtime 尚未装配任何 provider）。 */
export async function getContextProviders(): Promise<CatalogEntry[]> {
  const res = await apiFetch('/api/context-providers');
  if (!res.ok) throw new Error(`context-providers ${res.status}`);
  return parseCatalogEntries(await res.json(), 'providers');
}

/** 窄化解析清单端点响应——仅 id/display_name/description 非空字符串的条目入选。
 *  顶层 key 用复数短名（modes/profiles/efforts/providers），调用方传入对应 key。 */
function parseCatalogEntries(body: unknown, key: string): CatalogEntry[] {
  const raw =
    typeof body === 'object' && body !== null && Array.isArray((body as Record<string, unknown>)[key])
      ? ((body as Record<string, unknown[]>)[key])
      : [];
  return raw.flatMap((m) => {
    if (typeof m !== 'object' || m === null) return [];
    const r = m as Record<string, unknown>;
    if (typeof r.id !== 'string' || !r.id) return [];
    if (typeof r.display_name !== 'string' || !r.display_name) return [];
    return [
      {
        id: r.id,
        display_name: r.display_name,
        description: typeof r.description === 'string' ? r.description : '',
      },
    ];
  });
}

/** 请求体字段表：字段 → [契约键, 值]，返回 null = **不发键**（= 后端默认）。
 *
 *  `Record<keyof T, …>` 是编译期完整性锁：payload 类型新增字段而此处未登记
 *  → tsc 失败。此前的手写白名单会**静默**把新字段丢掉——请求照发、后端拿
 *  不到，是最难查的一类失效。字段判空规则因语义而异（falsy / undefined /
 *  非空数组），故此表只统一「构造」，不强行统一「判空」。
 *
 *  契约键的类型是 `keyof T & string` 而非裸 string：本项目的 payload 键与线上
 *  契约键同名，把这条不变量写进类型——写错键名（如 'max_step'）直接编译失败，
 *  而不是发出一条后端不认的请求。 */
type BodyEntry<T> = [keyof T & string, unknown] | null;

type BodyFields<T> = Record<keyof T, (payload: T) => BodyEntry<T>>;

/** 依字段表构造请求体（null 条目跳过）。键序 = 表内声明序。 */
function buildBody<T extends object>(payload: T, table: BodyFields<T>): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const serialize of Object.values(table) as ((p: T) => BodyEntry<T>)[]) {
    const entry = serialize(payload);
    if (entry) body[entry[0]] = entry[1];
  }
  return body;
}

/** create 路径字段表（与下方 sendMessage 的 amend 四项同词汇、各自登记）。 */
const START_SESSION_FIELDS: BodyFields<StartSessionPayload> = {
  task: (p) => ['task', p.task],
  workspace: (p) => (p.workspace ? ['workspace', p.workspace] : null),
  cwd: (p) => (p.cwd ? ['cwd', p.cwd] : null),
  max_steps: (p) => (p.max_steps !== undefined ? ['max_steps', p.max_steps] : null),
  auto_approve: (p) => (p.auto_approve !== undefined ? ['auto_approve', p.auto_approve] : null),
  model: (p) => (p.model ? ['model', p.model] : null),
  permission_mode: (p) => (p.permission_mode ? ['permission_mode', p.permission_mode] : null),
  agent_profile: (p) => (p.agent_profile ? ['agent_profile', p.agent_profile] : null),
  reasoning_effort: (p) => (p.reasoning_effort ? ['reasoning_effort', p.reasoning_effort] : null),
  context_providers: (p) =>
    p.context_providers && p.context_providers.length > 0 ? ['context_providers', p.context_providers] : null,
};

/** POST a new session. Returns the raw Response — SSE stream is consumed by caller.
 *  401 throws UnauthorizedError (after broadcasting) — fail fast, no empty stream.
 *
 *  「有值才带键」的**单一执行点**（与 sendMessage 同一契约，见 lib/amend.ts）：
 *  空值 / 空数组不发键 = 后端默认；调用方只做字段名映射，不判空。 */
export async function startSession(payload: StartSessionPayload): Promise<Response> {
  return apiFetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(buildBody(payload, START_SESSION_FIELDS)),
  });
}

/** create 会话失败时后端给的可行动原因（`{detail}` 的两种合法形状，见 readErrorDetail）。
 *
 *  单独开这个缝的原因：「在此项目中新建任务」确认面（#169 AC12）要把后端 detail
 *  **原样**留在浮层里，而 useSession 的失败通道是给用户看的一句话——它还带着
 *  "422 = 未知模型"的旧语义（App 据那句话刷新模型目录）。两者混在一起会让
 *  「cwd 目录不存在」被显示成「模型不可用」。由调用方自己读，两条语义各自成立。 */
export async function startSessionErrorDetail(res: Response): Promise<string> {
  return readErrorDetail(res);
}

/** POST /api/sessions/{id}/messages（PRD §5.3 续聊入口）。
 *  后端两种响应：
 *    - launched → SSE 流（与 POST /api/sessions 同形），返回原始 Response 供 consumeSSE 消费；
 *    - queued / steered → JSON 确认（Content-Type: application/json），调用方需检查
 *      res.headers.get('content-type') 区分。
 *  空闲会话（无在途 run）→ launched 直驱新 run，返回 SSE。
 *  在途会话 → queued 入队（JSON），消息在下个 run 自然消费。 */
export interface SendMessagePayload {
  content: string;
  mode?: 'queue' | 'steer';
  max_steps?: number;
  /** 续聊 amend 字段（后端 Q2 批次起 /messages 接受，与 create 路径同词汇）：
   *  - model: GET /api/models 的 name；
   *  - agent_profile: GET /api/agent-profiles 的 id；
   *  - reasoning_effort: GET /api/reasoning-efforts 的 id；
   *  - context_providers: GET /api/context-providers 的 id 列表。
   *
   *  生效范围：仅「空闲会话 → launched 新 run」时应用；在途 run 的 queued
   *  消息忽略它们（runtime 已固定，不抢断不改写）。空值不发键 = 后端默认，
   *  与 POST /api/sessions 的 create 分支同一模式。
   *
   *  ⚠ 已知 Gap：`context_providers: []` 同样不发键，但契约把 `[]` 定为「显式
   *  空集」（交接 §3.1；后端区分 None/[]）。前端选择器无法表达「零个」，
   *  改语义需先定契约。 */
  model?: string;
  agent_profile?: string;
  reasoning_effort?: string;
  context_providers?: string[];
}

/** 续聊路径字段表——amend 四项与 START_SESSION_FIELDS 同词汇；mode / max_steps
 *  有后端默认值，故缺省在此补齐（与 create 路径「缺省即不发键」不同）。 */
const SEND_MESSAGE_FIELDS: BodyFields<SendMessagePayload> = {
  content: (p) => ['content', p.content],
  mode: (p) => ['mode', p.mode ?? 'queue'],
  max_steps: (p) => ['max_steps', p.max_steps ?? 10],
  model: (p) => (p.model ? ['model', p.model] : null),
  agent_profile: (p) => (p.agent_profile ? ['agent_profile', p.agent_profile] : null),
  reasoning_effort: (p) => (p.reasoning_effort ? ['reasoning_effort', p.reasoning_effort] : null),
  context_providers: (p) =>
    p.context_providers && p.context_providers.length > 0 ? ['context_providers', p.context_providers] : null,
};

export async function sendMessage(sessionId: string, payload: SendMessagePayload): Promise<Response> {
  return apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // 「有值才带键」的单一执行点（与 startSession 同一契约，见 lib/amend.ts）：
    // 调用方只做字段名映射，空值 / 空数组的丢弃只在这里发生。
    body: JSON.stringify(buildBody(payload, SEND_MESSAGE_FIELDS)),
  });
}

/** GET /api/sessions/{id}/stream?after_seq=N（T4 #97，契约回执 §3）：重放 durable
 *  事件（after_seq < seq ≤ 游标，按 seq 序）后接续在途流——后端先订阅后取游标，
 *  无缝无重复；重放帧与 live 帧同形状（不含 event_id），前端一套 reducer 两条
 *  通道。返回原始 Response 由 consumeSSE 消费；404 = 会话不存在（终止重连）。 */
export async function streamSession(sessionId: string, afterSeq: number): Promise<Response> {
  return apiFetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/stream?after_seq=${Number.isFinite(afterSeq) ? afterSeq : -1}`,
  );
}

/** POST /api/sessions/{id}/approve — interactive approval decision (#37, PRD §2.2).
 *  Backend resolves the pending approval via PendingApprovalQueue.resolve().
 *  Response (200): {"status":"resolved","approval_id":"...","decision":"approve_once"}
 *  409 (`ApprovalAlreadyResolved`) = 幂等已决 → AlreadyResolvedError → 调用方翻卡片。
 *  ⚠ 404 **不是**幂等已决：后端 404 有四个来源（session 不存在 / 审批队列缺失 /
 *    `approval_id` 不在队列 / 事件过期；`web/app.py:1157-1166`），**无法**与
 *    「已解析且已出队」区分。把它当成功 = 决策其实没生效却显示「已批准」的
 *    安全假象，正是 OBS-015 要消灭的那类 bug。故 404 与其它非 2xx 同级 →
 *    plain Error → 卡片保持 pending + 可重试。
 *    真已决的兜底不靠错误码：`permission/resolved` 投影事件会把卡片移出待决队列。
 *  Other non-ok = real failure (decision did NOT reach backend) → plain Error.
 *  OBS-015 fix: the caller must distinguish these two — flipping the card to
 *  "decided" on a network error is a dangerous false positive for security. */
export async function postApproval(
  sessionId: string,
  approvalId: string,
  approved: boolean,
): Promise<{ status: string; approval_id: string; decision: string }> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      approval_id: approvalId,
      approved,
      decision: approved ? 'approve_once' : 'deny',
    }),
  });
  if (res.status === 409) throw new AlreadyResolvedError('审批已决（幂等）');
  if (!res.ok) throw new Error(`审批失败（${res.status}）`);
  return res.json();
}

// ── Models（后端契约回执 §3，T10 #103：多模型不写死，grill Q2 拍板）──

/** GET /api/models 目录条目。零密钥字段；name 是 POST /api/sessions 的选择键；
 *  思考能力不进元数据（显示侧由 reasoning 事件族驱动，有则显示无则不显示）。 */
export interface ModelCatalogEntry {
  name: string;
  provider: string | null;
  model: string | null;
  default: boolean;
}

/** GET /api/models。窄化解析（零伪造）：仅 name 非空字符串的条目入选，
 *  可选字段缺失记 null；models 数组缺失/形状不符 → 空数组——调用方据此
 *  降级隐藏选择器入口，绝不伪造列表。 */
export async function getModels(): Promise<ModelCatalogEntry[]> {
  const res = await apiFetch('/api/models');
  if (!res.ok) throw new Error(`models ${res.status}`);
  const body: unknown = await res.json();
  const raw =
    typeof body === 'object' && body !== null && Array.isArray((body as { models?: unknown }).models)
      ? ((body as { models: unknown[] }).models)
      : [];
  return raw.flatMap((m) => {
    if (typeof m !== 'object' || m === null) return [];
    const r = m as Record<string, unknown>;
    if (typeof r.name !== 'string' || !r.name) return [];
    return [
      {
        name: r.name,
        provider: typeof r.provider === 'string' ? r.provider : null,
        model: typeof r.model === 'string' ? r.model : null,
        default: r.default === true,
      },
    ];
  });
}

// ── Cancel（后端契约回执 §3，T5 #98：detached-run 显式中断唯一入口）──

/** POST /api/sessions/{id}/cancel。200 {"status":"cancelling"|"no_active_run"}
 *  ——两者都是幂等成功（Esc 与 run 恰好刚终结的竞态是常态，契约明示不是错误）；
 *  404 = 会话不存在。非 2xx 抛 Error——调用方 cancelStream 静默降级，
 *  流终态/错误路径是 UI 收尾权威。run/failed(data.reason='cancelled') 随后
 *  经流广播（订阅者照常收到终态帧）。 */
export async function cancelSession(sessionId: string): Promise<{ status: string }> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) throw new Error(`cancel ${res.status}`);
  return res.json();
}

// ── Recover（后端新端点，df4f7d8 §1.1）──

/** 恢复失败的可区分错误：status 404 = 会话不存在；409 = 存在需人工裁决的高风险
 *  操作（detail 说明原因，本期只展示，不做裁决交互——不变量 #14 不盲跑）。 */
export class RecoverError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** POST /api/sessions/{id}/recover — 幂等。200 返回该 session 全量事件数组
 *  （与 GET events 同构），调用方直接走既有 projectHistory 重建管线（不变量 #22：
 *  不引入第二套会话真相）。 */
export async function recoverSession(sessionId: string): Promise<AgentEvent[]> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/recover`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  if (res.status === 404) throw new RecoverError(404, '会话不存在');
  if (res.status === 409) {
    const detail = await readErrorDetail(res);
    throw new RecoverError(409, detail || '存在需要人工裁决的高风险操作');
  }
  if (!res.ok) throw new RecoverError(res.status, `恢复失败（${res.status}）`);
  return res.json();
}

// ── Session-level model switch（T7 #137，PRD §2.3）──

/** POST /api/sessions/{id}/model 的响应体。
 *  ``effective_model_id`` 是 service 解析出的规范 picker id——不能回显请求值，
 *  否则上游 model_name / "default" 别名会与事件里的 to_model_id 对不上。 */
export interface ModelChangeResult {
  status: string;
  provider: string;
  model_id: string;
}

/** 切换会话当前模型并写 ``model/changed``（PRD §2.3）。
 *
 * - 切换不打断在途 run——下一轮 run 从事件流派生当前模型生效
 * - ``GET /api/models`` 的默认条目（is_default=true）也是合法 POST target = 切回默认链
 * - 响应回传规范 model_id（service 解析出的 picker id），不回显请求值
 *
 * 错误码：
 * - 404 = session 不存在
 * - 422 = provider/model_id 不在 catalog
 */
export async function changeSessionModel(
  sessionId: string,
  provider: string,
  modelId: string,
): Promise<ModelChangeResult> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/model`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, model_id: modelId }),
  });
  if (!res.ok) throw new Error(`change model ${res.status}`);
  return res.json();
}

// ── Fork（T7 #137，PRD §2.4）──

/** POST /api/sessions/{id}/forks 的响应体。 */
export interface ForkResult {
  session_id: string;
  from_seq: number;
}

/** 从历史用户消息 seq 派生 child session（PRD §2.4）。
 *
 * - 锚点消息本身不进 child seed（child 侧由用户重新发送，pi /fork 同款语义）
 * - child 继承父会话当前模型
 * - HTTP 端点不生成 tail summary（确定性、无模型调用）
 * - copy-on-fork：父 workspace 整目录复制为 child 的
 *
 * 错误码：
 * - 404 = session 不存在
 * - 409 = 在途 run（历史未 settled）
 * - 422 = from_seq 不是合法 fork 锚点（不是用户消息 seq）
 */
export async function forkSession(
  sessionId: string,
  fromSeq: number,
): Promise<ForkResult> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/forks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ from_seq: fromSeq }),
  });
  if (!res.ok) {
    // BUG-001 fix：解析后端 detail 给用户看（可用边界列表等）。
    let detail = '';
    try { detail = (await res.json())?.detail ?? ''; } catch { /* keep '' */ }
    throw new Error(detail || `fork ${res.status}`);
  }
  return res.json();
}

// ── Projects（WS-4 / #154 端点，WS-5 / #155 前端消费）──

/** 项目操作失败——带 HTTP 状态码，调用方据此给**具体**原因而不是"操作失败"。
 *
 *  状态码语义（后端 `domain_errors.py` 两张表 + `web/projects.py`）：
 *  - 403：跨源被来源闸拒绝（ADR-0025 D1）——本地信任模式下非本机 Origin；
 *  - 404：项目 id 不存在 / 注册的路径不存在（create 不会 mkdir）；
 *  - 409：请求与账本现状冲突——attach 时会话 cwd 与项目路径不一致，
 *    或重排锚点不在该项目账本里；
 *  - 422：入参形态非法——非绝对路径 / 该路径不是目录 / 标题空白。
 *
 *  `detail` 是后端给的中文原因（可能为空）：调用方优先显示它。 */
export class ProjectError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** 把项目写操作的异常翻成给用户看的一句话（对话框与侧栏错误条共用）。
 *
 *  `ProjectError` 优先用**后端 detail**——409 会给出"会话 cwd 与项目路径不一致"
 *  这类可行动原因，翻译成"操作失败"等于把它扔掉；其余异常用 `message`；都没有才
 *  用调用方的兜底文案。 */
export function describeProjectError(error: unknown, fallback: string): string {
  if (error instanceof ProjectError) return error.message || fallback;
  const message = (error as Error | null)?.message;
  return message || fallback;
}

/** 按路径把目录注册为项目（幂等：同规范路径 → 返回既有实体）。
 *  路径不存在 → ProjectError(404)；非绝对路径 → ProjectError(422)。 */
export async function createProject(path: string, title?: string | null): Promise<Project> {
  const res = await apiFetch('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(title ? { path, title } : { path }),
  });
  if (!res.ok) throw await projectError(res, '注册项目失败');
  return requireProject(await res.json());
}

/** 全部项目（**注册表顺序**，新建项目前插）。空数组 ≠ 出错：调用方据此显示
 *  「还没有项目」而不是错误横幅。 */
export async function listProjects(): Promise<Project[]> {
  const res = await apiFetch('/api/projects');
  if (!res.ok) throw await projectError(res, '加载项目失败');
  const body: unknown = await res.json();
  if (!Array.isArray(body)) return [];
  // 形状不符的单条丢弃（零伪造）——但**不因此丢掉其余项目**。
  return body.flatMap((raw) => {
    const p = parseProject(raw);
    return p ? [p] : [];
  });
}

/** 重命名项目（`setTitle`）。 */
export async function renameProject(projectId: string, title: string): Promise<Project> {
  const res = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw await projectError(res, '重命名失败');
  return requireProject(await res.json());
}

/** **软删除**项目：只摘注册记录与账本，目录/用户文件/会话日志一概不动，
 *  成员会话回到未分组。响应 `detail` 必须原样展示给用户（AC5）。 */
export async function deleteProject(projectId: string): Promise<ProjectDeleted> {
  const res = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw await projectError(res, '删除项目失败');
  // 形状防御：非对象（null / 数组 / 字符串）一律当"没给"处理，别让读字段抛
  // TypeError——那时调用方拿到的是"读属性失败"，而不是"软删除成功了但回执为空"。
  const raw: unknown = await res.json().catch(() => null);
  const body = (typeof raw === 'object' && raw !== null ? raw : {}) as Partial<ProjectDeleted>;
  return {
    id: typeof body.id === 'string' ? body.id : projectId,
    deleted: body.deleted === true,
    sessions_detached:
      typeof body.sessions_detached === 'number' ? body.sessions_detached : 0,
    detail: typeof body.detail === 'string' ? body.detail : '',
  };
}

/** 把会话加入项目（幂等）。会话不存在/无 cwd → 404；cwd 与项目路径不一致 → 409。 */
export async function attachSessionToProject(
  projectId: string,
  sessionId: string,
): Promise<Project> {
  const res = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!res.ok) throw await projectError(res, '加入项目失败');
  return requireProject(await res.json());
}

/** 把会话移出项目（幂等：不在本项目 → 无写操作；会话日志逐字节不动）。 */
export async function detachSessionFromProject(
  projectId: string,
  sessionId: string,
): Promise<Project> {
  const res = await apiFetch(
    `/api/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}`,
    { method: 'DELETE' },
  );
  if (!res.ok) throw await projectError(res, '移出项目失败');
  return requireProject(await res.json());
}

/** 项目内重排（DOM `insertBefore` 语义：`before=null` → 追加尾部）。
 *  锚点不在该项目账本里 → ProjectError(409)。 */
export async function reorderProjectSession(
  projectId: string,
  sessionId: string,
  before: string | null,
): Promise<Project> {
  const res = await apiFetch(
    `/api/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/order`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ before }),
    },
  );
  if (!res.ok) throw await projectError(res, '调整顺序失败');
  return requireProject(await res.json());
}

/** 非 2xx → ProjectError（detail 优先，缺失时用调用方给的兜底前缀 + 状态码）。 */
async function projectError(res: Response, fallback: string): Promise<ProjectError> {
  const detail = await readErrorDetail(res);
  return new ProjectError(res.status, detail || `${fallback}（${res.status}）`);
}

// ── 宿主侧目录列举（WS-7 / #170，ADR-0028）──

/** GET /api/host/dirs —— 宿主侧目录列举：只读、一层、仅目录（ADR-0028 D3–D5）。
 *
 *  `path` 省略 = 列**根**（Windows 盘符 / POSIX `/`），响应的 `path` 为 null。
 *  失败时抛 `ProjectError`（detail 优先）：403 无权限 / 404 不存在 / 422 不是目录
 *  等全部由后端 detail 说明，界面**原样显示**——PRD §4.4 的错误矩阵就是按"前端不
 *  翻译"设计的，这里多加一句自己的话就会把矩阵里的措辞盖掉。
 *
 *  形状窄化：非法条目丢弃但**不牵连其余**（与 listProjects / listMemories 同一条
 *  纪律）；`path`/`parent` 只认字符串，其余一律当"没有"（那时界面显示的是"没有当前
 *  目录"而不是一个编造的路径）。 */
export async function getHostDirs(path?: string | null): Promise<HostDirsListing> {
  const query = path ? `?path=${encodeURIComponent(path)}` : '';
  const res = await apiFetch(`/api/host/dirs${query}`);
  if (!res.ok) throw await projectError(res, '加载目录失败');
  const raw: unknown = await res.json().catch(() => null);
  const body = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  const entries = Array.isArray(body.entries)
    ? body.entries.flatMap((raw) => {
        if (typeof raw !== 'object' || raw === null) return [];
        const entry = raw as Record<string, unknown>;
        if (typeof entry.name !== 'string' || !entry.name) return [];
        if (typeof entry.path !== 'string' || !entry.path) return [];
        return [{ name: entry.name, path: entry.path }];
      })
    : [];
  return {
    path: typeof body.path === 'string' && body.path ? body.path : null,
    parent: typeof body.parent === 'string' && body.parent ? body.parent : null,
    truncated: body.truncated === true,
    entries,
  };
}

/** 记忆请求失败：状态码 + 后端 detail 原文（detail 优先，是 AC 要展示的那句话）。 */
export class MemoryError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** 记忆列表（GET /api/memories，**按创建时间倒序**分页）。
 *
 *  `limit` 由后端夹在 1..200（本函数不猜上界，越界由后端 422 说话）；`offset` 是
 *  「跳过的条数」——"加载更多"传已显示的条数。空数组 ≠ 出错：调用方据此显示
 *  「还没有记忆」而不是错误横幅（与 `listProjects` 同一条纪律）。
 *
 *  形状不符的单条**丢弃但不牵连其余**：一条坏行不能把整页变成"加载失败"。
 */
export async function listMemories(limit = 50, offset = 0): Promise<MemorySummary[]> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  const res = await apiFetch(`/api/memories?${params.toString()}`);
  if (!res.ok) throw await memoryError(res, '加载记忆失败');
  const body: unknown = await res.json();
  if (!Array.isArray(body)) return [];
  return body.flatMap((raw) => {
    const memory = parseMemory(raw);
    return memory ? [memory] : [];
  });
}

/** 硬删一条记忆（DELETE /api/memories/{id}）——**不可恢复**，调用方必须先二次确认。
 *
 *  状态码语义（后端 `web/memory.py` 的契约，前端不合并它们）：
 *    200 `{id, deleted:true}` / 404 id 不存在 / 403 不属于当前入口 / 503 记忆未装配。
 *  404 与 403 都**不**当成功：用户对着具体一条点删除，"已经不在了"与"不给删"是
 *  两种不同结果，UI 要分别说（这也是前端不维护第二套真相的必然要求——本地删掉
 *  而后端拒绝会直接违背不变量 #22）。
 */
export async function deleteMemory(memoryId: string): Promise<MemoryDeleted> {
  const res = await apiFetch(`/api/memories/${encodeURIComponent(memoryId)}`, { method: 'DELETE' });
  if (!res.ok) throw await memoryError(res, '删除记忆失败');
  const raw: unknown = await res.json().catch(() => null);
  const body = (typeof raw === 'object' && raw !== null ? raw : {}) as Partial<MemoryDeleted>;
  return {
    id: typeof body.id === 'string' ? body.id : memoryId,
    deleted: body.deleted === true,
  };
}

/** 非 2xx → MemoryError（detail 优先，缺失时用兜底前缀 + 状态码）。 */
async function memoryError(res: Response, fallback: string): Promise<MemoryError> {
  const detail = await readErrorDetail(res);
  return new MemoryError(res.status, detail || `${fallback}（${res.status}）`);
}

/** 记忆能力未装配（503）——**配置状态，不是故障**：UI 要显示「记忆未启用」而不是
 *  "加载失败/重试"，否则用户会一直点重试去修一个不存在的故障（不变量 #21）。 */
export function isMemoryDisabled(error: unknown): boolean {
  return error instanceof MemoryError && error.status === 503;
}

/** 记忆错误 → 展示文案：MemoryError 的 message 就是后端 detail（或兜底前缀），
 *  其余异常（网络层 TypeError 等）用 message 或 fallback。与 `describeProjectError`
 *  同构但**分开**：两个能力各自演进，共用一个会让某一侧的语义渗到另一侧。 */
export function describeMemoryError(error: unknown, fallback: string): string {
  if (error instanceof MemoryError) return error.message || fallback;
  const message = (error as Error | null)?.message;
  return message || fallback;
}

/** 窄化解析一条记忆（零伪造）：id/content/created_at 必须是非空字符串、scope 必须是
 *  已知取值、metadata 必须是对象；任一不符 → null。**不补默认值**：`scope` 猜错会让
 *  用户以为这条记在别的名下，`created_at` 补空串会让"记于何时"变成谎言。 */
function parseMemory(raw: unknown): MemorySummary | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const row = raw as Record<string, unknown>;
  const { id, content, created_at: createdAt, scope, metadata } = row;
  if (typeof id !== 'string' || !id) return null;
  if (typeof content !== 'string') return null;
  if (typeof createdAt !== 'string' || !createdAt) return null;
  if (scope !== 'user' && scope !== 'session') return null;
  return {
    id,
    content,
    scope: scope as MemoryScope,
    metadata: typeof metadata === 'object' && metadata !== null
      ? (metadata as Record<string, unknown>)
      : {},
    created_at: createdAt,
  };
}

/** 窄化解析项目（零伪造）：id/path/title/session_ids 形状不符 → null（调用方丢弃该条）。
 *
 *  `status` 是**例外**：只有明确等于 `'missing-dir'` 才取该值，其余（含未知字符串、
 *  缺失）一律 `'ok'`。理由——它是"目录可能不见了"的**告警标志**，不是存在性断言；
 *  把未知值当告警会让每条项目都亮黄点（假告警），而把项目整条丢掉更糟：项目会从
 *  UI 消失、它的会话被误判成未分组。未知状态值不值得付出这两个代价。 */
function parseProject(raw: unknown): Project | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.id !== 'string' || !r.id) return null;
  if (typeof r.path !== 'string' || !r.path) return null;
  if (typeof r.title !== 'string') return null;
  const sessionIds = Array.isArray(r.session_ids)
    ? r.session_ids.filter((v): v is string => typeof v === 'string' && v.length > 0)
    : [];
  return {
    id: r.id,
    path: r.path,
    title: r.title,
    status: normalizeProjectStatus(r.status),
    session_ids: sessionIds,
    created_at: typeof r.created_at === 'string' ? r.created_at : '',
    updated_at: typeof r.updated_at === 'string' ? r.updated_at : '',
  };
}

/** 单实体端点：200 但形状不符 → **响亮失败**而不是返回伪造项目。
 *  （列表端点相反：丢掉坏条目、保留其余，见 `listProjects`——一条坏数据不该让整
 *  个侧栏空掉，但一个"注册成功"的假实体更糟：它会让 UI 显示一个后端并不存在的项目。） */
function requireProject(raw: unknown): Project {
  const project = parseProject(raw);
  if (!project) {
    throw new Error('项目响应形状不符（后端契约可能已变更）');
  }
  return project;
}

function normalizeProjectStatus(raw: unknown): ProjectStatus {
  return raw === 'missing-dir' ? 'missing-dir' : 'ok';
}

