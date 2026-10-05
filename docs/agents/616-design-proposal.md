# #616 设计方案：陈旧委派账行名的公开清除通道

> 状态：设计方案（待用户裁决），不施工。
> 依据：issue #616 票面；#564 (a)+(b) 裁决（关单评论）；实码核实（见"事实依据"）。
> 遵循：AGENTS.md §6.1 方案先行调研 + §3 启动检查表（本票为设计票：Vision/模块规格/Reuse/Phase 相关条款已实读，见下）。

## 1. 背景与问题

#564 修复了"一次打错请求把会话永久卡死"：resume 带未注册工具名时，eager CAS/ensure 在校验之前先把坏名并入 `session_budgets.tool_call_limits`（merge-only：按名并集 + 取 min，`delegation_tree.py:447-454`），随后请求被 422 拒绝，但坏名永久留存在账行里。

用户裁决 (a)+(b) 组合（#564 关单评论）：
- (a) 声明校验前置到 eager CAS/ensure 之前 —— 已在 `0b44536c` 落地，新请求不再污染账行；
- (b) 陈旧账行名降 `logger.warning`（`assembly.py:559-568`）—— 存量坏名不再阻塞 resume，但**永久留存**，每次 resume 重复告警，且账行与真实 registry 长期漂移。

本票 = (b) 裁决时明确另立的"存量陈旧账行的公开清除通道"。不变量 §7.13（Operation Ledger 必须支持 reconcile）的同向诉求：账实漂移应有受控的对账/清理通道，而不是只读告警。

## 2. 事实依据（实码核实）

- `session_budgets.tool_call_limits`：ceiling 表（"对某工具名的调用上限"），merge-only 并入（并集 + 按名取 min），`NULL` = 无上限。`UPDATE` 显式写路径存在（`ensure_session_budget` 480-495、`update_session_limits` 710-871），表本身**不是** append-only。
- `session_budget_events`：审计表，append-only（no_update/no_delete 触发器，`delegation_tree.py:141-150`）；已有 10 个 kind（`limits_tightened`、`limits_updated`、`tools_recorded` 等），全部经 `_append_session_event` 写入，**不**落 JsonlSessionStore 的 typed SessionEvent。
- 待清对象是 `tool_call_limits`（ceiling 表），不是 `tool_calls_by_tool`（消耗事实表，后者是历史事实，必须保留）。
- 陈旧性是 registry-relative 概念：只有服务端持有根 registry（`assembly.py:502-569` 根 registry 名字集 + 陈旧名降告警），"谁陈旧"必须服务端权威判定。
- Permission 代码实际只有三档（session 级 `PermissionPolicy`：READ_ONLY/WORKSPACE_WRITE/DANGER_FULL_ACCESS；tool 级 `ToolPermission`：READ_ONLY/WORKSPACE_WRITE/DANGER，`tooling/contract.py:94-140`）；`needs_approval` 的域是"模型发起的一次 Tool Call"（`tooling/approval.py:80-104`），清除账行是宿主/会话管理动作，走不进 ToolExecutor。
- 同类先例：`archive` / `delete_session` 用 `require_trusted_origin`（ADR-0025 D1），硬删加显式确认（ADR-0026/0029）；最接近的清理范例是 `clear_delegation_parent`（`sqlite.py:582-595`，幂等、返回修复行数）。
- CLI 手工分发（`cli.py:913 _main_dispatch`），Web API session 端点全在 `web/app.py`；本仓纪律"三个入口共用一份规则"（`run_budget.py:1577`，ADR-0045 D8）。

## 3. 方案依据（§6.1，独立来源 ≥2）

| 来源 | 机制 | 映射到本票 |
|---|---|---|
| Kubernetes GC（`ownerReference` 缺席自动回收）+ `kubectl delete` 定向删除 | 悬空引用由服务端权威判定并清理，客户端可点名操作 | 通道形态：服务端算 stale 集合（默认整会话重整）+ 可选 `--tool` 收窄；CLI 是 API 的瘦客户端 |
| AWS IAM 悬空 ARN 语义 | 悬空引用合法留存、求值时惰性失效、响亮标记不静默；清理由 IAM 权限授权，非运行时审批 | 权限：走宿主管理授权（trusted origin），不进 Tool Call 审批队列；陈旧名降告警 = "容忍悬空但不静默" |
| Terraform `terraform state rm` | 从可变的 state 真源摘掉不再管理的资源条目，底层 infra 不动，留操作记录 | merge-only 兼容：显式重写 ceiling 表 + 记 purge 事件；不动消耗事实表 |
| Git `gc` + reflog | 清不可达对象，reflog 保留可追溯记录 | 可观察性：删名 + append-only 审计事件，历史可回放对账 |
| etcd revision 乐观并发 | 写操作推进 revision，观察者可见变更 | 事件 version 必须 bump，否则投影/CAS 看不到变更 |

## 4. 设计决策

### 决策点 1：通道形态与操作粒度

**形态选项：**
- A. Web API only：`POST /api/sessions/{id}/budget/purge-stale-tools`（沿用本仓"显式动词"约定）。
- B. CLI only：`cli.py _main_dispatch` 加分支。
- C. **两者，共用同一个 service 方法（推荐）**：本仓纪律"三个入口共用一份规则"（ADR-0045 D8），通道只是适配器。对标 K8s API server + kubectl（同一 API 对象、CLI 是瘦客户端）。

**粒度选项：**
- A. 单条账行名（`--tool X`）：精确幂等，但"谁陈旧"的判据依赖根 registry，客户端不掌握，等于把 #564 的判断责任推回给可能算错的调用方。
- B. 整会话重整：服务端用根 registry 投影一次性算 `row_keys ∉ root_registry`，一次清完。
- C. **B 为主 + `--tool` 可选收窄（推荐）**：stale 是 registry-relative 概念，只有服务端能权威判定。对标 K8s GC（按缺席自动找对象）+ `kubectl delete <name>` 定向操作。

**推荐：C + C**——`POST /api/sessions/{id}/budget/purge-stale-tools` 与 `agent-harness budgets clear-stale-tools --session <id> [--tool X]`，共用 `SessionService.purge_stale_session_tool_limits()`。

### 决策点 2：权限与审批

- A. **归入宿主/会话管理类（推荐）**：等同 `archive`——`require_trusted_origin` + 审计事件，**不进** `needs_approval`。理由：`needs_approval` 的域是 Tool×Policy（模型发起的 Tool Call），拿它闸非 tool 动作是范畴错误；三档 PermissionPolicy 描述的是"模型能动什么"，不描述"operator 能维护什么账"。对标 K8s RBAC（GC 由 authz 授权，不走 Pod 准入审批）、AWS IAM（清理由 IAM 权限授权，非运行时审批）。
- B. 借用 session 三档做授权（单名清 = WORKSPACE_WRITE、整会话重整 = DANGER_FULL_ACCESS）。
- C. 独立审批/带外确认：整会话重整加显式确认（比照 `delete_session` 的二次确认，ADR-0026）。

**推荐：A 为主 + C-lite**——单名清除仅 `require_trusted_origin`；整会话重整额外要求显式确认（影响面是一次多键；且本操作可由 `update_session_limits` 重新加回，属低爆炸半径，用轻确认而非审批队列）。

### 决策点 3：可观察性

- A. **只落 `session_budget_events` 新 kind（推荐）**：`tool_limits_purged`，与现有 `limits_tightened`/`limits_updated` 同路。append-only 触发器只禁 UPDATE/DELETE，不禁 INSERT；同事务内与行重写一起提交。detail 建议：`{"purged": {"<name>": <ceiling>}, "remaining": {...}, "stale_names": [...], "reason": "unregistered", "source": "web"|"cli"}`；`version` 写新版本号并同比 bump `session_budgets.version`（对标 etcd revision，否则投影/CAS 看不到变更）。
- B. 同时落一条 typed SessionEvent：跨流耦合，且预算变更现状不落 SessionEvent，会造成同类事实两套载体（违反 D8 同源与不变量 #22）。
- C. 只 logger 不发事件：清除后无 durable 记录，无法对账——正是 #616 要消除的"不可见残留"。

**推荐：A**。对标 AWS CloudTrail（API 调用审计）、Git reflog（重写后仍留可追溯记录）。

### 决策点 4：merge-only 语义兼容

- A. tombstone（加标记列）：与 merge-only 相容，但侵入最大——`_row_session_limits`、merge 路径、投影、headroom 判定每一处都要查墓碑，且账行与生效值永久分离。对标 Kafka log compaction tombstone。
- B. **显式重写（推荐）**：单事务内从 `tool_call_limits` JSON 删除 stale 键 → 追加 `tool_limits_purged` 事件 → bump version → commit。只动 ceiling 表，不动 `tool_calls_by_tool` 消耗事实。`session_budgets` 本就可 UPDATE；删除"对不可调用名"的 ceiling 不放宽任何可执行性，不违反 ADR-0044 D1「配置只能收窄」；headroom 判定只在集合非空时收紧，删键只会让 effective 集合变小，不产生新 409。若该工具未来重新上线，旧 ceiling 丢失 = 回到 unlimited，是诚实状态。对标 **Terraform `state rm`**（机制映射最贴切：从可变 state 真源摘条目、底层不动、留操作记录）、Git `gc`、K8s 孤儿 GC。
- C. 派生时过滤（读时忽略陈旧名、不动行）：零写入，但正是 `assembly.py:559-568` 的现状——坏名永久留存、每次 resume 重复告警，**不满足 #616 的消除目标**，只能作为 pre-purge 行的兜底保留。

**推荐：B，并把 C 保留为存量兜底**。

## 5. 推荐组合（一页）

```
POST /api/sessions/{id}/budget/purge-stale-tools
agent-harness budgets clear-stale-tools --session <id> [--tool X]
  → SessionService.purge_stale_session_tool_limits()
  → 单事务：UPDATE session_budgets.tool_call_limits（删 stale 键）
           + INSERT session_budget_events(kind=tool_limits_purged, version=new_version, detail=…)
           + version+1
  → require_trusted_origin；整会话重整加显式确认
  → 不动 tool_calls_by_tool / SessionEvent 流；幂等（重跑 0 行）
```

## 6. 待用户裁决的开放项

1. 整会话重整是否需要"确认面"（倾向要，比照硬删口径）；
2. 事件 `version` 是否必须 bump（倾向必须，否则投影/CAS 看不到变更）。

## 7. 下一步（裁决后）

- 验收标准细化：TDD（陈旧账行存在 → 走清除通道 → warning 消失、resume 行为不变、事件流/账行可回放对账）；Kill/Recovery 不受影响；全量门禁 §14.10 + 两轴独立审查 + 台账行。
- 本方案由 codebuddy（deepseek-v4.1-flash）经两轮 -p 分析产出：第一轮只读事实摸底（代码 file:line），第二轮决策点选项分析；文档由子代理按 §8.9 写盘纪律落盘。
