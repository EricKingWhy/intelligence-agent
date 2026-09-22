# 第三方 Skill 来源与选型（vendored）

> 本目录下的 skill 是**第三方开源项目的逐字节副本**，不是本仓自造。
> 存在的理由：协议 `docs/SDD_WORKFLOW_PROTOCOL.md` 的各个阶段要**引用**它们，而不是重写一遍等价文字。
> 用户 2026-09-22 的明确要求：「能复用 pstack 直接复用，不要自己写 skills……就是引用」。
>
> **本目录里的文件不得就地改写**（改了就丢失 provenance，「逐字节副本」这个 claim 就不成立）。
> 要改行为，改本仓的协议 / 脚本，或向上游提 PR。升级 = 换上游 commit + 整目录重放 + 更新下表 sha256。

## 1. 来源

| 项 | 值 |
| --- | --- |
| 上游仓库 | `https://github.com/cursor/plugins` |
| 上游子目录 | `pstack/` |
| 上游 commit | `53e579f1481697931fc44f5445171397cfa2b24b`（2026-09-21 19:40:52 -0700，`Merge pull request #407 …`） |
| 抓取方式 | `git clone --depth 1 --filter=blob:none --sparse` → `git sparse-checkout set pstack`（不装插件、不跑任何上游脚本） |
| 抓取时间 | 2026-09-22 |
| 上游许可 | **MIT**，`Copyright (c) 2026 Lauren Tan`，全文见本目录 `pstack-LICENSE.txt`（逐字节副本） |
| 上游作者 | poteto / Lauren Tan（见上游 `README.md`） |

MIT 要求「许可与版权声明随副本一并保留」——`pstack-LICENSE.txt` 就是为满足这一条而放的本目录**同级**副本。
再分发本目录内容时请连带该文件。

## 2. 为什么只搬 9 个（上游共 47 个）

上游 `pstack/skills/` 有 **47** 个 skill。本目录搬了 **9** 个（≈19%），判据是**有没有一个明确的消费阶段**：
这个 skill 会被 `docs/SDD_WORKFLOW_PROTOCOL.md` 的某一步**按名字引用**吗？没有就不搬。

**没搬的那 38 个分三类**：

1. **本仓已有等价物** —— 例如 `principle-guard-the-context-window`（本仓 `AGENTS.md` §2 已有「不要整文件读中文大文件」的读数纪律）、
   `principle-fix-root-causes`（本仓已用 Matt 的 `diagnosing-bugs`）。
   两份等价规则同时存在，迟早漂移成两套口径。
2. **属于别的宿主** —— `setup-pstack` / `poteto-mode` / `make-bot-ui` 依赖 Cursor 的插件与自动化宿主，
   本仓的 Agent 不共享那套机制；照搬会变成"看起来有、其实调不动"的死引用。
3. **带可携带的攻击面** —— 见 §4 的扫描结果。上游把**可执行脚本**与 skill 混放
   （`poteto-mode/scripts/**` 有 `.ts` 编排器与 GitHub token 处理、`make-bot-ui` 有 `curl … | sudo sh`）。
   本目录**只搬散文**（`.md` / `.tsv`），不搬任何可执行文件。

## 3. 阶段 → skill 映射（协议里引用的就是这张表）

| 七阶段主干 | 引用的 skill | 在这一步干什么 |
| --- | --- | --- |
| Tickets（拆分） | `principle-sequence-verifiable-units` | 拆成「每个单元以**可验证状态**收尾」的序列；不在当前单元绿之前推进；提交顺序本身要让 reviewer 能重放（经典形态 = 先失败测试、后修复） |
| Implement / TDD | `tdd` | 修 Bug 或新增行为时先让坏行为可执行；**测试路径不划算时不许静默跳过**，要明说理由并换成最接近的可执行检查 |
| Implement / Tests | `principle-test-behavior-not-implementation` | 写 / 改 / 留测试时的判据：把该测试 import 的函数全改成返回 `undefined`，它还过吗？过 ⇒ 重写断言或删掉 |
| Implement / Review | （**不引第三方**） | 双轴独立审查保持 Matt 原版，用户 2026-09-22 明确「不能改变」 |
| Runtime Verification | `principle-prove-it-works` | 声明完成前对**真实产物**取证，不用代理指标 / 自报 / "能编译"；**能脚本化就脚本化**，产出的读数留给 reviewer 重跑 |
| Runtime Verification | `blast-radius` | 算改动在**别处**会破坏什么；按"确定性阶梯"推进，**到不了第 4 级（跑真代码）的安全事实必须明文标 `unproven`** |
| Runtime Verification（基建） | `create-verification-skill` | 产 feature map：特征 ↔ 用户视角入口 ↔ 怎么驱动 ↔ 坑。**「没被执行过的生成物是草稿，不是交付物」** |
| Runtime Verification（基建） | `maintain-verification-skill` | feature map 的维护环：源波次（每特征一个只读子代理）∥ live 波次（协调者亲自驱动每个特征）；最坏一个 PR |
| Evidence Gate | `principle-encode-lessons-in-structure` | 同一条指令写第二遍时，编码成 lint / 元数据 / 运行时检查 / 脚本；**挑当前允许的最强机制** |
| Evidence Gate | `show-me-your-work` | 决策轨迹 TSV（`ts / phase / decision / why / evidence / result`）；原文明说「**别的 skill 把轨迹路由到这里，不要自造一套**」——与本仓台账同构 |

`create-verification-skill` / `maintain-verification-skill` 是 issue **#292**（feature map + `gate0.py --affected`）的机制来源；
`principle-encode-lessons-in-structure` 与 `show-me-your-work` 是 issue **#293**（读数落盘 `docs/gate/<sha>.json`）的机制来源。

## 4. 安全审计（2026-09-22，机械扫描 + 逐文件读）

**扫描口径**：用正则 `curl|wget|https?://|nc -|ssh|scp|eval|exec(|base64|\.env|\.ssh|id_rsa|credential|password|secret|token|api[_-]?key|rm -rf|chmod 777|sudo`
扫上游 `pstack/skills/` **全部**文件（不只搬的那些），逐行打印命中。共 **60** 处命中。

**结论**：

- 命中**全部落在未搬运的 skill 内**（`poteto-mode/scripts/**`、`make-bot-ui`、`reflect`、`interrogate`、
  `typescript-best-practices`、`setup-pstack` 等）。最重的两条：
  `make-bot-ui/SKILL.md:89` = `curl -fsSL https://tailscale.com/install.sh | sudo sh`；
  `poteto-mode/scripts/watch-pr/github.ts` 有 URL 凭据与 token 处理。
  → 这就是不做全量安装的具体代价，不是抽象担心。
- **本目录搬进来的 9 个**（＋4 个 reference 数据文件）命中数 = **3**，且全是同一个形状：
  `create-verification-skill/references/feature-map-example/*.md` 里的示例本地地址 `http://127.0.0.1:4173`。
  **零网络调用、零凭据读取、零 `eval`、零删除命令。**
- 13 个搬运文件**逐字节**与上游一致，sha256 见 §5，`copy → 回读 → 比对` 全 OK。
- **未搬运的可执行文件**：`show-me-your-work/scripts/log.sh`。它是纯本地 TSV 追加器（无网络、无凭据），
  唯一的"安全相关"行为是**防御性**的（把 `=` `+` `-` `@` 开头的单元格前缀单引号，防表格公式注入）——
  也就是说**它本身没有风险**。不搬它的真实理由是两条：
  ① 本仓不需要它（台账是 Python 落盘的）；
  ② `docs/**` 下的 `.sh` **不命中覆盖闸门的 `DOC_PATTERN`**（只认 `.md/.txt/.rst/.tsv/.json/.yaml`），
  搬进来会让那个提交无法走 docs-only 白名单——这正是 `docs/SDD_WORKFLOW_PROTOCOL.md` §7 第 8 条记的那类陷阱。
  ⇒ 该 skill 正文里提到的 `scripts/log.sh` 在本仓**是悬空引用**，按本仓工具替代。

## 5. 逐字节副本清单（升级时逐行复核这张表）

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
| `principle-test-behavior-not-implementation/SKILL.md` | 2600 | `bd6ecea5b16ce30d79dc5918d80ddf40c681b244e40bdcdf709f821e1ae19c7e` |
| `pstack-LICENSE.txt` | 1088 | `0b179de8c206d95074651dc54810d1278cbe3c7edef85f6d3eb09c7bd2c2f02f` |
| `show-me-your-work/SKILL.md` | 5541 | `f1e0e24b79ed3fd025b87eab7d9d660187212972060e8be8f4b7487abadf5e68` |
| `show-me-your-work/references/decision-log-template.tsv` | 39 | `e41f14876ec851a603b03a168c4ce63b46c4cd2588b8e4668a8c988d9b37ffa5` |
| `tdd/SKILL.md` | 3580 | `ab4c4593da45a9496839ff29fe8b9f026948742847a6ff5265803935011737b4` |

## 6. 怎么用（对任何 Agent 都成立，不依赖 skill 装载机制）

**按路径读，按内容执行**——不需要"安装"、不需要插件宿主、不需要在某个特定工具里注册：

```text
在协议指定的阶段，读 docs/agents/skills/<skill-name>/SKILL.md，并按它的 body 执行。
```

这样做的原因：本仓的 Agent 不固定（ZCode / Codex / WorkBuddy / Claude Code 谁在干活谁是主开发，
见 `AGENTS.md` 文件头），而各家 skill 的装载机制不同。按**仓库内相对路径**引用是唯一对所有 Agent
同时成立的形式，且这些文件随仓库版本化、可审计、可 diff。

⚠ **注意**：上游的 frontmatter 带 `disable-model-invocation: true`（在 Cursor 里表示该 skill 只允许
用户手动 `/` 调用）。那是**上游宿主的语义**，本仓不依赖它——按 §6 的路径引用方式读，不受该字段影响。
本目录不改上游字节，故该字段原样保留。
