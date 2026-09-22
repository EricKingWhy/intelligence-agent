# 第三方 Skill 来源、选型与**重叠审计**

> 本目录下的 skill 是**第三方开源项目的逐字节副本**，不是本仓自造。
> 存在的理由：协议 `docs/SDD_WORKFLOW_PROTOCOL.md` 的各阶段要**引用**它们，而不是重写一遍等价文字。
> 用户 2026-09-22 要求：「能复用 pstack 直接复用，不要自己写 skills……就是引用」，并追加了两条硬约束：
> **① 只下"我们能用上的"；② `mattpocock` 那套是主开发 skills，pstack 只是辅助 —— 与 Matt 功能重合的下进来
> 就是干扰模型，不要下。**
>
> **本目录文件不得就地改写**（改了就丢失 provenance，「逐字节副本」这个 claim 不成立）。
> 要改行为，改本仓协议 / 脚本，或向上游提 PR。升级 = 换上游 commit + 整目录重放 + 更新 §6 的
> **对象 sha1 / 字节 / sha256**（口径见 §6，取 git 对象、不取工作树）。

## 1. 来源

| 项 | 值 |
| --- | --- |
| 上游仓库 | `https://github.com/cursor/plugins` |
| 上游子目录 | `pstack/` |
| 上游 commit | `53e579f1481697931fc44f5445171397cfa2b24b`（2026-09-21 19:40:52 -0700） |
| 抓取方式 | `git clone --depth 1 --filter=blob:none --sparse` → `git sparse-checkout set pstack`（**不装插件、不跑任何上游脚本**；上游 README 的安装方式是 `/add-plugin pstack`） |
| 抓取时间 | 2026-09-22 |
| 上游许可 | **MIT**，`Copyright (c) 2026 Lauren Tan`，全文见本目录 `pstack-LICENSE.txt`（逐字节副本） |
| 上游作者 | poteto / Lauren Tan |

MIT 要求「许可与版权声明随副本一并保留」——`pstack-LICENSE.txt` 就是为满足这一条放的**同级**副本。
再分发本目录内容时请连带该文件。

## 2. 重叠审计：与 Matt 主开发 skills 的逐项对照

**这是选型的第一道闸门。** 判据不是"名字像不像"，而是**功能是否重合**：
把两者正文摊开对照，**同一个步骤会不会有两种说法**。重合 ⇒ **不下载**（下进去只会让模型在选择上分心，
且可能覆盖 / 遮蔽已装的那份）。

对照对象是本机已装的 Matt skills（`~/.workbuddy/skills/mp-*`，同时已装在
`~/.codex/skills/` 与 `~/.zcode/skills/`）。

| pstack skill | Matt 侧对照 | 判定 |
| --- | --- | --- |
| `tdd` | **`tdd`（Matt 原版）** | ❌ **不下载**。两条硬证据：① **同名撞目录** —— `~/.codex/skills/tdd/SKILL.md` 与 `~/.zcode/skills/tdd/SKILL.md` 已存在，**同一份内容、只差行尾**（codex 侧 **3549 B / LF**、zcode 侧 **3587 B / CRLF**；归一化行尾后 sha256 同为 `cb01f66b…`），装进去会覆盖或遮蔽；② `mp-eng-implement` 正文明文「**Use /tdd where possible**, at pre-agreed seams」⇒ TDD 这一步的路由**已经归 Matt**。 |
| `principle-test-behavior-not-implementation` | **`tdd`（Matt 原版）的 `## What a good test is` + `## Anti-patterns`** | ❌ **不下载**。同一份内容：Matt 写「Tests verify behavior through public interfaces, **not implementation details**」；反模式第一条 `Tautological` = 「the assertion recomputes the expected value the way the code does … Expected values must come from an independent source of truth」。pstack 那条的判据（"调用者视角 + 字面期望值"、"自指 / 常量钉死 / mock-only"）与它**逐项对应**。 |
| `principle-sequence-verifiable-units` | `to-tickets` 的小切片规则 + `tdd` 的「Red before green」 | ⚠ **判定为"部分重合，保留但从宽标注"**。Matt `to-tickets` 已写「A completed slice is **demoable or verifiable on its own**」＋ `tracer bullet`；pstack 这条**多出来的是"提交 / PR 的堆叠顺序本身要能自证给 reviewer"**（「Each commit lands on its own and the sequence reads as an argument」），对应本仓 §8.4 第 3 条「本批必须逐票落 commit —— 压成一个 commit，读数就不属于任何单票、等于没测」。**这一层 Matt 侧没有**，故保留；若需求方判定仍算干扰，本目录可直接删它，不影响其余 6 个。 |
| `show-me-your-work` | `handoff`（Matt） | ⚠ **判定为"不重合，但接近"**（保留供需求方再裁）。Matt `handoff` 是「把当前对话压成交接文档给下一个 agent」；这条是「**长跑作业的决策轨迹 TSV**（`ts/phase/decision/why/evidence/result`）」。功能不同。但本仓台账文化已经在做同一件事，**真正的增量只有格式规范与两条纪律**（evidence 是指针不是散文 / append-only 永不改历史），供 issue #293 用。 |
| `blast-radius` | （无） | ✅ 保留。Matt `to-tickets` 只在"宽重构"语境提过 blast radius 这个词；这条是**完整方法论**：确定性阶梯 1 自说 → 2 指到 `file:line` → 3 证明坏情况不可达 → 4 **跑真代码** → 5 在运行中的应用里复现，且「**任何到不了第 4 步的安全事实必须写 `unproven`**」。这是 issue #292 的方法论底座。 |
| `principle-prove-it-works` | `code-review` / `implement`（Matt） | ✅ 保留。Matt `code-review` 审的是 **diff**（Standards / Spec 两轴），`implement` 要求"跑 typecheck + 单测 + 全量一次"。这条管的是另一件事：**对真实产物取证、不用代理指标 / 自报 / "能编译"，并且"能脚本化就脚本化"**，读数留给 reviewer 重跑。与本仓「先取证再断言」同源。 |
| `principle-encode-lessons-in-structure` | （无） | ✅ 保留。「同一条指令写第二遍时，编码成 lint / 元数据 / 运行时检查 / 脚本」＋「**挑当前允许范围内最强的机制**」。是 #292（feature map）与 #293（读数落盘）的**动机表述**。 |
| `create-verification-skill` | （无） | ✅ 保留。产 feature map（`Sub-features` / `How to get to it (user POV)` / `Driving it with <harness>` / `Gotchas`）+ 一句验收判据：「**A generated skill that was never executed is a draft, not a deliverable**」。issue #292 的方法来源。 |
| `maintain-verification-skill` | （无） | ✅ 保留。feature map 的**维护环**（源波次 ∥ live 波次；最坏一个 PR；维护期间禁止改产品代码）。 |

**结论：上游 47 个 → 抓取候选 9 个 → 实际留存 7 个**（撤掉 2 个：`tdd`、
`principle-test-behavior-not-implementation`）。

### 2.1 已撤销的 2 个（留证，防止以后又被加回来）

| 曾搬运的目录 | 搬进来时的 `SKILL.md` sha256（前 32 位；**入库对象**口径，非检出形态） | 撤销理由 |
| --- | --- | --- |
| `tdd` | `011cab0ecc04a3632121efb493ae4d60…`（入库对象口径；3536 B） | 与 `~/.codex/skills/tdd` + `~/.zcode/skills/tdd` **同名撞目录**；且 Matt `implement` 已拥有 `/tdd` 的路由 |
| `principle-test-behavior-not-implementation` | `87e40efe4e486f7ea639d2ed1fc0abe6…`（入库对象口径；2575 B） | 与 Matt `tdd` 的 `## What a good test is` + `Tautological` 反模式**同一份内容** |

### 2.2 从未抓取的 38 个（三类理由）

1. **本仓 / Matt 已有等价物** —— `principle-guard-the-context-window`（本仓 `AGENTS.md` §2 的读数纪律）、
   `principle-fix-root-causes`（Matt `diagnosing-bugs`）、`principle-never-block-on-the-human`
   （与本仓 §9.1.1「票面变更控制」**直接冲突**，明确不采用）、`poteto-mode` / `setup-pstack`
   （≈ Matt `implement` 的宿主版本，用户 2026-09-22 举的就是这个例子）。
2. **属于别的宿主** —— `make-bot-ui`、`automate-me` 等依赖 Cursor 的插件与自动化宿主，
   本仓 Agent 不共享那套机制，照搬会变成"看起来有、其实调不动"的死引用。
3. **带可携带的攻击面** —— 见 §5 的扫描结果。本目录**只搬散文**（`.md` / `.tsv` / `.txt`），
   **不搬任何可执行文件**。

## 3. 阶段 → skill 映射

**本节不保留内容**：映射表与主从关系（哪一步用 Matt 主开发、哪一步用本目录的辅助 skill）是
**协议 `docs/SDD_WORKFLOW_PROTOCOL.md` §9** 的唯一事实源；本文件只负责**来源、选型与逐字节校验**。

⚠ 2026-09-22 两轴独立审查实测：两处各写一份**已经分叉**（同一行在两处措辞不同、路径写法不同：
协议用仓库内全路径、本节用裸相对路径）——正是 `principle-encode-lessons-in-structure` 要消灭的形状。
故此处删表，只留指针。

## 4. 怎么用（对任何 Agent 成立，不依赖 skill 装载机制）

**按路径读，按内容执行**——不需要"安装"：

```text
在协议指定的阶段，读 docs/agents/skills/<skill-name>/SKILL.md，并按它的 body 执行。
```

本仓的 Agent 不固定（ZCode / Codex / WorkBuddy / Claude Code，见 `AGENTS.md` 文件头），各家 skill
装载机制不同；按**仓库内相对路径**引用是唯一对各家同时成立的形式，且随仓库版本化、可 diff、可审计。

### 4.1 若要**装进宿主**（codex / zcode）—— 两件必须先知道的事

1. **`disable-model-invocation: true` 会拦住模型**。上游 47 个 skill 里 **46 个**的 frontmatter 带这个字段（唯一例外 `setup-pstack`；实测 `re.M` 匹配
   `^disable-model-invocation:`）
   （在 Cursor / Claude Code / Codex 的语义是「**只允许用户 `/` 手动调用，模型不得自动调用**」——
   本机 `~/.codex/skills/handoff/SKILL.md` 用的是同一字段，说明 codex 认它）。
   而本仓协议要的恰恰是"**在某个阶段由模型调用**"⇒ **原样装进去等于装了个模型碰不到的 skill**。
   装之前必须去掉该字段，例如：

   ```bash
   src=docs/agents/skills/<skill-name>
   dst=~/.codex/skills/<skill-name>          # 或 ~/.zcode/skills/<skill-name>
   mkdir -p "$dst" && cp -r "$src"/. "$dst"/
   python - "$dst/SKILL.md" <<'PY'
   import sys, re
   p = sys.argv[1]
   t = open(p, encoding='utf-8').read()
   t = re.sub(r'^disable-model-invocation:\s*true\s*\n', '', t, flags=re.M)
   open(p, 'w', encoding='utf-8', newline='').write(t)
   PY
   ```

2. **装之前查同名撞目录**。这次的教训就是 `tdd` 撞上了已装的 Matt 版。装任何 skill 前先：

   ```bash
   ls ~/.codex/skills/<name> ~/.zcode/skills/<name> 2>/dev/null
   ```

   有输出 ⇒ **停下来判断**：是同一功能吗？是 ⇒ **不要装**（这正是 §2 的理由）；不是 ⇒ 改名后再装。

### 4.2 「引用形式成立」≠「正文每步都能在本仓执行」（2026-09-22 两轴审查实测）

按路径读到的正文，是上游在**别家宿主**（Cursor）里写的。其中有**两类内容在本仓执行不了**，
照做会卡住或被静默跳过。取用时**只取增量**：留下方法论，跳过宿主绑定。

| 上游写法 | 出处（本目录副本的行号） | 本仓现状 |
| --- | --- | --- |
| `how` / `why` / `arena` / `unslop` / `principle-build-the-lever` | `blast-radius/SKILL.md:11,38,48`、`principle-sequence-verifiable-units/SKILL.md:22`、`show-me-your-work/SKILL.md:36` | **上游有、本目录未搬**（选型只留 7 个）⇒ **悬空引用**。遇到这些行按语义就近落在本仓既有步骤，不要去找那个 skill |
| `.cursor/skills/verify-<app>/…`（生成物落点） | `create-verification-skill/SKILL.md:25,36` | Cursor 专属路径；本仓落到 issue #292 指定的落点 |
| `agent-transcripts/`、`~/.cursor/projects/*/` | `show-me-your-work/SKILL.md:56` | 同上，本仓读不到 |
| `scripts/log.sh` | `show-me-your-work/SKILL.md:38` | 未搬（真实理由见 §5 末条）；本仓台账由 Python 落盘 |

本节只登记**已核实**的条目，不追求穷举。核验可复跑：
`grep -n -E 'how|why|arena|unslop|build-the-lever|\.cursor|agent-transcripts' docs/agents/skills/*/SKILL.md`。

## 5. 安全审计（2026-09-22，机械扫描 + 逐文件读）

**扫描口径（可复跑）**：把下面这串正则拼成一条交替式（`re.I`），对**每个文件全文**跑 `re.findall`
（计数单位 = **匹配次数**；二进制文件按 `errors='replace'` 解码后同样跑）：

```text
curl wget https?:// \bnc\b \bssh\b \bscp\b \beval\b exec\( base64 \.env \.ssh id_rsa
credential password secret token api_key "rm -rf" "chmod 777" sudo
```

| 扫描对象 | 文件数 | 命中文件 | 匹配次数 | 命中行数 |
| --- | --- | --- | --- | --- |
| 上游 `pstack/skills/` | 122 | 29 | **78** | 69 |
| 本目录留存（**不含 `PROVENANCE.md` 自身**） | 12 | 4 | **4** | 4 |

⚠ **口径更正（2026-09-22 两轴审查）**：本节早先写「上游共 **60** 处」而**未记口径**，复跑对不上
（同一批正则按"匹配次数"得 78、按"命中行数"得 69）；早先写「留存 7 个命中数 = 3」也少算了 1 处。
上表为**本轮实测**。另注：单扫本目录时 `PROVENANCE.md` 自身会额外命中 **27** 次——它引用了这串
正则本身，故上表把它排除单列。

**结论**：

- 命中**全部落在未搬运的 skill 内**。最重的两条：
  `make-bot-ui/SKILL.md:89` = `curl -fsSL https://tailscale.com/install.sh | sudo sh`；
  `poteto-mode/scripts/watch-pr/github.ts` 有 URL 凭据与 token 处理。
  ⇒ 这是「不做全量安装」的**具体**代价，不是抽象担心。
- **本目录留存部分命中 4 次**：3 次是 `create-verification-skill/references/feature-map-example/*.md`
  里的示例本地地址 `http://127.0.0.1:4173`；第 4 次是 `create-verification-skill/SKILL.md:17` 讲
  "怎么驱动 app"时举的散文例 `curl-able`（不是可执行片段）。
  ⇒ **零网络调用、零凭据读取、零 `eval`、零删除命令。**
- 所有搬运文件在 **git 对象层**与上游**逐字节**一致：12 个文件里有 11 个可直接与上游比 blob sha1
  （**逐条相等**，LICENSE 同），且与上游 `pstack/skills/` 的 47 个 skill **同名同 sha1**；
  见 §6（该表 2026-09-22 已从"工作树口径"改为"git 对象口径"）。`copy → 回读 → bytes 比对` 亦全 OK。
- **未搬运的可执行文件**：`show-me-your-work/scripts/log.sh`。它**本身无风险**（纯本地 TSV 追加器，
  唯一的"安全相关"行为是防御性的：把 `=` `+` `-` `@` 开头的单元格前缀单引号，防表格公式注入）。
  不搬的真实理由是两条：① 本仓不需要（台账由 Python 落盘）；② `docs/**` 下的 `.sh` **不命中覆盖闸门的
  `DOC_PATTERN`**（只认 `.md/.txt/.rst/.tsv/.json/.yaml`），搬进来会让该提交无法走 docs-only 白名单
  —— 正是协议 §7 第 8 条记的那类陷阱。⇒ 该 skill 正文提到的 `scripts/log.sh` 在本仓**是悬空引用**；
  **另有 4 类悬空 / 宿主专属引用**（未搬的 `how`/`why`/`arena`/`unslop`/`build-the-lever`、
  `.cursor/skills/verify-*`、`agent-transcripts/`），逐条清单见 §4.2。

## 6. 逐字节副本清单（升级时逐行复核这张表）

**口径（2026-09-22 两轴审查后更正）**：下表的字节 / sha256 **一律取 git 对象**
（`git cat-file blob <rev>:<path>`），**不取工作树**。原因：本机 `core.autocrlf=true` ⇒ 这些
`.md` / `.tsv` / `.txt` 检出时被转成 CRLF，**工作树的字节与 sha256 与上游不一样**；而**入库对象
（= 上游 blob）恒为 LF**。旧表记的正是工作树形态 ⇒ 在任何默认 / Linux 检出上（含 `git show`、
`git archive`、CI）**复跑一律对不上**，是一把坏尺子（真实缺陷，2026-09-22 两轴审查发现）。

「本目录文件是上游的逐字节副本」这句话**在 git 对象层成立**：下表 `对象 sha1` 可与上游
`api.github.com/repos/cursor/plugins/git/trees/53e579f1…` 的同名条目**逐条相等**（12/12 已验）。

`PROVENANCE.md` 自身不入表（它的内容包含这张表，自指无意义）。其余 **12** 个文件：

| 相对路径 | 对象 sha1（= 上游 blob sha1） | 入库字节（LF） | 入库 sha256 | 本机检出字节（CRLF） |
| --- | --- | --- | --- | --- |
| `blast-radius/SKILL.md` | `298d030cb606…` | 4016 | `20cb2945f5fe62055166f745107274af5f19428ca033e53a9b8bc69650d3ded1` | 4066 |
| `create-verification-skill/SKILL.md` | `f869e2612299…` | 5879 | `644f2551403c1bca01a2855b34611b6e7be0ce0dc5b204514c376c0f6a6e6ac4` | 5923 |
| `create-verification-skill/references/feature-map-example/README.md` | `fb64570cfab3…` | 2601 | `cb7bd782cf89968a4ba3d58a5151db837430db92d19a6f52a906973b77b516ba` | 2648 |
| `create-verification-skill/references/feature-map-example/create-note.md` | `21357566b692…` | 2926 | `644a44c74f35d38c2feb7cd05a0121fdebbb623e8dc376c185b565a581d1ccf7` | 2965 |
| `create-verification-skill/references/feature-map-example/search.md` | `1f8e57d3aaf1…` | 3478 | `6e87b9e7f2791a7776ba1bb83f371cd285c306c67f19d245cc4dd6ca3015c823` | 3523 |
| `maintain-verification-skill/SKILL.md` | `a2680b91fead…` | 4922 | `515c0eaa054b3f6be1b1fb06f2c2f173c80fddb58bbcac57576f89c479bc68e8` | 4961 |
| `principle-encode-lessons-in-structure/SKILL.md` | `df9cd5ff2fb3…` | 2211 | `77044c83a9ac6df78fc643887ea5f1f11f082adaf14b717c060cdaed03cce863` | 2242 |
| `principle-prove-it-works/SKILL.md` | `4563023d4d9f…` | 1879 | `ae13d287984a5864312b6286899d4ee2b60f8272b5e5e8fcc586a7c3ea51020b` | 1912 |
| `principle-sequence-verifiable-units/SKILL.md` | `dd008c371ee8…` | 2029 | `761390804ee36c32f5338d8db47ec54463d40ff96dbb296dfafabcd5f2593eec` | 2051 |
| `show-me-your-work/SKILL.md` | `6e98dd24e619…` | 5459 | `831e85ba3f84f38bf338cd03e6af050fea357752e25a825e334c99f59d7f2077` | 5541 |
| `show-me-your-work/references/decision-log-template.tsv` | `db220376ecb8…` | 38 | `6779378006a2f5225e8c06deb84f1d913fb532041ebbb6e50d94600bfe3f4b76` | 39 |
| `pstack-LICENSE.txt` | `6b5400237fdf…` | 1067 | `bc957ca6bee02792566a1a028d105e02e247c6e77cf057061674273da77b200e` | 1088 |

⚠ **注意**：§4.1 的"去掉 `disable-model-invocation`"是对**安装副本**做的操作。
本目录文件在 **git 对象层**保持上游字节（**sha256 因此始终可校验**）；但**本机检出**因
`core.autocrlf=true` 是 CRLF，盘上字节 ≠ 上游字节 —— 两者都写在上表里，**取用一律以对象列为准**。
不要把安装时改过的文件拷回来。


## 7. 已安装到宿主的记录（2026-09-22）

需求方 21:38 指令：「我要装进 codex / zcode，你替我装吧」＋「**可以去掉 `disable-model-invocation`，
但只能是让模型可以自动调用 pstack 的 skills；其他比如 mattpocock 的 `handoff` 必须要手动调用，
绝对不能所有的 skills 都让模型自动调用**」。

| 宿主 | 目录 | 装入数 |
| --- | --- | --- |
| codex | `~/.codex/skills/` | **7** |
| zcode | `~/.zcode/skills/` | **7** |

**对安装副本做的唯一变换**：删掉 frontmatter 里的 `disable-model-invocation: true` 整行
（`'disable-model-invocation: true\r\n'` = **32 字节**；注意该字段名本身是 30 字符，不是 31 —— 手算过一次差了 1 位）。
`name` / `description` 与正文一字未动，`references/` 下的文件逐字节 verbatim。
行尾**保持检出形态（CRLF）**：上游 / 入库对象其实是 **LF**，本机 `core.autocrlf=true` ⇒ 检出为 CRLF，
安装副本复制的就是**检出形态**。宿主惯例本来就是 CRLF（实测 codex 48 CRLF / 34 LF、zcode 62 CRLF / 2 LF）
⇒ **不需要转行尾**。（2026-09-22 两轴审查更正：原文写"行尾保持**上游的** CRLF"——上游是 LF，口径写错了。）

**校验口径（可复跑）**：

- 14/14 安装目录：文件集相同、`SKILL.md` 行数差**恰为 1**，且
  `installed == source[:k] + source[k+1:]`（`k` = **本机检出副本**里该字段所在行；源按工作树 CRLF 形态
  逐字节比）⇒ 证明「**只有那一行被去掉**」；
  `references/` 下文件逐字节相同。
- **未碰任何已有 skill**：两宿主里"非本次安装"的条目最新 mtime 分别是 **13004 分钟 / 14562 分钟**前
  （≈9–10 天），我的 7 个是 **0.3 分钟**前 ⇒ 没有改写任何既有文件。
- **`handoff` 仍是手动调用**：`~/.codex/skills/handoff/SKILL.md` 与 `~/.zcode/skills/handoff/SKILL.md`
  实测**仍带** `disable-model-invocation` ✓ —— 这正是需求方点名要保住的那条。
- 仍带该字段的 skill 数：codex **15** / zcode **18**（本次只改了新增的 7 个）。
- 宿主目录条目数：codex 93、zcode 72（各 = 原数 + 7）。

**撤销**：这 14 个目录都是本次**新建**的（装前逐条查过，两宿主均无同名目录），
删掉它们即可完全回退，**不需要恢复任何原文件**：

```bash
for d in blast-radius create-verification-skill maintain-verification-skill \
         principle-encode-lessons-in-structure principle-prove-it-works \
         principle-sequence-verifiable-units show-me-your-work; do
  rm -rf "$HOME/.codex/skills/$d" "$HOME/.zcode/skills/$d"
done
```

⚠ **`~/.zcode/skills/llms.txt` 不是全量注册表** —— 它只列 13 个设计类 skill
（`implement` / `tdd` / `code-review` 都不在里面），skill 发现靠**目录扫描**。**本次没有改它。**
