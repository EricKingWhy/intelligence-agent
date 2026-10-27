# ADR-0056 — Memory 转向第 0 步：内容与操作状态的 owner 边界

- **Status**: Accepted（所有者裁决 1/2，2026-10-22 晚亲裁；本 ADR 只记录已裁边界，不实现任何一步）
- **Date**: 2026-10-22
- **Deciders**: 用户（裁决 1–7、16、20）+ 本 Agent（边界整理与点名）
- **Related**: Issue #1490（本 ADR 对应票）；后续施工票 #1491（V1 退役）、#1492（冻结备份）、
  #1493（身份/版本映射）、#1494（唯一提交 seam）、#1495（读侧切换）、#1496（SQLite/索引退役）、
  #1507（ADR-0014 facade 退役）；ADR-0042、ADR-0046（memory）、ADR-0014
- **Supersedes**: 只取代下列 ADR 中与内容/操作状态归属相关的条款（逐条见 §7），
  **不取代**任何整份 ADR，也不改写其 Phase 历史正文
- **非目标**：本 ADR 不选任何非 SQLite 持久格式、不删代码、不碰数据、不替 #1491–#1496 开工

---

## 1. Context

2026-10-19 所有者转向决定：长期记忆内容整体切 Markdown，SQLite 与 Milvus 只做 RAG。
2026-10-22 晚对 7 张记忆卡逐项裁决，其中两条直接定义了本 ADR 的边界（原文）：

> 裁决 1（card-01）：**A — 无例外**。作业/审批/设置等操作状态终态全部离开 SQLite；
> 过渡如需例外须另行显式批准，不采用长期例外。

> 裁决 2（card-02）：**A — 接受边界划分**。Markdown 只做记忆内容权威；
> 作业/审批/设置/墓碑等操作状态各自指定明确 owner（票面逐个点名），不搞统一单一存储，不推迟。

「删除 SQLite 表」不等价于「改成 Markdown 真相」：SQLite 里除正文外还有 records 的
ID/root/version/状态/来源、staged 审批 payload 与 pin、outbox 派生动作、tombstone（含内容/
来源 hash）、FTS 正文副本、settings 四开关、formation jobs（幂等键/lease/终态/结果）。
这些状态**无法从两个当前 Markdown 文件重建**，因此必须先点名 owner，才允许切换。

## 2. 边界：哪些事实归本 ADR 管

**在范围内**：Memory 域的内容与操作状态，即 §3 表的八类。

**明确不在范围内**（不因本 ADR 改变，也不得被读成「全仓禁 SQLite」）：

- Agent Harness 的 Operation Ledger、Checkpoint、SessionEvent/JSONL；
- Knowledge/RAG 域的 SQLite 与 Milvus（含 `knowledge_sources.tenant_id`、partition/filter）；
- 鉴权、Runtime permission、审批策略、扫描、provenance 的**机制本身**（裁决 7：一律不动）；
- 产品层的 Project 目录/会话分组（它是组织概念，不是记忆路由）。

## 3. owner 点名表

每行四要素：**职责 owner** / **恢复入口** / **数据格式决策去向** / **完成判据**。
「阻塞切换」= 该行未闭合前，不得进入 #1495 的读侧切换与 #1496 的旧库退役。

### 3.1 内容（Markdown 权威）

- **职责 owner**：Memory 内容提交 seam（`MemoryCommit` 深模块，由 #1494 建立）。
  它是当前内容与 revision 的**唯一**可变事实入口；任何 tool/service/formation/审批/批量删除
  都只向它交意图，不得自带第二套写序。
- **恢复入口**：新 Session 首读磁盘快照（现有 `memory/v2/markdown_store.py` 的
  `render_snapshot`）；重启后的提交侧恢复由 #1494 的 receipt 对账承担。
- **数据格式决策去向**：**已闭合**。Markdown 文件（MEMORY.md / USER.md）是唯一可变的当前内容
  事实，沿用仓内既有 MarkdownStore 结构；本 ADR 不新增格式、不把任何操作状态写进正文。
- **完成判据**：当前内容唯一可变事实在文件；SQLite 独有内容不被当前检索返回（#1495 AC1）；
  静态图 + 真实入口证明不存在第二内容写者（#1494 AC5）。
- **状态**：**已闭合**（owner 已由裁决 1/2 确定）；切换仍依赖 #1494/#1495 落地。

### 3.2 job / lease / 幂等

- **职责 owner**：Formation 状态职责——后台形成作业的排队、租约、幂等键、阶段与终态。
- **恢复入口**：现状为 `memory/v2/jobs.py` 的 NOT_TERMINAL 恢复扫描；新 owner 的恢复入口
  必须由 #1494 设计并证明（重启续跑或归档、lease 丢失不 commit、不重放有副作用操作）。
- **数据格式决策去向**：**未闭合** → #1494 的正式设计（本 ADR 不选格式）。
- **完成判据**：同一 idempotency key 至多产生一次逻辑更新；lease 丢失零 commit；
  in-flight / recoverable 集合为空，或每项都有明确恢复 owner 与测试过的恢复路径。
- **状态**：**阻塞切换**。

### 3.3 approval / pin

- **职责 owner**：Approval 状态职责——待审 payload、pin 的 revision、claim/过期回收、审批结果。
- **恢复入口**：现状为 staged 的 CAS 路径；新 owner 入口由 #1494 设计（重启能核实文件 revision
  后置 approved，或保留为 reconcile）。
- **数据格式决策去向**：**未闭合** → #1494（本 ADR 不选格式）。
- **完成判据**：pending 仍 pending、不自动批准；approved 只能在对应 Markdown digest 核验后写入；
  pin 缺失或版本不匹配时拒绝并留待人工重审。
- **状态**：**阻塞切换**。

### 3.4 settings

- **职责 owner**：本机设置职责——extraction / recall / fallback / write_approval 四开关。
- **恢复入口**：启动时读取当前生效设置；新 owner 入口由 #1494 设计。
- **数据格式决策去向**：**未闭合** → 格式归 #1494；旧 `PK(tenant_id, user_id)` 的键归并
  逐项裁决归 #1493。**不借迁移新增身份路由**（裁决 7：禁换名 `owner_id` 复制三元路由）。
- **完成判据**：四开关各有明确新 owner 且原值可核；区分「缺行用默认」与「显式设默认」；
  旧键不得静默折叠。
- **状态**：**阻塞切换**。

### 3.5 tombstone

- **职责 owner**：删除抑制职责——墓碑记录的删除理由、内容/来源 hash、过期策略。
- **恢复入口**：治理删除与检索注入路径读墓碑以拒绝旧输入重放；新 owner 入口由 #1494 设计。
- **数据格式决策去向**：**未闭合** → #1494（本 ADR 不选格式）。
- **完成判据**：删除后旧输入重放继续被拒绝；墓碑未达既有过期策略前不得清理；
  缺失或旧 version 不可重建时保留为隔离记录，不编造墓碑。
- **状态**：**阻塞切换**。

### 3.6 receipt（提交回执 / reconcile）

- **职责 owner**：提交恢复职责——operation id、每目标 previous/new digest、
  PREPARED / PUBLISHED / COMMITTED / NEED_RECONCILE 状态机（命名待 #1494 定）。
- **恢复入口**：重启后按 operation id + old/new digest 判定「未发布 / 已发布 / 冲突」；
  #1494 建立。
- **数据格式决策去向**：**未闭合** → #1494 正式设计。本 ADR 只钉一条语义：
  不确定即 NEED_RECONCILE，不盲重跑、不把部分成功伪装成全成功。
- **完成判据**：kill 覆盖「文件发布前 / 替换后 receipt 前 / 审批状态更新前 / 两文件部分发布」
  四个窗口均可对账；错误响应携带 operation id。
- **状态**：**阻塞切换**。

### 3.7 history / provenance

- **职责 owner**：只读迁移档案与映射职责——旧 UUID/root/version、来源事件与 Session 关联、
  旧 tenant/user/project 作为**迁移来源标签**（不再作为路由）。
- **恢复入口**：离线只读档案（#1492 的一致性归档，保留期 2027-10-23）+ 映射表（#1493）。
  二者都不在运行路径上。
- **数据格式决策去向**：裁决 6 A 原先要求加密只读备份；用户于 2026-10-26 明确裁决
  #1492 无需加密，改为明文 tar 离线只读归档。此修订只替换加密要求，不改变保留期限、
  离线/只读边界或到期销毁须另行裁决的要求；哈希校验不提供来源认证或防恶意篡改。
- **完成判据**：每个旧源项有明确归宿（映射 / 档案 / 显式冲突清单），逐行 hash、旧 ID 与
  provenance 可核；冲突未关闭不得删旧数据。
- **状态**：**不阻塞切换**（只读档案不参与运行），但**阻塞第 8 步销毁**。

### 3.8 派生索引与 outbox（可重建派生态）

- **职责 owner**：检索派生职责——FTS / 进程内 index / outbox 期望队列。它**不拥有任何当前事实**，
  只允许由 Markdown 重建、可随时丢弃；不得决定权限、删除、召回版本或事实是否存在。
- **恢复入口**：从当前已批准 Markdown 重建。**已登记缺口**：ADR-0042 D7.1 描述的
  `scripts/rebuild_memory_v2_index.py` 已不存在，`MemoryV2IndexRelay.rebuild_from_authority`
  当前零调用者——重建入口需要 #1496 单独裁清。
- **数据格式决策去向**：派生态格式不承载事实，**不影响切换**；`index.py` 与 outbox 合同的存废
  由裁决 16 钉死在记忆转向第 7 步同批单独裁（#1496）。
- **完成判据**：清空可重建的内存检索后，同集合/同 revision 可恢复（#1495 AC4）；
  启动/提取/审批/治理/重启均不创建 Memory DB（#1496 AC1）。
- **状态**：**不阻塞切换**，阻塞 #1496 的删除。

## 4. 阻塞切换清单

以下五项未闭合前，记忆转向**不得**进入第 5 步（读侧切换）与第 7 步（旧库退役）：

1. job / lease / 幂等的非 SQLite 持久 owner 与重启恢复验证（§3.2）；
2. approval / pin 的非 SQLite 持久 owner 与 CAS 恢复验证（§3.3）；
3. settings 四开关的新 owner 与原值映射（§3.4）；
4. tombstone 的删除抑制事实 owner 与过期策略载体（§3.5）；
5. receipt / reconcile 的状态机与恢复入口（§3.6）。

**这五项不是「例外」，是「未完成」。** 未完成就保持切换未完成——不为赶进度删掉审批、幂等或
墓碑保障，也不把过渡期的 SQLite 表说成符合终态。

## 5. 红线（照录裁决原文，不得由工程侧改写）

- **终态无 Memory-SQLite 例外**：作业/审批/设置等操作状态终态全部离开 SQLite（裁决 1）。
- **过渡例外须所有者另行显式批准**范围、适用表、期限与退出验收；**本 ADR 不自设任何长期或
  默认过渡例外**（裁决 1 + C 卷宗 §5.3 分歧 1）。
- **「多个 owner」不等于「多个 SQLite 例外」**：owner 数量不构成保留 SQLite 的理由（裁决 2）。
- **未闭合项必须写成「阻塞切换」**（§4），不许含糊带过。
- **具体非 SQLite 持久格式在正式设计闭合后才能切换**；本 ADR 只点名 owner 与决策去向，
  不替后续票选格式。
- **不把操作状态塞进 Markdown 正文**：Markdown 只承载内容。
- **不扩大为「全仓禁 SQLite」**：Core Ledger / Checkpoint / SessionEvent / RAG 不在本 ADR 范围。

## 6. 六项后继保障 → 后继 AC 落点

| 保障 | 当前载体 | 后继 AC 落点 |
|---|---|---|
| 统一 Tool 路径 | 直接 tool、service、formation、审批、批量删除分叉写入 | **#1494** AC1（全部经同一 commit）、AC5（静态图+真实入口证明唯一写者） |
| 扫描（delimiter / secret / threat） | 写入前扫描、读盘后逐条扫描 | **#1494** AC4（来源/扫描/pin/revision 拒绝零落盘） |
| 审批 | staged + pin + 显式批准 | **#1494** AC1（可信用户动作不被无依据二次审批、模型/自动写仍守批准策略）、AC2 |
| 来源（provenance） | 记录来源事件/Session；旧身份标签 | **#1493** AC1（逐行 hash/旧 ID/provenance 可核）+ **#1494** AC4 |
| 幂等 | formation idempotency key + lease | **#1494** AC4（同 key 只一次逻辑更新、lease 丢失零 commit） |
| 墓碑 | tombstone 拒绝旧输入重放 | **#1494** AC2（tombstone 有非 SQLite owner）+ **#1495** AC4（墓碑重放拒绝）+ **#1496** AC3（墓碑抑制） |

横向不变量（不新增 AC，只约束上述票不得破线）：

- **#1491**（V1 退役）不得删混合文件的活断言（RAG/Skills/Context/删会话后 append）；
- **#1492**（冻结备份）必须覆盖 §3 全部八类，含 staged/jobs/tombstone/settings 与 FTS 正文副本；
- **#1507**（ADR-0014 facade 退役）承接裁决 20 对 ADR-0014 决定 7/9 的重开。

**本波不许动工**：#1491–#1496 与 #1507 均只做文档衔接，不在本票范围。

## 7. 十份 Memory ADR 的处置索引

逐条当前适用关系写在各自 ADR 的头部「当前适用关系」块；决定级重开写在对应决定下方。

| ADR | 处置 |
|---|---|
| 0008 | 保留双层 Capability/Context Provider 与「可选故障不拖垮 Core」；子决策 1/3/4 的 scope、LangMem 默认与存储组合被取代 |
| 0009 | Memory 的 tenant/user/project 路由条款被取代；IdentityContext 机制与认证/RAG 身份残留保留 |
| 0024 | Memory 专属五件套 seam 标为历史；通用 Capability seam、生命周期归属、未知名硬失败保留 |
| 0026 | 降为旧档案解释；新读写删走统一 commit/receipt，不复制 outbox；「丢弃告警」不构成迁移授权 |
| 0031 | 工具名/返回形状/错误码/注入标记保留并需显式定义；旧 ID/DB 检索路径收窄 |
| 0042 | D2–D8、D12、D7.1 决定级重开；D1 信封、D6 语义三分、D11 非目标保留；D9/D10 指针随 owner 变更核对 |
| 0043 | D13/D15 的项目身份路由被取代；别名/单射/敏感 9 类/R5 显式标记保留 |
| 0044 | Context 中「SQLite 与派生索引仍是业务数据所有者」被取代；metadata-only、脱敏、来源、质量门槛保留 |
| 0046-memory | D1 的「V2 独占、无 V1 fallback」保留，「SQLite 唯一记录权威 / Milvus 派生」被取代；D2–D7 作为 #303 历史执行保留且其销毁授权不可复用 |
| 0049 | 两 scope 不再作身份路由；不新增 task/agent 身份层；Project 目录分组独立保留 |

## 8. 方案依据（照 C 卷宗 §5.1 原核实状态引用，不升级表述）

- **ADAPT**：仓内已存在的 MarkdownStore（文件内容、expected revision、扫描、原子单文件替换）。
- **参考 Hermes**：MEMORY/USER 有界文件 + 会话开始冻结注入 + 显式 write-approval。
  它验证「文件内容权威 + 可审阅写入 + session cache 分离」这一机制，**不证明**本仓旧 ID/墓碑/
  formation 事务迁移已正确，也不要求照搬上游 profile/patch 合同。
- **参考 AWS transactional outbox**：跨介质双写有失败窗口、交付可能重复、消费者需幂等与排序；
  本 ADR 只借其幂等/reconcile 判据，**不引入 AWS 服务**。
- **裁决卡已核实先例**（card-01）：Maildir（状态编码在同一文件名标志位、全程无数据库）、
  Git（全部数据在 `.git` 目录内、无数据库）——支撑「操作状态也可以离开数据库」；
  Django Deprecation Timeline（弃用必有具体移除期限）——支撑「过渡必须有期限」。
  三者均为**已核实**，但都不是「Markdown 内容权威 + 作业/审批状态永久留在 SQLite」的同形实例。
- **无代码复制**；未来实质 Port 须查上游 license。

## 9. 未决与不做

- 本 ADR **不选**任何非 SQLite 持久格式（§3 各行的「未闭合」即是待 #1494 闭合的决策去向）。
- **不删任何文件、不改任何产品代码、不碰任何数据**；旧数据销毁未授权（仅批离线只读归档至
  保留期，销毁另行裁决）。
- **不抢跑** #1491–#1496 与 #1507 的任何一步。
- 尚未开票的后续项：§3.8 的重建入口缺口（ADR-0042 D7.1 的运维命令已不存在）需在第 7 步前
  单独处置；本 ADR 只登记，不在此开票。
- **工程规格承接说明（C 卷宗 §4.3）**：下列 Engineering Spec / PRD 章节在转向后仍带**旧权威**
  （SQLite 是 Memory 唯一内容 owner）或**旧 scope**（tenant/user/project 路由）语言。它们的修订
  **由裁决 1/2/7/16/20 的已裁范围承接授权，不在本票（#1490）执行**，须在记忆转向第 7 步
  （#1496）**之前**由单独工作项完成；已按所有者「发现可开票问题先单独开票」的规矩开票
  **#1543**（创建时未打 `in-progress`，尚未开工）。逐组点名（8 行照 C 卷宗 §4.3 原表，未概括；
  `SPEC_ROOT` = `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/`，不是根 `docs/spec/`）：
  1. `SPEC_ROOT/06_CONTEXT_ARTIFACT_MEMORY.md` §6 :108–166（默认 LangMem、多原语与
     scope/relevance/recency 职责）；
  2. 同 Spec §7 :168–178（`user_global`/`project` 两 scope 与扩 scope 规则）；
  3. 同 Spec §8–9 :180–197（LangMem 默认/替换验收、文件检索/治理、故障/恢复准入）；
  4. `SPEC_ROOT/00_PROJECT_VISION.md` §4 与 `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md`
     §4 LangMem :91–95（旧默认 LangMem 与新 Hermes 文件实现的适用关系）；
  5. `SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md` Phase 6 :109–122（旧 LangMem/scope 交付与 Gate，
     不追溯改写已完成历史）；
  6. `SPEC_ROOT/01_SYSTEM_ARCHITECTURE.md` §5–6 与
     `SPEC_ROOT/07_STORAGE_PERSISTENCE_RECOVERY.md` 的独立 Ledger/Checkpoint 边界
     （「哪种事实由谁拥有」，不把多载体一概判双主）；
  7. `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md` §4.2–4.3 :117–126、§6.1–6.6 :212–302
     （profile×scope、身份信封/API、SQLite 唯一 owner）；
  8. 同 PRD :32/:72/:96–97、§5 删除 :170、§7 与验收 :316/:333/:352–353/:372–379
     （旧权威/形成作业/召回/cutover AC 与新提交语义对账）。

  各组「应保留的不变量」见 C 卷宗 §4.3 右列（Capability+Context Provider、完整保存≠完整注入、
  不由模型选权限/来源、optional failure 不拖垮 Core、Phase 历史不改写、SessionEvent/Operation/
  Artifact 各自 owner、类型/来源/审批/删除抑制语义、receipt 与 failure/recovery 证据），修订时
  不得一并删除。本 ADR 与 #1490 均不改这些文档。
  逐条清单见 #1543 正文（该票已逐行照录八组保留项）。
- **C 卷宗 §4.2 的条件项（只登记，不处置）**：① **ADR-0013 条件重开**——RAG 用单租户 partition
  与 SQLite source registry；只有所有者要求全产品去掉 tenant 列时才单列 RAG 数据映射，本转向只移除
  Memory 路由、不动其数据，也不把 Knowledge 专属协议/collection 与 Memory 合并；② **ADR-0020b
  核对并保留**——provider 名 `memory`、用户可选 provider 集合、`None` 与 `[]` 差异继续适用，只需
  更新指向已退役 V1 类的例子/测试夹具，不为改存储重开整个 Context 消费合同；③ **`GLOSSARY.md`
  :259–305 正名**——`IdentityContext`、`MemoryCapability`、`MemoryScope`、`MemoryEntry`、
  `MemoryStore`、`Extractor`、`LangMem`、`Outbox`、`retrieve_memory`/`remember_this` 需同步正名，
  已被共享引用的定义先保留适用上下文（删 V1 实现 ≠ 删全部概念）。三项均属后续需处置项，本 ADR
  只登记、不处置，也不据此扩大本票范围。

## 10. 验证约定

本 ADR 的可机械核验部分：八类 × 四要素齐备；§4 五项标「阻塞切换」；全文无自设 SQLite 例外或
默认过渡期；§6 六项保障各有后继 AC 落点且落点票均处于 OPEN。验证方式见 #1490 的完工回报。
