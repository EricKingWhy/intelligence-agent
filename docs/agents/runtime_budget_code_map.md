# 运行预算链代码地图（Runtime 线 T4–T12 共用）

> **用途**：把「预算 / 暂停 / 恢复 / 计数」这条链的**入口点**集中到一处，省掉每票开工前
> 在 85 KB 协议 + 820 KB tracker + 冻结规格之间反复定位的时间。**它是导航件，不是事实源**：
> 语义以代码与规格为准（`SPEC_ROOT/02 §5.1`、`03 §3.4`、`11 §6.1`、`ADR-0044`）。
>
> **行号会烂**（本文件的值是 2026-09-25 实测）：动手前用符号名复核 ——
> `grep -rn "def <符号名>" src/ agent_harness/`，不要照抄行号下刀。
>
> **维护**：哪个符号被改名 / 搬家，就在同一票里改本文件对应行（它随分支走，T6–T12 继承）。

## 1. 一次 run 的钱与账，各自在哪里产生

| 事实 | 产生点 | 落点（durable） | 计数点（`02 §5.1`） |
| --- | --- | --- | --- |
| 一次**被接纳的模型决策** | `agent/runtime.py` 的 `_drive` 循环体 | `model/completed` | `agent_turns` |
| 一次**实际发出的 Provider 请求**（primary / fallback / closeout 各一次；被拒 / 传输失败**也**算） | `model/fallback.py` 的 `_slot()` / `ainvoke` / `astream`；closeout 走 `agent/runtime.py` 的 `_closeout_continuation`（`self._raw_model.ainvoke`，**绕过 coordinator**） | `model/request`（T5 新增） | `model_requests` |
| 一次工具调用 / 一次工具尝试（含重试） | `tooling/executor.py` | `tool/call` / `tool/result` | `tool_calls` / `tool_attempts`（T6） |
| Provider 自报的 token | 响应 `usage_metadata` → `agent/runtime.py` 的 `_usage_from_response` | `model/completed.data.usage`、`run/completed.data.usage_total` | `total_tokens`（T5） |
| Provider 归属的 USD | 响应 `response_metadata["cost"]` → `model/accounting.py` 的 `cost_usd_from_response` | 同上的 cost 键（十进制**字符串**） | `cost_usd`（T5） |
| 委派 | `multiagent/tools.py`（`DelegateTool`） | `agent/delegation-started` / `-finished` | `delegations`（T10 做树聚合） |

**三个作用域不合并**（`ADR-0044` D2）：`local.*`（本实例保险丝，`agent/budget.py` 的 `resolve_local_fuse`）、
`run.*`（跨 pause / resume 累计，`agent/run_budget.py`）、SessionBudget（T10）。

## 2. 关键文件与符号（复核用）

**`src/agent_harness/agent/run_budget.py`** —— 预算账本本体，全部状态**从持久化事件重建**：

- `RunTurnLimits`（→ T5 起改名 `RunLimits`，加 requests / tokens / cost 三维）、`BudgetConsumed`（T5 新增）
- `PausedRun` / `RunBudgetState` / `LaunchRunBudget(version, limits, consumed_*, run_id, turn_index)`
- `derive_run_budget(events, run_id)` —— 派生账本（replay 等价的唯一判定点）
- `pause_trigger(...)` —— 准入判定，**位置在 loop 顶部、任何工作之前**；`TRIGGER_RUN_TURNS` / `TRIGGER_LOCAL_TURNS`
- `closeout_capacity(...)` —— 收口预留（`RESERVED_CLOSEOUT_TURNS = 1`）
- `resume_ceiling_ok` / `validate_resume` —— **CAS 的唯一判定点**（`expected_version` + 绝对 ceiling 真高于已消耗 + `resume_basis`）
- `build_limits_snapshot` / `build_pause_data` / `build_resume_data` / `as_run_started_budget`
- `normalize_continuation` / `deterministic_continuation` / `latest_paused_run`

**`src/agent_harness/agent/runtime.py`** —— 执行面：

- `_usage_from_response`（usage 抽取）、`_RunFinalizer`（终态载荷，持 `usage_total` 引用）
- loop 顶部准入判定、`astream`（流式）/ `ainvoke`（非流式）两个模型调用点、空 / DSML 拒绝路径、
  `drain_transitions`（fallback 切换事件）、usage 累加处（T5：**前移**，让被拒响应也计 usage）
- 终态臂：`_terminal_paused` / `_terminal_completed`（`cost_usd` 现在是 `None + TODO(spec 12)`）/ `_terminal_failed_run` / `_terminal_cancelled`
- `_pause_trigger`、`_closeout_continuation`

**`src/agent_harness/model/`**：

- `fallback.py`：`ModelFallbackCoordinator` —— `_guarded_stream` / `_slot` / `ainvoke` / `astream` / `drain_transitions` / `_try_switch`
  与 `agent/runtime.py` 的连接靠 `drain_transitions`（**一次决策可见多次实际请求**的地方）
- `accounting.py`（T5 新增）：`ProviderAccounting`（**声明**式能力，不探测）、`HARNESS_MODEL_ACCOUNTING`、
  `cost_usd_from_response`、`REQUEST_OUTCOME_*`、`PROVIDER_ROLE_*`
- `provider.py`：`create_chat_model`（`max_retries=0`、`request_timeout=300`、`reasoning_effort` 线格式翻译）

**`src/agent_harness/session/`**：

- `event.py`：`EVENT_TYPES` + 事件常量（`MODEL_COMPLETED` / `MODEL_FAILED` / `MODEL_REQUEST`(T5) / `RUN_PAUSED` / `RUN_RESUMED` …）；
  `RUN_TERMINAL_TYPES`（**不含** `run/paused`）
- `session.py`：`begin_run(..., budget=)`（只在显式配置时写 `budget` 键）、`end_run(..., usage_total, cost_usd, ...)`（completed 才写 `cost_usd`）
- `service.py`：创建路径的 `LaunchRunBudget`、续跑路径（`resume_and_launch`）、`_paused_resume_state` / `_launch_budget_from` / `_plan_paused_resume` / `_commit_paused_resume`
- `store.py`：append = 整行 + flush + fsync；**`json.dumps` 是裸的**（`Decimal` 会炸 ⇒ cost 用字符串）
- `interrupt.py`（尾部 pause 不算未终结）、`fork.py`（pause = −1 / resume = +1）、`runmanager.py`（暂停态抑制接力投递 + `session_lock`）

**`src/agent_harness/web/`**：

- `app.py`：`LocalBudgetRequest` / `RunBudgetRequest`（含 `_reject_unimplemented_dimensions()`）/ `BudgetRequest` /
  `budget_claims()` / `ws_budget_claims()` / `ResumeRequest` / `SendMessageRequest`
- `websocket.py`、`domain_errors.py`（形状非法 422 / 状态对不上 409 的映射）
- CLI 侧：`cli.py`（`--run-max-agent-turns` 一族 + `resume`）

**前端投影面**（本仓根上的 `web/`，属完整门禁范围）：

- `web/src/lib/projection.ts`（**A 链也在改这个文件** ⇒ 交集风险，两处都在同一区域追加时按 §14.7 做并集解析）、
  `web/src/lib/runBudget.ts`、`web/src/components/PausedPanel.tsx`、`web/src/types.ts`
- `web/src/generated/event-types.ts` —— **生成物**（`scripts/gen_event_types.py`），不许手改

## 3. 判据 / 守卫（改这块时哪些会红）

| 判据 | 位置 | 什么时候红 |
| --- | --- | --- |
| 逐场景 durable 序列 / `discarded` 尾部 / 终态载荷键集 | `tests/agent/test_event_sequence_golden.py` | 增删事件、改终态键集 |
| 流帧是持久化日志的**前缀** | 同文件 `test_durable_frames_mirror_the_persisted_log` | 新事件没被 SSE 镜像（只能进 `discarded` 并声明） |
| `run/paused` 键集与 `consumed == {"agent_turns": 2}` | 同文件 `test_pause_payload_key_set_is_pinned` | 改 `run/paused` 载荷（T5 会动它 ⇒ 同步改） |
| 生成物同步 | `scripts/gate0.py` 守卫 + `tests/test_event_vocabulary_generated.py` | 改了事件类型却没重新生成 |
| 覆盖闸门 | `scripts/check_review_coverage.py`（**唯一可运行权威**，`.sh` 是冻结语义参考） | 有 commit 没有对应审查行 |
| 门禁落盘 | `docs/gate/<sha>.json`（`scripts/gate0.py` 裸全量写出：6 条机械车道 + 墙钟 + 工具版本 + 工作树证据） | 手抄读数（**已禁止**）；重车道读数必须来自可复跑命令 |

## 4. 后续票各自落在哪（避免每票重新找位置）

| 票 | 主题 | 主要落点 |
| --- | --- | --- |
| T6（`#314`） | 工具配额（`tool_calls` / `tool_attempts` + `tool_call_limits` 维度） | `tooling/executor.py`（计数点）、`run_budget.py`（维度）、`web/app.py`（请求形状） |
| T7（`#315`） | deadline + 副作用 reconcile | `run_budget.py`（`deadline_at` 判定位置 = 每次接纳新工作之前）、`tooling/reconcile.py`、`web/app.py`（409 条件「未结清副作用」） |
| T8（`#316`） | quiescence + `CompletionPolicy`（依赖只有 `#308`，**可与 T5 并行**） | `agent/runtime.py` 的 `run/completed` 之前六条件、`agent/guards.py` |
| T9（`#317`） | stuck 检测（一次 replan、归一化指纹） | `agent/guards.py` + `run_budget.py`（`REASON_STUCK` 与 continuation） |
| T10（`#318`） | SessionBudget 持久化 + 委派树池化 | `multiagent/provider.py`（`_run_child` 现在**不向父会话传播 usage / request**）、`session/store.py`、`run_budget.py` |
| T11（`#319`） | 五类场景真实 3/3（同一 SHA/tree） | `evaluation/live_gate/scenarios/*`、`docs/live_gate/**` |
| T12（`#320`） | 移除 `max_steps`（expand–migrate–contract 的最后一步） | `agent/budget.py`、`agent/runtime.py` 的 fuse 终态、配置面 |

## 5. 环境与工具事实（每票都会遇到）

- 提交必须走**显式父管线**：`git add` → `git diff-index --cached <parent> --name-only` → `git write-tree`
  → `git commit-tree <tree> -p <parent> -F <msgfile>` → `git rev-list --parents -n1 <new>` **必须打印两个 sha** → `git update-ref`。
  禁裸 `git commit`（HEAD 悬空会造 root commit）。
- 长跑（全量 / Live Gate）前台约 120s 被 SIGTERM ⇒ `nohup bash <脚本>.sh > /dev/null 2>&1 &` + 进度文件轮询。
- 全量测试走 `scripts/run_tests_clean.sh`（清 `PYTHONPATH`；走 shim 会有 `tests/evaluation/*` 假红）。
- 真实 Live Gate 顺序：**先跑场景 → 提交 `docs/live_gate/**` → 再跑 Gate-0**（Gate-0 判据②要求未跟踪清单只有 `.zcodeignore`）。
- `.env` 的**值**永不打印 / 不提交 / 不进证据；只准列 key 名。
- 中文大文件（tracker 820 KB、归档 1 MB、协议 85 KB）只用 `Read`(offset/limit) / `Grep`；禁 `cat`/`tail`/`sed`/`head`。
