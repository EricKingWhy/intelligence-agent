# SDD 提速改造 · 工作记录与外部复核包

> **状态**：批 1 已落盘并推送（`refs/heads/main` = `22aa291`）。**批 2 已施工完毕**（代码/文档已落盘，待审查与落台账）；批 3 待做。
> **用途**：本文件是「SDD V3.1 开发流程提速」这条线的完整工作记录 + **审计包**，
> 供**独立审查者（能力更强的模型）**复核。
> **写法纪律**：本文所有数字均为**当场实测**，随附可复跑命令；无法当场复跑的历史读数会显式标注来源。
> **自包含**：审查者无需本线对话上下文即可复核。

---

## 0. 请审查者优先回答的问题

按重要性排序。每条都希望得到**可判定的结论 + 依据**，而不是"看起来合理"。

1. **有没有为了提速而实质放松质量的地方？** 这是需求方最关心的一条。尤其：协议 §8.3 新增第 7 条
   （「工具已经管的事，审查不再报」）是否等价于"少审一层"？
2. **批 1 撤回 `AGENTS.md §9` 指针化的三条证据是否成立？** 有没有第四条被漏掉的、或某条其实不成立的？
3. **诊断结论「药是改机制，不是压字符」证据是否充分？** 若充分，批 2/批 3 的方向是否正确？
4. **批 2 是否过度设计？** 有没有更简单的等效做法？（完整设计见本文 **§5**）
5. **批次顺序（1 → 2 → 3）是否正确？** 批 3「必须最后做」的理由是否成立？
6. **python 版闸门与既有 `check_review_coverage.sh` 的双实现一致性如何保证？** 会不会口径漂移？
7. **pre-push hook 是否只是"假安全感"**（本地可 `--no-verify` 绕过）？值不值得做？
8. **协议 §8.3 第 7 条的「前提条款」是否足够**，仍会不会"一端不报、另一端也不报"？
9. **有没有更好的做法被漏掉了？**（本线可能陷入局部最优）
10. 本文档自身的**事实错误**（数字、章节号引用、命令）请逐条指出。

---

## 1. 背景：需求方诉求

原话要点（2026-09-22）：

- SDD V3.1 流程**太慢、总是出错出 bug、前后端容易打架**；
- 要在**代码质量不下降**的前提下**提高开发速度**；
- 允许优化 `AGENTS.md` 与 SDD 流程；
- 关键澄清：**「不能为了过度的删减而影响开发的效果和质量」**，要清理的是「**没有必要的设计**」
  （举例如「完成一个简单的小功能却要跑 1 小时」）；
- 硬约束：**「mattpocock 的 code-review 不能改变」**；
- 认可 pstack 的**增量验证**（失败时不整条流水线从头跑）。

外部输入（供参照，非本仓依据）：

- **pstack**（Lauren Tan / Cursor 官方插件）原则：`encode-lessons-in-structure`、`prove-it-works`、
  `guard-the-context-window`、`fix-root-causes`、`migrate-callers-then-delete-legacy`、
  `never-block-on-the-human`（**最后一条与本仓 §9.1.1 冲突，明确不采用**）。
- 一份 GPT 分析建议的定稿主链：
  `Grill → Spec → Tickets → Implement(TDD/Tests/Matt Dual-Axis Code Review) → Runtime Verification → Evidence Gate → Merge`，
  其中「真正贵的只有 Code Review + Runtime Verification 两层」。
- **本线对上述外部输入做过两处纠错**（见 §8.1）。

---

## 2. 诊断数据（全部当场实测）

### 2.1 仓库规模

```bash
git rev-list --count HEAD          # 1481
git rev-parse --short HEAD         # 22aa291
```

| 文件 | 字符 | 行 |
| --- | --- | --- |
| `docs/SDD_WORKFLOW_PROTOCOL.md` | 18,801 | 397 |
| ↳ 其中 §8 区块（提速增补） | **11,051** | 200（= 全文 **59%**） |
| `AGENTS.md` | 19,778 | 841 |
| `docs/review_ledger.tsv` | 78,213 | 221 |
| `docs/SDD_TICKET_TRACKER.md` | **420,673** | 4,443 |
| `docs/PHASE_STATUS.md` | 43,104 | 106 |

### 2.2 提交构成（移动窗口，注意窗口会滑动）

```bash
git log -120 --name-only --no-renames --pretty=format:'@@%H'
```

最近 **120** 笔中 **docs-only = 80（66.7%）**；涉及文件数按顶层目录：

| 顶层 | 文件数 |
| --- | --- |
| `docs/` | **161** |
| `tests/` | 55 |
| `src/` | 29 |
| `web/` | 18 |
| `scripts/` | 4 |

⇒ `docs/` 涉及文件数是 `src/` 的 **5.5 倍**。

> ⚠ **窗口纪律**：同一指标换窗口结论同向但倍数不同。本线早先另一窗口（近 8 天）实测
> `docs/ +32,804（42.0%）` vs `src/ +8,268（10.6%）` = 4.0×。**报这类比例必须带窗口。**

### 2.3 单票墙钟的去向（本线早先实测）

| 票 | 总墙钟 | 其中"处置审查意见" | 占比 | 该票 docs 新增 |
| --- | --- | --- | --- | --- |
| B-35 (#247) | 165 分 | 137 分 | **83%** | 0 行 |
| B-36 (#281) | 53 分 | 35 分 | **66%** | 0 行 |

⇒ 慢的**不是实现**，是**审查轮次**。这是本线"药在改机制、不在压字符"结论的直接依据。

### 2.4 工具链牙齿强度（当时实测）

| 侧 | 工具 | 配置到位 | 被强制执行 |
| --- | --- | --- | --- |
| 前端 | `oxlint`（`.oxlintrc.json`）、`tsc -b`、`vitest run` | ✅ | ❌ |
| 后端 | `ruff` | ❌ **`pyproject.toml` 无 `[tool.ruff]` 段**（只跑默认规则集 E4/E7/E9/F） | ❌ |
| 两侧 | CI（`.github/workflows/`）、`.pre-commit-config.yaml`、`.githooks/` | —— | ❌ **全无** |

生成物同步守卫**有**：`tests/test_event_types_generated.py`、`tests/test_event_vocabulary_generated.py`、
`tests/session/test_streaming_vocabulary.py`。

### 2.5 上游技能的原始口径（本仓 mattpocock 技能为**原版**）

- `mp-eng-code-review`：**两轴各一子代理**（Standards + Spec）、**每轴 ≤400 字**、**一次性**。
  其原文含一句**本仓此前未搬**的话：`Skip anything tooling already enforces`。
- `mp-eng-implement`：**仅 16 行**且 `disable-model-invocation: true` ⇒ **不会自动触发**
  `code-review`。（⚠ 外部 GPT 输入曾断言"你的 `/implement` 已自动调 `/code-review`"——**该前提不成立**。）

---

## 3. 三项已定决策（需求方拍板，2026-09-22）

1. **协议瘦身 → 先删再加**；
2. **增量验证不破 §8.7**，但**同一棵树（sha + `^{tree}`）的读数由脚本落盘后可复用**；
3. **Gate-0 落点 = 脚本 + 本地 pre-push hook**。

---

## 4. 批 1：已落盘并推送

**远端复核**（唯一权威判据）：

```bash
git ls-remote --heads origin main   # 22aa291386e9ce562207b762baad298d0226ba56
```

| commit | 内容 |
| --- | --- |
| `5c7db24` | `docs(protocol): §8.3 删无上限重审入口 + 搬回 Matt 原版工具豁免条款`（只改协议，+18/−2） |
| `22aa291` | `docs(review-ledger): 登记 5c7db24 白名单（docs-only）` |

拆两个 commit 是**闸门机制逼出来的**：正文 commit 是 docs-only，放行需台账白名单行；
而该登记动作**恰好只改台账**⇒ 由闸门脚本自动放行。合成一个 commit 会**自指**（需白名单覆盖自己）。

### 4.1 改了什么

**(a) §8.3 第 4 条：删「例外（不算超预算）」→ 改「无第二轮」**

原文允许「修复新引入的代码面再出 P0/P1 ⇒ 按同一标准再修 + 再窄复验一轮」且**不计入预算**。
本条是「修后重审」被**无上限展开**的入口。改为：该轮若在本轮修复新引入的代码面上再给出 P0/P1
⇒ **停止修复**、如实登记轮数与残余、**交需求方裁决**。

**(b) §8.3 新增第 7 条：「工具已经管的事，审查不再报」**

来源为 Matt 原版 `/code-review` 明文 `Skip anything tooling already enforces`——**本仓此前未搬**，
故本条属**向原版靠拢**，不是改动 Matt 的 code review 设计（满足需求方硬约束）。

**附前提条款**（防止开天窗）：以「工具**配置到位**且**被执行过**」为准，逐项生效；
未具备的项两轴**仍须照报**；明文禁止以本条为由跳过语义 / 规格 / 边界 / 权限的实质审查。

### 4.2 撤回记录：`AGENTS.md §9.1–§9.4` 指针化（**已撤回，未纳入提交**）

本线曾把 `§9.1–§9.4` 压成「见 skill `karpathy-guidelines` §N」并**已落盘**，
在复核阶段被三条独立证据推翻，已用 `git show HEAD:AGENTS.md` 二进制写回（**未用 `git checkout --`**），
工作树与 HEAD blob 字节相同（sha1 `46e50882`），**零改动**：

1. `docs/archive/handoffs/HANDOFF_AGENTS_MD_CONSERVATIVE_AUDIT.md:454-455` 的 **R3 表** =
   本仓的「用户已否决清单」：`R3-1` 压缩 §9.5/§9.6、`R3-2` 压缩 §9.2/§9.4 **均已被否决**，
   理由栏写「**用户明确要求补齐**」「压缩等于**回退用户刚做的决定**」。
2. `docs/CODE_REVIEW_2026_09_04.md:13`（另见 `docs/CODE_REVIEW_STEP_ID_FIX.md:10`）明文把
   `AGENTS.md §7 不变量 + §9 Karpathy guidelines` 列为 **Matt Standards 轴的审查基线**
   ⇒ 指针化等于**掏空审查基线**。
3. `git grep -i karpathy`（tracked）**零命中正文**；`karpathy-guidelines` skill 只存在于
   WorkBuddy **本机**用户级技能目录，而 `AGENTS.md` 文件头声明管**所有** Coding Agent
   （ZCode / Codex / WorkBuddy / Claude Code…）⇒ **跨 Agent 失效**。

**结论**：批 1 的性质由「文档瘦身」更正为「**协议机制修订**」。体量 17,849 → 18,801 字符（**+952**），
省的是**轮次墙钟**，不是字符。

---

## 5. 批 2：施工内容（本次）

三个交付 + 一个前置。

### 5.1 前置发现：**现有闸门在本机根本跑不起来**

`scripts/check_review_coverage.sh` 是本仓**唯一的机械闸门**，但它依赖 `dirname` / `wc` / `comm` /
`grep` / `sort` / `mktemp`，而本机 WorkBuddy 沙箱的 bash shim **缺 coreutils**（实测全部
`command not found`）⇒ **该闸门在当前环境下从未被真正执行过**，只能靠人工用 python 复刻口径。

⇒ 批 2 的首要交付因此调整为：**让闸门真的能跑**。

### 5.2 交付 A：`scripts/check_review_coverage.py`（python 版闸门）

**为什么必须有它**：`.sh` 版是本仓唯一机械闸门，但依赖 `dirname`/`wc`/`comm`/`grep`/`sort`/`mktemp`，
而施工环境的 sh 缺 coreutils ⇒ **该闸门在施工环境里从未真的执行过**（`docs/SDD_TICKET_TRACKER.md:2157`
早已登记"本沙箱跑不了"）。本交付把它变成"真的能跑"。

- 口径**逐条对齐** `.sh`：最早 `base` 取法（`base` 保持台账里的**字面量**，故末行可能打印 `089524a~1`
  这类 rev 表达式）、`rev-list <tip> --not <base>` 取并集、`DOC_PATTERN`、白名单 docs-only 自校验、
  「恰好只改 `docs/review_ledger.tsv`」自动放行、死条目告警（不失败）、`--list` 缺口退 2。
- **`.sh` 是语义参考、冻结**：协议按**行号**引用它的 `DOC_PATTERN`（`:51`）与 fail-closed 形状
  （`:74-75`）⇒ **不得**把它改成包装脚本、**不得**重排它的行。双实现的一致性靠"同 tip 同结论"守。
- 性能：全程 **4 次 git 子进程**——`rev-list --parents HEAD`（拿全图）→ `cat-file --batch-check`
  （批量解 rev）→ `log --no-walk --format=%H%x09%h%x09%s`（批量短名 + subject）→
  `show --no-renames --pretty=format:%x01%H --name-only`（批量文件表）；之后全在内存算可达性。
- 实测：**3.6–7.1s**（对比 `.sh` 的逐条 fork：>11 分钟未完，见 §9.4）。
- 施工踩坑（都留在代码注释里）：`git rev-parse --short` **只接受单个 rev**（134 个参数直接
  `fatal: Needed a single revision`）；`cat-file --batch-check` 的 `%(objectname:short)` 原子不展开；
  `%h` 与 `rev-parse --short` 实测 **8/8 逐条相等**，故选后者。

### 5.3 交付 B：`scripts/gate0.py`（≤60 秒快速门禁）

> **与原计划的偏差（诚实记录）**：原写 `gate0.sh`。施工中发现**写 `.sh` 就是重犯本批要修的那个病**
> ——施工环境的 sh 缺 coreutils，任何 `.sh` 车道都会在"推送那一刻"炸掉。⇒ 改为 **python**
> （项目自身语言、零 coreutils 依赖、跨平台），hook 只留几行 POSIX 内建。

- 6 条车道：`git diff --check` + `ruff check .` + `oxlint` + `tsc -b` + 生成物同步守卫（2 文件 6 例）
  + 覆盖闸门（交付 A）。
- 实测墙钟：**热 20–22s / 冷 ≈40s**（预算 60s）。逐条读数见 `docs/agents/verification.md` §2。
- **不做按路径跳过**（与原计划"改动面分类"的偏差）：全量已满足预算，而"按改动路径跳过某条车道"
  属**放松**（跨层影响难以穷举）⇒ 改动面只作**信息展示**，不影响跑什么。
- **fail-closed**：工具缺失 / 超时 / 无法执行 = **失败**，不算"跳过"，不算通过。
- 开关：`--since <rev>`（额外查该范围空白/冲突标记 + 报告改动面）、`--only <lane>`（失败后增量重跑）、
  `--list`。首行打印 `tip=<sha> tree=<tree>`（协议要求读数可指到树）。

### 5.4 交付 C：`.githooks/pre-push`

- 调 `scripts/gate0.py`；`git config core.hooksPath .githooks` 启用（**本地配置、不随仓库分发**）。
- 实测：`git hook run pre-push --to-stdin=<ref 列表>` ⇒ **RC=0 / 6 车道全 PASS / 20.5s**，
  `--since` 正确从 stdin 取到远端 sha。另核：`.git/hooks/` 下**没有任何生效的自定义钩子**
  ⇒ 切 `hooksPath` 不会禁用既有钩子（先查再切，不假设）。
- **顺带修掉一个会让 hook 在所有平台失效的坑**：`.gitattributes` 只钉了 `*.sh` / `*.ps1`，
  而 hook **无扩展名** + `core.autocrlf=true` ⇒ 会被检出成 CRLF，`exit 1` 这种行直接坏
  （正是该文件注释里描述的 `bad interpreter` 病根）。已加 `/.githooks/* text eol=lf`；
  hook 文件本体实测 **0 个 CRLF**。
- 边界：**本地便利，不是安全边界**（`--no-verify` 可绕），**不能**替代 CI 或人工审查。

### 5.5 交付 D：`docs/agents/verification.md`（lane 表）

- 13 条车道（含未接入/人工车道）+ 决策表 + 本机沙箱跑法差异 + 读数纪律 + Gate-0 与完整门禁的边界。
- 覆盖 `AGENTS.md` §14.10 门禁清单与协议 §7 第 6 条点名的工具：`ruff` / `pytest` / `tsc` / `vitest` /
  `oxlint` / `playwright` / `vite build` / 生成物同步守卫 / 覆盖闸门。
- 未在本批实测的车道**显式标注"未在本批实测"**，不填数字（避免"看起来有读数"）。

### 5.6 批 2 的验收（可执行）

| 验收项 | 判据 | 实测 |
| --- | --- | --- |
| 交付 A：自身可跑 | 本机 `.venv` python 直跑、exit 0 | ✅ 3.6–7.1s |
| 交付 A：同 tip 同结论 | 三元组 + ❌ 集合与 `.sh` 参考版逐项相同 | 参考版跑完后对照（见 §9.4） |
| 交付 B：预算 | 全车道墙钟 ≤60s | ✅ 热 20–22s / 冷 ≈40s |
| 交付 B：**负向验证** | 存在未归属 commit ⇒ 必须非 0 | 用**本批自己的代码提交**做（真实负向，不伪造） |
| 交付 C：生效 | `git hook run pre-push` 真的走到 Gate-0 | ✅ RC=0、20.5s |
| 交付 D：覆盖 | 覆盖 §14.10 清单的全部工具 | ✅ 13 条 |
| 零漂移 | 代码面之外无意外改动、闸门读数不变 | 见 §9.3 |


---

## 6. 批 3：计划（**必须最后做**）

内容：`check_review_coverage` **双读兼容** + `docs/review_ledger.d/<sha>.tsv`（去 append 冲突）
+ `docs/gate/<sha>.json`（机器产出的门禁读数，根治"手抄读数"类事故）。

**为什么必须最后**：批 3 动的是**闸门本体**，而前两批的审查行还要写进**老格式**台账。
若先动闸门，前两批就没有可用的判据来源。

**贯穿原则**：**每一批必须由「它没有改的那套规则」来判**——否则会掉进闸门脚本注释里已记录的
「记账要不要被记账」自指死循环（本仓实测绕过三轮）。

---

## 7. 已知瑕疵 / 残余 / 未闭合项

| # | 项 | 性质 | 处置 |
| --- | --- | --- | --- |
| 1 | 协议 L295 引用 `§8.3.1`，而全文**无该小节** | **既存缺陷**（悬空引用） | 按 `AGENTS.md` §8 Scope Lock **只登记不修**；批 2/3 择机 |
| 2 | `§8.3` 标题「审查预算与"修后重审"的有界收口」**未覆盖第 7 条**（后者的主题是"审查报什么"） | 可发现性 | **仍未改**：批 2 只动了 §8.3 第 7 条的**前提块事实**（下表第 7 行），没动标题——改标题会牵动 §8.3 的被引用面（协议 L23 / L394 等），留给批 3 |
| 3 | 台账有 **33 条死白名单条目**（写了但未被用到） | 冗余 | 闸门脚本明示**不失败**；清理由批 3 的台账重构承担 |
| 4 | `docs/SDD_TICKET_TRACKER.md` 单文件 **420,673 字符** | 体量 | 本线未处理，需单独立项 |
| 5 | 后端 `ruff` **无任何配置文件** | ~~牙齿弱~~ ⇒ **原判断被实测推翻**（见 §8.4） | **不补配置**（补了只是把上游默认集抄一遍，反而更难复核）；改为更正协议事实 + 记录"强度随 ruff 版本漂移" |
| 6 | 两侧无 CI | 无远端强制 | 本线**不**引入 CI（需求方未要求；pre-push 只是本地替代，且只在本 clone 生效） |
| 7 | 协议 §8.3 第 7 条的**前提块事实**在批 2 后过期（原文写「无 pre-push 强制」、「后端 ruff 无配置 ⇒ 牙齿弱」，且判据写成「仓库里有没有配置」） | **同批已更正** | 改为「判据 = **环境内跑没跑过**」+ 记录 Gate-0 的真实覆盖范围 + 写明**豁免不得跨环境传递** |

---

## 8. 施工者自曝的失误（诚实记录）

### 8.1 纠正外部输入的两处错误前提

- 外部输入称「你的 `/implement` 已自动调 `/code-review`，不必再跑」⇒ **不成立**：本机
  `mp-eng-implement` 仅 16 行且 `disable-model-invocation: true`。
- 外部输入称「按 Matt 设计排序，Runtime Verification 应放 Code Review **之后**」⇒ 其前提（上一条）已断；
  且 RV 与 Code Review **无数据依赖**、本仓 §8.2 已证明并行可行 ⇒ 正确结论是**二者并行**，
  真正该占"最前"的是 **Gate-0（≤60 秒）**。

### 8.2 本线施工者当日连错 4 次的**验收断言**（全部在断言侧，非数据侧）

1. **方向写反**：把「旧规则已删」的检查写成「旧文本应存在」⇒ 它 FAIL 其实是**好事**，但报告会误导。
2. **用错命令默认行为**：`git log --name-only` 对 merge **恒空**，而真实脚本用 `git show`（走 `--cc`）。
3. **全文件位置断言**：`led.index('5c7db24') < led.index('45a367f')`——后者在**审查行段**也出现 ⇒ 比较无意义。
4. **子串切段**：`split('[whitelist]')` 命中头部注释 `# [whitelist] 段：…` ⇒「段首」变成注释续行。

⇒ **纪律**：「看起来能找到」的最简写法就是假红的来源；**每条"报错"先问「是我的断言错了还是数据错了」**。
本轮 **4/4 全是断言侧**——若照单全收，就会去改一份**没有问题的台账**。

### 8.3 一处环境坑（已记录，非代码问题）

本机沙箱对 `refs/remotes/origin/` 命名空间**读不到也写不进**：`git update-ref
refs/remotes/origin/main <sha>` 报 **rc=0 但 ref 不出现**（假成功）⇒ `git status -sb` 恒显示
`...origin/main [gone]`（**假警报**）。**判断"推没推"只认 `git ls-remote --heads origin`。**
同轮确认 `refs/heads/`(5) 与 `refs/tags/`(4) **零损伤**。

### 8.4 一处**被推翻的自身判断**：ruff「牙齿弱」（→ 必须实测，不能推断）

批 1 我把「后端 `ruff` **无 `[tool.ruff]` 配置**（仅默认规则集）」写成「牙齿弱 ✗」，并据此把
"补 ruff 配置"列进批 2 交付。施工中**实测推翻**：

```bash
git ls-files | grep -E 'ruff\.toml|\.ruff\.toml|setup\.cfg'          # → 无任何配置文件
printf '# -*- coding: utf-8 -*-\nimport os\n' \
  | ruff check --isolated --stdin-filename x.py -
# → 2 errors（UP009）：**--isolated 忽略一切配置** ⇒ 这些规则来自 ruff 0.16.3 的**上游默认集**
```

⇒「默认规则集」并不弱（`S110` / `BLE001` / `PLW1510` / `FURB188` / `PIE810` 都在默认集内）。
**教训：「没有配置文件」≠「没有规则」——规则集强度必须实测，不能从"有没有配置"推断。**

### 8.5 交付 B 自身被本仓 `ruff` 抓到 7 条 finding（工具确实在干活）

写交付 A 时被 `ruff check .` 抓到 **7 条**，全部是我写的：`UP009`（多余 UTF-8 声明）、
`S110` + `BLE001`（`try/except Exception/pass`）、`PLW1510`（`subprocess.run` 缺显式 `check=`）、
`FURB188` ×2（`endswith` + 切片 ⇒ `removesuffix`/`removeprefix`）、`PIE810`（合并 `startswith`）。
**这本身就是 §8.3 第 7 条前提成立的实证**（工具能报 ⇒ 审查不必重复报）。

### 8.6 交付 A / B 施工中我自己写出的 2 个 bug（都被"读数对不上"抓出来）

1. `git rev-parse --short` **只接受单个 rev**：批量传 134 个 ⇒ `fatal: Needed a single revision`。
   改用 `git log --no-walk --format=%h`（实测与 `rev-parse --short` **8/8 逐条相等**）。
2. `split("\n")` 未先 `rstrip` ⇒ 把 1 行读成 2 行（于是误报"返回 2 行，期望 1 行"）。

⇒ 两个都**不是逻辑错**，而是**对 git / 字符串行为的错误假设**；拦住它们的仍然是"读数对不上三元组就停"。


---

## 9. 证据与工具索引

### 9.1 可复跑命令

```bash
git rev-parse --short HEAD
git log -120 --name-only --no-renames --pretty=format:'@@%H'
git ls-remote --heads origin main
git grep -n "不算超预算"                     # 旧条款残留（应只剩删除说明那一处）
git grep -n "按同一标准再修"                 # 应为零命中
git grep -n "§8\.3 第 [0-9]"                 # 第 4 条被引用的位置
python scripts/gate0.py --list                       # Gate-0 的 6 条车道
python scripts/gate0.py                              # 全车道（≤60s，20–40s 实测）
.venv/Scripts/python.exe scripts/check_review_coverage.py --list   # 覆盖闸门三元组
PATH="<PortableGit>/usr/bin:$PATH" bash scripts/check_review_coverage.sh --list  # 参考实现（很慢）
```

### 9.2 判定一个 commit 是否有台账归属（闸门口径）

```bash
git show --no-renames --pretty=format: --name-only <sha>   # merge 走 --cc，与 git log 不同
```

### 9.3 本线用到的机械判据

- **覆盖闸门（可运行实现，本批新增）**：`python scripts/check_review_coverage.py`，实测 **3.6–7.1s**。
- **覆盖闸门（语义参考实现，冻结）**：`scripts/check_review_coverage.sh`。
  ⚠ 本文早先写它「本机跑不动」——**已更正**：coreutils 其实就在 Git 自带目录里
  （`<PortableGit>/usr/bin`，`dirname`/`wc`/`comm`/`sort`/`mktemp` 全套），
  `PATH="<PortableGit>/usr/bin:$PATH" bash scripts/check_review_coverage.sh` **能跑**，只是**很慢**
  （逐条 fork git；本批实测 >11 分钟未结束）。
- **Gate-0**：`python scripts/gate0.py`（6 车道；热 20–22s / 冷 ≈40s）。
- **闸门当前读数**：`09ca47a..HEAD` = **451 提交 / 已审 317 / 待判定 134 / ❌ 0**（python 版，exit 0）；
  死白名单告警 **33 条**（与 §7 第 3 行的既有登记一致，构成一次独立印证）。


### 9.4 本文件的位置与状态 / 待补的对照读数

- 路径：`docs/agents/SDD_ACCELERATION_AUDIT.md`
- 本文为**活文件**：批 2 已施工（§5 按实测改写、§7/§8 已更正）；批 3 完成后继续更新。
- **待补（唯一未闭合的验收项）**：`.sh` 参考版在**同 tip** 上的完整读数。本批已启动该对照，但
  它逐条 fork git、**>11 分钟未结束** ⇒ 不阻塞其余验收：python 版本身已在同 tip 给出
  `451 / 317 / 134 / ❌ 0` 且 exit 0，且它的口径是**照着 `.sh` 注释里记录的全部实测坑**写的
  （含 `--no-renames`、BOM/CR 剥离、`awk NF`、merge 的"核对不了就不放行"）。
