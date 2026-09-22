# SDD 提速改造 · 工作记录与外部复核包

> **状态（2026-09-22 收工复核；本文上一条状态已过期，按实测改写）**：**批 1 已推送**（`origin/main` = `22aa291`）。
> **批 2 的完整链条已走完并落在本地**：施工 `66cc1fa`/`9527559`/`22992e4`/`acde040` → 两轴独立审查 →
> findings 全修 → 修后重审 → 更正 `ebb28b4`/`9bb51c4b` → 台账笔 `aa5f9e2a` → 收口 docs 笔 `337a858b` →
> 台账白名单笔 `ea4f7c65`；覆盖闸门在**工作树**与**干净检出**两个口径都曾 exit 0。
> ⚠ **本地 `refs/heads/main` 已不是 `ea4f7c65`**——**另一条线**（#200 `cached_tokens` 真机取证 + 第十五轮真机
> 逐控件审计）在 **20:49:01 / 20:49:07** 于同一工作树追加了 `cd02c61` 与 `29cb5081` 两笔。
> 当前 `refs/heads/main` = `29cb5081`，**领先 `origin/main`（`22aa291`）17 笔，全部未推送**
> （用户 20:36 明令「暂时不要推」）。
> ⚠ **闸门现状 = RED**：`python scripts/check_review_coverage.py` **exit 1**；`09ca47a..HEAD` **468 提交 /
> 已审查 331 / 待判定 137**，`❌` **2** 条 = `cd02c61`（docs-only 却未声明白名单）+ `29cb5081`
> （**代码提交**，按 §7 第 8 条**永远不能走白名单**，必须有真实审查行）。这两笔**不是本线产出**，
> 本线**不擅自替它写审查行**（代码提交的审查必须独立且真实）——见 §10.5。
> **本轮新增（本线）**：协议新增 **§9「阶段 → 外部 Skill 引用」** + 搬运 9 个 pstack skill（逐字节，MIT）
> 到 `docs/agents/skills/`——见 **§10**。该笔是 docs-only，走白名单，并**主动登记「待补审」**。
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
   （**§9.4 已给出实测对照结果**：三元组与共同判定行全等，唯一差异是短 sha 显示宽度。请复核该对照的证据力。）
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
`grep` / `sort` / `mktemp`，而本机 WorkBuddy 沙箱 bash 的 **PATH 里没有 coreutils**（实测全部
`command not found`）⇒ 直接跑它在第一行 `cd "$(dirname "$0")/.."` 就死 ⇒ **该闸门在本机的
默认跑法下从未执行过**，只能靠人工用 python 复刻口径。

⇒ 批 2 的首要交付因此调整为：**让闸门真的能跑**。

### 5.2 交付 A：`scripts/check_review_coverage.py`（python 版闸门）

**为什么必须有它**：`.sh` 版是本仓唯一机械闸门，但依赖 `dirname`/`wc`/`comm`/`grep`/`sort`/`mktemp`，
而施工环境 bash 的 **PATH 里没有 coreutils** ⇒ 直接跑它在第一行就死 ⇒ **该闸门在施工环境的默认
跑法下从未执行过**（`docs/SDD_TICKET_TRACKER.md:2157` 早已登记"本沙箱跑不了"）。本交付把它变成
"默认就能跑、且快"。

> **2026-09-22 更正（批 2 施工中，两轴 Standard 轴 findings）**：coreutils **并非不存在**——它们在
> Git 自带目录里（`<PortableGit>/usr/bin`）。
> `PATH="<PortableGit>/usr/bin:$PATH" bash scripts/check_review_coverage.sh` **能跑**，只是**很慢**
> （逐条 fork git；全量 >25 分钟未完成，见 §9.4）。原话"跑不动"说的是**默认 PATH**，不是"不可能跑"；
> 已同步更正的位置：本文件 §5.1/§5.2、协议 §8.3 第 7 条前提块、`check_review_coverage.py` 的模块
docstring；`gate0.py` 里只有一处内联注释提到 coreutils，也已同批改掉（两轴 Standards 轴指出原句
把四个位置混为一谈，属实，已按位置逐一列明）。

- 口径**逐条对齐** `.sh`：最早 `base` 取法（`base` 保持台账里的**字面量**，故末行可能打印 `089524a~1`
  这类 rev 表达式）、`rev-list <tip> --not <base>` 取并集、`DOC_PATTERN`、白名单 docs-only 自校验、
  「恰好只改 `docs/review_ledger.tsv`」自动放行、死条目告警（不失败）、`--list` 缺口退 2。
- **`.sh` 是语义参考、冻结**：协议按**行号**引用它的 `DOC_PATTERN`（`:51`）与 fail-closed 形状
  （`:74-75`）⇒ **不得**把它改成包装脚本、**不得**重排它的行。双实现的一致性靠"同 tip 同结论"守。
- 性能：全程 **4 次 git 子进程**——`rev-list --parents HEAD`（拿全图）→ `cat-file --batch-check`
  （批量解 rev）→ `log --no-walk --format=%H%x09%h%x09%s`（批量短名 + subject）→
  `show --no-renames --pretty=format:%x01%H --name-only`（批量文件表）；之后全在内存算可达性。
- 实测：**3.6–7.1s**（对比 `.sh` 的逐条 fork：**>25 分钟**未完，见 §9.4）。
- 施工踩坑（都留在代码注释里）：`git rev-parse --short` **只接受单个 rev**（134 个参数直接
  `fatal: Needed a single revision`）；`cat-file --batch-check` 的 `%(objectname:short)` 原子不展开；
  `%h` 与 `rev-parse --short` 在同一入参形态下实测 **8/8 逐条相等**；因为前者能一次吃多个 rev，
  **故选前者**（原文误写作"故选后者"，2026-09-22 更正）。

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
  而 hook **无扩展名** + `core.autocrlf=true` ⇒ 会被检出成 CRLF，`exit 1
` 这种行直接坏
  （正是该文件注释里描述的 `bad interpreter` 病根）。已加 `/.githooks/* text eol=lf`；
  hook 文件本体实测 **0 个 CRLF**。
- 边界：**本地便利，不是安全边界**（`--no-verify` 可绕），**不能**替代 CI 或人工审查。

### 5.5 交付 D：`docs/agents/verification.md`（lane 表）

- 13 条车道（含未接入/人工车道）+ 决策表 + 本机沙箱跑法差异 + 读数纪律 + Gate-0 与完整门禁的边界。
- 覆盖 `AGENTS.md` §14.10 与协议 §7 第 6 条**点名的工具**：`ruff` / `pytest` / `tsc` / `vitest` /
  `oxlint` / `playwright` / `vite build` / 生成物同步守卫 / 覆盖闸门。
  ⚠ 原表述写作"覆盖 §14.10 清单"，**过强**（两轴 Spec 轴 findings）：§14.10 里 Working Tree clean /
  Diff 可解释 / 未误删文件 / 未覆盖其他 Agent 成果 / 无 Scope 外修改等项是**人工判据**，
  不在机器车道里，本文只覆盖**工具可判**的那部分。
- 未在本批实测的车道**显式标注"未在本批实测"**，不填数字（避免"看起来有读数"）。

### 5.6 批 2 的验收（可执行）

| 验收项 | 判据 | 实测 |
| --- | --- | --- |
| 交付 A：自身可跑 | 本机 `.venv` python 直跑、exit 0 | ✅ 3.6–7.1s |
| 交付 A：同 tip 同结论 | 三元组 + ❌ 集合与 `.sh` 参考版逐项相同 | ✅ `22aa291`：两版**同为 `451 / 317 / 134`**（`.py` 另给 `❌ 0 / exit 0`）；共同判定行 **9/9 逐字相同、0 分歧**。详见 §9.4 |
| 交付 B：预算 | 全车道墙钟 ≤60s（**目标**；超了只告警，不改变判定） | ✅ 热 20–22s / 冷 ≈40s |
| 交付 B：**负向验证** | 存在未归属 commit ⇒ 必须非 0 | 用**本批自己的代码提交**做（真实负向，不伪造） |
| 交付 C：生效 | `git hook run pre-push` 真的走到 Gate-0 | ✅ RC=0、20.5s |
| 交付 D：覆盖 | 覆盖 §14.10 清单的全部工具 | ✅ 13 条 |
| 零漂移 | 代码面之外无意外改动 | ✅ `git diff --name-only 22aa291..HEAD -- src tests web` **为空**（本批只动 `scripts/`、`.githooks/`、`.gitattributes`、`docs/`） |
| **全量门禁**（`AGENTS.md` §14.10） | 全量 `pytest` **0 failed** | ⚠ **1 failed / 3095 passed / 13 skipped（619s，树 `6b95cef2da69`）** —— 唯一失败 `tests/observability/test_flush_lifecycle.py::test_web_lifespan_flushes_on_shutdown`，已定性为**环境**（外部进程持 `.instance.lock`），**非本批回归**；三重证据 + 受控实验见 §8.7 |


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
| 8 | 全量 `pytest` 在 `9bb51c4b` 上 **1 failed**（`test_web_lifespan_flushes_on_shutdown`） | **环境**（外部进程持 OS 级排他锁），非本批回归；三重证据 + 受控实验见 §8.7 | 解除条件：终止该外部 uvicorn（或换 session root）后复跑该用例，期望 `3 passed`。**不许**用 `ALLOW_SHARED_ROOT=1` 凑绿（那是降级放行，会把真锁冲突静默掉） |

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

### 8.6 交付 A / B 施工中我自己写出的 3 个 bug（都被"读数对不上"抓出来）

1. `git rev-parse --short` **只接受单个 rev**：批量传 134 个 ⇒ `fatal: Needed a single revision`。
   改用 `git log --no-walk --format=%h`（实测与 `rev-parse --short` **8/8 逐条相等**）。
2. `split("\n")` 未先 `rstrip` ⇒ 把 1 行读成 2 行（于是误报"返回 2 行，期望 1 行"）。
3. **（批 3 之前、交付 B 自己打出来的）** `surface_report` 对 `git status --porcelain` 的行**先 `strip()`
   再切前 3 列**：而 ` M x` 的**前导空格本身就是状态列的一部分** ⇒ 路径被吃掉一个字符，
   实测把 `scripts/…` 显示成 `cripts/…`。拦住它的是**我自己那条车道的输出**（"改动面：3 文件 — `cripts 2`"
   这个明显不存在的目录名）。已在 `gate0.py` 改为"先按原行切 3 列、再 strip"，并回读确认显示恢复为
   `scripts 2  .zcodeignore 1`。

⇒ 三个都**不是逻辑错**，而是**对 git / 字符串行为的错误假设**；拦住它们的仍然是"读数对不上就停"。
**第 3 条的教训最强**：如果不是我要求门禁把自己看到的改动面打出来，这个 bug 会静默留在脚本里。

#### 8.6.1 附带确认的一条 git 行为（给未来的对照者）

`%h` / `git rev-parse --short` 的输出宽度**取决于入参形态**（本仓 git 2.52.0.windows.1，可复跑）：

```bash
git log --no-walk --format=%h 09ca47a1                                     # → 09ca47a1  (8 位：入参就是 8 位缩写)
git log --no-walk --format=%h 09ca47a12c45bf273e46494be0fedef143cd89d3     # → 09ca47a   (7 位：入参是全长)
git rev-parse --short 09ca47a12c45bf273e46494be0fedef143cd89d3             # → 09ca47a   (7 位)
```

⇒ **同一提交可以有 7 位与 8 位两种显示**。这条直接决定了 `.sh` ↔ `.py` 的对照纪律：
**读数文本不可逐字比对，判定集才能**（详见 §9.4）。

**再补一条（两轴 Spec 轴 findings，复核后更正）**：宽度不只随**入参形态**变，还随**跑它的 git
二进制**变——同一条 `.sh` 在本会话里打印 8 位（`09ca47a1`），在审查者的调用上下文里打印 7 位
（`09ca47a`）。⇒ 本节早先那种"宽度差 = 实现差异"的读法**不成立**：宽度是**环境属性**。



### 8.7 全量门禁唯一 1 条失败：**环境**（外部进程持 `.instance.lock`），附带受控实验

**读数**（树 `6b95cef2da69` / tip `9bb51c4b`，`PYTHONUTF8=1 PYTHONPATH=` 全量跑）：

```text
tests=3095  failures=1  errors=0  skipped=13  time=619.065
FAILED tests/observability/test_flush_lifecycle.py::test_web_lifespan_flushes_on_shutdown
  agent_harness.instance_lock.InstanceLockError: 另一个进程已在写同一个 session root，启动被拒绝。
    锁文件：D:\intelligence-agent-backend\.agent\workspace\.instance.lock
    占用者：pid=25888 / started_at=2026-09-22T10:48:32Z / root=D:\intelligence-agent-backend\.agent\workspace
```

**归属判定的三重证据**（不是"应该是环境问题"，是可判定的三条）：

1. **机械**：本批 `git diff --name-only 22aa291..HEAD -- src tests web` = **空** ⇒ 不可能引入这条失败。
2. **隔离复跑仍红**：只跑该文件（3 例，6.92s）⇒ `1 failed, 2 passed` ⇒ 排除"全量串跑的跨用例污染 /
   SSE 闩锁竞态"（`ADR-0038` 那一类机制）。
3. **根因可指认**：`msvcrt.locking(LK_NBLCK)` 抛 `PermissionError`（锁**真的被别的进程持有**）；
   锁文件里报的占用者是 **`python.exe -m uvicorn agent_harness.web.app:create_app --host 127.0.0.1
   --port 8000`，创建于 2026-09-22 18:48:12** —— 早于本批开工、**不是本线起的**。
   这与文档既有的两处登记是同一个坑：`docs/SDD_TICKET_TRACKER.md:928`、
   `docs/integration/FRONTEND_MEM5_INTEGRATION_PROMPT.md:107-108`。

**受控实验（把"锁"从"环境"里单拎出来）**：`Settings.workspace_dir` 默认值是**相对路径**
`.agent/workspace`（`src/agent_harness/config.py:106`）⇒ 换 cwd 就换了 session root。
在钉住同一棵树 `6b95cef2da69` 的克隆里跑同一条用例、同一解释器、同一用例集合：

```text
主工作树（锁被外部 uvicorn 持有）  : 1 failed          RC=1
克隆（workspace=<clone>/.agent/…）  : 3 passed in 6.14s RC=0
```

⇒ **唯一变量是 session root / 锁占用**，根因确认。**不作假绿**：不动 `ALLOW_SHARED_ROOT=1` 逃生门
（那是"知情的降级放行"，会把真锁冲突静默掉），也不去 kill 别人的进程。

⚠ **不推翻** `ADR-0038 §4` 对"外部持锁"假说的排除：那条实验覆盖的是**另一组用例**
（`test_workspace_files_api + test_web_stream + test_web_api`）且用的是"只取该锁"的受控进程；
本次是**真在跑 app 的 uvicorn**，两者不矛盾。

**残余（唯一未闭合项）**：本批因此**拿不到"全量门禁 0 failed"的读数**。解除条件写在 §7 第 8 行。

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
- **闸门读数（两棵树，别混用）**：
  · **`22aa291`（批 2 落盘前，交付 A 的验收读数）**：`451 / 317 / 134 / ❌ 0`、exit 0 ——
    2026-09-22 已在**钉住该 sha 的克隆里现场复现**（`git clone` → `checkout --detach 22aa291` →
    把当前 `.py` 放进去跑），不是靠记忆。
  · **`9bb51c4b`（批 2 六笔落盘后、写台账前）**：`457 / 317 / 140`、**❌ 6**、exit 1 —— 这 6 笔就是
    本批自己的提交，**在补审查行与白名单声明之前必然显示为未归属**，是**预期的中间态**而不是缺陷
    （先落代码、后记账，正是本批要说明的次序；`ebb28b4` 被逐个点名）。
  · 死白名单告警 **33 条**（与 §7 第 3 行的既有登记一致，构成一次独立印证）。


### 9.4 本文件的位置与状态 / `.sh` ↔ `.py` 对照结果

- 路径：`docs/agents/SDD_ACCELERATION_AUDIT.md`
- 本文为**活文件**：批 2 已施工 + 已过两轴独立审查 + findings 已修；批 3 完成后继续更新。

#### 9.4.1 `.sh` ↔ `.py` 双实现对照（2026-09-22，**全量**完成）

对照条件（同一棵树、同一份台账）：两个实现都在**钉住 tip `9bb51c4b`（tree `6b95cef2da69`）的本地
克隆**里跑，台账取该 clone 自带的那份。

| 对照项 | `.sh`（语义参考，冻结） | `.py`（可运行实现） | 结论 |
| --- | --- | --- | --- |
| 覆盖区间 | `09ca47a1..HEAD` | `09ca47a..HEAD` | 同一提交；宽度差异见 9.4.2 |
| 三元组 | `457 / 317 / 140` | `457 / 317 / 140` | **逐项相同** |
| 退出码 | `1` | `1` | **相同** |
| 逐条判定条数 | **140**（❌ 6） | **140**（❌ 6） | **逐条全等**：仅 `.sh` 独有 0 条、仅 `.py` 独有 0 条 |
| ❌ 集合 | `22992e4` `66cc1fa` `9527559` `9bb51c4` `acde040` `ebb28b4` | 同左 | **完全相同**（恰好是本批 6 笔未记账提交） |
| 文本（剥掉 sha 宽度后） | —— | —— | **0 处不一致** |

⇒ 交付 A §5.6「同 tip 同结论」的判据**已全量满足**（此前只做到"共同前缀 9 条"，现已补齐）。
复跑方式（`.sh` 的 `T_START`/`T_END` 会把 tip 打出来，可自证在同一棵树上）：

```bash
git clone --local --no-hardlinks D:/intelligence-agent-backend <clone>
cd <clone> && git checkout --detach 9bb51c4b
python scripts/check_review_coverage.py                                     # 457/317/140, ❌6, exit 1
PATH="<PortableGit>/usr/bin:$PATH" "<PortableGit>/bin/bash.exe" scripts/check_review_coverage.sh
```

#### 9.4.2 短 sha 的显示宽度：**环境属性，不是实现差异**（本段曾被写错，已更正）

`.sh:104` 是 `printf '覆盖区间: %s..HEAD' "$(git rev-parse --short "$base")"`；`.py` 用
`%h` —— **两者都走 git 缩写**。宽度取决于「入参形态 + 跑它的 git 二进制」，实测同一提交
既有 8 位（`09ca47a1`）又有 7 位（`09ca47a`）两种显示（见 §8.6.1 的三条可复跑命令）。

> ⚠ **更正记录（两轴 Spec 轴 P2）**：本小节上一版把差异归因给「`.sh` 回显台账字面量、`.py`
> 归一化」，并据此宣称这是"唯一的真实差异"。**该归因是错的**：`.sh:104` 用的是
> `git rev-parse --short`（不是回显），且同一条 `.sh` 在审查者的上下文里打印的就是 7 位。
> 已按实测改写；`check_review_coverage.py` 的对应注释同步收敛为**指针**（§16.1：不重复叙述）。

⇒ 对照纪律（只需记住这一条）：**比判定集，不比读数文本**；差异必须能被归因到「显示」而不是
「判定」，否则才是漂移。

#### 9.4.3 本批仍未闭合的项（如实登记，不含结转项）

1. **全量 pytest 的 0 failed**：本机拿不到（外部进程持 `.instance.lock`）——见 §8.7，解除条件见 §7 第 8 行。
2. **`tests/` 对这两个新脚本零覆盖**（两轴 Standards 轴 P4）。本批不加测试（Scope Lock），
   但这是已知欠缺：它们的判据目前只由「与 `.sh` 的全量对照 + 本文的复跑命令」兜着。
3. **`split_fields()` 的 tab 折叠**：当前台账 103 行审查行里 **0 行**会触发差异（审查者实测）。
   保留它的理由是「语义等价于 bash `IFS=$'\t' read`」（双实现口径等价的前提），不是防未来输入；
   该说明已写进函数 docstring，并给出"若判定为投机抽象则回退成 `split`"的代价。
4. **`5c7db24` / `ebb28b4` 的审查范围归属**：`5c7db24`（批 1 协议修订）本次**补审**；它指名的
   `scripts/gate0.sh` 从未存在（`git log --all -- scripts/gate0.sh` 为空），已由批 2 的 `ebb28b4`
   改成 `gate0.py` —— 上一轮审查若只取 `b529aa5...22aa291` 会把该更正笔漏在范围外，已按包含
   `ebb28b4` 的范围登记。


---

## 10. pstack 复用落地（2026-09-22 本轮新增）

### 10.1 需求方指令，以及我此前理解错在哪

需求方 2026-09-22 20:49 原话：「**pstack 你得好好借鉴，你可以把里面有价值的 skills 下载下来，不能全量安装，
你用到几个就下几个，能复用 pstack 直接复用，不要自己写 skills**，你可以引导模型，比如在某个阶段让模型
调用某个 skill 去执行，就是引用。」

同日 21:27 追加两条硬约束（**这两条改变了选型结果**）：

> 「pstack 里面你下了哪些？**只能下载我们能用上的**啊，比如 Matt 的 `/implement` 和 pstack 的
> `/poteto-mode` 功能一致，下载 `/poteto-mode` 完全就是干扰模型了，**mattpocock 的是主开发 skills，
> pstack 只是打辅助**，而且我会在 codex 和 zcode 里面都装上你下载的 skills。」

**自曝**：本文 §1 那份「pstack 原则对照 6 条」是**读二手摘要**写的——我只搬了**原则的文字**，
**一个 skill 本体都没搬，也从未 clone 上游**。而 pstack 里**恰好就有增量验证的机器**：
`principle-sequence-verifiable-units` 就是「失败后不整条重跑」这条原则的表达式，
`create-verification-skill` 直接产出 feature map。需求方点出这一点是对的，我此前把「借鉴」做成了「复述」。

**第二轮自曝（21:27 追加）**：首轮我搬了 **9** 个，其中 **2 个与 Matt 主开发 skills 功能重合**——

- `tdd`：`~/.codex/skills/tdd/SKILL.md` 与 `~/.zcode/skills/tdd/SKILL.md` **已存在**（同一份 3541 B 的
  Matt 英文版）⇒ **同名撞目录**，装进去会覆盖或遮蔽；且 `mp-eng-implement` 正文明文
  「**Use /tdd where possible**, at pre-agreed seams」⇒ 这一步的路由**本来就归 Matt**。
- `principle-test-behavior-not-implementation`：与 Matt `tdd` 的 `## What a good test is`
  （「Tests verify behavior through public interfaces, **not implementation details**」）
  与 `## Anti-patterns` 第一条 `Tautological`（「the assertion recomputes the expected value the way
  the code does … Expected values must come from an independent source of truth」）**是同一份内容**。

**这两个已撤销**，撤销证据（搬进来时的 sha256 前 32 位）留在
`docs/agents/skills/PROVENANCE.md` §2.1，防止以后又被加回来。

### 10.2 实际做了什么（可复跑）

| 步骤 | 读数 / 命令 |
| --- | --- |
| 抓上游 | `git clone --depth 1 --filter=blob:none --sparse https://github.com/cursor/plugins.git` → `git sparse-checkout set pstack`。**不装插件、不跑任何上游脚本** |
| 上游版本 | `53e579f1481697931fc44f5445171397cfa2b24b`（2026-09-21 19:40:52 -0700） |
| 许可 | **MIT**，`Copyright (c) 2026 Lauren Tan`；全文**逐字节副本**在 `docs/agents/skills/pstack-LICENSE.txt` |
| 上游 skill 总数 | **47**（全部 ≤ 300 行，零依赖、零构建） |
| **最终留存** | **7 个 skill**（≈15%）+ 3 个 feature-map 示例 + 1 个 TSV 模板 + 1 份许可 = **12 个内容文件**（`PROVENANCE.md` 另计） |
| 逐字节校验 | 12 个内容文件 `copy → 回读 → bytes 比对` **全 OK**；sha256 清单见 `PROVENANCE.md` §6 |
| 安全扫描 | 正则（`curl`/`wget`/`https?://`/`nc`/`ssh`/`scp`/`eval`/`exec(`/`base64`/`.env`/`.ssh`/`id_rsa`/credential/password/secret/token/api_key/`rm -rf`/`chmod 777`/`sudo`）扫上游 **全部**文件 ⇒ **60 处命中，全部落在未搬运的 skill 内**（最重两条：`make-bot-ui` 的 `curl … \| sudo sh`、`poteto-mode/scripts/watch-pr/github.ts` 的 token 处理）；留存的 7 个**只命中 3 处**，全是 `create-verification-skill/references/feature-map-example/*.md` 里的示例本地地址 `http://127.0.0.1:4173` |
| 上游全集对比 | 只搬散文（`.md`/`.tsv`/`.txt`），**不搬任何可执行文件** |

### 10.3 选型判据（**两道闸门**，第二道是 21:27 才补上的）

**闸门 ①（有没有消费阶段）**：这个 skill 会被协议**按名字引用**吗？没有就不搬。
未搬的 38 个里：① 本仓 / Matt 已有等价物；② 属别的宿主（`setup-pstack` / `poteto-mode` /
`make-bot-ui` / `automate-me` 依赖 Cursor 插件与自动化宿主）；③ 带可携带的攻击面（见 §10.2 扫描）。

**闸门 ②（与 Matt 主开发 skills 是否功能重合）**：把两者正文摊开对照，**同一步骤会不会有两种说法**。
重合 ⇒ 不下载。这一道是 21:27 之后补的，命中 2 个（见 §10.1）。
完整逐项对照表（含"保留但从宽标注"的 2 个判断项：`principle-sequence-verifiable-units` 与
`show-me-your-work`）在 `docs/agents/skills/PROVENANCE.md` **§2**。

**留存的 7 个，每一个都对应一条 Matt 侧没有的能力**：

| skill | Matt 侧为何覆盖不到 |
| --- | --- |
| `principle-sequence-verifiable-units` | Matt `to-tickets` 管**拆票**；这条多出的是「**提交 / PR 的堆叠顺序本身要能自证**给 reviewer」，对应本仓 §8.4 第 3 条「逐票落 commit，压成一个 commit 读数就不属于任何单票」 |
| `blast-radius` | Matt `to-tickets` 只在"宽重构"语境提过 blast radius 这个词；这条是完整方法论（确定性阶梯 1→5，「**到不了"跑真代码"一级的安全事实必须写 `unproven`**」）——issue #292 的底座 |
| `principle-prove-it-works` | Matt `code-review` 审的是 **diff**；这条管「对**真实产物**取证、不用代理指标 / 自报 / "能编译"，**能脚本化就脚本化**」 |
| `create-verification-skill` | 无对照。产 feature map（`Sub-features` / `How to get to it (user POV)` / `Driving it with <harness>` / `Gotchas`）——issue #292 的方法来源 |
| `maintain-verification-skill` | 无对照。feature map 的维护环（源波次 ∥ live 波次；维护期间禁止改产品代码） |
| `principle-encode-lessons-in-structure` | 无对照。「同一条指令写第二遍时，编码成结构」——#292 / #293 的动机表述 |
| `show-me-your-work` | Matt `handoff` 是"把对话压成交接文档"；这条是"**长跑作业的决策轨迹 TSV**"。功能不同，但本仓台账已覆盖其大半 ⇒ **判断项**，增量只有格式规范与两条纪律（evidence 是指针 / append-only 永不改历史），供 #293 |

### 10.4 引用怎么落进协议

- 新增 **§9「阶段 → 外部 Skill 引用（vendored，不自造）」**：一张表把每阶段该读的文件钉住，开头先写明
  **主从关系**（Matt 主开发 / pstack 辅助），且**与 Matt 重合的一律不 vendored、不安装**。
- 在 **§1.2 第 2 条**（逐票 focused tests）加了**使用点指针**，并明确 TDD 与测试质量**归 Matt**。
- **执行语义 = 「按仓库内相对路径读该文件，并按正文执行」**，不写成"调用某工具里的某 skill"。
  理由：`AGENTS.md` 文件头写明谁在干活谁是主开发（ZCode / Codex / WorkBuddy / Claude Code），
  各家 skill 装载机制不同；按**仓库内相对路径**引用是唯一对各家**同时**成立的形式。
- **§2 的双轴独立审查一个字没动**（需求方硬约束「mattpocock 的 code-review 不能改变」）。

### 10.5 **装进 codex / zcode 之前必须知道的两件事**（需求方 21:27 说明会安装）

1. **`disable-model-invocation: true` 会拦住模型**。上游每个 skill 的 frontmatter 都带这个字段
   （语义 = 「**只允许用户 `/` 手动调用，模型不得自动调用**」；本机 `~/.codex/skills/handoff/SKILL.md`
   用的是同一字段，说明 codex 认它）。而协议要的恰是"**在某个阶段由模型调用**"⇒ 原样装进去等于
   装了个模型碰不到的 skill。`PROVENANCE.md` **§4.1** 给了一段可直接复制的去字段命令。
2. **装之前查同名撞目录**：`ls ~/.codex/skills/<name> ~/.zcode/skills/<name>`。有输出就**停下来判断**
   —— 同一功能就不要装（这正是 `tdd` 的教训）。
   `PROVENANCE.md` §4.1 同时给了这两条命令。

### 10.6 明确登记：这一笔走白名单，且**待补审**

新增/修改的文件全部命中 `DOC_PATTERN`（`.md` / `.tsv` / `.txt`），按 §7 第 8 条可由 `[whitelist]` 段放行。

⚠ **但批 1 的教训正是**（`b529aa5..22aa291` 补审）：**协议正文改动只走白名单声明、从未经任何独立审查，
是本线的已知缺陷形状**。因此本笔**主动登记「待补审」**，并把这句话写进台账白名单行的 reason。
本轮不动用它的理由是：**§9 是纯增引用、零放松**，`§8.7「明确不做的事」一条未改`，
且新增内容不改变任何既有步骤的判据。

**与此并列、但本线不动的**：`cd02c61` / `29cb5081` 两笔（另一条线）缺归属。其中 `29cb5081` 含
`src/agent_harness/agent/runtime.py`，属**代码提交**，按 §7 第 8 条**永远不能走白名单**，
必须有**真实且独立**的审查行。本线**不代为补审查行**（那等于伪造审查），只在此如实登记。

### 10.7 本文 §1 那 6 条原则的现状

§1 的「pstack 原则对照」现在**部分被 §9 的引用替代**：`encode-lessons-in-structure` 与 `prove-it-works`
已由 vendored skill 承担，协议里不再重述其内容——这正是 `principle-encode-lessons-in-structure`
自己要求的形状。**明确不采用**的 `principle-never-block-on-the-human` 结论不变
（与本仓 §9.1.1「票面变更控制」冲突）。
