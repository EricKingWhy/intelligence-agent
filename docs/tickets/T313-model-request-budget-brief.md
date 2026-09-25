# T5（`#313`）预算链施工简报 —— Provider 请求 / token / cost 计数与 ceiling

> **这是什么**：本票开工前「读规格 + 设计」的沉淀物，随分支走（`AGENTS.md` §16.1 的落点纪律：
> 这里只放**操作性事实 + 指针**，机制叙述的完整版在规格与 ADR 里，本文件不复制它们的正文）。
> **不是**票面副本：票面（Must Do / Must Not / AC / Verification）以 GitHub `#313` 为准，本文件只回答
> 「这一票要动哪些文件、每条 AC 落在哪里、哪些坑已经踩过」。
>
> **行号会烂**：本文件里的 `file:line` 是 2026-09-25 的实测值，动手前用符号名 `grep` 复核一次
> （复核方法：`grep -rn "def <符号名>" src/`），不要照抄行号下刀。

## 1. 契约在哪（开工前必读，按顺序）

| 内容 | 位置 | 读它干什么 |
| --- | --- | --- |
| 七个 counter 的**唯一定义与计数点** | `SPEC_ROOT/02_AGENT_RUNTIME.md` §5.1 | `agent_turns` / `model_requests` / `tool_calls` / `tool_attempts` / `total_tokens` / `cost_usd` / `delegations` 各数什么、在哪数 |
| 预算载荷与暂停 / 恢复语义 | `SPEC_ROOT/03_SESSION_EVENT_MODEL.md` §3.4、§5 | `run/paused` / `run/resumed` 的字段与不变式；**账目事件的表示方式是实现自由的**（逐次接纳落账 / 稳定边界落账 / 等价 append-only 形式），只要 AC 与 replay 等价成立 |
| 预算配置面与 HTTP 形状 | `SPEC_ROOT/11_STREAMING_API_WEB_UI.md` §6.1 | run 作用域六个维度、`422` 与 `409` 的分工、投影必须含什么 |
| 预算分层与暂停生命周期 | `docs/adr/0044-run-budget-pause-resume.md` D2–D6 | 三级预算不合并、cost 不臆造、durable pause 生命周期、deadline 判定位置、cancel≠pause、stuck 归属、quiescence 六条 |

`SPEC_ROOT` = `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/`（**不是**根上的 `docs/spec/`）。

## 2. 本票的六条设计决策（前置结论，别再重新论证）

- **D1 计数点用 durable 事件，不用内存快照**：`model_requests` 的计数点是新事件 `model/request`
  （已落地在本分支 `f4e373e`：`session/event.py` + `EVENT_TYPES`）。理由：快照无法满足
  「刷新 / replay 后计数器仍一致」（AC-8），且与 T4 已立的「单一镜像」原则冲突。
- **D2 计数位置**：一次**实际** Provider 请求 = 一次计数，位置在
  `model/fallback.py` 的替身协调器内（primary / fallback 各自一次）+ closeout 调用点
  （`agent/runtime.py` 的 `_closeout_continuation`，它走 `self._raw_model.ainvoke`，**绕过**
  coordinator ⇒ 必须单独计）。被拒绝 / 传输失败的请求**也在**计数内（它们不增 `agent_turns`）。
  durable 事件必须同时进 SSE 镜像（golden 的「流帧是持久化日志的前缀」判据），只在取消臂追加的
  记录要在 golden 的 `discarded` 里声明。
- **D3 强制点**：准入判定在 `agent/runtime.py` 的 loop 顶部（任何 model / tool / child 工作**之前**），
  扩展 `agent/run_budget.py` 的 `pause_trigger` 增加 `requests` / `tokens` / `cost` 三个维度；
  `closeout_capacity` 同样扩展到各维度（预留 1 次收口请求，`RESERVED_CLOSEOUT_TURNS=1` 的同类表达）。
  边界语义：`consumed >= ceiling` 即不得**有意**开始新的调用（绝不在超限后仍发起请求）。
- **D4 开工前 422**：新增「ceiling 可执行性」判定 `validate_ceiling_enforceability(limits, accounting)`，
  用**声明式**能力（`model/accounting.py` 的 `ProviderAccounting`），不看响应元数据事后判定 ——
  422 必须发生在**任何 Provider 请求之前**且零副作用。生产链路 `reports_cost=False` ⇒
  显式 `max_cost_usd` 在生产上恒 422（这是规格要求的诚实行为，不是缺陷）；有能力的那条路
  由测试替身通过 `response_metadata["cost"]` 走通。另加一条：对**消耗基数未知**的历史暂停
  （老 pause 事件里没有该维度）不得接受该维度的 ceiling。
- **D5 形状**：`consumed` / `limits` 里「不可用」用 `null` 表达（**不是 0**）；cost 以
  **十进制字符串**入事件（`Decimal` 在 `json.dumps` 下会炸，见 §4 的坑），算术在 `Decimal` 里做。
- **D6 改动面**：`RunTurnLimits` → `RunLimits`（含 `max_model_requests` / `max_total_tokens` /
  `max_cost_usd`），新增嵌套 `BudgetConsumed`；token 累加**前移**（被拒绝的空响应也要计 usage）；
  closeout 那次调用的 usage / cost 入账；`end_run(cost_usd=<真实值|None>)` 替换现在的
  `cost_usd=None,  # TODO(spec 12)`；投影面 = `run/started` 的 budget + `run/paused` / `run/resumed` +
  （候选）`GET /api/sessions/{id}/budget`；两个生成物重新生成。
  **待开工时定稿的一处**：AC-8 的「Web 显示」是只到投影（`web/src/lib/projection.ts` + 生成物）还是
  也要一个 UI 面板 —— 本票 Must Not 没有排除 UI，但 T4 已引入 `PausedPanel`，
  决定前先看 `web/src/components/PausedPanel.tsx` 能否直接承载。

## 3. AC → 落点 → 判据

| # | AC（`#313` 简写） | 实现落点 | 判据（测/证据） |
| --- | --- | --- | --- |
| 1 | 一个决策可对应多次实际请求，且每次恰计一次 | `model/fallback.py` 计数 + `model/request` 事件 | 新单测：primary 失败 → fallback 成功 ⇒ `model_requests=2`、`agent_turns=1` |
| 2 | primary / fallback / child / closeout 都入对账 | 同上 + closeout 调用点；child 归 T10（本票只保证不误计） | `tests/agent/test_model_fallback_runtime.py` + 新用例 |
| 3 | token 只用 Provider 自报，不可用不转 0 | `model/accounting.py` + `agent/runtime.py` 的 usage 累加 | 新单测：响应无 usage ⇒ 投影 `unavailable`，**不是** 0 |
| 4 | cost 只来自可靠归属，不臆造费率 | `cost_usd_from_response`（只认 `response_metadata["cost"]`） | 新单测 + 生产路径恒 `unavailable` 的断言 |
| 5 | 任一路径无可靠强制 ⇒ 首个请求前拒绝 | `web/app.py` 的 `_reject_unimplemented_dimensions` 改成「按声明能力判定」 | API 用例：`max_cost_usd` 显式 ⇒ 422 且**零副作用**（无 `run/started`、无 session 变更） |
| 6 | 已接纳的请求被预留 / 限界，绝不有意越线 | `agent/run_budget.py` 的 `pause_trigger` / `closeout_capacity` | 边界单测：`consumed == ceiling` ⇒ 拒绝准入；`consumed == ceiling-1` ⇒ 放行 |
| 7 | 撞线落 `run/paused`，抬高**绝对** ceiling 后同 run 续跑且不重置 | T4 既有路径 + 三维度 | `tests/agent/test_run_pause_resume.py` 扩展；Live Gate 场景 `budget-pause-resume-same-run` 真跑 |
| 8 | API / 流 / CLI / Web 投影一致，刷新 / replay 后仍一致 | `session/service.py` 投影 + `web/app.py` + `cli.py` | replay 等价用例（`derive_run_budget` 对 `store.read_events` 重算 == 内存态） |
| 9 | 受控 primary 失败 → 真实 fallback，证据含两侧 id 与计数、无凭证 | Live Gate 专用场景（**有配置 fallback 时才跑**，否则 `BLOCKED`） | 真实证据目录 + `scripts/live_gate.py validate --require-pass` |
| 10 | 适用场景同一 SHA/tree 上 3/3；缺前置判 `BLOCKED`/`SKIPPED` | 全部 Live Gate | 证据 `docs/live_gate/<ts>-<sha>-<scenario>/`，**先提交证据再跑 Gate-0** |
| 11 | 既有 fallback / usage 回归全绿 | — | `tests/agent/test_usage_accounting.py`、`test_model_fallback_runtime.py`、golden |

## 4. 已踩过的坑（照着躲，不要重新发现）

1. **`Decimal` 不能进 `json.dumps`**：`session/store.py:181` 用裸 `json.dumps` 写事件 ⇒ cost 必须
   序列化成十进制**字符串**；Pydantic v2 的 `model_dump_json` 会替你转成字符串，**两条路径形状必须一致**。
2. **网关的 `usage.cost` 活不下来**：vendored `langchain_openai` 的 `_create_usage_metadata`
   只映射已知 usage 键，未知键被丢弃 ⇒ 只能读 `response_metadata`（它的键集是
   generation_info + `model_provider` / `model_name` / `system_fingerprint` / `id` / `finish_reason` / `headers`）。
3. **golden 是硬约束**：`tests/agent/test_event_sequence_golden.py` 钉了逐场景 `durable=` 精确序列、
   `discarded=` 尾部、终态载荷键集，以及「流帧是持久化日志的**前缀**」（`test_durable_frames_mirror_the_persisted_log`）。
   动事件序列 = 必须同步改 golden，且**新增事件必须被镜像**（否则只能进 `discarded` 并声明）。
4. **生成物有守卫**：`scripts/gen_event_types.py` → `web/src/generated/event-types.ts`、
   `scripts/gen_event_vocabulary.py` → `docs/EVENT_VOCABULARY.md`；`scripts/gate0.py` 里有同步守卫，
   `tests/test_event_vocabulary_generated.py` 也会红。改完源必须重新生成，**不许手改生成物**。
5. **Live Gate 的读盘顺序**：场景的终态读盘必须在**执行侧收尾之后**，且现在两个场景自带
   「独立复读、逐条同序」的静止自证（本票 O3 已修 `smoke.py` / `long_task.py`，原缺陷见 `089de04`）。
   顺序反了会得到一次**取证缺陷长得像产品缺陷**的假 FAIL（`validator.py`：「证据写 N 行，轨迹实为 N+1 行」）。
6. **变异 / 红证只能在主工作树之外的副本里做**，且副本里必须显式 `PYTHONPATH=<副本>/src`
   （`.pth` 指向主仓 `src`，不设就出现「副本测试生效、副本 src 静默无效」）；改副本前先探行尾（`core.autocrlf=true` ⇒ 副本内是 CRLF），
   `.replace()` 必须断言命中数 `== 1`。
7. **工具面**：禁 `| head`；中文大文件只用 `Read`(offset/limit)/`Grep`（GBK 控制台会显示成乱码）；
   前台命令约 120s 被 SIGTERM ⇒ 长跑用 `nohup` + 进度文件轮询；`git add` 后必须走显式父管线提交。

## 5. 交付清单（与 `docs/SDD_WORKFLOW_PROTOCOL.md` §7/§8 对齐）

1. 实现 + 测试（含边界：恰好到线 / 差一格 / 不可用值 / 非法形状 422 / 陈旧版本 409）；
2. 红证：每条新判据在改动前的树上红、改动后绿（在副本里做）；
3. 两轴独立审查（Standards ∥ Correctness/Spec，派单带本文件 §3 的 AC→落点表与预算）→ 处置 → 修后重审；
4. 全量门禁（`scripts/run_tests_clean.sh tests/`）+ 前端重车道（`vitest` / `build` / `oxlint`）+ Gate-0 裸全量；
5. **Live Gate 真实 3/3**（真实模型 + 生产工具；有 fallback 配置时另跑受控 primary 失败场景），失败尝试一律保留入库；
6. 台账行（序号从 **312** 起）+ tracker 段（**只写操作性事实 + 指针**，不写机制复述）+ PHASE_STATUS 索引 + 当月归档明细；
7. `python scripts/check_review_coverage.py` exit 0 → 合入 `main` 并 push（用户 2026-09-25 起授权「每票审查通过后合入 main 并 push」）→ `#313` 评论（**不关单**，按 §14.12 由主线处理）。
