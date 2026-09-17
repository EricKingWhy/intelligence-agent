# DSH 设计对照审计（2026-09-17，用户点名项「能拿来抄的直接抄」）

> **这份文件回答一个问题**：DeepSeek Harness（`dsh`）那些被我们记下来的可借模式，
> 我们**已经抄了哪些、哪些还没抄、哪些是刻意不抄**。
>
> **证据来源与边界（先说清，免得后人以为我读了源码）**：初稿写作时本机 `D:\DeepseekHarness\` 是**空目录**、
> 且 GitHub 不可达（直连与 `127.0.0.1:7897` 代理都失败，实测 `curl https://api.github.com` = 000），
> 那时 DSH 一侧只能转引本仓内**早先 clone 状态下、带 file:line 引用的一手调研**
> [`docs/RESEARCH_DEEPSEEK_HARNESS_WEB.md`](RESEARCH_DEEPSEEK_HARNESS_WEB.md)（728 行，
> §13 已给出 12 条"可直接借用"与 4 条"DSH 不提供"）。
> **网络恢复后已补做现场抽查**（`git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness`，
> 落在临时目录、未进本仓；HEAD `0d1f500`）：抽了 4 条引用逐字复核，**全部成立**，
> 并把结果写回下表（第 8/9/11/12 行的 DSH 栏现带现场行号）。**仍未现场核实的只有一类**，
> 见 §4。我们这一侧的每一条**都是实测**，命令与文件写在表里。

---

## 1. 逐条对照（DSH §13 的 12 条可借模式）

| # | DSH 模式 | 我们 | 证据 / 说明 |
| --- | --- | --- | --- |
| 1 | 事件日志是唯一真相，消息是它的投影 | **已有（同构）** | `docs/EVENT_VOCABULARY.md` + append-only `session/session.py`；前端 `web/src/lib/projection.ts` 是纯投影。不变量 #3/#5 |
| 2 | 两个传输、一个信任边界（RPC + 单一复用流） | **已有，传输刻意不同** | REST + **SSE**（DSH 用 WebSocket mux；`docs/adr/0016-streaming-ui-detached-run.md`）。信任边界在 `require_trusted_origin` + `AuthSeamMiddleware`（`web/projects.py` / `identity`）。**这是刻意的偏离**（企业代理环境下 SSE 更友好），DSH 自己也把它列为"你要自研" |
| 3 | prompt 只回回执，回复走另一条流 | **部分：混合形态** | `service.py:228 to_response()` → `{status, queue_id?}`（queued/steered 只回执 ✓）；但 `status=launched` 时**响应体本身就是 SSE 流**（`web/app.py:1839`）。混合是有意的（少一次往返），代价见 #4 |
| 4 | 服务端权威快照 + 无缺口跟随（**不用客户端游标**） | **偏离（我们有客户端游标）** | 重连带 `?after_seq=`（`web/src/lib/api.ts` 的 follow 通道），代际/迟到/纠正语义见 `docs/adr/0030-*.md`。**风险登记**：DSH 的论点是"没有客户端游标可以搞坏"；我们的游标是**可被前端浪费但不会静默丢事件**（#221 已把"迟到落定必须消费"钉住）。未改，见 §3 T3 |
| 5 | 显式 `queue` vs `steer`（不静默自动插入） | **已有** | `POST /messages` 的 `queue_id` / `steer_id`（`web/app.py:329/337/1820`）+ `GET /sessions/{id}/queue` 由**事件流**重建（不是内存镜像）；机制在 ADR-0030 |
| 6 | 取消默认**保留**收件箱（`keepInbox: true`） | **已有** | `service.py:997 cancel()` → `run_manager.cancel()` **只动 run**，不碰消息队列；队列独立且可跨崩溃重建（启动日志：`未投递输入重建完成：扫描 32 个会话`） |
| 7 | 审批 fail-closed、一次性、用 `callId` 绑定到已流出的工具调用 | **已有** | 审批卡 + `POST /approvals` 的 409 幂等（`web/src/lib/api.ts:523 AlreadyResolvedError`）；工具调用与其结果靠 `tool_call_id` 配对（不变量 #6） |
| 8 | 压缩作为**可对账的表面操作**（`surfaceOp: replace` 遮蔽被压缩区间） | **已有（同构，且多一层括号）** | **实测纠错**（初判「缺口」是错的）：压缩写的是 **4 事件括号** `compaction/start(source_seq_start/end)` → `context/compacted(summary, source_seq_start/end, compacted_turn_count, token_estimate, fallback_used)` → … → `compaction/end(bracket_id)`（`context/builder.py:136`，词汇表 `session/event.py:76-82`）；**原始事件保留在 JSONL 里（shadowed）、`derive_messages` 跳过**——这正是 DSH 的「遮蔽 + 保留可重建性」；Inspector 有消费面（`web/src/components/StepDetail.tsx:660` 的压缩行）。**DSH 栏现场复核**（`docs/subsystems/session.md:343`）：`surfaceOp` 是 `SurfaceEventType` 的必填字段，"the surface deliberately **shadows the ranges a replacement summarizes**"——语义与我们一致 |
| 9 | 模型选择进「已记录的请求信封」（可回放、可审计） | **部分（差异在「请求侧」）** | **实测**：回答侧落的是 `model/completed.data.model`（从**响应**元数据取：`runtime.py:216 _model_name_from_response` + `:922-924`；provider 不回显就**不落这个键**）；**请求侧没落**——`model/started.data` 只有 `{"step": N}`（`runtime.py:848-849`）。⇒ 网关/自定义端点下「这一轮想用哪个模型」在 durable 历史里查不到，回退链只在 `model/fallback` 事件里（ADR-0033 §3）。**DSH 栏现场复核**（`docs/subsystems/session.md:169-204`）：确有 `request/header` 事件，载荷是 `EpochHeader`＝"call configuration (**provider, model, reasoning effort**, and sampling scalars)"，按 `initial`/`resume`/`change`/`series` 追加快照，`foldRequestHeader(events)` 取最新快照重建 ⇒ **DSH 的请求侧确实记了模型**；另有一条**单独的** route metadata（`RouteMetadata.model`，`:196-204`）**仅在 provider/model/capacity 变化时**追加。见 §3 T1（已开票 **#226**） |
| 10 | 在 turn 边界 fork + 明确的拒绝对 + 血缘标记 | **已有（血缘落事件）** | **实测**：`session/forked` 的载荷是 `{parent_session_id, boundary_user_message_seq, fork_point_seq}`（+ 可选 `tail_summary`）——`session/fork.py:207-212`；fork 边界/拒绝语义见 ADR-0017。**血缘是 durable 事件**，不只在 header 里 |
| 11 | 句柄式单写者 + 有界 write-behind + `flush` 耐久屏障 + 崩溃后由读者补一个**有类型的** closer | **部分（形态不同）** | 我们有 JSONL 事件日志 + checkpoint + 孤儿回收（`run/failed` 的 `reason=orphaned`，`docs/adr/0033-*.md`）——"崩溃后界面不会停在假在途"这条**达到了**；DSH 那种"句柄式单写者 + 批量窗口 + 读者补 closer"的**机制**我们没有逐条对齐。**DSH 栏现场复核**（`docs/subsystems/persistence.md:106`，逐字）：`session/event` 是同步通知，后端按 session id 路由进**活跃写句柄的有界 write-behind 窗口**（不阻塞生产者；"持久化已保证每个 id 只有一个活跃写句柄"）；首个待处理事件开启**固定批处理窗口**、后续事件加入但**不重置截止时间**；`session/flush` 取消等待并**排空至完全停稳**，循环把它当作"领取下一个普通轮次之前的顺序与错误观察检查点"；后台写入被拒时**按序保留事件、暂停自动路径**，下一次 flush 重试并向调用方响亮拒绝。⇒ 机制描述成立，但**它替我们解决了什么可观测问题仍未被验证**（见 §4） |
| 12 | 有类型的错误码**原样跨线**（不许后端文案当协议） | **部分：`/api/memories` 刚做到（#225），其余仍是纯字符串** | 新做的：503 的 `detail = {code, message}`，前端按 `code` 分流（`web/src/lib/api.ts:1414 isMemoryFault`）。**其余的 503/409 家族仍是字符串 + 前端按状态码猜原因**：`web/src/lib/api.ts:947` 的 artifacts 503 是前端**自己**贴的 `'no-storage'`（后端没给码）。**DSH 栏现场复核**（`docs/api-gateway.md:127`，逐字）：`RemoteError` "carrying their own code, `session/not-found` or `session/agent-busy`, which the Gateway **encodes onto the wire unchanged**; only an unclassified throw folds into `gateway/internal`" ⇒ 除了"原样跨线"，DSH 还多两条我们没做的：**码是带命名空间的**（`session/…`、`gateway/…`）、**未分类异常折成一个显式的通用码**（不是让调用方按状态码猜）。见 §3 T2（已开票 **#227**） |

## 2. DSH 刻意不提供、而我们已有（这些是我们的扩展，不是缺口）

| DSH 不提供 | 我们 | 证据 |
| --- | --- | --- |
| 多租户 / 每用户身份（DSH 只有一个 host HMAC cookie） | **有** | `docs/adr/0009-multitenant-identity-isolation.md` + `identity/` + JWT 中间件 |
| 每工具 / 每资源粒度的自动批准 | **有** | 权限档位（`permission_mode`）+ 审批卡 |
| SSE 传输 | **选了 SSE** | ADR-0016 |
| 跨服务的 Operation Ledger / 副作用对账 | **有** | 不变量 #13/#14 + `docs/adr/0026-*.md` |

## 3. 候选工作项（当时**只登记**；网络恢复后已按 §14.12 开票）

判断依据：§8 Scope Lock（不顺手改）+ §4.4（只有用户/主开发/票面分配的任务才写代码）。
三条都**没有现场可复现的用户症状**，所以它们该走"开票 → 决策 → 实现"，不是当晚顺手改。
**开票结果：T1 → #226，T2 → #227**（T3 只登记，不开票）。

### T1（P3，可审计性缺口）`model/started` 不带「请求侧模型名」——已开票 **#226**

- **实测事实**：`model/started.data` = `{"step": N}`（`runtime.py:848-849`）；回答侧模型名只从
  **响应**元数据取（`_model_name_from_response`，拿不到就**不落键**，`runtime.py:216 / 922-924`）。
- **症状**：①接网关 / 自建端点（provider 不回显 `model`）时，durable 历史里**连「实际用了哪个模型」都没有**；
  ②任何情况下都查不到「这一轮**请求**的是哪个模型」——回退发生时只能靠 `model/fallback` 事件旁证
  （ADR-0033 §3 已记过「终态只指最后一次尝试」这个相邻问题）。
- **与 DSH 的差**：DSH 把模型选择放进**已记录的请求信封**（现场复核 via `docs/subsystems/session.md:169-204`；
  原转引 `session.md:148-170` 指的是同一节，行号按 clone 的 HEAD `0d1f500` 已订正）——回放/审计时
  请求与回答两侧都在。
- **为什么不当晚开工**：这会改事件载荷形状（`model/started` 加字段），属跨端契约变更；且当前症状是
  「信息不全」而非「信息错误」。按 §8/§4.4 应有票面 ⇒ **已开 #226**（票面含命名待决策项与"求请求侧名字"的验收）。

### T2（P3，与 #225 同形的潜伏耦合）其余 503/409 家族没有机读码，前端按状态码猜原因——已开票 **#227**

- **实测**：`web/src/lib/api.ts:947` —— artifacts 内容端点 503 ⇒ 前端**自己**构造
  `new ArtifactContentError('no-storage', detail, 503)`。今天成立，只因"503 在这个端点只有
  一个原因"。**后端哪天加第二个 503 原因，就会逐字重演 #225**（前端把故障说成"部署没配存储"）。
- **要抄的就是 #225 这一套**：后端给 `code`，前端只认码（不认状态码、不认文案）。
  **现场复核又多了两条可抄的**（`docs/api-gateway.md:127`）：DSH 的码带**命名空间**
  （`session/not-found`、`gateway/internal`）——正好避开我们 #222 §3 记过的"扁平命名空间里
  `cancelled` 这个字面量与任意异常类名共字段"的潜伏耦合；以及**未分类异常折成一个显式通用码**
  （`gateway/internal`），而不是让调用方按状态码猜。
- **为什么不当晚开工**：它是"未来会踩"的耦合，不是当前缺陷；且改的是**又一个**跨端契约定型
  （artifacts 家族），值得单独一票 + 两轴审查 ⇒ **已开 #227**。
- **最小验收**：后端该端点的 503 带 `code`；前端判别只读 `code`；红证 = 造第二个 503 原因时
  界面**不会**说错话（今天会）。

### T3（登记，不一定要改）重连用客户端游标 vs DSH 的服务端权威快照

- **现状**：`?after_seq=` 由前端持有（与 #3 的"launched 内联流"选择同源）。
- **DSH 的论点**：重连 = 开一条新流，服务端给完整快照 + 在途基线，**客户端没有可搞坏的游标**。
- **我们的实际风险**：#221 之后"迟到落定必须消费"已被钉住，游标搞坏**不会静默丢事件**；
  但架构上多了一层前端状态（代际、迟到、纠正）。
- **处置**：写进本文件备查，**不改**（要改就是重做流协议，与 ADR-0016/0030 冲突，收益不明）。

## 4. 本次**没有**现场核实的条目（留给下一次，别当结论用）

1. **第 11 条（崩溃恢复）**：真杀进程制造"半截 turn"，看我们的日志与界面长什么样。已知的只有
   `run/failed{reason=orphaned}` 这条兜底存在——DSH 那边是"读者补一个 typed `interrupted` closer"，
   两者**是否等价**没验过。**网络恢复后已确认 DSH 一侧的机制描述属实**
   （`docs/subsystems/persistence.md:106`），但**"它解决了我们哪个可观测问题"仍未被验证**——
   要验就得做一次真杀进程对照实验，本次没做。
2. ~~DSH 一侧的细节需要 clone 后抽查~~ → **已做**：`git clone --depth 1` 到临时目录（HEAD `0d1f500`），
   抽了 4 条引用（第 8/9/11/12 行）**逐字复核全部成立**，并把现场行号写回 §1 对应行。
   clone 落在 `%TEMP%`、**未进本仓**（抽查完即删，仓库不留外部源码副本）。

> **两条初判被实测推翻（提醒后来者别凭直觉下结论）**：原稿把「压缩不进事件流」（第 8 条）与
> 「fork 血缘没落事件」（第 10 条）都写成了缺口——**一查代码两个都不成立**：压缩是
> `compaction/start|end` 括号 + shadowed 原事件（`context/builder.py:136`），血缘是
> `session/forked` 的 `parent_session_id`（`session/fork.py:207-212`）。这套仓库已经有这些机制，
> 只是**名字与 DSH 不同**。相关行已按实测改写。

> **第 9 条是唯一"越查越像缺口"的一条**：它不是"我们没做"，而是"我们记在了回答侧、没记请求侧"
> （见 §3 T1）——这类差异只有把两边都读一遍才看得出来。

