# #873 全局包作用域与版本状态：成熟产品调研

研究日期：2026-10-10
研究目标：为“用户全局插件包安装与版本记录隔离，同时项目显式启用；升级/回退与项目选择分离；待重启/运行版本准确呈现”整理第一方产品证据、复用判定和当前代码缺口。
研究目标仓库：worktree HEAD 03efb8384e88f071aac2edb4a7b945017daea1fe。Issue #873 当日为 OPEN，标签含 in-progress；本次未更改 issue、代码或测试。

## 1. 启动检查表

| 前置项 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision 相关原则 | goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/00_PROJECT_VISION.md：§2.1 Lightweight、§2.4 Extensible、§2.5 Reuse First、§3 | READY |
| 当前任务规格 | 同目录 08_PLUGIN_CAPABILITY_SYSTEM.md §6.2–§6.3；GLOSSARY.md Phase 7 插件包 / Skill 术语；docs/adr/0052-cross-market-plugin-import-and-adaptation.md D1–D5 | READY |
| Reuse 相关判定 | 13_OPEN_SOURCE_REUSE_MATRIX.md §§2–3（Pi、DeepSeek Harness）；docs/agents/reference-sources.md 对应来源条目 | READY |
| Phase 依据 | 14_IMPLEMENTATION_ROADMAP.md Phase 7、Phase 8 与 Post-Phase 7/8 follow-up | READY |
| 本任务触发细则 | docs/SDD_WORKFLOW_PROTOCOL.md §1.3（双来源与方案依据）、§8.9（JSON、原子替换、字节核对）；docs/agents/issue-tracker.md；Issue #873 | READY |

本子任务只做调研和报告写入；实现纪律与测试门禁尚未触发。

## 2. 结论摘要

1. 七个产品都提供了作用域、配置或版本机制，但没有一个照搬后能完整满足 #873。常见产品把“用户级安装”直接做成跨项目激活；这样会违反本项目“全局安装后不得自行进入 Context、Tool、脚本执行面”的验收条件。
2. 最值得吸收的是组合机制：Pi 的分作用域存储、不可变版本引用和项目信任门；VS Code 的工作区绑定 Profile、逐工作区禁用和扩展宿主重启提示；Claude Code 的 Scope/Version/Status 可见性及 Pending Reload 状态；DSH 的 profile 依赖边界与进程启动边界。
3. 不应新建第二套包管理器。复用现有 SkillPackageManager 的快照、升级、回退和安全路径处理，再按 scope 路由包目录与安装记录；Runtime 只能读取明确选择的包记录。
4. 有一个实质范围冲突：Issue #873 的 AC 要求包先保持未激活，且把“逐项目启用 / 跨作用域冲突选择”列为 T5；Spec 08 §6.2 / ADR-0052 D3 已要求每个项目显式选择 scope、同 ID 无隐式优先级，并定义选择 global 后全局版本变更在重启后生效。建议 #873 限于全局安装、版本治理及“不自动激活”的验证，把选择 UI/命令和冲突选择留给 T5；两票的记录语义仍须遵循 Spec D3。
5. 当前 Runtime 已将传统全局 Skills 目录加入自动发现目录。若把 #873 管理的全局包放入该目录，包可能立刻进入 Skill catalog，违反 AC。全局包管理根必须与自动发现根分开；T5 明确选择后才能投影到 Runtime。

## 3. Issue 与冻结规格的关系

Issue 原文：[ #873 全局包安装与独立版本记录](https://github.com/EricKingWhy/intelligence-agent/issues/873)。

| 主题 | Issue #873 | Spec 08 §6.2 / ADR-0052 D3 | 处理含义 |
| --- | --- | --- | --- |
| 安装作用域 | 项目与 global 使用独立目录/版本记录，互不改写 | 相同 | 两边应各有包快照、manifest 与版本历史 |
| 默认激活 | 新项目能看到可启用的 global 包，但 Context、Tool、脚本均不得自动增加 | 全局安装默认关闭；项目显式选择包 | #873 可建立可枚举的全局包库存，但安装器不得接入 SkillDiscovery / Capability Runtime |
| 启用与冲突 | 明确列为 T5 范围外 | 每个项目显式选择 scope；同 ID 双份时未选 scope 就拒绝，不允许隐式优先级或 shadow | 把 T5 作为后续交付；#873 先守住“没有选择就没有运行” |
| 全局升/回退 | 改全局固定版本，不自动改变每个项目的启用选择；区分运行版本与待重启版本 | 选择 global 的项目在重启后使用 global 安装记录的新锁定版本；项目同 ID 安装保持不变 | “选择不变”应指项目仍选择 global；若票面意指运行版本也永远固定旧版，则与 D3 不同，需主开发澄清 |
| 失败 | 权限、兼容、冲突显式失败，不可悄悄落到项目范围 | 升级先暂存/预检，失败保持旧活动版本和记录 | 保持 fail-closed，错误中注明 scope 和目标版本 |

ADR-0052 D3 的“升级不改变选择”和“选择 global 的项目重启后共同使用 global 新版本”可以同时成立：项目的 scope 选择稳定，global 指针更新后，新 Runtime 按该选择读取新版本；项目私有副本不受影响。

## 4. 当前仓库的可复用实现与缺口

当前入口是 [SkillPackageManager](../../src/agent_harness/skills/package_manager.py#L45)。它已提供项目包目录、版本目录和项目 manifest（package_manager.py:57–60），并已有本地/Git 安装、显式更新、回退、启停、清单与 snapshot 校验（73–75、174–189、211、271、376–397、479–497）。Manifest 目前只接受 scope=project（522），所以 global 生命周期尚未落地。构造器的 global_skills_dir 目前是全局 Skill 搜索/冲突检查路径，不是已实现的 global 包管理器（52–67、653–680）。

运行时发现面是关键安全边界：[capability/wiring.py](../../src/agent_harness/capability/wiring.py#L288-L323) 将 global_dir、项目目录与托管项目目录一并传给 SkillDiscovery。因此，若新 global 安装复用传统 ~/.intelligence-agent/skills，global 包会进入自动发现目录；仅拆开 manifest 或增加 CLI 参数不能保证“未启用”。最小安全方向是将新托管 global 包保存在独立于自动发现目录的根，并让启用路径只消费项目显式选中的包。这个结论来自当前代码事实。

复用判定：

- **REUSE**：沿用 SkillPackageManager 的内容快照、SHA 校验、升级失败保旧版本、回退快照、reparse/symlink 边界检查及显式兼容状态。
- **ADAPT**：在同一管理器里按 scope 选择独立目录与 manifest；不能把当前仅用于发现/冲突检查的 global_skills_dir 误当已存在的全局安装实现。
- **PORT DESIGN**：借 Pi 的 scope 根与 pin；借 VS Code 的工作区关联和启停/重启界面；借 Claude Code 的 Scope、Version、Status、Pending Reload 呈现；借 DSH 的锁定依赖与运行进程固定加载集。
- **DEFER**：市场浏览、自动更新、通用宿主 Profile 系统、scope 自动优先级、同名 shadow 和未显式选择时的 Runtime 自动发现。
- **BUILD**：只在现有管理器和 Runtime 投影无法表达这些不变量时补最小 Python 逻辑；本次未发现需要新框架或依赖的理由。

## 5. 成熟产品逐项对照

读取日期均为 2026-10-10；下表优先使用厂商官方文档。

### Pi

个人安装写入 ~/.pi/agent/settings.json，项目包写入 .pi/settings.json，项目声明须经过项目 Trust。npm 精确版本与 Git tag/commit 可 pin；个人与项目可分别安装，但同身份项目项通常替换个人项，autoload:false 才会作为过滤差量。它还允许对资源类型作白名单过滤。见 [Pi Packages](https://pi.dev/docs/latest/packages)。

**契合**：scope 分开、不可变来源版本、项目信任门和资源过滤都可借鉴。
**差异**：个人包默认加载；同身份包存在项目覆盖语义。它不能用作本项目的隐式 scope precedence。Pi 源码解决的是 TS 宿主包解析；本项目已有 Python 包快照管理，不需要复制整套 resolver。

### DeepSeek Harness（DSH）

DSH 将 profile 作为具名运行组合存于 Harness home；profile 持有自己的 out-of-tree 插件和配置。CLI 要求 dsh plugin --profile <name> add/remove/update，将 pnpm 操作转发到 profile 目录；README 说明成功改动 profile manifest 和 bundle 列表，但当前运行 profile 维持启动时集合，需重启后采用新集合。见 [Architecture](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/architecture.md) 与 [CLI Reference](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/apps/cli/reference/README.md)。

**契合**：安装依赖、profile 记录和正在运行进程所用集合之间有明确边界；pnpm 管理的 profile 依赖状态比自建版本解析器更值得复用。
**差异**：profile 是整套 Runtime 组合，不是“机器 global 库 + 每项目选择”；包成员变化直接改变 profile 启动组成。因此只借锁与重启模型，不移植成第二个配置层。

### Visual Studio Code

Extensions 可指定安装版本；可对扩展全局或当前 Workspace 禁用，操作后提示重启 extension host。Workspace 的 .vscode/extensions.json 是推荐清单，打开工作区时提示用户安装，不直接替用户启用。Profiles 可与 folder/workspace 关联；扩展可显式“应用到所有 profiles”。Workspace Trust 的 Restricted Mode 会禁用或限制多种代码执行功能。见 [Extension Marketplace](https://code.visualstudio.com/docs/configure/extensions/extension-marketplace)、[Profiles](https://code.visualstudio.com/docs/configure/profiles)、[Workspace Trust](https://code.visualstudio.com/docs/editing/workspaces/workspace-trust)。

**契合**：最清晰地展示了“安装可用性”和“当前工作区启用”可以分开；工作区绑定、全局/本地禁用、重启提示和 Trust 都是合适的设计参考。
**差异**：Profiles 管的是配置组合，扩展的安装/更新仍由 VS Code 自己管理；官方资料没有承诺与 #873 一样的项目级独立不可变包版本账本。自动升级语义也不适合作为显式升级/回退的依据。

### Claude Code

支持 user、project、local 三种安装 scope。User scope 会在本机所有项目启用；Project 写入仓库设置；Local 写入本地设置。作者可将 plugin 配置成安装后默认关闭。claude plugin list 展示 Version、Scope、Status；变更可能显示 reload required/pending，运行 session 保留旧版本直到 reload 或下次启动。不同 scope 有 local > project > user 的显式产品优先级。见 [Install and manage plugins](https://code.claude.com/docs/en/plugins/install)。

**契合**：状态面直接展示 Scope/Version/Status，且真实区分活动与待 reload；这是 #873 “运行版本准确呈现”的最佳 UI 机制参考。
**差异**：用户安装默认启用于所有项目；同插件跨 scope 有优先级；会自动 reload 或在后续 session 应用。它的冲突策略与本项目 Spec D3 相反，不复制该优先级。

### Cursor

安装插件时可选 project 或 user scope，并能按 user/workspace/team scope 过滤已安装项。管理页可以独立启停 MCP；disabled server 不加载也不进入聊天。团队 marketplace 提供 Default Off（用户自行选择安装）、Default On 和 Required 分发模式。开发本地插件需要 reload window；同名 marketplace 插件优先于 local copy。见 [Plugins](https://prod.cursor.com/docs/plugins)。

**契合**：scope 选择、可过滤的清单和 Default Off/On/Required 是清晰的可用性/启用状态设计。
**差异**：产品管理的是插件安装/团队分发模式；文档没有给出 global/project 各自锁版本及运行中版本账本。Marketplace 优先本地副本也不能照搬为本项目的自动 shadow。

### OpenCode

插件可以用 package@version 明确 pin，支持全局 ~/.config/opencode/plugins 和项目 .opencode/plugins 发现目录；两边启动时都会加载。CLI 支持 global package add/list/check/update/remove。配置数组按低到高优先级合并，列表项可禁用/重新启用。见 [Plugins v2](https://opencode.ai/v2/docs/plugins)。

**契合**：精确版本声明、全局包管理 CLI 与项目/用户配置位置可作接口参考。
**差异**：全局与项目插件都自动发现并加载，配置层叠带有优先级；没有本任务要求的“全局可见但默认不运行”及运行/待重启版本清单。

### Cline

Cline SDK、CLI、Kanban 支持全局与项目插件；全局托管安装在 ~/.cline/plugins/_installed 下，项目插件在 .cline/plugins。全局插件会在所有 session 可用，项目插件仅对该项目可用；本地目录安装会拷贝到插件 store，Git 安装可能安装依赖。该功能当前不适用于 Cline VS Code/JetBrains 扩展。见 [Plugins](https://docs.cline.bot/customization/plugins) 与 [Plugin Install](https://docs.cline.bot/sdk/plugin-install)。

**契合**：清晰的物理目录分隔和 --cwd 项目作用域值得参考。
**差异**：安装在 global 目录就会让插件进入所有 session；物理隔离本身不能实现项目显式启用。插件安装可能运行依赖安装过程，也不符合 Spec D5 的无脚本预检边界。

## 6. 设计提炼与最小落点

建议把包库存、项目选择、进程运行版本视为三个已经由 Spec 冻结的事实面：

1. **库存**：每个 scope 有独立包目录与安装记录；global 安装/更新/回退只写 global 记录和快照。
2. **项目选择**：记录项目选择的 package ID 与 scope。选择缺失时保持不可见、不可用；相同 ID 跨 scope 时不推断优先级。
3. **运行事实**：Runtime 持有启动时实际加载的摘要/锁定版本；安装或切换版本后，状态并列展示运行版本和待重启版本。失败不能伪装成已活动。

落实时优先扩展同一个 SkillPackageManager 的目录/记录路由，复用已有原子快照与校验；不要让“发现全部 global Skills”充当“列出可供选择的包”。先按 #873 做全局库存隔离和安装不激活；显式项目选择与跨 scope 冲突处理按照其依赖票 T5 完成。若产品要在 #873 内同时交付 T5 行为，应先由主开发处理 Issue 范围，因为票面当前将其列为 out of scope。

## 7. 官方产品来源（均于 2026-10-10 读取）

- Pi，Packages: https://pi.dev/docs/latest/packages
- DeepSeek Harness，Architecture（commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc）: https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/architecture.md
- DeepSeek Harness，CLI Reference（同 commit）: https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/apps/cli/reference/README.md
- VS Code，Extension Marketplace: https://code.visualstudio.com/docs/configure/extensions/extension-marketplace
- VS Code，Profiles: https://code.visualstudio.com/docs/configure/profiles
- VS Code，Workspace Trust: https://code.visualstudio.com/docs/editing/workspaces/workspace-trust
- Claude Code，Install and manage plugins: https://code.claude.com/docs/en/plugins/install
- Cursor，Plugins: https://prod.cursor.com/docs/plugins
- OpenCode，Plugins v2: https://opencode.ai/v2/docs/plugins
- Cline，Plugins: https://docs.cline.bot/customization/plugins
- Cline SDK，Plugin Install: https://docs.cline.bot/sdk/plugin-install

## 8. 本地源码核查与许可证

以下为官方仓库本地浅克隆的路径与固定 commit；仅查看，没有复制代码。若后续实际移植，必须保留上游许可与版权声明。

| 来源 | 本地证据（file:line） | 固定 commit | License | 复用判断 |
| --- | --- | --- | --- | --- |
| Pi | D:\reference\pi\packages\coding-agent\src\core\package-manager.ts:920-940（scope resolve 与项目胜出）；:2085-2135（user/project npm 根）；:2166-2174（Git 根） | 1b347794e2a630e4359f2584f4eea388145d0ddf | MIT，Copyright © 2025 Mario Zechner | 分根、项目 trust、package identity 值得 PORT DESIGN；项目 precedence 不适用 |
| Pi Docs/source package manifest | D:\reference\pi\packages\coding-agent\docs\packages.md:19-21, 38, 121-125 | 同上 | MIT | 官方说明 personal/project、pin 和身份合并规则 |
| DeepSeek Harness | D:\reference\deepseek-harness\apps\cli\src\args.ts:188-198；apps\cli\src\plugin.ts:64-106；apps\cli\reference\README.md:81-93 | 5badb15009ae1756c3afe0ae0cef1faafc290ccc | MIT，Copyright © 2026 DeepSeek | profile 目录、锁与进程重启模型仅作设计参照；不直接 PORT Cordis/pnpm 运行模型 |
| 本仓库当前实现 | src/agent_harness/skills/package_manager.py:45-75, 211-297, 376-497, 522, 653-680；src/agent_harness/capability/wiring.py:288-323 | 03efb8384e88f071aac2edb4a7b945017daea1fe | 本项目 | 直接复用现有管理器；实现前必须解决 global 目录自动发现风险 |

Pi 与 DeepSeek Harness 的源码仓库均为 MIT；本次仅审阅，未移植代码。
