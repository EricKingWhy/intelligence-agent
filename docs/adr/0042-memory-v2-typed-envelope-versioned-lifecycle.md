# ADR-0042 — Memory V2：类型化信封、版本化生命周期与派生索引

- **Status**: Proposed（契约已实现并实测通过；V1/V2 并存期，cutover 归 MEM-V2-7）
- **Date**: 2026-09-23
- **Deciders**: 用户（PRD 契约逐条冻结，2026-09-22）+ 本 Agent（机制设计）
- **Related**:
  - Issue #297 / MEM-V2-1（父票 #296）
  - `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`（下称 PRD；§4 产品模型、§6.1 信封、§6.6 持久化归属、§7 固定/有界/自由）
  - `docs/research/2026-09-22-production-long-term-memory-systems.md`
  - ADR-0008（Memory Capability 架构）、ADR-0009（多租户身份隔离 / 5 层 scope 枚举）、
    ADR-0024（Memory provider seam）、ADR-0026（硬删 + outbox 操作类型 + last-write-wins）、
    ADR-0031（`retrieve_memory` / `remember_this`）
- **Supersedes**: **无**。本 ADR 在 MEM-V2-7 之前**不取代任何旧决策**——V1 的行为逐字保留。
- **将于 MEM-V2-7 supersede 的旧决策**：§D10 逐条列出（这是 AC8 要求的显式登记）。
- **Refines**: ADR-0008 子决策 1（"scope 只实现 USER + SESSION"）——V2 新开
  `user_global` / `project` 两档，**但不改动 V1 的 `USER` / `SESSION` 行为**。

---

## 1. Context

### 1.1 为什么现在做 V2

PRD 的出发点是三条已被观察到的缺陷，不是"想要更漂亮的 schema"：

| 缺陷 | 现状证据 | PRD 要求 |
| --- | --- | --- |
| 记录没有类型 | V1 `MemoryEntry` = generic `content + metadata`，语义全靠字符串 | §4.1 三种 kind，各自**固定字段**的 payload |
| scope 冒充长期记忆归属 | V1 只有 `USER` / `SESSION`；`SESSION` 被当成一个"档位" | §4.3 `user_global` / `project`；**SESSION 不是长期记忆的 scope** |
| 生命周期不可表达 | 覆盖写 + 硬删，没有版本历史，没有"失效但仍可读" | §5.4 / §6.1 version、superseded、invalidated |

### 1.2 与既有决策的冲突面（为什么需要一份新 ADR）

| 旧决策 | 冲突点 | 本票怎么处理 |
| --- | --- | --- |
| ADR-0009：5 层 `MemoryScope` 枚举，V1 只实现 `USER` + `SESSION` | V2 的合法值是 `user_global` / `project`，与 V1 集合**不重合** | 不复用同一枚举：V2 自带 `MemoryScope`（同名不同义，见 `memory/v2/types.py` 模块文档）。V1 枚举一个字节都不动 |
| ADR-0026 D4：「刻意不加 `updated_at` / `version`」 | V2 的核心不变量就是 `version` 单调 | 旧结论**的触发条件已成立**（它自己写了"将来若要按更新时间排序，那时再加列 + 迁移"）。但**加列这件事留给 MEM-V2-7**：V2 用**新表**承载，V1 表结构不动 |
| ADR-0026 D1：`update` = `upsert by id`；`forget` = 硬删无墓碑 | V2 的 `update` 派生新版本并保留历史；`invalidate` 保留内容 | V2 的 `update` / `invalidate` 是**新方法**，与 V1 的 `update` / `forget` 并存。硬删与墓碑归 MEM-V2-7 |
| ADR-0026 D3：outbox 的"脏 `operation` 按 upsert 自愈" | V2 outbox 是**新表**，可以带 `CHECK` 约束 | V2 outbox 装 `CHECK (operation IN ('upsert','delete'))`，不继承 D5 的"补列迁移"路径（新表没有老库形状） |
| ADR-0024 D1：provider 边界 = 整个 `MemoryComponents` 包 | 新增记忆操作面会不会变成"第二套 provider 体系"？ | **不冲突，且必须遵守**：V2 活在 `builtin` provider 内部；对外新增 `MemoryV2Capability` Protocol 只是 `builtin` 内部的端口，不新增 provider |
| ADR-0031：`retrieve_memory` / `remember_this` / `forget_memory` 指向 V1 capability | V2 有新的操作面 | 本票**不动**这三个工具（Must Not Do）。它们指向 V2 的时机归 MEM-V2-7 |

### 1.3 为什么是 expand 而不是 cutover

用户当前有**真实记忆数据**在跑。ADR-0026 的硬删语义、V1 的 `USER`/`SESSION` 语义、
LangMem 的 run-end 写回全都在生产路径上。一次性切换会同时改动四件事（表结构、scope 语义、
生命周期语义、写回时机），任何一件出问题都不可归因。

⇒ 本票只交付**一条完整的 V2 通路**（create / read / update / invalidate / search），
V2 写入**不接** run-end 自动写回（即 V2 目前只有显式调用者），破坏性与切换归 MEM-V2-7。

---

## 2. Decision

### D1 — 信封 = 判别联合（kind × payload），全程 fail closed

`MemoryRecordV2` 的 `payload` 是 `Annotated[Semantic|Episodic|Procedural, Field(discriminator="kind")]`：

- 未识别的 `kind` 在**解析期**失败，不会落到任何分支；
- `kind` 字面量在信封与 payload 上各写一次，一致性由 after-validator 强制，不靠调用方自觉；
- 契约模型一律 `ConfigDict(extra="forbid", frozen=True)`——"模型输出里多带一个字段"不能静默变成合法记录；
- `schema_version` 是 `Literal[2]`：**未知版本必须 fail closed**，否则 V3 的载荷会被 V2 的读者当成合法数据。

### D2 — 内容字段与生命周期字段分离：`MemoryDraftV2` vs `MemoryRecordV2`

调用方提交的是 `MemoryDraftV2`——**只有内容**，没有 `id` / `root_id` / `version` /
`tenant_id` / `user_id` / 时间戳。这些字段由存储层从可信上下文补齐（PRD §6.2：
"Runtime replaces all identity fields with trusted request/session identity"）。

理由：校验规则（§4.1/§4.2/§4.3 的组合规则）必须在**写入前**就生效。如果只有完整信封做校验，
调用方就必须先瞎编一个 id/version/时间戳才能让记录过校验——那等于把服务器拥有的字段
变成调用方的输入。两半共用 `_MemoryContentFields`（含 `_check_common_contract`）。

### D3 — tier 与 scope 是两件事，各有硬规则

- `tier ∈ {profile, collection}`（§4.2）：**访问层级，不是第二份真相**，两者共用同一批记录与生命周期。
- `profile` **只允许 `user_global` 的 `semantic`**（§4.2）。理由：profile 是"每次可自动注入的
  紧凑画像"，把 project 作用域的事实注入到所有会话就是越界。
- `scope ∈ {user_global, project}`；`project` 必须带 `project_id`，`user_global` 必须不带
  （两者互为反证，任一侧单独校验都能被绕过）。
- `source_type` 中 **`user_edit` 是唯一允许空 provenance 的档位**（§6.1 原文：`source_event_ids`
  "may be empty **only** for direct UI edits without a session"）。`automatic`（推断出来的）与
  `explicit_command`（会话里下的命令）都指得出事件，也必须指得出——空 provenance 的记忆
  无法被审计，也就无法被撤回。

### D4 — 身份只来自可信上下文；读写两侧的失败语义刻意不同

`TrustedMemoryIdentity(tenant_id, user_id, project_id)` 由可信入口解析（请求上下文 / 会话绑定），
**请求体里的身份一律不信**（R1 末条）：

- 集合类方法（`get` / `list_versions`）：不可见 = **伪装成不存在**（`KeyError`）。
  区分"不存在"与"不是你的"等于把"别人的 id 是否存在"告诉调用方。
- 写类方法（`update` / `invalidate`）：**如实拒绝**（`PermissionError`）。
  入口层据此给 403 而不是 500。
- 项目作用域写入额外核对 `record.project_id == trusted.project_id`，不匹配抛
  `UntrustedIdentityError(PermissionError)`——**继承 `PermissionError` 是刻意的**，与 V1
  `SqliteMemoryRecordStore.delete` 的既有口径同源。

### D5 — 版本生命周期的不变量在数据库层，不在调用方纪律

- 每条逻辑记忆有稳定 `root_id`，`version` 单调递增；
- **至多一条 active**，由 SQLite **部分唯一索引**强制：

  ```sql
  CREATE UNIQUE INDEX memory_v2_one_active
      ON memory_v2_records(root_id) WHERE status = 'active';
  ```

- `update` 在**同一事务**里先让出 root_id 上的 active 槽位（旧版置 `superseded` +
  记 `superseded_by`）再插入新版本；
- **版本号由存储层决定**（`previous.version + 1`），调用方给不了——R3 要求单调性是存储的
  保证，不是调用方的纪律；
- `update` **不允许改写 scope / project**：允许 draft 改写等于允许"把一条 user_global 记忆
  改成项目记忆"或反向——两者的可见性集合不同，那是越权而不是编辑；
- 并发写同一版本时，败者必然失败（先读到 active 已置 superseded → `_require_active` 抛
  `ValueError`；或撞上唯一索引 → 转成 `ValueError`），且败者事务整体回滚，
  胜者写下的 `superseded_by` 不会被改脏。两条路径都是 `ValueError`，所以结果不依赖调度顺序。

### D6 — `invalidate` 保留内容，不是删除

`invalidate` 把 active 置为 `invalidated`，**保留内容与历史**（§5.4.2）。它只是把记录移出
active 检索面。V2 **没有硬删入口**——墓碑/删除 API 是 MEM-V2-7 的 Must Do，本票的 Must Not Do。

`MemoryStatus.DELETED` 作为 §6.1 tombstone 契约的**占位**存在，本票不产生它。

### D7 — SQLite 权威，Milvus 派生；索引失败永不回滚已提交事实

- 所有写方法都是"**记录行 + outbox 意图**同一 `BEGIN IMMEDIATE` 事务"。outbox 写失败时
  记录行不许单独存在（否则既查不到、也无法修复索引）。
- **新表**：`memory_v2_records` + `memory_v2_outbox`。不改造 V1 的
  `memory_records` / `memory_outbox`——就地改造会让"V2 写入"与"V1 读取"在同一列上语义分叉，
  而 #297 要求 V1 继续可运行。
- outbox 语义沿用 ADR-0026 D3 的结论（**每个 id 一行 = 索引期望状态**、同 id 后写覆盖前写、
  自足路由列），但**带 CHECK 约束**（见 §1.2 表）。
- `PendingMemoryChangeV2` 是**自足**的：删除变更没有"当前记录行"可依赖，relay 必须在记录行
  已改状态之后仍能正确删向量，所以路由列取 outbox 自己的值，JOIN 只用来取"要索引的内容"。
- `revision` 是乐观令牌：relay 同步的是哪个 revision，就只允许 ack 那个；
  **revision 变化同时重置重试预算**（新版本不是旧毒丸的证据）。
- `acknowledge` 失败与索引失败**分开归因**：前者意味着"我们连账都记不上"，后者意味着
  "外部索引不可用"，两者排障方向完全不同。
- 连续 `MAX_CONSECUTIVE_FAILURES = 5` 次后进入**本进程死信**（不再空转，outbox 行保留可观察）。
  计数器活在进程内存里 ⇒ 重启自愈。

### D8 — 检索命中必须回 SQLite 复核（AC7）

`resolve_active_hits` 是检索路径上的**必经函数**，不是调用方的可选步骤。三重过滤缺一不可：

1. 记录在 SQLite 里必须**存在**（挡伪造 / 过期命中）；
2. 状态必须是 **active**（superseded / invalidated 不得被检索到 → AC5）；
3. 归属必须落在调用方的**可信身份**内（`store.get` 的授权语义 → 跨用户 / 跨项目 / 跨租户）。

理由：Milvus 是派生索引。少这一步，"索引里残留一条已失效 / 跨用户的向量"就等于
"把别人的内容注入本轮对话"。

### D9 — provider 边界沿用 ADR-0024，只在 `builtin` 内部新增端口

- 对外新增 `MemoryV2Capability` **Protocol**（7 方法：`create` / `update` / `invalidate` /
  `read` / `list_active` / `versions` / `search`）。这是 V2 的 provider-neutral 面：
  调用方（工具、API、后续 formation 流水线）只依赖这七个方法，不知道背后是 SQLite + Milvus
  还是别的实现。**边界是 Protocol，不是 `MemoryV2Service`**——后者只是当前唯一实现。
- **AgentRuntime 不新增任何 provider 专属分支**（Must Not Do）。
- `MemoryV2Service.search` 刻意**不**顺手 flush 索引：写路径与索引收敛是两件事，混在一起会让
  "检索一次"变成"写一次数据库"（并发下还会与 relay 抢锁）。这与 V1 既有语义一致。
- ADR-0024 D1 的 provider 边界（整个 `MemoryComponents` 包）**不变**：
  V2 是 `builtin` provider 内部的东西，`provider_name` 不因此改变。

### D10 — 将在 MEM-V2-7 被取代的旧决策（**AC8 的显式登记**）

下列条目**今天仍然有效**，V1 逐字按原样运行。它们是"clean-slate cutover"时要处理的对象，
MEM-V2-7 必须**在票面上**显式取代它们（而不是"顺手改掉"）：

| # | 旧决策（锚点） | cutover 时的处置 | 为什么必须显式取代 |
| --- | --- | --- | --- |
| 1 | ADR-0009 子决策 2 + SESSION 隔离补充：V1 的 `MemoryScope` = `USER` / `SESSION`，SESSION 绑 `session_id` 做 namespace 隔离 | V2 的 `user_global` / `project` 取代它成为长期记忆归属；`session` 退出长期记忆 scope | 这不是"加两个枚举值"，是**归属语义的替换**：SESSION 的合法值被删除，`session_id` 从授权维度降级为 provenance（R6） |
| 2 | ADR-0026 D4：「刻意不加 `updated_at` / `version`」（附条件："将来若要按更新时间排序，那时再加列 + 迁移，并同步更新此结论"） | **条件已成立**。cutover 时为 V1 记录补版本，或用 V2 表承载后废弃 V1 表 | 旧 ADR 自己写明了触发条件与"同步更新此结论"的义务——本 ADR 即该结论的更新点，但**改动本身**归 MEM-V2-7 |
| 3 | ADR-0026 D1：`update` = `upsert by id`（同 id 覆盖，无历史） | 由 V2 的"派生新版本 + 旧版 superseded"取代 | 同一动词的**语义反转**（覆盖 vs 追加）。两条语义并存会让"更新了一条记忆"有两种可观察结果 |
| 4 | ADR-0026 D1 + Consequences ①：`forget` = **硬删无墓碑** | 引入 §6.1 的 tombstone 契约（`deleted` 状态 + 只留 hash）与最终删除 API | 用户当初选硬删是在"没有 tombstone 契约"的前提下做的；PRD §6.1 新增了该契约，取舍前提变了 |
| 5 | ADR-0026 D3 / D5：outbox 的"脏 `operation` 按 upsert 自愈"+ 就地 `ALTER TABLE` 补列迁移 | V2 outbox 有 `CHECK` 约束，不需要该兜底；cutover 后老库迁移路径退出 | 兜底是**无 CHECK 老库上的唯一防线**，它随老库一起退役。留着会让"未识别的 operation 值"在 V2 里也变成可接受输入 |
| 6 | ADR-0031 D2/D3/D6（§3 的工具契约面）：`retrieve_memory` / `remember_this` / `forget_memory` 指向 V1 capability 与 `MemoryScope.USER` | 三个工具的注入语义与调用面改为 V2 | 工具是对模型的**契约**，它们的 `description` 与环境行为必须与底层一致；否则模型按旧描述调用会打到已退役的语义。D6 正是"描述与底层不一致"的既有实例 |
| 7 | ADR-0008 子决策 1：V1 最小闭环（`update`/`delete` 留接口）+ 子决策 2 的三原语面（`store`/`recall`/`search`） | 由 V2 的七方法面取代 | ADR-0026 已部分 refines（动词纳入交付），但**面本身**（三原语 vs 七方法）到 cutover 才收敛 |
| 8 | ADR-0008 子决策 3/4 中"LangMem 通过 `BaseStore` 适配我们的存储" | LangMem 在 formation / consolidation 侧的去留，由 MEM-V2-2 决定后在此收口 | 本票**不动** LangMem（V2 尚未接自动写回）；此处登记是为了让 MEM-V2-7 不能以"没人提过"为由跳过 |

> **反向说明（本 ADR 明确不取代的）**：ADR-0024 的 provider 边界、ADR-0010 的
> CapabilityError 四码、ADR-0021 / ADR-0020b 的 Context Provider 消费模型，在 cutover 后**继续有效**。

### D11 — 非目标（明确排除）

- 不做最终删除 / tombstone API（MEM-V2-7）。
- 不做 Web UI（MEM-V2-5）。
- 不做后台模型抽取流水线（MEM-V2-2）。
- 不做最终召回排序 / 质量评估（MEM-V2-6）。
- 不引入 Graphiti 或图数据库。
- 不让 Milvus 成为事实源。
- 不激活 V2 的 run-end 自动写回。
- 不改 V1 的任何行为、不删任何真实记忆数据。

---

## 3. Rationale

### 3.1 为什么用新表而不是扩 V1 的表

V1 的 `memory_records` 由 #156/#158/#159 冻结，`MemoryEntry`（generic content + metadata）
与 PRD §6.1 的信封形状不兼容：V1 没有 kind / payload / tier / scope / version / status /
provenance。就地改造会让"V2 写入"和"V1 读取"在同一列上语义分叉。

代价是短期双表并存；换来的是**两条路径各自完整**，cutover 才是一次干净的替换，
而不是一串"边改边跑"的高风险迁移。

### 3.2 为什么"至多一条 active"要交给数据库

应用层自觉在并发下必然失守（TOCTOU：先读状态再写）。部分唯一索引让"两条 active"在
**插入那一刻**被数据库拒绝，不依赖任何调用方纪律。判别性测试 =
`tests/memory/v2/test_v2_store.py::test_concurrent_updates_leave_exactly_one_active_version`
（两个写者基于同一版本并发派生，断言恰好一个成功、一个 `ValueError`、
`list_active` 只剩胜者、`superseded_by` 未被败者改脏）。

### 3.3 为什么 `MemoryDraftV2` 与 `MemoryRecordV2` 分成两个模型

见 D2。补充一条实测理由：如果只有一个模型，测试里为了构造"非法 draft"就必须先造一个
完整合法记录再改坏它——那会掩盖"非法输入在**写入前**就被拒绝"这件事，AC2 的
"without a partial SQLite or Milvus write" 就无从断言。

### 3.4 为什么 relay 不做成常驻 asyncio 任务

本票只交付**可调用的收敛原语**（`flush()`）。常驻任务属装配层生命周期（ADR-0024 D6
明确 `initialize()` / `close()` 归 provider），而 V2 目前没有装配进 `builtin` provider。
把它做成常驻任务会让"V2 尚未接线"变成"后台已经在跑"，与 Must Not Do 冲突。

---

## 4. Consequences

### 正面

- 记忆有了**可退役**的生命周期：`superseded` / `invalidated` 使"旧事实不再出现"与
  "旧事实仍可审计"同时成立（V1 的硬删做不到后者）。
- 检索面的安全性变成**可判定的**：`resolve_active_hits` 一个函数覆盖 AC5 + AC7，
  不依赖每条调用路径各自记得做授权。
- "至多一条 active"与"索引最终一致"都有**数据库层 / 事务层**的保障，不靠调用方纪律。
- V1 零改动 ⇒ 升级风险为 0，cutover 的爆炸半径被推迟到一个专门的票里处理。

### 负面 / 权衡

1. **双表并存期的认知成本**：仓库里同时有两套记忆模型、两套 `MemoryScope`（同名不同义）。
   缓解：两处模块文档互相指认，本 ADR 是集中锚点。这是 expand 阶段**必然**的代价。
2. **V2 目前没有自动写回**：只有显式调用者能写 V2。在 MEM-V2-2 之前，V2 通路是"通电但没接线"。
3. **`MemoryV2Service.search` 不 flush 索引** ⇒ 写入后短时间内检索仍可能看不到新记录
   （直到 relay 跑完一轮）。用户可见语义是"最终一致"，与 V1 一致（ADR-0026 代价④）。
4. **`PAYLOAD_FIELD_MAX_CHARS = 500` 是 PRD 未钉的收紧**：PRD 只钉了 `content` / `evidence`
   的上界。收紧（而非放宽）不违反 §6.1 的"不得弱化或替换固定字段"，但它是**本 ADR 引入的
   约束**——若后续票需要更长的 procedure 文本，改这里要同步改 PRD。
5. **重试计数活在进程内存**：重启会清空死信集合（这是**有意**的——重启后应当重试一次）。
   代价是无法跨进程对账"哪些 id 被判死"，只能靠 outbox 行 + 日志观察。

---

## 5. Implementation evidence

实现全部落在 `src/agent_harness/memory/v2/`（**新增目录**；V1 文件零改动）：

| 文件 | 承载的决策 |
| --- | --- |
| `types.py` | D1（判别联合 / `Literal[2]` / `extra="forbid"`）、D2（draft 与 record 共用 `_MemoryContentFields`）、D3（tier/scope/provenance 规则）、D4（`TrustedMemoryIdentity` / `UntrustedIdentityError` / `assert_trusted_identity`） |
| `store.py` | D5（版本 + 部分唯一索引 + `_require_active` + scope/project 禁改）、D6（invalidate 保留内容）、D7（记录行 + outbox 同事务、`PendingMemoryChangeV2` 自足、`revision` ack） |
| `index.py` | D7（`MemoryV2VectorIndex` 端口、`MemoryV2IndexRelay` 的失败保留 / 死信 / revision 重置、ack 与索引失败分开归因）、D8（`resolve_active_hits`） |
| `capability.py` | D9（`MemoryV2Capability` Protocol + `MemoryV2Service` 组合） |
| `__init__.py` | 显式导出契约面（避免"包里任何东西都能被 `import *` 拿到"） |

测试（全部**新增**，`tests/memory/v2/`）：

| 文件 | 覆盖 |
| --- | --- |
| `_records.py` | 三种 payload 的工厂（含 `make_draft` / `make_record`），各测试文件共用 |
| `test_v2_types.py` | AC2 的边界与反控：内容/数值边界（含 `inf` / `-inf` / `nan`）、kind-payload 判别、tier/scope 组合、provenance（`user_edit` 是唯一豁免档）、身份冲突、不可变性 |
| `test_v2_store.py` | AC1（roundtrip）、AC3（跨会话项目可见性 + 跨项目/用户/租户不可见）、AC4（版本 + supersession + 版本历史）、AC5（失效不出 active 面）、AC6 前半（outbox 入队 / ack revision / 自足路由）、**并发 active 不变量**、`list_active` 的 project 谓词判别性、`profile` 存档往返 |
| `test_v2_index.py` | AC6（注入 upsert **与 delete** 失败 ⇒ 已提交事实保留 + outbox 可恢复 + 重放恰好收敛一次）、死信预算与 revision 重置、账本故障不消耗索引预算、AC7（伪造/过期/跨归属命中复核） |
| `test_v2_capability.py` | 经 `MemoryV2Capability` Protocol 走完整生命周期（create → search → update → invalidate）+ 实现与 Protocol 的方法面一致性 |

### 5.1 实测读数（本机，2026-09-24，修复本轮两轴审查 findings 之后重测）

```
$ .venv/Scripts/python.exe -m pytest tests/memory/v2 -q --no-header
114 passed in 5.36s

$ .venv/Scripts/python.exe -m ruff check .
All checks passed!

$ .venv/Scripts/python.exe -m pytest tests/memory -q --no-header     # 全量 = V1 225 + V2 114
339 tests, 1 failed / 338 passed in 198.04s
── 唯一失败：tests/memory/test_memory_lifecycle.py
   ::test_concurrent_writers_converge_on_one_row_and_one_index_state
   形态恒为 sqlite3.OperationalError: attempt to write a readonly database
   （V1 既存环境 flaky，见 §5.2）
── V2 的 114 条本次全绿。**这不能读作"V2 免疫该通道"** —— 见 §5.2 末尾的"结论边界"。
```

> ⚠ **本节的证据外延在 2026-09-24 被两轴审查驳回并已更正**：修复前本节写的是
> "102 条全绿 + `-k concurrent` 连跑 10 次 10/10 ⇒ V2 不受该环境通道影响"。
> 后半个推论不成立（§5.2 末尾给出理由）。本节现在只声明**观测**，不声明**性质**。

### 5.2 全量套件里那条 V1 失败：判定为**既存 flaky，非本票回归**

`tests/memory/test_memory_lifecycle.py::test_concurrent_writers_converge_on_one_row_and_one_index_state`
报 `sqlite3.OperationalError: attempt to write a readonly database`。

调查过程一度出现"看起来是我的 import 触发的"假象，因此把**全部读数与排除步骤**都留在这里
（这是一节方法论记录：小样本上的相关性会骗人）。

| # | 树 | 命令 | 结果 |
| --- | --- | --- | --- |
| 1 | 工作树（含 V2） | `pytest tests/memory` | 327 tests, **1 failed** |
| 2 | 工作树 | 同上，重复 3 次 | 327, **1 failed**（3/3：同一用例、同一 `index=125`、同一错误） |
| 3 | 工作树 | `pytest tests/memory --ignore=tests/memory/v2` | 225, **0 failed** |
| 4 | **纯净基线树**（`git archive HEAD`，无 V2 代码） | `pytest tests/memory` | 225, **0 failed**（第 1 次） |
| 5 | 工作树 | `--ignore=tests/memory/v2 -p json` | 225, **1 failed** ← 与 V2 无关的扰动也让它变红 |
| 6 | 工作树 | `--ignore=tests/memory/v2 -p agent_harness.memory.v2.types` | 225, **1 failed** |
| 7 | **纯净基线树** | `pytest tests/memory`（plain 复跑） | 225, **1 failed** ← **决定性：没有 V2 也会红** |
| 8 | **纯净基线树** | `pytest tests/memory -p json` | 225, **1 failed**（同一用例、同一 index） |

排除步骤（按发生顺序）：

1. **执行顺序**：junitxml 中该用例是第 **125** 条，第一条 V2 用例是第 **225** 条
   ⇒ 失败发生时**没有任何 V2 测试执行过**，排除"V2 测试改了共享状态"。
2. **基线 A/B**：`git archive HEAD` 取纯净树。两项正控：该树确认**不含**
   `src/agent_harness/memory/v2` 与 `tests/memory/v2`；且实测 `agent_harness` 确实从基线树导入
   （`PYTHONPATH` 生效）——否则就是"拿文件跟它自己比"。
3. **初步推断（后被证伪）**：`--ignore=tests/memory/v2` 只去掉 V2 **用例**，V2 **源码**仍在树里
   ⇒ 0 failed。当时据此推断"触发通道 = 收集期 import 了 V2 包"。
4. **自我证伪**：`-p json`（stdlib、no-op 插件，与 V2 毫无关系）同样让该用例变红
   ⇒ 触发器不是 V2 的 import，而是"进程受到轻微扰动"这件事本身。
5. **闭合**：纯净基线树 plain 复跑 **1 failed**（第 7 行）。基线在无 V2 时同样复现 ⇒ 结论成立。

**结论**：该用例在**本机沙箱**里 flaky，形态恒为 `attempt to write a readonly database`，
与 V1 无关，也**不是本票引入的回归**（第 7 行是决定性读数：无 V2 的纯净树上同样复现）。
频率**没有可信估计**（基线 2 次里红 1 次，样本太小）；也不能排除"测试越多越容易触发"，
但那属**负载/环境**通道。
可疑诱因（**未验证，仅列方向**）：SQLite 的 WAL/journal 与沙箱文件保护守卫在
`%TEMP%` 下的竞争——本仓 `safe-delete` 守卫的告警实测就指向
`pytest-of-…/garbage-<uuid>`，说明该目录处在守卫视野内。

#### 结论边界：本节证不了"V2 免疫该通道"（2026-09-24 两轴 Standards 轴指出，复核后确认成立）

上面每一行证明的都是"**这条 V1 用例**在无 V2 的树上同样红"。它**不**支持下面这个外推：

> 本票新增的并发用例用的是**同一种落盘方式**（`tmp_path` ⇒ 同一 `%TEMP%` 下方 + 每操作
> 新连接 + `BEGIN IMMEDIATE`）。同一通道若打中它，暴露出来的症状**不是** `OperationalError`，
> 而是"败者抛出的异常类型不等于 `ValueError`"（该用例断言败者必为 `ValueError`）
> ⇒ 表现为**本票自己的红**，与 V1 那条一样不可归因。

因此：

- "V2 的 114 条本次全绿"只能读作**本次运行未被触发**（观测），不能读作**V2 免疫**（性质）；
- 本票的并发用例只能作为"在未被环境干扰的运行里 R3/R4 成立"的证据，**不能**作为
  "V2 并发安全性已被稳定验证"的证据；
- 该非确定性是**施工环境**问题（`%TEMP%` 落在 `safe-delete` 守卫视野内），处置归环境/工具线，
  **不改变本票的实现结论**，也不允许用放宽断言 / 缩小范围的方式让它变绿（§8 Scope Lock）。

---

### 5.3 本轮审查 findings 的处置与变异红证（2026-09-24）

两轴独立审查（Standards + Spec，各一只读子代理，fixed point `7dc5eb4`）判 `NEEDS-FIX`：
`P0=0`；两轴独立收敛到**同一个 P1**。逐条处置如下，每条都用"回退修复 ⇒ 目标用例变红"作判别性证明。

| # | finding | 处置 | 红证（回退 ⇒ 红在哪条） |
| --- | --- | --- | --- |
| **P1** | `list_active` 的项目谓词写成"调用方有 `project_id` 就按 `project_id` 比较" ⇒ `user_global` 行（`project_id` 为 `NULL`）被**静默**筛掉，破坏 §4.3 / R5。两轴独立复现。 | 谓词改为 `AND (scope <> 'project' OR project_id = ?)`——项目过滤**只**对 `project` 作用域生效 | 回退谓词 ⇒ `test_list_active_user_global_is_visible_with_a_project_context` 红 |
| **P2** | AC6 / R7 的 **delete 失败**半边无判别性测试（`fail_delete` 零引用） | 新增 `test_index_delete_failure_keeps_the_committed_state_and_the_intent`（失败保留状态与意图 + 恢复后恰好在 `delete_calls` 里出现一次），并给 AC5 用例补 `contains` / `delete_calls` 断言（原断言只看 `search` 结果，query 不匹配也同样为空） | 回退需改实现（本仓库不改生产代码来造红证）；该用例同时钉住"失败路径不得假装成功" |
| **P2** | `acknowledge` 失败与索引失败**共用**重试预算 ⇒ 一次账本故障可永久毒住一条健康的 key | 拆开：`_apply` 成功即清预算；ack 失败只记日志、**不**消耗预算（`MemoryV2IndexRelay` docstring 同步） | 回退成共用预算 ⇒ `test_acknowledge_failure_does_not_poison_the_index_retry_budget` 红 |
| **P3** | 死信预算与 revision 重置无测试 | 新增两条：预算耗尽后**不再尝试**但 outbox 行保留；同 id 的 revision 变化重置预算 | 回退死信判断 ⇒ 预算用例红 |
| **P3** | `explicit_command` 允许空 provenance（§6.1 的豁免面只有 `user_edit`） | 收紧为"`user_edit` 是唯一豁免档" | 回退成只拦 `automatic` ⇒ `test_provenance_sources_reject_empty_source_event_ids[explicit_command]` 红 |
| **P3** | `tier = profile` 未经存储层往返验证 | 新增 `test_profile_tier_roundtrips_through_the_store` | 回退 `_values` 的 tier 为常量 ⇒ 该用例红 |
| **P3** | `allow_inf_nan=False` 冗余（`ge`/`le` 已挡住非有限值） | 删掉该参数，并补 `inf` / `-inf` 反控（`nan` 原本已有） | 该断言由 `ge`/`le` 提供；见 `test_importance_out_of_range_rejected` / `..._strength_...` |
| **P3** | 死代码：`MemoryV2Service.store` property、`MemoryV2IndexRelay.stop()`、`indexed` 列（只写不读）、4 个包级导出常量零消费者、`MemoryV2Capability` 未导出 | 全部删除；改为在 `__init__.py` 导出 `MemoryV2Capability`（D9 说"边界是 Protocol"，导出它才与之一致） | 结构性删除，无红证（见下"为何这几条没有变异"） |
| **P3** | ADR-0031 的决策号误引（D8 实为 `tool_scope`，与 capability/USER 无关） | §D10 第 6 行改为 `D2/D3/D6`（D6 正是"描述与底层不一致"的既有实例） | 文档更正 |
| **P3** | 本 ADR 自述的"需被 PRD / PHASE_STATUS 引用"义务未履行（`grep 0042` 零命中） | 在 `docs/PHASE_STATUS.md` 的 Memory V2 条目下加指针 | 文档更正 |

**为什么"删死代码"这几条没有变异红证**：删除件的判别性由**编译/导入期**承担——
`MemoryV2Service.store` 与 `MemoryV2IndexRelay.stop` 一旦被删，任何残留引用会直接
`AttributeError`；`indexed` 列的删除会让 `acknowledge` 少一次写，而该写没有任何读者
（`grep -rn indexed src/agent_harness/memory/v2/ tests/memory/v2/` 零命中）。
为这类删除造"回退 ⇒ 变红"是把可编译性当测试，收益为负。

变异红证脚本（`.scratch/mutation_proof_297.py`，非交付物）本轮 5/5 全部按预期变红，
且脚本在 `finally` 里用原始字节恢复并逐字节比对（不触碰任何 git 写操作）。


按 Scope Lock（AGENTS.md §8）本票**只报告、不顺手修**；处置归属 V1 记忆线。
备选方向：让并发写者各自用独立 DB 文件，或把该用例的并发写串行化。
**不允许**用放宽断言 / 缩小范围的方式让它变绿。

---

## 6. License / 上游复用

本票**没有实质复制或移植任何上游代码**：`memory/v2/**` 是依据 PRD 与本仓既有模式
（"每操作新连接 + `busy_timeout`"、outbox 的"每个 id 一行 = 期望状态"）新写的。
`memory/v2/store.py` 的 `_connect` 模式与 V1 `memory/sqlite_record_store.py` 同款，
但两者同属本仓库（同一 LICENSE），不构成第三方来源。

⇒ 无需新增第三方 license notice。若后续票（MEM-V2-2 的 formation）实质移植上游
LangMem / 其他项目的代码，**该票**必须补 license notice——本 ADR 在此登记该义务。

---

## 7. 验证与登记约定

- **门禁**：`ruff check .`（gate0 的 `ruff` 车道）必须 clean；`pytest tests/memory/v2` 必须全绿
  （本机 114/114）；`pytest tests/memory`（含 V1）只允许出现 §5.2 登记的那一条 V1 环境性失败
  （形态恒为 `attempt to write a readonly database`）。任何**新增**失败一律按本票回归处理。
  判别性证据见 §5.1 与 §5.3（本轮 findings 的逐条变异红证）。
- **覆盖**：本票的 commit 必须在 `docs/review_ledger.d/` 有归属行（`scripts/check_review_coverage.py`）。
- **ADR 索引**：本 ADR 已在 `docs/PHASE_STATUS.md` 的 Memory V2 条目下被引用（2026-09-24 补），
  否则后续票找不到它。
