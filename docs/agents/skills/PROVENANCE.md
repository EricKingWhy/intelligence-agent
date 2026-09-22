# 第三方 Skill 来源、选型与**重叠审计**

> 本目录下的 skill 是**第三方开源项目的逐字节副本**，不是本仓自造。
> 存在的理由：协议 `docs/SDD_WORKFLOW_PROTOCOL.md` 的各阶段要**引用**它们，而不是重写一遍等价文字。
> 用户 2026-09-22 要求：「能复用 pstack 直接复用，不要自己写 skills……就是引用」，并追加了两条硬约束：
> **① 只下"我们能用上的"；② `mattpocock` 那套是主开发 skills，pstack 只是辅助 —— 与 Matt 功能重合的下进来
> 就是干扰模型，不要下。**
>
> **本目录文件不得就地改写**（改了就丢失 provenance，「逐字节副本」这个 claim 不成立）。
> 要改行为，改本仓协议 / 脚本，或向上游提 PR。升级 = 换上游 commit + 整目录重放 + 更新 §6 的 sha256。

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
| `tdd` | **`tdd`（Matt 原版）** | ❌ **不下载**。两条硬证据：① **同名撞目录** —— `~/.codex/skills/tdd/SKILL.md` 与 `~/.zcode/skills/tdd/SKILL.md` 已存在（同一份 3541 B 的 Matt 英文版），装进去会覆盖或遮蔽；② `mp-eng-implement` 正文明文「**Use /tdd where possible**, at pre-agreed seams」⇒ TDD 这一步的路由**已经归 Matt**。 |
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

| 曾搬运的目录 | 搬进来时的 `SKILL.md` sha256（前 32 位） | 撤销理由 |
| --- | --- | --- |
| `tdd` | `ab4c4593da45a9496839ff29fe8b9f02…` | 与 `~/.codex/skills/tdd` + `~/.zcode/skills/tdd` **同名撞目录**；且 Matt `implement` 已拥有 `/tdd` 的路由 |
| `principle-test-behavior-not-implementation` | `bd6ecea5b16ce30d79dc5918d80ddf40…` | 与 Matt `tdd` 的 `## What a good test is` + `Tautological` 反模式**同一份内容** |

### 2.2 从未抓取的 38 个（三类理由）

1. **本仓 / Matt 已有等价物** —— `principle-guard-the-context-window`（本仓 `AGENTS.md` §2 的读数纪律）、
   `principle-fix-root-causes`（Matt `diagnosing-bugs`）、`principle-never-block-on-the-human`
   （与本仓 §9.1.1「票面变更控制」**直接冲突**，明确不采用）、`poteto-mode` / `setup-pstack`
   （≈ Matt `implement` 的宿主版本，用户 2026-09-22 举的就是这个例子）。
2. **属于别的宿主** —— `make-bot-ui`、`automate-me` 等依赖 Cursor 的插件与自动化宿主，
   本仓 Agent 不共享那套机制，照搬会变成"看起来有、其实调不动"的死引用。
3. **带可携带的攻击面** —— 见 §5 的扫描结果。本目录**只搬散文**（`.md` / `.tsv` / `.txt`），
   **不搬任何可执行文件**。

## 3. 阶段 → skill 映射（协议 §9 引用的就是这张表）

**主开发方法的归属先写清**：本仓的 `Grill → Spec → Tickets → Implement(TDD/Tests) → 双轴 Review`
这几步**一律用 Matt 的那套**（`grilling` / `to-spec` / `to-tickets` / `implement` / `tdd` /
`code-review` / `diagnosing-bugs`），**本表只在 Matt 覆盖不到的地方补辅助方法**。

| 阶段 | 引用的 skill | 在这一步干什么 |
| --- | --- | --- |
| Tickets（拆分与排序） | `principle-sequence-verifiable-units/SKILL.md` | 提交 / PR 的**堆叠顺序本身要能自证**给 reviewer（先失败测试后修复、先基线后处理）；不在当前单元绿之前推进 |
| Implement / TDD | **Matt `tdd`（不在此目录）** | 由 Matt 主开发 skills 承担，本目录不再重复 |
| Implement / Review | **Matt `code-review`（不在此目录）** | 双轴独立审查保持 Matt 原版，用户明确「不能改变」 |
| Runtime Verification | `principle-prove-it-works/SKILL.md` | 对**真实产物**取证；**能脚本化就脚本化**，读数留给 reviewer 重跑 |
| Runtime Verification | `blast-radius/SKILL.md` | 算改动在**别处**会破坏什么；到不了"跑真代码"一级的安全事实**必须明文标 `unproven`** |
| Runtime Verification（基建） | `create-verification-skill/SKILL.md` | 产 feature map；**没被执行过的生成物是草稿，不是交付物** |
| Runtime Verification（基建） | `maintain-verification-skill/SKILL.md` | feature map 的维护环（源波次 ∥ live 波次） |
| Evidence Gate | `principle-encode-lessons-in-structure/SKILL.md` | 把重复出现的指令编码成结构，不写第三遍文字；挑允许范围内最强的机制 |
| Evidence Gate | `show-me-your-work/SKILL.md` | 决策轨迹 TSV 的格式与纪律（evidence 是指针 / append-only） |

## 4. 怎么用（对任何 Agent 成立，不依赖 skill 装载机制）

**按路径读，按内容执行**——不需要"安装"：

```text
在协议指定的阶段，读 docs/agents/skills/<skill-name>/SKILL.md，并按它的 body 执行。
```

本仓的 Agent 不固定（ZCode / Codex / WorkBuddy / Claude Code，见 `AGENTS.md` 文件头），各家 skill
装载机制不同；按**仓库内相对路径**引用是唯一对各家同时成立的形式，且随仓库版本化、可 diff、可审计。

### 4.1 若要**装进宿主**（codex / zcode）—— 两件必须先知道的事

1. **`disable-model-invocation: true` 会拦住模型**。上游每个 skill 的 frontmatter 都带这个字段
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

## 5. 安全审计（2026-09-22，机械扫描 + 逐文件读）

**扫描口径**：正则 `curl` / `wget` / `https?://` / `nc` / `ssh` / `scp` / `eval` / `exec(` / `base64` /
`.env` / `.ssh` / `id_rsa` / `credential` / `password` / `secret` / `token` / `api_key` / `rm -rf` /
`chmod 777` / `sudo`，扫上游 `pstack/skills/` **全部**文件（不只搬的那些），逐行打印命中。共 **60** 处。

**结论**：

- 命中**全部落在未搬运的 skill 内**。最重的两条：
  `make-bot-ui/SKILL.md:89` = `curl -fsSL https://tailscale.com/install.sh | sudo sh`；
  `poteto-mode/scripts/watch-pr/github.ts` 有 URL 凭据与 token 处理。
  ⇒ 这是「不做全量安装」的**具体**代价，不是抽象担心。
- **本目录留存的 7 个**（＋3 个 feature-map 示例 ＋1 个 TSV 模板）命中数 = **3**，
  且全是同一个形状：`create-verification-skill/references/feature-map-example/*.md` 里的示例本地地址
  `http://127.0.0.1:4173`。**零网络调用、零凭据读取、零 `eval`、零删除命令。**
- 所有搬运文件**逐字节**与上游一致，sha256 见 §6，`copy → 回读 → bytes 比对` 全 OK。
- **未搬运的可执行文件**：`show-me-your-work/scripts/log.sh`。它**本身无风险**（纯本地 TSV 追加器，
  唯一的"安全相关"行为是防御性的：把 `=` `+` `-` `@` 开头的单元格前缀单引号，防表格公式注入）。
  不搬的真实理由是两条：① 本仓不需要（台账由 Python 落盘）；② `docs/**` 下的 `.sh` **不命中覆盖闸门的
  `DOC_PATTERN`**（只认 `.md/.txt/.rst/.tsv/.json/.yaml`），搬进来会让该提交无法走 docs-only 白名单
  —— 正是协议 §7 第 8 条记的那类陷阱。⇒ 该 skill 正文提到的 `scripts/log.sh` 在本仓**是悬空引用**。

## 6. 逐字节副本清单（升级时逐行复核这张表）

`PROVENANCE.md` 自身不入表（它的内容包含这张表，自指无意义）。其余 **12** 个文件：

| 相对路径 | 字节 | sha256 |
| --- | --- | --- |
| `blast-radius/SKILL.md` | 4066 | `cdc483bc0720f8c6d06d9051c7c0f9f180dce6f4452bcfbb458649cd0929ed4d` |
| `create-verification-skill/SKILL.md` | 5923 | `638e0560abfafd0571752930be244eed8cd46750667fe870c99ec6196b9ccebf` |
| `create-verification-skill/references/feature-map-example/README.md` | 2648 | `9f75fc7925f811327c9cbc3d4c152117381ea32517dc7acc1f63f6f0879b6f31` |
| `create-verification-skill/references/feature-map-example/create-note.md` | 2965 | `ca170daf8d458e230534028f38c77162e69bb2cd0d08fbd63650519773a8ca54` |
| `create-verification-skill/references/feature-map-example/search.md` | 3523 | `4354bea1d267938804f57febea2e729207981e537699a6df819cb86fbf83fe8f` |
| `maintain-verification-skill/SKILL.md` | 4961 | `38553f39e53fdbb1bac7357dd0550e4df1546e819dc9c48387b88d1fbcd5ef5c` |
| `principle-encode-lessons-in-structure/SKILL.md` | 2242 | `64c2752bfd1eeae654b17b9c87858f9b8503101aa4d0c51279cc182794057605` |
| `principle-prove-it-works/SKILL.md` | 1912 | `26f891ee4964b2e767e5e5ea27f9e84a752b3bf9ca82e795e978345a4bac76b1` |
| `principle-sequence-verifiable-units/SKILL.md` | 2051 | `a8322efbd8b226fd068245fd66da74774032ec5d4c9b07f3c4b1196017fe9414` |
| `pstack-LICENSE.txt` | 1088 | `0b179de8c206d95074651dc54810d1278cbe3c7edef85f6d3eb09c7bd2c2f02f` |
| `show-me-your-work/SKILL.md` | 5541 | `f1e0e24b79ed3fd025b87eab7d9d660187212972060e8be8f4b7487abadf5e68` |
| `show-me-your-work/references/decision-log-template.tsv` | 39 | `e41f14876ec851a603b03a168c4ce63b46c4cd2588b8e4668a8c988d9b37ffa5` |

⚠ **注意**：§4.1 的"去掉 `disable-model-invocation`"是对**安装副本**做的操作。本目录里的文件保持
上游字节，**sha256 因此始终可校验**。不要把安装时改过的文件拷回来。
