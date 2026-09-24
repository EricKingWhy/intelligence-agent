# ADR-0043 — Memory V2：证据引用的运行时别名、契约读法与已登记缺口

- **Status**: Proposed（机制已实现并实测通过；V1/V2 并存期，cutover 归 MEM-V2-7）
- **Date**: 2026-09-24
- **Deciders**: 用户（票面更正 T6b 裁决「候选 A」，2026-09-24）+ 本 Agent（机制设计）
- **Related**:
  - Issue **#298 / MEM-V2-2**（父票 #296），票面 `docs/tickets/mem-v2-2-durable-formation-adjudication.md`
  - `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`（下称 PRD）：§5.3 模型角色与预算、
    §5.6 开关、§5.7 政策（§5.7.2 的 9 类敏感）、§6.1–§6.3 信封与两段契约、§6.5 事件与隐私、
    §7.3 实现自由
  - **ADR-0042**（Memory V2：类型化信封 / 版本化生命周期 / 派生索引）——本 ADR **不改信封**，
    只确定「模型能引用什么、以什么键引用」；§D11 是 ADR-0042 §D7 的索引缺口在**生产装配面**上的补充登记
  - ADR-0024（Memory provider seam）、ADR-0031（`retrieve_memory` / `remember_this`）、
    ADR-0037（投影引用稳定性与事件版本）
- **Refines**: ADR-0042 §D1（信封）在「证据字段」这一层的读法
- **Supersedes**: **无**

---

## 1. Context

### 1.1 为什么需要这份 ADR

MEM-V2-2 的 Formation / Adjudication 需要模型**说明它是从哪儿知道的**：§6.1 要求每条候选的
`evidence` 非空，R5 的「两条独立事件」要在证据上数，R6 的「用户事实必须有直接用户证据」也要
在证据上判。与此同时 R2 明确**不向模型投影任何事件信封字段**（`event_id` / `seq` / `session_id`）。

两句同时成立时，`evidence[].event_id` 这个字段就成了夹缝：契约（§6.2/§6.3）要求它是
「本轮的真实 event id」，而安全投影**不可能**把真 id 递给模型。T3–T5 各切片刻度内都自洽，
合起来是断的——这就是 T6 收口时抓到的**跨切片 P0**（§1.2）。

本 ADR 把这条链的每一环写成决策，并把本票**承认的能力边界**与**结构缺口**一并登记。
它同时兑现多个 docstring 里已经存在、但此前只指向一份「尚未落盘」文档的引用义务
（`formation.py`、`policy.py`、`projection.py`、`runner.py` 各有指向）。

### 1.2 别名机制的来历（T6 的跨切片 P0，用户已批准修法）

- `projection.py` 按 R2 **刻意不投影**任何事件信封字段（含真实 `event_id`，有测试钉着）；
- 而 `CandidateEvidence.event_id` 与 `policy._reject` 的 provenance 判据都要求它是**本轮真实 id**；
- ⇒ 模型在**结构上**无法产出可解析的证据，生产路径上每个候选都会落 `unsupported_source`
  （AC3 点名的类别 ⇒ **自动记忆零写入**），而观测上只显示「模型没给出可解析的证据」。

既有用例之所以全绿，是因为 fixture 把真 id 直接写进了候选——**测试编码了一个模型到不了的世界**。
这是本票反复惩罚、也最难自查的缺陷类型（§5.5 第 4 条）。

用户裁决「候选 A」：投影按出现顺序给本轮可被引用的事件发**运行时别名**，政策层以别名为键空间，
执行器在**写盘前**翻回真实 id。`refs=None` 时行为与改动前**逐字不变**。

### 1.3 本 ADR 的范围

**在**：证据引用的键与对照表、`ref` 这个 wire 字段的定性、两处契约的读法、三条能力边界与三处
结构缺口的登记、以及 T7a/T7b 的接缝与门禁证据。

**不在**：检索与 UI（#299 / #301）、显式命令与治理 API（#300）、质量评估（#302）、
V1 退场（#303）、真实 Gate（#304）。这些面在本 ADR 里只以「缺口 + 归属」的形式出现。

---

## 2. Decision

### D1 — 证据引用的键 = 运行时别名（`ref`），不是真实 `event_id`

投影**按阅读顺序**（先 `current_run` 的消息、后 `tool_calls`）给每个可被引用的本轮事件发一个
确定性别名 `e1`…`eN`，作为载荷里的 **`ref`** 字段出现。`别名 → 真实 event_id` 的对照表留在
`FormationInput.refs`（**运行时侧，不进载荷**）。

三条性质是有意的：

1. **别名是投影的产物，不是日志的引用**：模型没有别的途径知道真 id ⇒ 「引一个真 id」这种输出
   解析不到，按 `unsupported_source` 被拒。错误方向是 **fail closed**。
2. **只有本轮事件有别名**：`earlier_messages` 的 `ref` 恒为 `None`。它们是上下文而不是 provenance
   （政策只拿本轮事件建键空间，给历史发 id 等于递给模型一条必然失败的路）。
3. **别名的顺序 = 载荷的阅读顺序** ⇒ 同一份事件序列永远得到同一份载荷，「可重放的 prompt」
   这条既有性质不受影响。

### D2 — `ref` 是 wire 字段，属 Implementation Freedom 之内

`ref` 不在 PRD §6.2 的字段表里，是本票新增的。判定为**实现自由之内**：

- PRD §7.3 明确「精确排序公式」等属实现自由，票面的 Implementation Freedom 亦写明可自行决定
  *prompt assembly internals*；
- 它**不新开** R2 排除清单里的任何一类材料：真实 `event_id` / `session_id` / `seq` 仍然不进模型输入；
- ⇒ **AC9 的判据不变**（「不允许把真 id 交给模型」这条仍然成立，且仍有用例钉着）。

### D3 — `CandidateEvidence.event_id` 保留字段名，生产路径上装**别名**

字段名不改：契约冻结，改名要动 §6.2/§6.3 与 governance。代价是它在生产路径上装的是别名，
**这个语义差在字段自身的 docstring 与本条里写明**，不靠读者猜。

翻回发生在**写盘前**：`executor._draft_from` 用同一张 `refs` 把别名翻成真实 id，去重保序后写进
`source_event_ids`。一条都翻不回来 ⇒ 逐条丢弃，归因码 `evidence_unresolved`（§6.1 要求 provenance
非空）。

### D4 — `refs` 是「模型能引什么」的**唯一**开关；默认 `None` ⇒ 行为逐字不变

`select_candidates(..., refs=None)` 时键就是真实 id，行为与别名引入前逐字一致。默认保持 `None`
是**刻意**的：默认成别名会让不带投影的调用方（显式命令路径、纯函数用例）**静默全落**
`unsupported_source`——一处沉默的过严，与生产路径上那个洞是镜像，同样致命。

### D5 — 键空间的门开在 `select_candidates` 的**调用点**，不在 `_evidence_keys`

`_evidence_keys` **只做翻译，不设门**：门在 `sources` 与 `qualifying` 那两处推导式的 `if ... in keys`
上——两处都只遍历 `event_list`，所以**投影没发过别名的事件压根进不了键空间**，
「模型引不到的东西解析不了」由此成立（fail closed）。

曾经在这里多加过一道 `if real in known` 过滤，实测对 `sources` / `qualifying` 都无影响
（它们本来就只看 `event_list` 里的 id），属**不产生行为的第二层**，已删。留下这条记录是为了
让下一个人不再加回来。

### D6 — 别名对照表是**单射**，且**只读**

同一个事件永远只拿一个别名（`_Aliases._issued` 是幂等备忘录）。这不是洁癖：政策侧要把
`别名 → 真实 id` 反过来建成键空间，一个事件拿到两个别名时那个反向映射会折叠到最后一个，
于是**载荷广告过的前一个 `ref` 解析不到**，而全链路不报错。触发它**不需要异常输入**——
同一 run 里出现重复的 `tool_call_id`（两条 `tool/call` 配对到同一条 `tool/result`）就够。

`refs` 暴露成 `MappingProxyType`（只读）：它是"模型能引什么"的定义，中途被改会同时打破
上面两条性质。

### D7 — §6.3 的 adjudication `result` 读作「**内容字段齐全**的 draft」

§6.3 说 `result` 是 "complete MemoryRecordV2 candidate"。本票读作**内容字段齐全**的 draft 形状，
而**不是**含服务器拥有字段的完整信封：§6.1 把 `id` / `version` / 时间戳都定义为服务器所有
（"Server-owned timestamps"），模型不可能合法地生产它们。类型上落实为 `extra="forbid"` ——
模型多写一个 `tenant_id` 不是「被忽略」而是**解析失败**（"forged identity" 因此是被拒绝，
而不是被忽略）。

### D8 — 敏感 9 类：运行时**不自建**分类器（承认的能力边界）

`Sensitivity.SECRET` 是模型的**收紧**信号（它说是就拒），而"模型说是 ordinary"不构成放行——
`find_secret` 是独立于模型的确定性扫描（前缀 / 结构）。**R7 的「independent of model
classification」只在 secrets 这一类上成立。**

PRD §5.7.2 的 9 类敏感（健康 / 财务 / 证件 / 精确位置 / 生物特征 / 亲密 / 政治宗教 / 法律 /
未成年人）**没有**运行时检测器，唯一信号是模型的 `sensitivity` 自陈。理由：词表不是分类器——
一份关键词表在这 9 类上既会大面积误报（正常记忆全被拒）又必然漏报，声称"运行时独立分类了"
等于给自己发一张**假绿灯**。所以这一层的执法方向是**授权**而非**检测**：

- 同意信号（`explicit_remember`）由运行时持有，模型输出里**没有位置**能表达它；
- 自动形成路径上它恒为 False ⇒ 所有 `sensitive` 候选都被拒（AC3 的 "unauthorized-sensitive"）；
- 形状层已保证「带类目 ⇔ 自陈 sensitive」，所以模型无法用"打个类目再自称 ordinary"绕过。

⇒ 残余风险**如实登记**：模型把健康/财务类内容**自陈为 `ordinary`** 即可写入。
AC3 的字面判据仍成立（它要求"未被授权的 sensitive"被拒，而"未被标为 sensitive"不在此列），
但"运行期完全独立于模型分类"这个更强的说法**在本票不成立**，不要对外这样讲。

### D9 — `project_id` 恒 `None`：作用域收敛到 `user_global`

本票没有**可信的项目绑定来源**（`types.TrustedMemoryIdentity` 自己写明"具体解析链在后续票据接线"）。
后果：执行器的检索与政策判据都只看到 `user_global` 一个作用域。

选**诚实的窄**：少形成一条 project 记忆，而不是把一条用户事实**错记到项目名下**。
（后者是脏数据——它会在别的项目里被检索到。）

### D10 — 投影的两处边界（如实登记）

- **当前 run 的条数不限**：R2 没给上限，run 本身有限，外层边界是 R10 的 32k 输入 token 预算。
  在这里加一条"顺手"的条数上限会砍掉 run 的**尾部**——恰恰是刚发生、最该形成记忆的那几步。
- **图片类内容不在投影范围内**：它们以 artifact 形式存在，只投影引用，不读内容。

### D11 — 派生索引在生产路径上**尚无驱动**（R2 的质量面）

执行器的检索（R2 的"最多十条相似 active 记忆"）走 `MemoryV2Service.search` → 索引；索引由
`MemoryV2IndexRelay.flush()` 从 SQLite outbox 收敛，而**全仓只有用例调用过 `flush()`**——
本票不引入它的驱动（周期循环或按 job 触发都是新机制，归属召回那一票）。

**后果说清楚**：接线之后，**生产**路径上那份"相似记忆"结构性地为空 ⇒ 裁决期看不到既有记忆 ⇒
模型只能一路 `ADD`。R2 的**字面**要求仍然成立（0 ≤ 10，每条上限亦然），受影响的只是更新/去重的
**质量面**。这是本票唯一一处"接了线却什么也检不到"的结构性空洞，归属 #299（召回）/ #303（切换）。

对应地，`tests/memory/v2/test_v2_executor.py::test_adjudication_sees_bounded_relevant_active_memories`
里有一行**手工** `await env.index.upsert(...)` 扮演 relay 跑过一遍——那是本票唯一一处
"用例依赖了生产不会发生的状态"，已在用例内就地标注（T8 两轴审查 P2）。

### D12 — 非目标

不做：检索/UI/cutover（后续票）、把未采纳的候选留在持久待办队列（票面明禁）、把 Langfuse
可用性变成 job 成功依赖、V1 路径的任何改动（V1 逐字保留，退场归 #303）。

---

## 3. Rationale

### 3.1 为什么不能把真 id 交给模型

`event_id` 是会话日志的内部句柄，与 `session_id` 同族：给它等于把"日志的寻址能力"递给一个
可能被 prompt injection 影响的组件。别名把这份能力**降级成一次性坐标**：它只在本次投影的
键空间里有意义，跨 run / 跨会话不可复用（每次 `build_formation_input` 重新从 `e1` 起号）。

### 3.2 为什么门开在调用点

如果把门写进 `_evidence_keys`（"不在 `known` 里的 id 一律丢掉"），那么**不该有候选能引用到它**
这件事就与"翻译"耦合在一起：将来有人只改翻译、不改过滤，或者反过来，都会让键空间与投影
发的别名集合悄悄分叉——而分叉的方向是**静默的过松或过严**，两种都不会报错。
让门只由两处**消费点的 `in keys`** 承担，则"模型能引什么"这句话有且只有一个定义处。

### 3.3 为什么"运行时独立分类"只在 secrets 上成立

秘密有**稳定的字面形状**（前缀 / PEM 头 / 结构），所以确定性扫描器是可行的、且误报率可接受；
9 类敏感**没有**这种形状（"我最近血压有点高"与"我最近有点忙"在字符层面同构）。承认这一点
比铺一张关键词表更安全：假绿灯会让运维以为这类内容已被保护。

---

## 4. Consequences

### 正面

- 证据链**闭合**：模型只能引它看得见的东西，而它看得见的每一件事都能翻回真实 id。
- 三条性质（fail closed / 只有本轮 / 可重放）各自**独立**成立，互不依赖调用方纪律。
- 真 id 仍然不进模型输入 ⇒ AC9 的判据、以及"投影是安全边界"这条既有性质都**没有被削弱**。
- 能力边界与结构缺口**写在文档里**，而不是留在作者的记忆里——下一个人不会以为
  "敏感 9 类已被独立检测"或"检索已经在跑"。

### 负面 / 权衡

- `CandidateEvidence.event_id` 在生产路径上**名不副实**（装的是别名）。这是**契约冻结 vs 语义
  清晰**的取舍，选了前者，代价是这个名字会误导人（已在 §D3 与本条显式登记）。
- 别名是**每次投影重新分配**的，所以它不能出现在任何跨 run 的持久物里。执行器必须**在写盘前**
  翻回真 id（这条由 `executor` 的结构保证：`refs` 一路传到 `_draft_from`）。
- 9 类敏感的实际保护**依赖模型自陈**。这是本票最强的能力边界（§D8）。
- 派生索引无驱动 ⇒ 去重/更新质量在生产上退化为"只能新增"（§D11）。

---

## 5. Implementation evidence

### 5.1 实测读数（本机，2026-09-24；T8 处置完两轴 findings 之后重测）

| 车道 | 命令 | 读数 |
| --- | --- | --- |
| Python lint | `./.venv/Scripts/python.exe -m ruff check .` | `All checks passed!`（exit 0） |
| V2 单测 | `tests/memory/v2` 15 文件**分两片** | **588 passed**（303 + 285；0 failed / 0 skipped） |
| V1 记忆回归 | `tests/memory`（除 `v2/`）分两片 | **225 passed**（128 + 97；**分片划分是人为的**，判据只看总数与失败集合） |
| Agent 回归 | `tests/agent` | **484 passed**，3 deselected |
| 装配 / capability / 词表守卫 / web 记忆 | `tests/capability` + `tests/test_assembly*.py`（7）+ 两个生成物守卫 + `tests/web/test_memory_api.py` | **178 passed** |
| 前端类型 | `web`: `node node_modules/typescript/bin/tsc --noEmit -p tsconfig.app.json` | **exit 0**（T6 起为 `TS2741` exit 2，本票修，见 §5.4） |
| 事件词表守卫 | `tests/test_event_vocabulary_generated.py` | 再生成**前** 2 条红 → 再生成后 6 passed（见 §5.4） |
| 变异红证（T7b 切片） | `.scratch/mutate_t7b_slice.py` | 基线 43/43 绿；M1–M4 **逐条按期望 red**；还原两文件**逐字节一致** + 复跑 43/43 |
| 变异红证（T8 处置） | `.scratch/mutate_t8.py <case>`（3 例） | 三例均「基线绿 / 变异红 / 还原逐字节一致 / 复跑绿」= **3/3 符合期望** |

用例数净增：`585 → 588`（+3，T8 补的三条判别性锁：R5 独立性、预算维度对齐、装配期配置类故障
上抛）。**总收集数不用于判定**（见 §5.2）。

**落点前复测**：写台账行之前把上表**逐条重跑**过一遍（`ruff`、V2 两片、V1 两片、`tests/agent`、
装配车道、前端 `tsc`、T8 三例变异）。读数与首次一致；唯一的差异是 V1 两片的**人为划分**
（107+118 → 128+97，总数同为 225）——这正是"只看总数与失败集合、不看分片"的实例。

### 5.2 环境约束的**结论边界**（本机施工环境的属性，不是仓库/产品的性质）

本票施工期间实测到四条环境行为，**逐条写在这里**，免得后来者把它们当成代码或产品的性质：

1. **前台命令约 120 秒被 SIGTERM**，显式 `timeout` 也一样，且**不会** auto-background。
   ⇒ 长跑门禁必须**按文件切片**，每片 < 115 秒。
2. **`run_in_background=true` 的任务会被塞进沙箱**——**即使同时传"跳过沙箱"标志**。
   判据 = junit 里出现 `sqlite3.OperationalError: attempt to write a readonly database`。
   ⇒ 想拿到"非沙箱 + 长跑"，唯一可行形态是**非沙箱前台 + 切片**。
3. **`nohup` 派生的子进程活不过工具调用**（进度文件停在 `start`）⇒ 不能用它绕开 1 与 2。
4. **`tests/memory/v2` 整目录跑不过**（4 次 SIGTERM，含 `--ignore` 三种组合）；
   拆成 8 + 7 两片各 ~5s / ~20s **全绿**。**未定位**，README 与本表按环境约束登记。
   ⇒ 判定口径是"**切片全绿**"，不是"整目录绿"。

**顺带登记两条既存 flaky 的归属**（不是本票回归）：
`tests/memory/v2/test_v2_jobs.py::test_concurrent_claims_have_exactly_one_winner` 与
V1 的 `test_concurrent_writers_converge_on_one_row_and_one_index_state` 在本沙箱下会报
`attempt to write a readonly database`。**独立复核**：用**裸 aiosqlite**（4 条连接并发
`BEGIN IMMEDIATE`，同一临时目录，不含本票任何代码）复现出**完全相同**的错误 ⇒ 环境。隔离重跑即绿。

**结论边界**：以上全部是**本机**读数与**本机**环境行为。要据此改代码或对外声明，必须先在
另一台机器上复跑；本 ADR 只声明"在这些机器上、这些命令下、观察到这些读数"。

### 5.3 两轴独立审查与 findings 处置（2026-09-24）

范围 `git diff 32ef89b..a88a0a0`（13 笔、36 文件、10493 插入），**固定点 = `a88a0a0`**，
Standards 与 Spec 各一独立只读子代理（贴合协议每轴 1 轮预算）。

**结论**：Standards **NEEDS-FIX**（`P0=0 P1=2 P2=2 P3=5 P4=2`）／Spec **PASS-WITH-FINDINGS**
（`P0=0 P1=1 P2=3 P3=2`）。两轴都**独立复核并确认**了本票的核心事实主张（T7b 的切片因果链、
装配三决定），也都独立复现了 §5.2 的沙箱噪声。

| # | 轴 / 级别 | finding | 处置 |
| --- | --- | --- | --- |
| S1 | Std P1 | 固定点前端 `tsc` **红**：`web/src/generated/event-types.ts` 有了 `memory/updated`，`web/src/lib/projection.ts::EVENT_SEMANTICS` 没登记 ⇒ `TS2741` | **修**：补 `[EventType.MEMORY_UPDATED]`（与 `MEMORY_DEGRADED` 同形），`tsc` 转 exit 0 |
| S2 | Std P1 | `docs/EVENT_VOCABULARY.md` 未随 `event.py` 再生成 ⇒ 守卫红 | **修**：再生成（40 持久化 + 2 仅广播）；见 §5.4 |
| S3 | Std P2 | `_wire_memory_formation` 的宽 `except Exception` 把 `model.config.ConfigError` 也吞成 OPTIONAL 降级，与 `roles.py` 的契约相反 | **修**：加 `except ConfigError: raise CapabilityError(..., code="init_failed")` + 新用例 `test_a_broken_model_catalog_fails_loudly_instead_of_degrading`（变异实测 red） |
| S4 | Std P2 | `_seconds_until_claimable` 的 docstring 与实现相反（"被按用户串行挡住 ⇒ None"） | **修**：按实测行为改写 + 写明"多武装一个无害定时器"的代价 |
| S5 | Std P3 / Spec F5 | `run_slice_bounds` 等处写「`Session.append` 的 `run_id` **没有默认值**」——**不成立**（`session.py:283` 是 `run_id: str \| None = None`）。结论（本轮 user 事件 `run_id is None`）成立，但**机制措辞是错的** | **修**：`runner.py`（docstring）× 2 文件 + `test_v2_runner.py` / `test_v2_runtime_seam.py` 的 docstring 与断言消息，统一改为"**调用点不传 run_id**，而它的默认值就是 `None`" |
| S6 | Std P3 | `_meets_procedural_threshold` 的"上界"未被证明：`while` 一路向前吞，孤儿 `user/message` 会被吞进本轮（构造出了反例，未证生产可达） | **修**：`while` → `if`（只吞一条）。合法输入上等价；孤儿形态下错误方向变成"少吞"（退化为可观测降级），不再把别的轮次拉进来 |
| S7 | Std P3 | `executor.DegradedReason` 的 docstring 引用了一个**不存在**的 `test_...` 占位符（把"未证"写成"已证"） | **修**：补真实用例 `test_degraded_reasons_cover_every_budget_dimension`（变异实测 red） |
| S8 | Std P3 | `eligibility` 的两条排除分支在生产不可达，而 runtime 的注释读起来像可达 | **修**：两侧注释各按实情改写（"由**不通知**兑现 + 纯函数层第二道兜底"） |
| S9 | Std P3 | `extraction_enabled` 在生产没有产出方（恒 `True`） | **验收**：在装配调用点写明"生产来源属后续票、本票恒 True"，不假装已接线 |
| S10 | Std P4 | `test_v2_executor._run_events()` 给本轮 user 消息写了 `run_id="run-1"`——**产出方到不了的形状** | **修**：改成 `run_id=None`（与生产口径一致） |
| S11 | Std P4 | `_MAX_QUERY_CHARS` 被截两次（`_run_query` 与 `_relevant`） | **修**：只留 `_relevant` 那一处（真正的边界），`_run_query` 的 docstring 写明理由 |
| P1 | Spec P1 | R7 的"运行期独立于模型分类"**只对 secrets 成立**（9 类敏感无运行期检测器） | **采纳并登记**：§D8 + §4；明确"不要对外讲更强的版本" |
| P2 | Spec P2 | R5 的"独立"判据实现正确但**没有任何有鉴别力的用例**（实测：去掉集合去重后 61 条全绿） | **修**：补 `test_a_procedural_candidate_citing_one_qualifying_event_twice_is_rejected`（变异实测 red） |
| P3 | Spec P2 | R2 的"相似记忆"在生产结构性为空；且一条用例**手工**扮演 relay（生产不会发生） | **登记 + 就地标注**：§D11；用例内注明"本票唯一一处生产到不了的状态" |
| P4 | Spec P2 | AC9 的 **log/trace** 分支无验证 | **修**：给既有 AC9 用例加 `caplog` 探针（级别开 DEBUG）并附**仪器自证**断言（`caplog.records` 非空才否证有效） |
| P5 | Spec P3 | R10 的 input-token 维度未在 executor 层端到端验证（只在账本层） | **登记**（三条共用同一条 `_Degraded(BudgetDimension.value)` 路径，风险低） |

**经查不成立**（两轴各自主动怀疑后核实为无问题，摘要）：
R2 的两处截断方向真被钉住（变异全红）／`select_candidates(refs=None)` 不会在生产路径上静默全拒／
`run_slice_bounds` 的向前吞**不会**吞到上一轮（另有反例见 S6）／按用户串行是**数据库 CAS**、
重启后仍有效／别名键空间不会同时接受真 id（双向用例）／泵的并发上限不会静默退化成 1（双侧界用例）／
AC7 不只靠 `run()` 里那行早退（数据库 CAS 才是承担者，属纵深防御）／事务边界与异常吞噬逐条审查无
`except: pass`。两轴均**未**发现 P0。

**审查预算说明**：按协议 §8.3「修后重审每轴 1 轮 / 无第二轮」，本轮 findings 的处置笔**未再开审查轮**；
每一条处置都附了判别性证据（新用例 + 变异读数）或就地登记，属于 §2 末段"修复提交也必须被覆盖"
在本轮的**如实例外**，登记在此。

### 5.4 T6 引入 `memory/updated` 的**三处未跟进落点**（T8 的意外收获）

T6（`2f5a32e`）把 `MEMORY_UPDATED` 登记进 `event.py` 并再生成前端产物，但有**三处下游**没跟上
（都不报错，或只在别处报错）：

1. `docs/EVENT_VOCABULARY.md` 未再生成 ⇒ `tests/test_event_vocabulary_generated.py` **2 条红**
   （实测：装回固定点版本跑守卫，报 `产物漏了 event.py 已注册的类型: ['memory/updated']`）。
2. `web/src/generated/event-types.ts` 有了新成员，而 `web/src/lib/projection.ts::EVENT_SEMANTICS`
   （`Record<EventTypeValue, EventSemantics>`）没登记 ⇒ **前端 `tsc` exit 2**（`TS2741`）。
   `event-types.ts` 的 `git log` 指向 `2f5a32e`，即**自 T6 起前端类型车道一直是红的**。
3. `tests/web/test_memory_api.py::test_audit_goes_to_structured_logs_not_session_events` 的
   子串过滤（`memory/update`）误伤新事件 ⇒ 该断言**自 T6 起一直红**（已在 `a88a0a0` 修：
   按 PRD §6.5 / R12 给 `memory/updated` 加例外并写明来历——它**必须**存在，且载荷不带内容）。

**教益**：加一个事件类型是一个**跨四条车道**的动作（`event.py` → 生成物 → 前端语义表 → 下游断言）。
三条车道各自都有守卫，但**没有任何一条守卫覆盖"另外三条还没跟上"**。
这三处都不是本票引入的缺陷，而是**本票的门禁把它照出来了**——登记在此，供后续引入事件类型时对照。

### 5.5 施工期教训（可复用）

1. **"死分支"这个论证要慎用**。S6 的反例说明：写"这个条件恒为真、是死分支，所以不用写"时，
   被省略的条件往往正是**挡住异常形态**的那一条。可判定的替代写法是**把吞并上限写成 1**
   （`if` 而不是 `while`），而不是论证"不会多于 1"。
2. **变异/快照型脚本与手工编辑不可并行**。T7b 的变异脚本在启动时快照目标文件、结束时回写；
   与手工编辑并行时，回写会**静默抹掉**手工改动（Edit 报的是成功）。⇒ **先把编辑做完，再跑脚本**；
   跑完立刻**行锚定回读**关键锚点。
3. **回归锁要锁"行为"，不要锁"实现形状"**。同一个纯函数可以有三种等价写法：本票有一条锁
   只问纯函数 `run_slice_bounds`，而"`_slice` 的**返回值**里放什么"无人钉——变异 M3（把历史并进
   本轮）因此首轮实测 **green**。补了一条行为锁之后才转 red。⇒ 加锁时问一句："**改变这一行的
   实现，会不会有锁变红？**"
4. **本票的复发性缺陷类型：「测试编码了一个产出方到不了的世界」**。它出现过三次
   （T6 的 fixture 写真 `event_id`；T7b 的 fixture 给 user 消息写 `run_id`；
   T8 审查又抓到 `_run_events()` 的 `run_id="run-1"`）。**判据**：每条 fixture 都要能指回
   一个真实产出方（哪个函数、哪一行、什么顺序）；指不回去就是编码了一个不存在的世界。

---

## 6. 验证与登记约定

- **本 ADR 的兑现状态**：§D1–§D7 的机制与 §D8–§D11 的边界都有**具名用例**或**显式登记**；
  凡本 ADR 声称"有用例钉着"的地方，用例名都写进了对应 docstring（S7 修的正是这条）。
- **上游复用**：无新增上游代码。别名机制是本仓自研，与 vendored 组件无关；`License` 一节因此省略。
- **复审触发条件**：#299 接上检索驱动（§D11 失效）／#300 接上显式命令路径与同意信号的**持久化**
  （§D8 的授权面变化）／#301 给 `memory/updated` 加 UI 展示（前端语义表可能从 no-op 升为真投影）／
  #303 退场 V1（本 ADR 不涉及的 V1 决策届时统一处置）。
- **本 ADR 的引用义务**：`formation.py`、`policy.py`、`projection.py`、`runner.py` 的 docstring
  都指向本文件——**改本 ADR 的决策条款时，要同步核对那四处引用是否仍然成立**。
