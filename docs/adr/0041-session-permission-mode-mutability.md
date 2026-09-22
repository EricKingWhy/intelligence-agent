# ADR-0041 — 权限档的会话内可变性：permission/changed 事件 +「最后一次胜」派生

**Status**: Accepted（2026-09-22 随 #282 合入 main，见 tracker「F18-A（#282）」段）
**Date**: 2026-09-21
**Related**: GitHub #282（F18-A 后端）/ #283（F18-B 前端）；
`docs/tickets/session-permission-mode-mutability-2026-09-21.md`；
先例：会话内模型可变（`model/changed`，`session/model_switch.py`）；`#236`（权限档「唯一来源」更正）

---

## 1. Context

权限档（`read-only` / `workspace-write` / `danger-full-access`）与 `auto_approve` 自项目早期起被**刻意**定为
「会话创建时定、之后不可变」的会话属性，与 `cwd` 同级：

- `src/agent_harness/session/approval.py:117-141`（`declared_permission_mode`）只认**第一条** `session/started`；
- `session/service.py:579-584 / 745-751`、`session/fork.py:186-188` 与之一致；
- 事件词汇无 `permission/changed`（`session/event.py:67-70` 只有 `PERMISSION_RESOLVED`）；
- `permission_mode` 只在 `POST /api/sessions` 被接收（`web/app.py:1006-1017`）；
- 前端权限 pill 因 `#236` 在会话内转为只读并显示「权限档在会话创建时确定，会话内不可修改」
  （前端仓 `web/src/components/Composer.tsx:21`）。

该决策**从未有 ADR**（`docs/adr/` 中无对应决议），理由只内联在注释里；且「mid-session 改档」被记录为
**产品决策待定** ≥6 次却**始终未开票**（`docs/SDD_TICKET_TRACKER.md:1979 / :132`、
`docs/PHASE_STATUS.md:60`、`docs/phase_status/2026-09.md:290,462`、
`docs/INTEGRATION_REPORT_WS6_WS7_UI_POLISH.md:88,103`）。本 ADR 补上这个治理缺口。

用户反馈「太死板」：想中途换档只能新建会话、丢上下文。

### 1.1 与 `cwd` 不可变性的区别（为何 `cwd` 仍不可变）

`cwd` 的不可变性是**结构性的**：整个 run 的工作目录（sandbox 根、相对路径解析、ArtifactStore 落点）
在 launch 时确定并与 WorkspaceRegistry 绑定；中途变更会让**同一会话的历史产物路径**失去解释。
权限档不同：它是**策略**而非**位置**，且（关键）**每次 launch 都被重新装配成 per-run 快照**（§1.2）
——因此「中途改、下一轮 run 生效」在结构上天然成立。

### 1.2 生效时机为何是「下一轮 run」（构造性，非新机制）

权限策略**不是**每次工具调用 live 读，而是 **per-run 快照**：

- `ToolExecutor.__init__` 绑 `self._policy = policy`（`src/agent_harness/tooling/executor.py:213,220`），
  审批判定读该冻结属性（`:784`）；
- 构造点 `assembly.py:366`，`policy` 来自 `assembly.py:265`；
- `build_runtime` 每次 launch 调一次（`session/service.py:608 create_and_launch` / `:786 resume_and_launch`）。

⇒ 在途 run **不受**中途改档影响是结构性的；本决策**唯一**要改的是**派生源**。

---

## 2. Decision

**D1 — 权限档与 `auto_approve` 在会话内可改、双向。** 升到 `danger-full-access` 需**前端显式确认**；
降档无需确认。后端**不**加额外授权闸门（确认是 UX 层责任）。

**D2 — 用一个 durable 事件 `permission/changed` 表达，data = `{permission_mode, auto_approve}`。**
不拆两个事件。加入 `EVENT_TYPES` 白名单；生成物走脚本（`scripts/gen_event_types.py` /
`gen_event_vocabulary.py`），**不得手改**。

**D3 — 派生优先级：「最后一条 `permission/changed` 胜，否则第一条带值的 `session/started`，否则默认链」。**
与 `session/model_switch.py:39-45`（`current_model_selection`）同形。`auto_approve` 同规则。

**D4 — 生效时机 = 下一轮 run。** 不中断、不重启在途 run（§1.2）。

**D5 — 有 pending 审批时禁止改档**（**两端**：后端 409，前端禁用 pill）。

**D6 — 三概念分离（#236 边界，不容复辟）**：
`declared_at_creation`（驱动「后端是否写键 / 交互式审批开关」）∥ `effective_now`（新）∥
`policy_at_approval`（`permission_policy`，逐事件折叠、**不动**）。
**绝不得**把 `session_permission_mode` 与 `permission_policy` 合并。

**D7 — fork 继承父会话当前 effective 档**（照 `model_switch.py:174-188` `inherit_parent_model`）；
resume 按 D3 派生。

**D8 — 唯一写入口**：`append_permission_change(session, change)`，落点 = **并入
`session/approval.py`**（#282 必做 2 的二选一之一；另一选项是新建 `session/permission_switch.py`）。
选择的理由：（a）本票的**派生源**（`declared_*` / `effective_*`）本就在 `approval.py`，写入口
与派生同处一模块即可「读写同源、一屏可见」，不必跨模块追两个文件；（b）`PermissionChange`
载体与 `ValidationError→未声明` 的容忍规矩也已在 `approval.py`，拆开会把一个概念切三处；
（c）`model_switch.py` 之所以独立成模块，是因为模型解析需要 `Settings` / catalog / provider
store 这一整套依赖（`resolve_model_target` / `assert_model_resolvable`），而权限档只是
「有限枚举 + 两个键」，没有同量级的依赖面——照抄模块划分会得到一个空壳。
端点 `POST /api/sessions/{id}/permission`（对齐 `web/app.py:1471` 的 `/model`）；服务层
`SessionService.change_permission_mode`（对齐 `change_model` 的 seq 重试循环）。

---

## 3. 未采纳方案（逐条留痕）

| 方案 | 为什么没做 |
| --- | --- |
| 「改档需重启 run 才生效」（显式重启流程） | §1.2 证明生效是**结构性免费**的（per-run 快照）；引入重启流程只增加状态机复杂度与失败面 |
| 「改档即中断在途 run 并自动重启」 | 破坏「在途 run 不被中途变更影响」这一既有保证；中断/重启会与审批队列、消息队列、Operation Ledger 的既有语义纠缠 |
| 拆两个事件（`permission/mode-changed` + `auto-approve/changed`） | 二者同属一个会话级策略、同闸门、同生效点；拆开只增加配对/一致性负担 |
| 让 `permission_mode` 走 `SendMessageRequest` / amend 面 | amend 面是「续聊补一轮消息」，语义不同；`cwd` 同为「创建时定」，把权限塞进消息面会造成两套入口 |
| 后端校验「升档必须带确认标志位」 | 确认是 UX 责任；后端强制标志位会造出一个前端必须伪造的契约字段（假安全） |
| 把权限改档并入 `permission_policy` | 二者是**两件事**（#236 已更正并修复「两套真相」）；合并即复辟缺陷 |
| 为「只读 pill 死胡同」单独开一张引导票 | 改档可行后 `PERMISSION_MODE_LOCKED_HINT` 成假话；在 #283 内**删除**该提示即消解问题（见 §4 未闭合） |

---

## 4. Consequences

**正向**

- 用户不必为换档重建会话、丢上下文。
- 形态与 `model/changed` 同构 ⇒ 派生、写入口、fork 继承、契约测试全部有可照抄的先例。
- 补上了「无 ADR」的治理缺口。

**代价 / 风险**

- 新增一个 durable 事件 ⇒ 需同步生成物与三处守卫（`tests/session/test_event_store.py:88` 硬编码期望集，必须手改）。
- 「三概念分离」被打破的风险常驻 ⇒ 由 #282 AC8 守卫。
- 前端 `#236` 的只读锁定与**逐字** title 断言的**反向**改动（#283），必须保持「投影真值」为唯一来源。

**未闭合（解除条件）**

- 事件 data 是否需要 `from/to` 审计字段：当前**不需要**；**解除条件** = 出现跨会话/跨 fork 的权限审计需求。
- 「在途 run 期间能否改档」的产品口径：#282 D5 定为**禁止**（409）；**解除条件** = 若将来允许「排队到下一轮」，需先定义「队列中改档被覆盖」的语义。
- **`auto_approve` 默认值**：请求体把它定为**必填**（漏传即 422），不设默认值——否则一个
  只改档位的调用会把批准策略一并翻掉。**解除条件** = 前端出现「档位与批准策略分两次改」的
  交互需求（届时需要显式定义「只改档位时 auto_approve 取哪一边」）。
- **`auto_approve` 在「档位已声明」的会话上是运行期惰性的**：`build_approval_callback` 先判
  `interactive`（档位非 danger ⇒ 交互式回调），此时 `auto_approve` 不参与判定——这是 F15
  #234 立下的优先级（档位声明优先），本票**刻意不动**它。因此改 `auto_approve` 只对「无档位
  声明」（create 的 deny 路由，或从未声明档位的会话）有运行期效果；对有档位的会话它仍被
  如实地记进事件流（会话级策略记录完整），只是不影响审批路由。**解除条件** = 出现「档位与
  批准策略正交」的产品需求，届时须先推翻 F15 的优先级并同步 `test_declared_permission_mode_takes_priority_over_auto_approve`。
- **`web/src/lib/projection.ts` 的一处 no-op 登记**：本票是后端票，但把
  `PERMISSION_CHANGED` 加进生成物 `event-types.ts`（AC1）会让前端 `EventTypeValue` 联合多出
  一个成员，而 `EVENT_SEMANTICS` 是 `Record<EventTypeValue, …>` ⇒ **不登记就编译失败**。故
  登记为 no-op（`apply: noopProjection`）并注明「真正的投影属 #283」。这是前端**唯一**被本票
  触碰的字节；F18-B 会把这一行替换成真实投影。**解除条件** = #283 合入。
- **`service.py` 的 seq 重试循环现在有两份**（`change_model` / `change_permission_mode`）：
  本票按「形态对齐 change_model」的要求复制，未顺手抽取共用 helper——service.py 的改动面被
  Scope lock 限定在「本票相关路径」，跨票重构 `change_model` 不属本票。**解除条件** = 出现
  第三处同类追加（届时抽 `_append_with_retry` 收益明确）。

### 4.1 已回填：pending 审批判据的精确口径（#282 D5）

判据 = **会话级待审批队列非空**：`self._approval_queues.get(session_id)` 存在且
`queue.pending_ids()` 非空 ⇒ 409（`PendingApprovalConflict`）。

- 与 `delete_session` 的「④ 挂起审批」检查**逐字同一判据**（同文件、同两行），刻意不发明第二套；
- 队列**存在但无待裁决**（`pending_ids()` 为空）⇒ 允许改档——判据是「有没有待裁决的会议」，
  不是「这个会话有没有队列」（队列在 run 存活期间常驻）；
- 会话不在 `_approval_queues` 里（无交互式审批，或 run 已终结并 GC）⇒ 无 pending，允许改档；
- 在途 run **本身**不阻止改档（D4：下一轮生效）——这正是它不复用 `ActiveRunConflict` 的原因。
  实测：`tests/session/test_permission_change.py::TestChangePermissionMode` 四个用例覆盖
  「有 pending / 空队列 / 无表项 / 在途 run 走 live Session」。
