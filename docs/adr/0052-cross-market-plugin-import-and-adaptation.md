# ADR-0052: 跨市场插件包导入与逐包薄适配

- **Status**: Accepted（用户于 2026-10-08 确认 PRD #868；规格对齐票 #869）
- **Date**: 2026-10-08
- **Scope**: post-Phase 7/8 follow-up
- **Related**: Engineering Spec 08 §6、09 §§1–2、14 Phase 7/8；ADR-0010、ADR-0011、ADR-0012；PRD #868

## Context

Phase 7 交付了显式 Capability Registry / wiring 与 SkillDiscovery；Phase 8 交付 MCP Client tools。它们没有交付跨市场安装生命周期。现有 ADR 对当时 V1 的 entry-point 扫描、Marketplace、远程 Skill 获取和 OAuth 有明确 DEFER。用户现在确认增加一组后续能力：标准 Skill 整目录与现有 MCP tools 可直接导入；Pi/DSH 原生插件逐个通过项目侧薄适配移植；支持本地/Git 来源、预检、project/global 安装、逐项目启用、显式升级与回退。

目标是复用现有 Capability、Tool、ContextProvider、Permission 和 Operation Ledger 合同，让常见来源在小改动下完整移植；不把任意宿主 ABI 宣称为通用兼容，也不让安装步骤成为执行旁路。

## Decisions

### D1. 导入是显式包管理，不是 Marketplace 或 Runtime 自动发现

用户必须指定本地目录或 Git 来源。Git 版本解析为完整 commit SHA；本地目录导入为不可被源目录后续改动静默改变的内容快照。两种来源都保存内容摘要。首次安装、更新、启用、停用和回退均为明确操作；不做搜索、排序、自动更新或 entry-point 扫描。

Runtime Capability 仍由 `CAPABILITIES` 与已登记 factory 装配。安装记录不直接创建 Provider，不改变 `AgentRuntime`，也不因存在包元数据就动态执行包代码。

### D2. 只支持三条明确的兼容车道

| 车道 | 可认领范围 | 适配边界 |
| --- | --- | --- |
| Agent Skills | 含根级 `SKILL.md` 的标准 Skill 目录，整个目录按原相对路径保存 | 延续现有目录发现与按需 load；依赖宿主工具/命令不支持时报告缺口，脚本不由安装器运行 |
| MCP | `MCPServerConfig` 可表达的 stdio / Streamable HTTP server 与 tools 原语 | 环境凭据沿用 ADR-0012；OAuth、resources、prompts、自定义 transport 标为首版不支持 |
| Pi/DSH 原生包 | 有本项目侧、绑定来源版本的逐包适配清单，且每个必要贡献有实现映射 | 接入现有 Capability / Tool / ContextProvider / 本项目原生 UI；不运行 Pi/DSH host runtime、Cordis 生命周期或私有 DOM |

包没有明确 MCP server 描述时，不猜启动命令；原生包没有适配清单时，报告需要适配。单有可下载源码、manifest 或能 import 不能成立“兼容”。

### D3. 安装记录、作用域、版本和激活分离

安装记录至少保存包 ID 与类型、来源和不可变解析版本、内容摘要、scope、适配器身份/版本、信任结论、兼容报告、当前/待重启状态和可回退快照。具体序列化格式由安装器实现票决定，但不能弱化这些语义。CapabilityDescriptor 与 `CAPABILITIES` 继续只表达 Runtime 装配合同，不承担包来源管理。

Project 与 global 包和版本记录互相隔离。安装包不等于启用：每个项目都显式选择要启用的包；global 安装对任何项目都默认关闭。若相同 ID 同时有 project 与 global 版本，未指定 scope 时拒绝装配；不采用隐式优先级或 shadow。显式选择 global 的项目使用该 global 安装记录当前锁定的版本；global 版本升级或回退只在命令被显式调用后发生，选择该 scope 的项目在重启后共同使用该记录的新版本，project 同 ID 记录不变。

升级必须给出明确目标 ref/version；获取到暂存位置后预检，通过后才切换选定版本。失败时活动版本和记录保持原样。回退只切换到此前完整保留的快照，不覆盖用户数据。启停/版本变更可以在重启后生效；状态面分别显示待重启选择和当前运行版本。

### D4. 预检逐项判兼容，不用“能装”代替“能用”

报告至少包含来源/解析版本/摘要、许可证声明、运行时依赖、凭据、权限和逐项贡献结果。标准包按格式直接识别；原生包的必要贡献清单由适配清单给出，并绑定来源 commit/摘要。

“完整兼容”意味着清单中的每个必要用户可见功能和运行语义都映射到本项目实现且有验收依据，必要 UI 亦包括在内。可以用本项目原生 UI 实现等价交互。必要项缺实现或验证时，报告为需要适配/不支持，不能以完整兼容启用；需继续处理的包可留作禁用快照。

### D5. 信任不由安装或适配声明授予

检查、下载、解包和升级预检不运行包内安装脚本、执行原生模块或启动 MCP server。来源信任与兼容程度分别判断。进程内 native code 仅允许用户明确核验并信任的本地内容摘要；版本或字节变化使信任重新核验。未知来源的 DSH/Pi 代码只可由本项目受控适配或经本项目已经实现的隔离边界承载；无受控路径时保持禁用。未信任 MCP server 不启动。

任何工具仍须经过本项目既有 Contract / Registry / Validation / Permission / Scheduler / ToolExecutor / Ledger / ToolResult / SessionEvent 路径。适配清单不可提升权限、定义第二套重试或绕过副作用对账。Skill 脚本由模型后续请求执行时也遵守现有命令工具权限。外来包贡献始终是可选扩展，不得成为 Agent Core 的启动依赖；经 Capability 接入时复用 §7 degradation 分类与 CapabilityError 语义。安装、信任、兼容及待重启状态必须与 Runtime Capability 的 init_failed 等错误分别诊断。

### D6. 原有 Phase Gate 和宿主特定 DEFER 保持原义

本 ADR 增加的是 Phase 7/8 完成后的后续范围，不改变原阶段的交付或 Gate，也不把历史 COMPLETED 解释成当时已有安装器。ADR-0010 所说“不做 entry-point 扫描/Marketplace”继续约束 Runtime 自动发现与应用内市场；用户显式本地/Git 导入不属于这两项。ADR-0011 的 SkillDiscovery 不负责远程获取仍成立；导入器可把选定 Git commit 物化为本地快照，再由 SkillDiscovery 发现。ADR-0012 的 OAuth/resources/prompts DEFER 仍成立。进度真相仍在 `docs/PHASE_STATUS.md`，执行状态记在 Tracker。

### D7. 先逐包适配，不提前抽通用 SPI

每个 Pi/DSH 原生包先有自己的兼容清单和本项目薄适配，适配复用本项目已存在的 contracts。只有第二个同类原生贡献确实重复出现时，才评估版本化通用 SPI；第二种不同 UI 需求出现前不抽 UI contribution API。`dsh-codex-effort-slider` 是首个完整样板：固定上游 commit `af723caf3387e64ae28aa69c4fd235b1b662e3ae`，将必要交互映射到本项目 reasoning-effort API 与 Composer。Issue #865 独占 Composer 控件、模型档位和 provider 请求映射；本 ADR 的移植票只负责来源识别、逐项兼容、适配激活和版本生命周期。

## Decision mapping

| 用户确认的 PRD 决策 | 本 ADR / Engineering Spec 位置 |
| --- | --- |
| 安装记录只管来源/版本/scope/启用/兼容，Runtime 仍由现有层执行 | D1、D3；spec 08 §6.1–6.2 |
| 标准 Skill/MCP 直接认，原生 DSH/Pi 逐包适配 | D2、D7；spec 08 §6.2、spec 09 §§1–2 |
| 预检不运行安装脚本，依赖逐项报告 | D4、D5；spec 08 §6.3 |
| Skill 整目录且保留相对资源；正文按需加载 | D2；spec 09 §2.1 |
| MCP tools、环境凭据先行；OAuth/resources/prompts 暂不支持 | D2；spec 09 §1.1 |
| Project/global 独立安装；global 必须逐项目启用；同 ID 显式选 scope | D3；spec 08 §6.2 |
| 启停允许重启；显示待生效与运行态 | D3；spec 08 §6.2 |
| Git commit 与摘要锁定；显式升级、预检、失败保留与回退 | D1、D3；spec 08 §6.2 |
| 只有可信本地 native code 可进程内加载；未知来源受控或隔离 | D5；spec 08 §6.3 |
| 第二个同类案例出现后再抽 SPI/UI contribution API | D7；spec 08 §6.2 |
| 滑条复用 #865 的 API/Composer，不夺其所有权 | D7；spec 08 §6.2 |
| 必要用户可见功能全映射才算完整兼容 | D4；spec 08 §6.3 |
| Optional 导入贡献失败不得拖垮 Core，且安装/信任/兼容状态与运行时错误分开诊断 | D5；spec 08 §§6.3、7；spec 09 §1.1 |

## Reuse evidence

| 来源 | 已核机制 | 本项目判定 |
| --- | --- | --- |
| DSH `5badb15009ae1756c3afe0ae0cef1faafc290ccc`，本地 `D:\reference\deepseek-harness`：`packages/boot/plugin-manager/README.md:31,40,54`；`docs/architecture.md:19,23,27,133` | Plugin Manager 处理 npm/Git/local、启停/移除及版本/profile；扩展绑定 Cordis 服务、bundle 和生命周期 | PORT DESIGN 包管理行为；不引入 Cordis host |
| Pi `1b347794e2a630e4359f2584f4eea388145d0ddf`，本地 `D:\reference\pi`：`packages/coding-agent/docs/packages.md:1-40`、`extensions.md:1-20` | Git/本地 package 组合 extensions/skills 等；扩展通过 Pi 专属 API 且在宿主进程运行 | PORT DESIGN 来源/作用域；不加载 Pi TypeScript runtime |
| Agent Skills / MCP 官方规范；PyPA entry points | Skills 规范定义目录内容；MCP 定义 tools/transports；entry points 只定义对象发现 | REUSE 标准格式/协议；Capability 与 Tool contract 仍由本项目拥有 |
| VS Code Extension Host / Manifest 官方文档 | 稳定 contribution API 与 host 版本范围让 UI 扩展不依赖私有 DOM | PORT DESIGN 受控映射原则；当前不提前建通用 UI API |

所有研究和代码位置见 `docs/research/2026-10-08-cross-market-plugin-portability.md`、`docs/research/2026-10-08-plugin-adaptation-research.md`；本 ADR 不复制或移植上游代码。若后续真的复制源代码，必须以具体 commit 的 LICENSE/NOTICE 为准并保留所需声明。

## Consequences

- Phase 7/8 的原 Gate 继续为既有完成态；跨市场包导入是另行验收的后续范围。
- 标准 Skill/MCP 可以复用现有格式和协议；安装 lifecycle、兼容报告与记录需由项目显式实现。
- Pi/DSH 原生包不能声称“一键 ABI 兼容”；本项目逐项评估、薄适配、补齐用户可见行为。
- 对未信任代码和必要贡献缺口采取 fail-closed，可能拒绝部分市场包；这是完整兼容和运行信任的前提。
