/** T8（#101）E2E fixture 助手——mock SSE 帧形状 = docs/BACKEND_CONTRACT_STREAMING_UI.md：
 *  text/delta durable 承接文本流、reasoning 族 envelope block_id、tool/output_delta
 *  按 channel、seq 每 session 单调。page.route 拦截 API，核心矩阵不依赖真后端。 */

import { expect, type Page, type Route } from '@playwright/test';

/** run 终态词汇（`ending` 缺省推导用）。**刻意自带一份**，不 import
 *  `src/lib/runState`：mock 若与应用共享同一个集合，应用漏掉某个终态时
 *  mock 也一起漏，两边同时错就永远测不出来（T8 的 `run/interrupted` 正是这么漏的）。 */
const RUN_TERMINAL_TYPES = new Set(['run/completed', 'run/failed', 'run/interrupted']);

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

/** ── WS 接流 mock（#206）──
 *
 *  live 流的**真实**通道是 `GET /api/ws`（交付层会把整个 HTTP 响应攒到流结束才下发，
 *  详见 `web/src/lib/wsStream.ts` 的实测表：POST 44.2s / GET 41.6s 才到响应头）。
 *  `page.route` 拦不到 WebSocket —— 不 mock 这条通道，界面在 e2e 里就没有可用的
 *  实时通道：握手落到 vite dev server 上失败 → 前端降级回 SSE → 于是「接流/重连」
 *  类断言考的是**降级路径**，不是用户真实走的路径。
 *
 *  这里按真后端的下行契约答话（`web/websocket.py`）：
 *
 *      subscribe(sid) → snapshot{events, has_active_run} → event* → done
 *
 *  缺省剧本 = 该会话没有在途 run：快照即全量（`has_active_run:false`，客户端按约定
 *  立即收尾，不空转也不重连）。有 `frames` = 有在途 run（`has_active_run:true`，
 *  客户端据此保持收流），送完帧再补 `done`——与旧 `fulfillSse` 的「帧 + 包体结束」
 *  等价，因此原有 spec 的语义不用改。
 *
 *  快照事件与 `GET /events` **同源**（`sessionEvents` / `mock.events`）：真后端两者
 *  读同一份 durable 日志，mock 若各说各话，就会出现"接流补的帧和历史对不上"这种
 *  只在 mock 里存在的现象。
 *
 *  心跳不模拟：客户端只应答 `server_ping`（不依赖它维持连接），模拟它没有观测价值。 */
export interface WsScript {
  /** 快照事件。缺省 = 与 `GET /events` 同源（sessionEvents / mock.events）。 */
  events?: FrameSpec[];
  /** 快照之后逐帧下发（= 在途 run 的增量）。给了它 ⇒ `has_active_run: true`。 */
  frames?: FrameSpec[];
  /** 显式声明快照的 `has_active_run`。缺省 = 有 `frames` 就 true。
   *  用例：服务端**有**在途 run、但本地游标之后暂时没有新事件（模型在思考）——
   *  此时客户端不该把流当成收尾。⚠ 它**只**声明这个字段：要真的维持连接（不发
   *  `done`）必须同时给 `ending: 'keep'`。 */
  hasActiveRun?: boolean;
  /** 快照 + frames 之后怎么收场。缺省按**帧内容**推导（真后端只在 run 终态事件后
   *  下行 `done`，见 `web/websocket.py` 的 `_relay_events`）：末帧是终态 → `done`；
   *  否则连接保持（`keep`）——「有在途 run 却没有终态帧」时发 done 会把客户端
   *  推进「流异常收尾 → 重连」的岔路，那种假红很难归因。显式可覆盖：
   *  - `done` = 送 done 收尾（等价旧 `fulfillSse` 的「帧 + 包体结束」）；
   *  - `keep` = 连接保持（考「停摆」「等待提示」需要它，`route.fulfill` 给不出长连接）；
   *  - `drop` = 直接断开、**不送 done**（真实网络断线）——重连类用例的起点。 */
  ending?: 'done' | 'keep' | 'drop';
  /** 首次下行前延迟（毫秒）：考「断线条延迟显示」「接流补帧」的时序。 */
  delayMs?: number;
  /** 一个帧都不发就关闭（服务端/代理拒掉这条订阅）：考零服务帧路径。 */
  closeNow?: boolean;
}

export type WsProvider = (ctx: {
  sessionId: string;
  /** 第几次接流（从 1 数）——区分「首次接流」与「重连/刷新后接流」。 */
  call: number;
}) => WsScript | undefined | Promise<WsScript | undefined>;

/** 装 WS 接流 mock（在 `routeApi` 内调用——快照要与 /events 同源，只有那里拿得到
 *  `sessionEvents`）。
 *
 *  ⚠ **必须 `await`**：`page.routeWebSocket` 的注册是异步的，`void` 掉再导航
 *  **不会生效**（实测：同一份 handler，awaited 拦得到、void 的拦不到，而
 *  `page.on('websocket')` 能看到连接已建立——那种"看起来跑了、实际走的另一条路"
 *  最难查）。`routeApi` 因此是 async 的，调用点一律 `await`。 */
async function installWsRoute(
  page: Page,
  mock: ApiMock,
  sessionEvents: Map<string, FrameSpec[]>,
): Promise<void> {
  let calls = 0;
  await page.routeWebSocket(/\/api\/ws$/, (ws) => {
    ws.onMessage((raw) => {
      let msg: { type?: string; session_id?: string; after_seq?: unknown };
      try {
        msg = JSON.parse(String(raw)) as typeof msg;
      } catch {
        return;
      }
      if (msg.type !== 'subscribe' || !msg.session_id) return; // pong / 未知上行：忽略
      // 记录每次订阅的**上行原文**（#208 游标契约的观测点）：断言"客户端带了游标"
      // 只能看这里——客户端把游标丢掉时服务端行为依旧正确（当 -1 从头发），
      // 于是"没带"这件事在界面上完全看不出来。
      mock.wsSubscribes?.push({ session_id: msg.session_id, after_seq: msg.after_seq });
      const sessionId = msg.session_id;
      calls += 1;
      const call = calls;
      void (async () => {
        const script = await mock.onWs?.({ sessionId, call });
        if (script?.closeNow) {
          ws.close();
          return;
        }
        if (script?.delayMs) await new Promise((r) => setTimeout(r, script.delayMs));
        const events = script?.events ?? sessionEvents.get(sessionId) ?? mock.events ?? [];
        const frames = script?.frames ?? [];
        const active = script?.hasActiveRun ?? frames.length > 0;
        ws.send(
          JSON.stringify({
            type: 'snapshot',
            session_id: sessionId,
            events,
            has_active_run: active,
          }),
        );
        for (const f of frames) {
          ws.send(JSON.stringify({ type: 'event', session_id: sessionId, event: f }));
        }
        // run 收口 → 服务端 relay task 下行 done（真后端在带终态帧后就是这么收尾的）。
        // `hasActiveRun: true` 而未声明 ending 也照样收尾：脚本没说要保持，就当成
        // 「这些帧之后 run 结束了」——比留一条永远不说话的连接更不容易误导出假绿。
        const ending =
          script?.ending ?? (frames.some((f) => RUN_TERMINAL_TYPES.has(f.type)) ? 'done' : 'keep');
        if (ending === 'drop') {
          ws.close(); // 异常收尾：没有 done（客户端只能靠 terminalSeen 判断）
        } else if (active && ending === 'done') {
          ws.send(JSON.stringify({ type: 'done', session_id: sessionId }));
        }
      })();
    });
  });
}

export interface ApiMock {
  /** GET /api/sessions（会话列表） */
  sessions?: unknown[];
  /** GET /api/sessions/{id}/events（历史重放） */
  events?: FrameSpec[];
  /** 调用方传入的空数组：fixture 把每次 WS `subscribe` 上行的 `{session_id, after_seq}`
   *  推进去（#208）。用来锁"客户端把本地游标带上了"——见 `installWsRoute` 的注释。 */
  wsSubscribes?: Array<{ session_id: string; after_seq?: unknown }>;
  /** 第 N 次之后的 `GET /api/sessions` 直接回 500（N 从 1 数）。
   *  用途：构造"归档写成功了、但随后的列表重拉失败"这个只在真机上偶发的窗口——
   *  界面此刻的行还是旧状态，必须**说出来**而不是沉默（#171 AC9 的就地报错）。
   *  不设 = 永远成功。 */
  sessionsListFailAfter?: number;
  /** GET /api/models（模型目录；缺省 = 空目录 → 选择器降级隐藏） */
  models?: unknown[];
  /** POST /api/sessions（live 流） */
  onSessionPost?: (route: Route) => Promise<void> | void;
  /** `GET /api/ws` 的接流剧本（每次 `subscribe` 调用一次；第 N 次 = 第 N 次接流）。
   *  缺省 = 会话无在途 run：快照即全量、立即收尾（见 `WsScript`）。 */
  onWs?: WsProvider;
  /** `GET /api/sessions/{id}/stream?after_seq=N` —— **只剩两条非主路径**用它：
   *  ① WS 不可用时的降级兜底（配合 `onWs: () => ({ closeNow: true })`）；
   *  ② 驱动 `stream/truncated` 控制帧（#208 起两条通道都发：SSE 走这里，
   *     WS 走 `onWs` 的快照分支——本参数只覆盖 SSE 那条）。
   *  实时流请用 `onWs`。不设 = `route.abort()`（没人 mock 时必然失败，不静默通过）。 */
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
  // ── #182 能力声明显隐 ──
  /** GET /api/capabilities 的条目（形状 = 后端 `capability/manifest.py::manifest_entry`；
   *  用 `capabilityFixture` 造插件条目）。
   *
   *  **缺省 = `[CORE_CAPABILITY]`**（#193）：真实后端在 `CAPABILITIES=""` 时返回的正是
   *  "只有 core 一条"——core 是**内置工具集**的声明，恒在，且声明
   *  `changes`/`terminal` 为 true（那两个面由 `write`/`edit`/`apply_patch`/`bash` 产出，
   *  不由任何插件产出）。此前缺省是空列表，那是 #193 修掉之前的事实；空列表那条路径
   *  仍有意义（老后端 / 显式 mock），所以想覆盖它就在用例里显式传 `capabilities: []`。
   *
   *  这里**不**给"默认全 true"的省事值：声明是能力的事，mock 给什么就渲染什么
   *  （否则绿灯只证明"前端与我的假后端一致"——本仓已栽过这个坑）。 */
  capabilities?: unknown[];
  /** capabilities 端点直接回错误（老后端 404 / 服务异常）→ 前端降级为缺省语义。 */
  capabilitiesError?: { status: number; detail: string };
  // ── #185 路由 / #186 消费：GET /api/sessions/{id}/artifacts/{aid} ──
  /** 内容切片（形状 = 后端 `ArtifactSlice.model_dump()`）。 */
  artifactContent?: unknown;
  /** 直接回错误（404 不在本会话 / 503 没配存储 / 422 形态非法）。 */
  artifactContentError?: { status: number; detail: string };
  /** 拦截口（计数 / 按 artifact_id 给不同内容）；返回 true = 已处理。 */
  onArtifactGet?: (route: Route, artifactId: string) => Promise<boolean> | boolean;
  /** GET /api/capabilities 的拦截口（计数 / 断言"端点真的被消费了"）；返回 true = 已处理。
   *  为什么要这个口子：**"端点没被调用"这类回归不会改变 tab 集**（缺省 payload 与
   *  "拿到真 payload" 都会渲染出面），所以只断言 tab 集会漏掉"前端压根没调这个端点"
   *  这种 bug——`onCapabilitiesGet` 计数把"端点确实被消费且没有请求循环"钉住。
   *  返回 false 走下面的默认分支。 */
  onCapabilitiesGet?: (route: Route) => Promise<boolean> | boolean;
  /** POST /api/sessions/{id}/messages（续聊入口；空闲会话 → 同形 SSE） */
  onMessagesPost?: (route: Route) => Promise<void> | void;
  /** POST /api/sessions/{id}/queue/flush（「立即发送全部」）。缺省 404——该端点
   *  此前没有 mock（也无人调用）。 */
  onFlushPost?: (route: Route) => Promise<void> | void;
  /** POST /api/sessions/{id}/model（T7 #137 模型切换；缺省 200 → 回传请求的
   *  provider/model_id，即真实端点的「规范 model_id」形状）。
   *  注入此回调即可**计数**或延迟响应（BUG-011 回归锁：双击只允许一个请求；
   *  延迟响应用来撑开「首个请求还没回来就又点了一下」这个窗口）。 */
  onModelPost?: (route: Route) => Promise<void> | void;
  /** POST /api/sessions/{id}/forks（T7 #137 分叉；缺省 200 → 派生 child）。
   *  注入此回调即可断言请求体（BUG-001 回归锁：from_seq 必须是 user/message 的
   *  seq，不是 turn.step_id）或伪造 422。 */
  onForkPost?: (route: Route) => Promise<void> | void;
  /** POST /api/sessions/{id}/recover（T8 #138 恢复；缺省 200 → 返回 mock.events）。
   *  真实语义：响应是与 GET events 同构的全量事件数组。 */
  onRecoverPost?: (route: Route) => Promise<void> | void;
  /** POST /api/sessions/{id}/approve（#37 交互式审批决策；缺省 200 → {status,approval_id,decision}）。
   *  注入此回调即可断言请求体（回归锁：批准 → decision='approve_once'、拒绝 → 'deny'；
   *  形状与后端 `session/approval.py` 的 allowed_decisions 一致）。 */
  onApprovePost?: (route: Route) => Promise<void> | void;
  // ── ADR-0030 #195/#196 多轮投递通道 ──
  /** GET /api/sessions/{id}/queue（待发送输入首屏补齐；缺省 200 → 空队列）。
   *  #195 队列条 AC：注入此回调即可构造「重启后仍有未投递输入」的首屏。 */
  onQueueGet?: (route: Route) => Promise<void> | void;
  /** POST /api/sessions/{id}/queue/{qid}/cancel（取消排队项；缺省 200 → cancelled）。 */
  onQueueCancelPost?: (route: Route) => Promise<void> | void;
  /** GET /api/sessions/{id}/context-usage（上下文容量看板；缺省 200 → **no_data 空桶**，
   *  与真后端"这个会话没有任何用量事实"同形）。
   *  #200 看板 AC：注入此回调即可构造有数据/未采集/无数据三种状态；
   *  #212 起多了第四态 `usage_only`（有总量、缺分类——快照丢了但事件流有用量）。 */
  onContextUsageGet?: (route: Route) => Promise<void> | void;
  // ── #172 / ADR-0029 会话硬删 ──
  /** 指定 id 的 DELETE /api/sessions/{id} 直接回这个错误——用来构造只在真机上才会
   *  自然出现的拒绝：409（有在途 run / 有挂起审批 / 是 fork 父会话；**状态码相同**，
   *  只有 detail 原句不同）与 404（"这个会话本就不在了"，第二次删除）。
   *
   *  status 404 的条目会**同时**把它从会话状态里摘掉：真后端在那一刻它确实不在，
   *  留着会让重拉之后那一行又冒出来——mock 语义与真机相反（#155 轮栽过这个坑）。
   *
   *  不设 = 按真后端语义**成功**：真的从会话列表与项目账本里摘掉它，`events` 取行上
   *  的 `event_count`（真后端也是删之前取），`detached_from_projects` 数它进过几个
   *  账本。所以"删完行没了、项目计数也掉了"考的是界面跟着后端走，不是本地隐藏。 */
  sessionDeleteErrors?: Record<string, { status: number; detail: string }>;
  // ── #171 会话归档（可逆标记）──
  /** 指定 id 的 POST/DELETE `/api/sessions/{id}/archive` 直接回这个错误——用来构造
   *  真机上才有自然来源的拒绝：409（**只在归档方向上**：有在途 run / 挂起审批，
   *  见后端 `SessionService.set_archived`）与 404（元数据/日志都在的会话才会走到这里，
   *  所以 404 只能这么造）。
   *
   *  与硬删的同类字段一样：不设 = 按真后端语义**成功**（真的改行的 `archived` 字段）。 */
  sessionArchiveErrors?: Record<string, { status: number; detail: string }>;
  /** 这些 id 视为"忙"（有在途 run）→ 归档回 409，但 `sessionArchiveErrors` 优先。
   *  与硬删的差别是有意的：后端只在**归档**方向挡 409，取消归档永远允许
   *  （否则一个正在跑的会话一旦被归档就再也没法取消归档了）。 */
  sessionArchiveBusyIds?: string[];
  /** POST/DELETE `/api/sessions/{id}/archive` 的拦截口（计数 / 断言请求形状）；
   *  `archived` = 动作后的目标态（POST → true、DELETE → false）。返回 true = 已处理。 */
  onArchiveRequest?: (
    route: Route,
    sessionId: string,
    archived: boolean,
  ) => Promise<boolean> | boolean;
  // ── WS-5 / #155 项目分组 ──
  /** 项目 fixture（缺省 = 无项目 → 所有会话都在未分组区）。
   *
   *  **有状态**：fixtures 在内存里真的改这份状态并按后端语义回响应（见
   *  `routeApi` 里的 projects 分支）——create 前插注册表、rename 改标题、
   *  attach/detach 改账本**并同步改会话行的 workspace**、软删除摘记录**并把
   *  成员会话的 workspace 清成 null**、order 按 insertBefore 语义重排。
   *  这样 e2e 断言的是"界面真的跟着后端的语义走"，而不是"界面读了我们塞的假值"。 */
  projects?: ProjectFixture[];
  /** `POST /api/projects` 里**不存在**的路径集合 → 404（真实后端不 mkdir）。
   *  用来锁"路径不存在时有清晰错误"这条 AC。 */
  projectMissingPaths?: string[];
  /** POST /api/projects/{id}/sessions 的拦截口（伪造 409「会话 cwd 与项目路径
   *  不一致」等**只能在真机上才自然出现**的拒绝）；返回 true = 已处理。
   *  与 `projectMissingPaths` 的分工：那个改的是"哪些路径不存在"，这个改的是
   *  "attach 这条请求本身回什么"。 */
  onAttachPost?: (route: Route) => Promise<boolean> | boolean;
  // ── MEM-5 / #160 记忆管理 ──
  /** 记忆 fixture（缺省 = 空列表 → 面板显示"还没有记忆"）。
   *
   *  **有状态**：DELETE 真的从这份状态里摘掉条目（见 `routeApi` 的 memories 分支），
   *  所以"删掉后关掉面板再打开，该条不在"断言的是真语义，而不是界面自己的本地隐藏。
   *  分页也按真实端点走（`limit`/`offset` 切片），"加载更多"因此可被真的驱动。 */
  memories?: MemoryFixture[];
  /** 记忆能力未装配 → GET/DELETE 都回 503 + 该 detail（AC4 降级态）。
   *  刻意用真实后端那句话（`web/memory.py`）：前端把"未启用"与"没有记忆"分开显示。 */
  memoryDisabled?: string;
  /** 这些 id 的 DELETE 回 403（`web/memory.py` 的"不属于当前入口"）——AC3 的
   *  失败回滚路径（真机上要构造一条 SESSION 记忆才自然出现）。 */
  memoryDeniedIds?: string[];
  /** 这些 id 模拟「列表拉取之后、点删除之前被别处删掉」：DELETE 回 404（真后端那句
   *  `记忆不存在：<id>`）**并且**把它从状态里摘掉——于是随后的重拉里那一行不在，
   *  行内错误条无处渲染。真机上这是一个并发窗口，只能在 mock 里构造；
   *  它锁的是"失败被界面吞掉"这条缺陷（批次审查发现）。 */
  memoryVanishedIds?: string[];
  /** GET /api/memories 的拦截口（断言分页参数或伪造 500）；返回 true = 已处理。 */
  onMemoriesGet?: (route: Route) => Promise<boolean> | boolean;
  // ── WS-6 / #169 项目内新建任务 ──
  /** 带 `cwd` 建会话时伪造失败（AC12：422 留在确认面）。spec 可以先设它、断言错误
   *  在浮层里，再设回 undefined 并重试——同一条路径因此能覆盖"可重试"。
   *  不设 = 按真后端语义成功（见 routeApi 的 POST /api/sessions 分支）。 */
  cwdSessionError?: { status: number; detail: string };
  /** #204：launch=false（只建会话不启动 run）时伪造失败。不设 = 按真后端语义
   *  成功：返回 `{session_id, permission_mode}` JSON（非 SSE），只写 session/started，
   *  无 run 帧。 */
  emptySessionError?: { status: number; detail: string };
  /** #204 裁定 §3 考点：launch=false 响应里回传**与请求不同的** permission_mode
   *  （模拟后端归一化/接管）——pill 必须显示**响应**的档位而不是前端本地选中值。
   *  不设 = 回显请求的档位（缺省 workspace-write）。 */
  emptySessionPermissionOverride?: string;
  // ── WS-7 / #170 宿主目录列举 ──
  /** 假目录树（`GET /api/host/dirs`）。**不设 = 空的根列举**（不是错误）：浏览器是
   *  新建项目对话框的一部分，不关心它的 spec 不该因此多出一条红色错误盒。
   *  错误矩阵由 `hostDirsErrors`（403/404/422 + detail）或假树里没有的路径显式构造。
   *
   *  spec 只声明"每个目录下有哪些子目录名"，`path`/`parent` 由 fixture 按目录结构
   *  **拼**出来（与真后端 `host_dirs.py` 同一口径：条目的 path = 父路径 + 名字，
   *  不做 realpath 展开；排序按 name 大小写不敏感）。这样 mock 里不会出现第二套
   *  路径拼接逻辑被 spec 抄一遍而悄悄写歪（#155 轮栽过 mock 语义与真机相反）。 */
  hostDirs?: {
    /** 键 = 目录绝对路径（分隔符可 `\` 或 `/`，与请求参数逐字符相等才命中）；
     *  值 = 该目录下的子目录名（顺序不限，fixture 排序）。 */
    tree: Record<string, string[]>;
    /** 根模式（`path` 缺省）的盘符/根列表。 */
    roots?: string[];
    /** 单个目录最多列举多少条（缺省 500 = 后端 `MAX_ENTRIES`）；超出 → truncated。 */
    maxEntries?: number;
  };
  /** 这些路径的列举直接回错误（错误矩阵就地显示：403 无权限 / 404 / 422 不是目录）。 */
  hostDirsErrors?: Record<string, { status: number; detail: string }>;
}

/** 项目 fixture（形状 = 后端 `web/projects.py::Project`，时间戳由 fixtures 补）。 */
export interface ProjectFixture {
  id: string;
  path: string;
  title: string;
  /** 账本手工序。 */
  session_ids: string[];
  status?: 'ok' | 'missing-dir';
}

/** 记忆 fixture（形状 = 后端 `web/memory.py::MemorySummary`，时间戳由 fixtures 补）。 */
export interface MemoryFixture {
  id: string;
  content: string;
  scope?: 'user' | 'session';
  metadata?: Record<string, unknown>;
  created_at?: string;
}

/** 会话行 fixture：只带 WS-5 相关字段，其余由调用方按需补（旧 spec 的裸对象同样可用）。 */
export function sessionRow(
  sessionId: string,
  workspace: { id: string; title: string } | null = null,
  over: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    session_id: sessionId,
    event_count: 3,
    first_event_time: T,
    last_event_time: T,
    first_user_message: `任务 ${sessionId}`,
    trace_id: null,
    trace_url: null,
    workspace,
    // #171：后端 `SessionSummary.archived` 是**必填布尔**（没有 meta 行 = false），
    // 所以 mock 也给一个字面量默认值——前端类型把它声明成可选就会让"漏读"变成
    // `undefined`（静默假），这条默认值让 mock 与真后端的形状一致。
    archived: false,
    ...over,
  };
}

/** 按会话 id 定位侧栏里的一行（行内 id 文本是 `session_id.slice(0, 12)`，所以短 id
 *  才匹配得上）。放在这里而不是各 spec 各写一份：窄屏 / 删会话 / 触摸可达几个车道都
 *  用同一条定位（见本文件顶部"多个 spec 共用同一份，避免各自复制后静默漂移"）。 */
export const rowOf = (page: Page, sessionId: string) =>
  page
    .locator('.session-row')
    .filter({ has: page.locator('.session-item-id', { hasText: sessionId }) });

/** 装 API mock（HTTP 路由 + WS 接流通道）。
 *
 *  ⚠ **必须 `await`**：WS 通道的注册（`page.routeWebSocket`）是异步的，且要早于
 *  第一次导航——漏掉 await 时它不会报错，只会**不生效**：界面连不上 mock 的 WS，
 *  悄悄走 SSE 降级路径（帧被攒到流结束），于是「接流/重连」类断言考的东西整个换了
 *  一条路。表现是 spec 失败（onWs 剧本不触发），不会假绿。 */
export async function routeApi(page: Page, mock: ApiMock): Promise<void> {
  // ── WS-5 #155：可变状态（每测试一份，互不串味）──
  // 注意 sessions **持有调用方数组的引用**，不拷贝：既有 spec 的约定是"fork 成功后
  // 往自己的 sessions 数组里 push child，下一次 GET 就能看到"（b-fork.spec.ts 依赖
  // 这一点）。除 #172 的硬删分支外，本车道只在原地改行的 `workspace` 字段，既不新增
  // 也不删除行；硬删是**真的**删（`splice`）——那正是它的语义，调用方的数组也跟着变
  // （spec 据此断言"删完重拉就没有这一行了"）。
  const sessionState: unknown[] = mock.sessions ?? [];
  const projectState: ProjectFixture[] = (mock.projects ?? []).map((p) => ({
    ...p,
    session_ids: [...p.session_ids],
  }));
  /** 记忆状态：DELETE 真的摘条目（"删掉后再打开面板看不到"才是真语义）。 */
  const memoryState: MemoryFixture[] = (mock.memories ?? []).map((m) => ({ ...m }));
  /** 带 cwd 建会话时**真的发生过**的帧（供 GET /events 回读：见该分支注释）。 */
  const sessionEvents = new Map<string, FrameSpec[]>();
  /** `GET /api/sessions` 的次数（`sessionsListFailAfter` 用；见该分支注释）。 */
  let listCalls = 0;

  // WS 接流通道（#206）：必须与 page.route 同时就位——界面一旦进 live 态就接 WS，
  // 漏掉这条通道只会让它悄悄走 SSE 降级路径（见 installWsRoute 的注释）。
  await installWsRoute(page, mock, sessionEvents);

  /** 后端 `web/projects.py::Project` 的响应形状（时间戳不是本车道断言的对象）。 */
  const projectView = (p: ProjectFixture) => ({
    id: p.id,
    path: p.path,
    title: p.title,
    status: p.status ?? 'ok',
    session_ids: p.session_ids,
    created_at: T,
    updated_at: T,
  });
  const findProject = (id: string) => projectState.find((p) => p.id === id);
  /** 从项目账本里摘掉这个会话，返回**实际摘掉几个账本**（真后端
   *  `WorkspaceIndex.detach_session` 的返回值就是它，`detached_from_projects` 用它）。
   *  硬删的成功分支与 404 分支共用——404 那条（"别处已经删了"）在真后端里账本也早
   *  就解除了，留着会让 rail 报一条"n 条会话日志缺失"的假缺失。 */
  const detachFromLedgers = (sessionId: string): number => {
    let detached = 0;
    for (const p of projectState) {
      const at = p.session_ids.indexOf(sessionId);
      if (at >= 0) {
        p.session_ids.splice(at, 1);
        detached += 1;
      }
    }
    return detached;
  };
  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, body: JSON.stringify(body), contentType: 'application/json' });
  /** 把某会话的 workspace 引用同步成"它现在属于谁"——与真实后端一致
   *  （attach/detach/软删除后 GET /api/sessions 的行立刻变）。
   *  替换而非改原对象：调用方的 fixture 行是**字面量常量**，就地改会污染同一文件里
   *  其它用例的期望（各 spec 之间共享模块级常量）。 */
  const setWorkspace = (sessionId: string, project: ProjectFixture | null) => {
    const at = sessionState.findIndex(
      (s) => (s as Record<string, unknown>)['session_id'] === sessionId,
    );
    if (at >= 0) {
      sessionState[at] = {
        ...(sessionState[at] as Record<string, unknown>),
        workspace: project ? { id: project.id, title: project.title } : null,
      };
    }
  };

  void page.route('**/api/**', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (path === '/api/health') {
      return route.fulfill({ status: 200, body: '{"status":"ok"}', contentType: 'application/json' });
    }
    if (path === '/api/sessions' && req.method() === 'GET') {
      // `sessionsListFailAfter`：先数够 N 次成功，之后一律 500（构造"归档后重拉失败"）。
      listCalls += 1;
      if (mock.sessionsListFailAfter !== undefined && listCalls > mock.sessionsListFailAfter) {
        return json(route, { detail: '会话列表暂时不可用' }, 500);
      }
      // #171 AC3：`include_archived` 是**后端**的契约（不带 = 不返回已归档行）。
      // mock 照真后端过滤，于是"UI 总是显式要全量"这条设计能被真的考到：漏掉那个
      // 参数时，开关打开也看不到归档行（前端本地过滤救不了它）。
      const includeArchived = new URL(req.url()).searchParams.get('include_archived') === 'true';
      const rows = includeArchived
        ? sessionState
        : sessionState.filter((s) => (s as Record<string, unknown>)['archived'] !== true);
      return json(route, rows);
    }
    if (path === '/api/sessions' && req.method() === 'POST') {
      if (mock.onSessionPost) return mock.onSessionPost(route);
      const body = (req.postDataJSON() ?? {}) as Record<string, unknown>;
      const cwd = typeof body.cwd === 'string' ? body.cwd : '';
      if (!cwd) return route.abort('aborted');
      // ── #204：launch=false（只建会话不启动 run）——按真后端语义（裁定 §2）：
      //  返回 `{session_id, permission_mode}` JSON（非 SSE）、只写 session/started、
      //  无 run 帧；给了 task 又 launch=false 是矛盾组合 → 422（照真后端）。
      const launchFalse = new URL(req.url()).searchParams.get('launch') === 'false';
      if (launchFalse) {
        if (mock.emptySessionError) {
          return json(route, { detail: mock.emptySessionError.detail }, mock.emptySessionError.status);
        }
        if ('task' in body) {
          return json(
            route,
            { detail: 'task 与 launch=false 互斥：要么带 task 启动 run（launch=true），要么只建会话（省略 task）' },
            422,
          );
        }
        const sid = `empty-${sessionState.length + 1}`;
        // #204 裁定 §3 考点：`emptySessionPermissionOverride` 模拟后端归一化/接管
        // （响应档位 ≠ 请求档位）——pill 必须显示响应的档位，不是前端本地选中值。
        const permissionMode =
          mock.emptySessionPermissionOverride ??
          (typeof body.permission_mode === 'string' ? body.permission_mode : 'workspace-write');
        let project = projectState.find((p) => p.path === cwd);
        if (!project) {
          project = {
            id: `p-${projectState.length + 1}`,
            path: cwd,
            title: cwd.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || cwd,
            session_ids: [],
          };
          projectState.unshift(project);
        }
        sessionState.unshift(
          // 空会话：没有 user/message → first_user_message 为 null（真后端语义）。
          sessionRow(sid, { id: project.id, title: project.title }, {
            event_count: 1,
            first_user_message: null,
          }),
        );
        project.session_ids.unshift(sid); // 账本前插（与 attach 语义一致）
        // durable log 只有一条 session/started（无 run 帧——launch=false 不启动 run）。
        sessionEvents.set(sid, [
          { type: 'session/started', data: { cwd, permission_mode: permissionMode }, seq: 1, session_id: sid, time: T },
        ]);
        return json(route, { session_id: sid, permission_mode: permissionMode });
      }
      // ── WS-6 / #169：带 cwd 建会话 —— 按真后端语义（ADR-0027 D2/D3）真的改状态：
      //  会话诞生在该目录、自动入组（未注册的 cwd 自动注册为项目，title = 目录末段名），
      //  于是"新会话落在该项目分组下"这条断言考的是界面跟着后端语义走，
      //  而不是"界面读了我们塞的假值"（fixture 的既有纪律）。
      if (mock.cwdSessionError) {
        return json(route, { detail: mock.cwdSessionError.detail }, mock.cwdSessionError.status);
      }
      const sid = `cwd-${sessionState.length + 1}`;
      let project = projectState.find((p) => p.path === cwd);
      if (!project) {
        project = {
          id: `p-${projectState.length + 1}`,
          path: cwd,
          title: cwd.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || cwd,
          session_ids: [],
        };
        projectState.unshift(project);
      }
      sessionState.unshift(
        sessionRow(sid, { id: project.id, title: project.title }, {
          first_user_message: typeof body.task === 'string' ? body.task : '新任务',
        }),
      );
      project.session_ids.unshift(sid); // 账本前插（与 attach 语义一致）
      const frames: FrameSpec[] = [
        // 帧序照真后端（#151 的机制）：session/started → run/started → user/message
        // → model/started → text/delta → model/completed → run/completed。
        // 缺 user/message 或 model/* 会让"回答真的渲染出来了"这条断言测不到东西。
        { type: 'session/started', data: { cwd }, seq: 1, session_id: sid, run_id: RUN, time: T },
        { type: 'run/started', seq: 2, session_id: sid, run_id: RUN, time: T },
        {
          type: 'user/message',
          data: { content: typeof body.task === 'string' ? body.task : '新任务' },
          seq: 3,
          session_id: sid,
          run_id: RUN,
          step_id: 1,
          time: T,
        },
        { type: 'model/started', data: { model: 'e2e-model' }, seq: 4, session_id: sid, run_id: RUN, step_id: 1, time: T },
        { type: 'text/delta', data: { delta: '好，我先看看这个目录。' }, seq: 5, session_id: sid, run_id: RUN, step_id: 1, time: T },
        { type: 'model/completed', data: { model: 'e2e-model' }, seq: 6, session_id: sid, run_id: RUN, step_id: 1, time: T },
        { type: 'run/completed', data: {}, seq: 7, session_id: sid, run_id: RUN, time: T },
      ];
      sessionEvents.set(sid, frames); // durable log = 刚才流的那些帧（见 /events 分支）
      return fulfillSse(route, frames);
    }
    // ── #185 / #186：artifact 内容读取（只读端点）──
    const artifactMatch = /^\/api\/sessions\/([^/]+)\/artifacts\/([^/]+)$/.exec(path);
    if (artifactMatch && req.method() === 'GET') {
      const aid = decodeURIComponent(artifactMatch[2]);
      if (mock.onArtifactGet) {
        const handled = await mock.onArtifactGet(route, aid);
        if (handled) return undefined;
      }
      const forced = mock.artifactContentError;
      if (forced) return json(route, { detail: forced.detail }, forced.status);
      return json(route, mock.artifactContent ?? { artifact_id: aid, lines: [], total_lines: 0, returned_lines: 0, truncated: false });
    }
    // ── #171 会话归档（有状态 mock：语义对齐 `web/app.py::archive_session`）──
    const sessionArchiveMatch = /^\/api\/sessions\/([^/]+)\/archive$/.exec(path);
    if (sessionArchiveMatch && (req.method() === 'POST' || req.method() === 'DELETE')) {
      const sid = decodeURIComponent(sessionArchiveMatch[1]);
      const target = req.method() === 'POST'; // POST = 归档 true；DELETE = 取消归档 false
      if (mock.onArchiveRequest && (await mock.onArchiveRequest(route, sid, target))) return undefined;
      const forced = mock.sessionArchiveErrors?.[sid];
      if (forced) return json(route, { detail: forced.detail }, forced.status);
      const at = sessionState.findIndex(
        (s) => (s as Record<string, unknown>)['session_id'] === sid,
      );
      // 404 的 detail 照抄后端 `SessionService._require_existing_session` 抛出的那句
      // （`session 'x' not found`）：自己编一句中文会让门槛内的绿灯只证明
      // "前端与我的假后端一致"。
      if (at < 0) return json(route, { detail: `session '${sid}' not found` }, 404);
      // 409 **只在归档方向**：后端 `set_archived` 的守卫顺序是「存在 → 在途 run」，
      // 而 `archived and run_manager.get_active(...)` 这个连词意味着取消归档永不 409。
      if (target && (mock.sessionArchiveBusyIds ?? []).includes(sid)) {
        // detail 逐字照抄后端 `service.py::set_archived` 抛的 `ActiveRunConflict` 原句。
        return json(
          route,
          { detail: `session '${sid}' has a run in flight; archive it after it finishes` },
          409,
        );
      }
      // 幂等：重复归档仍是 true（后端用 upsert/set_archived，不是"翻转"）。
      sessionState[at] = { ...(sessionState[at] as Record<string, unknown>), archived: target };
      return json(route, { id: sid, archived: target });
    }
    // ── #172 / ADR-0029 会话硬删（有状态 mock：语义对齐 `web/app.py::delete_session`）──
    const sessionDeleteMatch = /^\/api\/sessions\/([^/]+)$/.exec(path);
    if (sessionDeleteMatch && req.method() === 'DELETE') {
      const sid = decodeURIComponent(sessionDeleteMatch[1]);
      const forced = mock.sessionDeleteErrors?.[sid];
      if (forced) {
        // 404 的条目**同时**摘掉这一行与它在项目账本里的条目：真后端在那一刻它确实
        // 不在（第二次删除就是这个），而用户走完一次真删时账本也已经解除过了——
        // 留在状态里会让重拉之后那行又冒出来、还带一条"n 条会话日志缺失"的假缺失。
        if (forced.status === 404) {
          const at404 = sessionState.findIndex(
            (s) => (s as Record<string, unknown>)['session_id'] === sid,
          );
          if (at404 >= 0) sessionState.splice(at404, 1);
          detachFromLedgers(sid);
        }
        return json(route, { detail: forced.detail }, forced.status);
      }
      const at = sessionState.findIndex(
        (s) => (s as Record<string, unknown>)['session_id'] === sid,
      );
      if (at < 0) return json(route, { detail: `session '${sid}' not found` }, 404);
      const row = sessionState[at] as Record<string, unknown>;
      // 事件数在**删之前**取：删完只剩文件系统，无从统计（真后端同一顺序）。
      const events = typeof row['event_count'] === 'number' ? row['event_count'] : 0;
      // 真后端的第二条判据：日志为空（event_count == 0）也是 404——"删得掉"与"看得见"
      // 永远一致，不给出一个列表里没有内容的 id 的删除假回执。**什么都不删**。
      if (events === 0) return json(route, { detail: `session '${sid}' not found` }, 404);
      sessionState.splice(at, 1);
      // 项目账本真的摘掉它，并按实际摘掉的账本数报 `detached_from_projects`——
      // "删完项目计数也掉了"这条断言因此考的是界面跟着后端语义走，而不是本地把行藏起来。
      const detached = detachFromLedgers(sid);
      return json(route, { id: sid, deleted: true, events, detached_from_projects: detached });
    }
    if (/^\/api\/sessions\/[^/]+\/events$/.test(path)) {
      // 带 cwd 建的会话：它的 durable log **就是**刚才流出来的那些帧（真后端同理——
      // run 收尾后前端会回读日志对账）。不给这份日志，流的结论会被下一次回读清空，
      // "回答真的渲染出来了"就永远测不到（mock 语义与真机不一致的另一种形态）。
      const sid = decodeURIComponent(path.split('/')[3] ?? '');
      const streamed = sessionEvents.get(sid);
      return route.fulfill({
        status: 200,
        body: JSON.stringify(streamed ?? mock.events ?? []),
        contentType: 'application/json',
      });
    }
    if (/^\/api\/sessions\/[^/]+\/stream$/.test(path)) {
      // 实时流主通道已是 WS（#205）；这条路只剩降级兜底与 `stream/truncated`
      //（该控制帧只在 SSE 通道发）。没人 mock 时 abort——降级路上的失败是本车道
      // 的合法一等场景（真机上 WS 也可能被代理拒），但不能是"忘了 mock"的默认。
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
    if (path === '/api/capabilities') {
      if (mock.onCapabilitiesGet && (await mock.onCapabilitiesGet(route))) return;
      if (mock.capabilitiesError) {
        return json(route, { detail: mock.capabilitiesError.detail }, mock.capabilitiesError.status);
      }
      return route.fulfill({
        status: 200,
        body: JSON.stringify({ capabilities: mock.capabilities ?? [CORE_CAPABILITY] }),
        contentType: 'application/json',
      });
    }
    if (/^\/api\/sessions\/[^/]+\/model$/.test(path) && req.method() === 'POST') {
      if (mock.onModelPost) return mock.onModelPost(route);
      const body = (req.postDataJSON() ?? {}) as { provider?: string; model_id?: string };
      return route.fulfill({
        status: 200,
        body: JSON.stringify({
          status: 'changed',
          provider: body.provider ?? '',
          model_id: body.model_id ?? '',
        }),
        contentType: 'application/json',
      });
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
    if (/^\/api\/sessions\/[^/]+\/queue$/.test(path) && req.method() === 'GET') {
      if (mock.onQueueGet) return mock.onQueueGet(route);
      return route.fulfill({
        status: 200,
        body: JSON.stringify({ items: [], steers: [] }),
        contentType: 'application/json',
      });
    }
    if (/^\/api\/sessions\/[^/]+\/queue\/flush$/.test(path) && req.method() === 'POST') {
      if (mock.onFlushPost) return mock.onFlushPost(route);
      // 真后端：无在途 run 且队列空 → idle JSON；本车道默认照 idle 答。
      return json(route, { status: 'idle' });
    }
    if (/^\/api\/sessions\/[^/]+\/queue\/[^/]+\/cancel$/.test(path) && req.method() === 'POST') {
      if (mock.onQueueCancelPost) return mock.onQueueCancelPost(route);
      return route.fulfill({
        status: 200,
        body: JSON.stringify({ status: 'cancelled' }),
        contentType: 'application/json',
      });
    }
    if (/^\/api\/sessions\/[^/]+\/context-usage$/.test(path) && req.method() === 'GET') {
      if (mock.onContextUsageGet) return mock.onContextUsageGet(route);
      return route.fulfill({
        status: 200,
        body: JSON.stringify({
          estimated: true,
          window_tokens: 200000,
          used_tokens: 0,
          thresholds: { auto_compact: 0.7, hard_guard: 0.85 },
          breakdown: { messages: 0, system_prompt: 0, skills: 0, other: 0, tools: { system: 0, mcp: 0 } },
          cache: { state: 'not_collected', reported_calls: 0, total_calls: 0, avg_hit_rate: null },
          state: 'no_data',
        }),
        contentType: 'application/json',
      });
    }
    if (/^\/api\/sessions\/[^/]+\/recover$/.test(path) && req.method() === 'POST') {
      if (mock.onRecoverPost) return mock.onRecoverPost(route);
      return route.fulfill({ status: 200, body: JSON.stringify(mock.events ?? []), contentType: 'application/json' });
    }
    if (/^\/api\/sessions\/[^/]+\/approve$/.test(path) && req.method() === 'POST') {
      if (mock.onApprovePost) return mock.onApprovePost(route);
      const body = (req.postDataJSON() ?? {}) as { approval_id?: string; decision?: string };
      return route.fulfill({
        status: 200,
        body: JSON.stringify({
          status: 'resolved',
          approval_id: body.approval_id ?? '',
          decision: body.decision ?? '',
        }),
        contentType: 'application/json',
      });
    }

    // ── MEM-5 / #160 记忆端点（有状态 mock：语义对齐后端 `web/memory.py`）──
    if (path === '/api/memories' && req.method() === 'GET') {
      if (mock.onMemoriesGet && (await mock.onMemoriesGet(route))) return;
      if (mock.memoryDisabled) return json(route, { detail: mock.memoryDisabled }, 503);
      const params = new URL(req.url()).searchParams;
      const limit = Number(params.get('limit') ?? '50');
      const offset = Number(params.get('offset') ?? '0');
      // 按后端语义切片（`web/memory.py` 的 limit/offset 分页）——"加载更多"因此
      // 真的会拿到下一页，而不是界面自己把一份全量数组切两半。
      const page = memoryState.slice(offset, offset + limit).map((m) => ({
        id: m.id,
        content: m.content,
        scope: m.scope ?? 'user',
        metadata: m.metadata ?? {},
        created_at: m.created_at ?? T,
      }));
      return json(route, page);
    }
    const memoryMatch = /^\/api\/memories\/([^/]+)$/.exec(path);
    if (memoryMatch && req.method() === 'DELETE') {
      if (mock.memoryDisabled) return json(route, { detail: mock.memoryDisabled }, 503);
      const memoryId = decodeURIComponent(memoryMatch[1]);
      // 403 = 领域层的归属校验（`MemoryRecordStore.delete` 的 namespace 匹配）：
      // 与 404「这条不在了」分开——AC3 的失败回滚就靠这条。
      //
      // detail **逐字照抄**真后端：`web/domain_errors.py::memory_http_error` 用
      // `str(exc)`，而 `sqlite_record_store.py:195` 抛的是
      // `PermissionError("Memory belongs to a different namespace")`（英文原句）。
      // 自己编一句中文会让门槛内的绿灯只证明"前端与我的假后端一致"（#155 轮已
      // 栽过同一个坑：mock 语义与真机相反）。
      if ((mock.memoryDeniedIds ?? []).includes(memoryId)) {
        return json(route, { detail: 'Memory belongs to a different namespace' }, 403);
      }
      const at = memoryState.findIndex((m) => m.id === memoryId);
      // 404 的 detail 同样照抄：`memory/errors.py::MemoryNotFound` → `记忆不存在：<id>`。
      if ((mock.memoryVanishedIds ?? []).includes(memoryId)) {
        if (at >= 0) memoryState.splice(at, 1); // "别处已删"：重拉时这一行没了
        return json(route, { detail: `记忆不存在：${memoryId}` }, 404);
      }
      if (at < 0) return json(route, { detail: `记忆不存在：${memoryId}` }, 404);
      memoryState.splice(at, 1); // 硬删：记录真的没了（不是软删/回收站）
      return json(route, { id: memoryId, deleted: true });
    }

    // ── WS-5 / #155 项目端点（有状态 mock：语义对齐后端 `web/projects.py`）──
    if (path === '/api/projects' && req.method() === 'GET') {
      return json(route, projectState.map(projectView));
    }
    if (path === '/api/projects' && req.method() === 'POST') {
      if (mock.onProjectPost && (await mock.onProjectPost(route))) return;
      const body = (req.postDataJSON() ?? {}) as { path?: string; title?: string };
      const target = body.path ?? '';
      if ((mock.projectMissingPaths ?? []).includes(target)) {
        // 逐字照抄真实后端在 Windows 上的 detail（`FileNotFoundError` 的
        // `str(exc)`）：**故意不做美化**——AC3 要证明的是"后端说了什么，界面就
        // 显示什么"，所以断言打在 `WinError 3` 与路径上；如果前端把它翻译成
        // 自己的话或吞掉原串，这条用例必须变红。
        return json(route, { detail: `[WinError 3] 系统找不到指定的路径。: '${target}'` }, 404);
      }
      const existing = projectState.find((p) => p.path === target);
      if (existing) return json(route, projectView(existing)); // 幂等：同规范路径
      const created: ProjectFixture = {
        id: `p-${projectState.length + 1}`,
        path: target,
        title: body.title || target.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '项目',
        session_ids: [],
      };
      projectState.unshift(created); // 注册表顺序：新建项目前插
      return json(route, projectView(created));
    }
    const projectMatch = /^\/api\/projects\/([^/]+)$/.exec(path);
    if (projectMatch) {
      const project = findProject(decodeURIComponent(projectMatch[1]));
      if (!project) return json(route, { detail: '项目不存在' }, 404);
      if (req.method() === 'PATCH') {
        const body = (req.postDataJSON() ?? {}) as { title?: string };
        project.title = body.title ?? project.title;
        for (const id of project.session_ids) setWorkspace(id, project);
        return json(route, projectView(project));
      }
      if (req.method() === 'DELETE') {
        // 软删除：只摘注册记录与账本；成员会话的 workspace 清成 null（会话本体不动）。
        const detached = project.session_ids.length;
        for (const id of project.session_ids) setWorkspace(id, null);
        projectState.splice(projectState.indexOf(project), 1);
        return json(route, {
          id: project.id,
          deleted: true,
          sessions_detached: detached,
          detail: `项目「${project.title}」已从注册表移除，${detached} 个会话回到未分组。目录、用户文件与会话日志均未删除（软删除，可重新注册同一目录）。`,
        });
      }
      return json(route, { detail: 'method not allowed' }, 405);
    }
    const attachMatch = /^\/api\/projects\/([^/]+)\/sessions$/.exec(path);
    if (attachMatch && req.method() === 'POST') {
      if (mock.onAttachPost && (await mock.onAttachPost(route))) return;
      const project = findProject(decodeURIComponent(attachMatch[1]));
      if (!project) return json(route, { detail: '项目不存在' }, 404);
      const body = (req.postDataJSON() ?? {}) as { session_id?: string };
      const sessionId = body.session_id ?? '';
      if (!sessionState.some((s) => s['session_id'] === sessionId)) {
        return json(route, { detail: `会话不存在：${sessionId}` }, 404);
      }
      if (!project.session_ids.includes(sessionId)) {
        // **前插**：真实后端 `workspace/index.py` 的 attach_session 写的是
        // `[session_id, *kept]`（`tests/web/test_projects_api.py` 锁住了这个顺序）。
        // 这里推队尾会让 e2e 在真机后端下必然失败——mock 的语义必须跟账本一致。
        project.session_ids.unshift(sessionId); // attach 幂等：已在账本里就不动
        setWorkspace(sessionId, project);
      }
      return json(route, projectView(project));
    }
    const detachMatch = /^\/api\/projects\/([^/]+)\/sessions\/([^/]+)$/.exec(path);
    if (detachMatch && req.method() === 'DELETE') {
      const project = findProject(decodeURIComponent(detachMatch[1]));
      if (!project) return json(route, { detail: '项目不存在' }, 404);
      const sessionId = decodeURIComponent(detachMatch[2]);
      const at = project.session_ids.indexOf(sessionId);
      if (at >= 0) project.session_ids.splice(at, 1); // 不在本项目 → 幂等 no-op
      setWorkspace(sessionId, null);
      return json(route, projectView(project));
    }
    const orderMatch = /^\/api\/projects\/([^/]+)\/sessions\/([^/]+)\/order$/.exec(path);
    if (orderMatch && req.method() === 'POST') {
      const project = findProject(decodeURIComponent(orderMatch[1]));
      if (!project) return json(route, { detail: '项目不存在' }, 404);
      const sessionId = decodeURIComponent(orderMatch[2]);
      const body = (req.postDataJSON() ?? {}) as { before?: string | null };
      const before = body.before ?? null;
      const from = project.session_ids.indexOf(sessionId);
      if (from < 0) return json(route, { detail: '会话不在该项目账本里' }, 409);
      // 自锚点是**无操作**（`ProjectService.reorder` 显式挡下：DOM 意义上"把
      // 自己插到自己前面"什么都没变）。不先判它，下面的"先删后按 indexOf 插"
      // 会因为锚点已被删掉而 indexOf = -1 → 插到倒数第二位，凭空改掉账本。
      if (before === sessionId) return json(route, projectView(project));
      if (before !== null && !project.session_ids.includes(before)) {
        return json(route, { detail: '锚点不在该项目账本里' }, 409);
      }
      project.session_ids.splice(from, 1); // insertBefore 语义
      if (before === null) project.session_ids.push(sessionId);
      else project.session_ids.splice(project.session_ids.indexOf(before), 0, sessionId);
      return json(route, projectView(project));
    }

    // ── WS-7 / #170：宿主目录列举（`path` 缺省 = 根模式）──
    if (path === '/api/host/dirs') {
      const tree = mock.hostDirs;
      // 缺省 = 空的**根**列举（不是 404）：浏览器是新项目对话框的一部分，
      // 不关心它的 spec（r-project-groups）不该因此多出一条红色错误盒。
      // 错误矩阵由 `hostDirsErrors` / 假树里没有的路径显式构造。
      if (!tree) {
        return json(route, { path: null, parent: null, truncated: false, entries: [] });
      }
      const target = new URL(req.url()).searchParams.get('path');
      if (target !== null && mock.hostDirsErrors?.[target]) {
        const err = mock.hostDirsErrors[target];
        return json(route, { detail: err.detail }, err.status);
      }
      const cap = tree.maxEntries ?? 500;
      if (target === null) {
        // 根模式：path/parent 都是 null（前端据此禁用「向上」「选择此目录」）。
        const roots = [...(tree.roots ?? [])].sort(compareNames);
        return json(route, {
          path: null,
          parent: null,
          truncated: false,
          entries: roots.map((r) => ({ name: r, path: r })),
        });
      }
      const names = tree.tree[target];
      if (!names) return json(route, { detail: `目录不存在：${target}` }, 404);
      const shown = [...names].sort(compareNames).slice(0, cap);
      return json(route, {
        path: target,
        parent: parentPath(target),
        truncated: names.length > cap,
        entries: shown.map((name) => ({ name, path: joinPath(target, name) })),
      });
    }

    return route.fulfill({ status: 404, body: '{"detail":"not mocked in e2e"}', contentType: 'application/json' });
  });
}

/** 排序口径 = 后端 `host_dirs.py::_sorted`：name 大小写不敏感，同键按原名兜底。 */
function compareNames(a: string, b: string): number {
  const la = a.toLowerCase();
  const lb = b.toLowerCase();
  if (la !== lb) return la < lb ? -1 : 1;
  return a < b ? -1 : a > b ? 1 : 0;
}

/** 子条目 path = 父路径 + 名字（**不**做 realpath 展开，ADR-0028 D3）。 */
function joinPath(base: string, name: string): string {
  const sep = base.includes('\\') ? '\\' : '/';
  return `${base.replace(/[\\/]+$/, '')}${sep}${name}`;
}

/** 上一级；盘根与无分隔符的路径没有上一级（后端 `parent in ("", canonical)` 同规则）。 */
function parentPath(target: string): string | null {
  const trimmed = target.replace(/[\\/]+$/, '');
  const at = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'));
  if (at < 0) return null;
  const sep = trimmed.includes('\\') ? '\\' : '/';
  let parent = trimmed.slice(0, at);
  if (parent.endsWith(':')) parent += sep; // 'D:' → 'D:\'（盘根照列）
  else if (parent === '') parent = sep; // '/home' → '/'
  return parent === trimmed ? null : parent;
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

/** #199：可用性 + 能力位的**载荷形状**夹具（键名与真后端一致：`is_available` /
 *  `unavailable_reason` / `supports_*`，`getModels` 逐字段窄化）。
 *
 * 四组覆盖四个必须分辨的情形（每组都要能在真实浏览器里被断言到）：
 *   - `zhipu`   —— 可用 + 能力位（工具/视觉/思考）；组内两个模型徽标**不同**，
 *                  证明徽标是 per-model 而不是 per-provider；
 *   - `custom`  —— **整组不可用**（`missing_api_key`，真实后端在无凭据时就是这个码）
 *                  ⇒ 该置灰 + 行尾原因 + 仍可展开；
 *   - `weird`   —— 整组不可用但原因是**未知码** ⇒ 行尾必须回落「不可用」，
 *                  不许把码原样打给用户、也不许冒称「未配置」（那是具体诊断，
 *                  `modelAvailability.reasonLabel` 的回落）；
 *   - `mixed`   —— 组内**部分**不可用 ⇒ **不许**置灰（把一个可用项说成不可用
 *                  比不置灰更糟）。
 *  ⚠ 这组载荷是**为覆盖纯函数分支手工构造的**，不是任何真实部署的快照：真后端只在
 *  provider preset / `AGENT_MODELS` 条目声明过能力位时才发 `supports_*`（且 preset 里
 *  只有 `supports_tools`），而"整组不可用"只发生在**自定义供应商**上——那个条目又不
 *  带能力位。也就是说"zhipu 三徽标齐全 + custom 置灰"这**同一画面**真机做不出来
 *  （详见设计稿 §3 的"可达性前置"）。夹具的职责是把两边的分支都喂到，不是证伪后端。 */
export const MODELS_WITH_AVAILABILITY = [
  {
    name: 'glm-4.6', provider: 'zhipu', model: 'glm-4.6', default: true,
    is_available: true, unavailable_reason: null,
    supports_tools: true, supports_vision: true, supports_reasoning_summary: true,
  },
  {
    name: 'glm-4.6-air', provider: 'zhipu', model: 'glm-4.6-air', default: false,
    is_available: true, unavailable_reason: null,
    supports_vision: true,
  },
  {
    name: 'custom:gpt-x', provider: 'custom', model: 'gpt-x', default: false,
    is_available: false, unavailable_reason: 'missing_api_key',
  },
  {
    name: 'weird:model-a', provider: 'weird', model: 'model-a', default: false,
    is_available: false, unavailable_reason: 'some_future_code',
  },
  {
    name: 'mixed:ok', provider: 'mixed', model: 'ok', default: false,
    is_available: true, unavailable_reason: null,
  },
  {
    name: 'mixed:no-key', provider: 'mixed', model: 'no-key', default: false,
    is_available: false, unavailable_reason: 'missing_api_key',
  },
];

/** `GET /api/permission-modes` 的载荷。**逐字对齐后端**：id = `PermissionPolicy`
 *  的三个值（`tooling/contract.py:102-104`），文案 = `PERMISSION_MODE_DESCRIPTIONS`。
 *
 *  ⚠ 原先这里是 `auto`/`ask`/`deny` + 英文名——**后端产不出这组值**：`POST /api/sessions`
 *  的 `permission_mode` 校验只认这三个枚举值，`auto` 会直接 422
 *  （`web/app.py:247-253`）。于是"提交 payload 字段名对齐后端契约"那条用例实际在断言一个
 *  后端必然拒绝的取值——文案对了、契约是假的。2026-09-17 改为真实载荷。 */
export const PERMISSION_MODES = [
  { id: 'read-only', display_name: '只读', description: '可读文件和运行只读工具，不可写入。', icon: 'lock' },
  { id: 'workspace-write', display_name: '工作区写入', description: '可读写工作区内文件；高危工具仍需审批。', icon: 'pencil' },
  { id: 'danger-full-access', display_name: '完全访问', description: '所有工具无需审批，含网络/系统副作用。仅在可信环境使用。', icon: 'unlock' },
];

/** `GET /api/agent-profiles` 的默认载荷（形状 = 后端 `AGENT_PROFILE_DESCRIPTIONS`
 *  + `agent/profiles.py::tool_scope_summary` 的 `tool_scope`）。
 *
 *  `tool_scope` 三个数**逐值镜像后端实测值**（main 17/17、coding 12/17、
 *  research_review 7/17）——后端那两个函数会随 scope 改动漂移，两侧各有一把锁
 *  （后端 `test_web_phase5_staged_endpoints.py::test_tool_scope_counts_match_declared_scopes`，
 *  前端 `lib/agentProfileScope.test.ts`）；改 scope 请同时看这两处。
 *  只写要断言的事实：`display_name`/`description` 的文案不参与断言。 */
export const AGENT_PROFILES = [
  {
    id: 'main',
    display_name: 'Main',
    description: '通用编排代理（默认）',
    icon: 'layers',
    tool_scope: { open: 17, total: 17, excluded: [] },
  },
  {
    id: 'coding',
    display_name: 'Coding',
    description: '代码编辑、调试和构建任务专用',
    icon: 'code',
    tool_scope: {
      open: 12,
      total: 17,
      excluded: [
        'delegate', 'inspect_artifact', 'read_knowledge_source',
        'retrieve_knowledge', 'web_search',
      ],
    },
  },
  {
    id: 'research_review',
    display_name: 'Research & Review',
    description: '研究、检索和审查任务专用',
    icon: 'search',
    tool_scope: {
      open: 7,
      total: 17,
      excluded: [
        'apply_patch', 'bash', 'delegate', 'edit', 'forget_memory',
        'git_diff', 'git_status', 'inspect_artifact', 'remember_this', 'write',
      ],
    },
  },
];

export const REASONING_EFFORTS = [
  { id: 'minimal', display_name: 'Minimal', description: '最少推理开销；最快但最不彻底。', icon: 'bolt' },
  { id: 'standard', display_name: 'Standard', description: '典型任务的平衡推理深度（默认）。', icon: 'gauge' },
  { id: 'deep', display_name: 'Deep', description: '最多推理开销；较慢但最彻底。', icon: 'telescope' },
];

/* #201：多选 Context provider 控件已删除（前端不再取 `GET /api/context-providers`），
   配套的 `CONTEXT_PROVIDERS` 目录 fixture 随之删除。`routeApi` 里的该端点 mock 保留：
   它描述的是一条**仍然存在**的后端契约（`ApiMock.contextProviders`），#200/#203 会再用。 */

/** 能力条目 fixture（形状 = 后端 `capability/manifest.py::manifest_entry`）。
 *
 *  `surfaces` **只写要断言的键**：省略的键前端按"未声明 → 保守取假"处理
 *  （与后端"未声明 surfaces 的 capability 只保证 chat/timeline"同方向）。
 *  传 `actions` 无意义（本批不消费），省略。 */
export function capabilityFixture(
  surfaces: Record<string, boolean>,
  id = 'coding',
): Record<string, unknown> {
  return {
    id,
    display_name: id,
    version: '1',
    provider_name: 'builtin',
    surfaces,
    actions: {},
  };
}

/** **core 条目**（后端恒发的那一条，#193）——`routeApi` 缺省 capabilities 用的就是它。
 *
 *  值必须与 `src/agent_harness/capability/manifest.py` 的 `CORE_SURFACES` /
 *  `CORE_ACTIONS` 一致：`changes`/`terminal` = true（内置工具产出的两个面）、
 *  `artifacts` = false（要部署配了 store 才读得到，不是无条件能力）、
 *  `retry` = false（后端没有该入口）。
 *
 *  漂移风险如实说明——**这是一份手工镜像，不是同源**：
 *  1. 后端改值 → 前端测试**不会**变红（这条链断在跨仓，本仓没有后端代码可读）；
 *  2. 只有"改这个 mock 且改错"才会红（`workspace-modes.spec.ts` 的"真实默认 → 三个面
 *     出现"＋`capabilities.test.ts` 的键集断言）。
 *  所以后端那一半必须由后端自己的测试锁（`tests/web/test_web_phase2_endpoints.py::
 *  TestCapabilities` 真走 HTTP 端点）。两侧一起改是唯一的同步手段；集成 AI 合并后请跑
 *  一次真实端点（`docs/ACCEPTANCE_LANE_ENV.md` §2）确认两侧同值。 */
export const CORE_CAPABILITY: Record<string, unknown> = {
  id: 'core',
  display_name: '内置工具',
  version: '1.0.0',
  provider_name: 'builtin',
  surfaces: {
    chat: true,
    timeline: true,
    changes: true,
    terminal: true,
    artifacts: false,
  },
  actions: { permissions: true, stop: true, retry: false, resume: true },
};

// ── 长目录 fixture（F-DEFER-1：搜索框显示阈值 > 5 条）──
// 阈值速查（源码）：#199 之后**只有** `OptionPicker` 还有搜索框，阈值 `options.length > 5`
// （三处档位下拉共用）。模型选择器已删搜索框，所以下面的长模型目录不再与"搜索框显示"挂钩——
// 它现在服务的是别的事实：provider 分组条数（4 组）与「多到旧实现必然要搜索」这个对照。
// 曾各自内联长目录 → 阈值/条数一改就静默漂移，故统一在此构造。

/** 造一个 id/display_name 结构的长目录（形状 = 三个档位端点共用的 `CatalogEntry`）。
 *
 * `highlight` 指定某一项的 display_name（供"键入过滤"类用例断言），默认 `前缀 N`。 */
export function longCatalog(prefix: string, n: number, highlight?: { index: number; label: string }) {
  return Array.from({ length: n }, (_, i) => ({
    id: `${prefix}-${i}`,
    display_name: highlight?.index === i ? highlight.label : `${prefix} ${i}`,
    description: `${prefix} 档位 ${i}`,
  }));
}

/** 长模型目录：MODELS（3）+ 2 条 = 5 条、4 个 provider——供「分组就是导航」用例
 *  （旧实现在这个规模会显示搜索框，正好当对照）。 */
export const SEARCHABLE_MODELS = [
  ...MODELS,
  { name: 'gpt-5-mini', provider: 'openai', model: 'gpt-5-mini', default: false },
  { name: 'gemini-3-pro', provider: 'google', model: 'gemini-3-pro', default: false },
];

/** 长模型目录 PLUS：MODELS（3）+ 4 条 = 7 条、6 个 provider——比 SEARCHABLE_MODELS 更长的
 *  目录，供「没有搜索框」用例（目录一长就更该有搜索框 ⇒ 更能证明它被删了）。 */
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
  // 退出动画期间 listbox 仍在 DOM 且可命中（与上方 `:visible` 注释同一成因）。
  // 不等它真正卸载，下一次 open 的 Enter 会撞在正在关闭的浮层上——表现为
  // 「浮层像是开了，选中却没生效」，且只在连续两次调用时复现（探针实测）。
  await expect(page.locator('[role="listbox"]')).toHaveCount(0);
}

/** 选目录里的**第一个模型**（`MODELS[0]`，provider = `MODELS[0].provider`），
 *  断言 trigger 文本变成该模型名（#199 两级飞出）。
 *
 * 与旧版的差别是**语义变了，不是断言变松**：一级只列 provider，模型在二级子菜单里，
 * 所以"下压 N 次"不再能表达目标——必须按 provider 展开再选。鼠标路径（hover 展开）
 * 与键盘路径（`→` 进二级）都由 Radix Sub 提供，这里走鼠标路径（更短、也给悬停迟滞
 * 一条真实覆盖）。
 *
 * 关闭态的信号是 `[role="menu"]`（菜单语义），不再是 `[role="listbox"]`：两级飞出在
 * 平台上就是「菜单 + 子菜单」，见 `ModelPicker.tsx` 顶部对语义变更的说明。 */
export async function pickFirstModel(page: Page): Promise<void> {
  await pickModel(page, MODELS[0].provider, MODELS[0].name);
}

/** 「这个面板里没有搜索入口」的**可失败**口径：任何输入控件 / 搜索语义都算。
 *
 * 为什么不能只数某个类名：`#199` 要证明的是「模型选择器没有搜索框」，而旧模型的搜索框类名是
 * `.model-picker-search-wrap`、新共享组件（`OptionPicker`）才是 `.picker-search-wrap`——
 * 在模型菜单里数后者永远得 0，那是**恒真命题**（两轴 review 的 Standards 轴把这个假绿挑出来了）。
 * 按标签/role 数才对实现细节免疫：将来谁把搜索框塞回来，无论挂什么类名都会红。
 *
 * `[cmdk-input]` 一并算上：cmdk 的输入框正是 `role="combobox"`，但显式写上让口径不依赖库实现。 */
export function noSearchInputIn(page: Page, scope: string) {
  return page.locator(
    `${scope} input, ${scope} textarea, ${scope} [role="combobox"], ${scope} [role="searchbox"], ${scope} [role="search"], ${scope} [cmdk-input]`,
  );
}

/** 焦点描述，口径统一为 `role|aria-haspopup|文本前 12 字`——供菜单键盘断言/轮询共用。 */
async function activeMenuItem(page: Page): Promise<string> {
  return page.evaluate(() => {
    const a = document.activeElement as HTMLElement | null;
    return `${a?.getAttribute('role') ?? '-'}|${a?.getAttribute('aria-haspopup') ?? '-'}|${(a?.textContent ?? '').slice(0, 12)}`;
  });
}

/** 按一个菜单键，并**等焦点真的移到位**才交还控制权。
 *
 * 为什么必须轮询而不是按键后直接读：Radix 的 roving focus 是在 `setTimeout` 里移焦的
 * （`react-roving-focus` 的 Item.onKeyDown 末尾：`setTimeout(() => focusFirst(candidateNodes))`），
 * 所以「按键 → 立刻读 `document.activeElement`」拿到的是**旧值**。探针实测：同一个键，
 * 立刻读会显示焦点没动、隔一帧再读就是新位置；把 keydown 直接派发到聚焦元素上（绕过
 * CDP 输入管线）并等 50ms 也总能移动。也就是说**按键没有丢，是读得太早**——早先那个
 * 「第一次 ↓ 丢、第二次才好」的现象就是这个竞态的表象，而不是产品缺陷。
 * 断言 `expected` 用 `toContain` 语义（形状见 `activeMenuItem`）。 */
export async function pressMenuItemKey(page: Page, key: string, expected: string): Promise<void> {
  await page.keyboard.press(key);
  await expect.poll(() => activeMenuItem(page), { message: `${key} 之后焦点应移到 ${expected}` }).toContain(expected);
}

/** 打开模型菜单（键盘路径）并等初焦落到菜单项上（同 `pressMenuItemKey` 的延迟移焦成因）。 */
export async function openModelMenu(page: Page): Promise<void> {
  const trigger = page.locator('.composer-model[aria-label="模型选择"]').first();
  await trigger.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('[role="menu"]').first()).toBeVisible();
  await expect.poll(() => activeMenuItem(page)).toContain('menuitem|');
}

/** 打开模型菜单 → 展开 `provider` 的二级 → 点中 `modelName`，断言 trigger 文本。
 *
 * 首尾各一次「菜单已彻底卸载」等待，成因与 `pickControl` 尾部那条完全相同（探针实测）：
 * Radix 的退出动画期间 menu 节点仍在 DOM，且 modal 菜单层把 `body` 设成
 * `pointer-events:none`——此时开下一个浮层会被它吞掉（键盘尤其明显：Enter 被正在
 * 卸载的菜单吃掉，目标浮层根本不出现；鼠标路径因为 Playwright 的可操作性重试天然
 * 吸收了这段窗口，所以只有键盘路径会炸）。 */
async function pickModel(page: Page, provider: string, modelName: string): Promise<void> {
  const trigger = page.locator('.composer-model[aria-label="模型选择"]').first();
  await expect(page.locator('[role="menu"]')).toHaveCount(0);
  await trigger.click();
  // 一级的 provider 行 = 带 aria-haspopup 的 menuitem（SubTrigger）
  const providerRow = page
    .locator('[role="menuitem"][aria-haspopup="menu"]', { hasText: provider })
    .first();
  await providerRow.hover();
  // 二级的模型行 = menuitemradio（「从这一组里选一个」）
  const modelRow = page.locator('[role="menuitemradio"]', { hasText: modelName }).first();
  await modelRow.click();
  await expect(trigger).toContainText(modelName);
  // 退出动画走完再交还控制权——否则下一次交互会撞上正在关闭的菜单
  await expect(page.locator('[role="menu"]')).toHaveCount(0);
}
