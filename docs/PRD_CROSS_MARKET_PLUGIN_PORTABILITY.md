# 跨市场插件低成本移植 PRD

> 状态：用户确认的产品需求；Engineering Specification 与 ADR-0052 已由 #869 对齐，后续按 #868 子票逐票实施。2026-10-08。

## Problem Statement

本项目已经有 Capability / Provider 接缝、Skill 发现和 MCP tools 客户端，但用户找到 DSH/Pi 市场插件时，不能把包导入、预检、启用并得到完整的用户可见功能。当前装配只认识内置 capability；Skill 的项目写入闭环只管理 SKILL.md，未管理整个外来目录；MCP server 依赖手写 CAPABILITIES 配置；DSH 插件还依赖本项目不存在的 Cordis 服务和 UI 宿主。用户需要的是常见插件类型小改动完整移植，而非运行任意市场原包的承诺。

## Solution

提供本地目录/Git 来源的导入、预检、安装、启停、升级和回退。标准 Agent Skills 整目录与现有范围内的 MCP tools 直接接入；DSH/Pi 原生插件保留源包，在本项目侧用薄适配清单逐个映射到既有 Capability、Tool、ContextProvider 和 UI。每个插件以用户可见功能清单验收；需要 UI 时允许用本项目原生界面实现等价交互。缺少任一声明的必要功能时明确报告并阻止以‘完整兼容’启用。

安装范围同时支持当前项目与用户全局；全局包在每个项目内显式启用。同 ID 的项目版和全局版不得静默覆盖，必须显式选择。启停允许重启后生效，并准确提示。Git 安装锁定 commit 与资源摘要；更新必须重新预检，失败后可回到旧版。受信任的本地原生代码才允许进程内加载；未知来源只能走受控适配或隔离。所有模型可调用工具继续走现有权限、Executor、Operation Ledger 与 SessionEvent。

## User Stories

1. 作为 Agent 使用者，我想选择本地目录或 Git URL，以便评估外部 Skill、MCP server 或已适配的原生包。
2. 作为 Agent 使用者，我想在启用前看到包来源、版本、许可、依赖、需要的权限和各项功能的兼容结果，以便知道能否完整使用。
3. 作为 Agent 使用者，我想导入整个 Skill 目录，使 SKILL.md 的脚本、参考资料和资源相对引用仍可用。
4. 作为 Agent 使用者，我想让 Skill 的正文继续按需加载，并让脚本调用受现有工具权限控制。
5. 作为 Agent 使用者，我想导入 stdio 或 Streamable HTTP MCP 工具配置，并在模型调用时保留现有校验、审批、重试与副作用对账。
6. 作为 Agent 使用者，我想为 MCP server 配置环境凭据；若它只能通过尚未支持的 OAuth 登录，我想在预检时得到明确原因。
7. 作为 Agent 使用者，我想把插件安装在单个项目或用户全局，并单独决定全局插件在哪些项目启用。
8. 作为 Agent 使用者，我想在项目版与全局版同 ID 时显式选择，以便知道实际运行的是哪一版。
9. 作为 Agent 使用者，我想看到启用或停用是否需要重启，以及重启后实际生效的版本。
10. 作为 Agent 使用者，我想对 Git 来源锁定 commit，显式升级并在失败后回退到上一个可用版本。
11. 作为插件移植者，我想在项目侧写一个小适配清单，声明外来包的贡献、依赖、权限及宿主版本，而不必改写上游源码。
12. 作为插件移植者，我想把 DSH/Pi 插件按真实贡献逐项映射到本项目现有接缝，并知道哪项功能需要原生 UI 适配。
13. 作为 Agent 使用者，我想安装目标 DSH 推理滑条的已适配版本，让当前模型公布的档位在 Composer 中完整可见、可操作且真实进入请求。
14. 作为 Agent 使用者，我想在插件功能不完整时看到具体缺口，避免把能下载或能启动误认为完整兼容。
15. 作为 Agent 使用者，我想让未知来源代码先停在预检或隔离边界，避免安装动作自动赋予进程内权限。
16. 作为维护者，我想让可选插件失败只影响该功能，基础 Agent 仍可运行，并能从诊断中区分未配置、禁用和初始化失败。

## Implementation Decisions

1. 安装清单只负责来源、版本、范围、启用状态和兼容结果；执行仍由现有 Capability 装配与 Tool Runtime 负责，不建立第二条工具路径。
2. 标准 Skill 目录与 MCP tools 优先直认。外来 DSH/Pi 包只有在项目侧适配清单声明受支持贡献后才可判为可移植；不嵌入 Cordis/Pi 的 TypeScript 宿主。
3. 安装检查不得把来源包的安装脚本当作预检自动执行。显式列出额外运行时、脚本、凭据和权限需求；是否满足逐项判断。
4. 标准 Skill 作为完整目录管理，保留相对资源；目录发现与正文按需加载语义不变。脚本若被调用，经过已有 Runtime 权限边界。
5. MCP 首版只覆盖 tools；沿用现有 stdio/Streamable HTTP、环境变量间接凭据与统一 ToolExecutor。OAuth-only、resources 和 prompts 不被误报为已支持。
6. 项目与全局各有独立安装记录和固定版本；全局安装不等于在每个项目启用。跨范围同 ID 冲突显式选择，不静默 shadow。
7. 启停以持久化配置为准，允许重启后生效；界面/CLI 必须区分‘已保存、待重启’与‘当前运行中’。活跃会话不从客户端缓存推断插件状态。
8. Git 来源保存具体 commit 和资源摘要；升级走新版本预检，再切换选择；失败仍能回到旧版，不覆盖用户数据。
9. 原生代码的进程内加载只面向用户明确受信任的本地来源；来自 Git 的未知代码不能因下载成功自动取得信任。权限不由适配清单自行提升。
10. 先用逐插件薄适配；第二个同类原生案例出现后再评估版本化通用 SPI。UI 亦先复用真实需求，第二种 UI 插件出现后再评估通用贡献点。
11. 目标滑条的已有实施票 #865 负责 Composer、模型档位与请求映射。本 PRD 的适配票复用其成果，只补外来包身份、兼容预检、启用和版本管理，不重复施工 Composer。
12. 完整兼容表示该插件声明的必要用户可见功能全部达到验收；报告应逐项列出直接复用、薄适配、重做宿主层和不支持，不以包能加载作为完成判据。

## Testing Decisions

- 测试用户可见行为和现有最高层接缝：预检结果、安装/启停/重启生效、目录可见与按需加载、MCP 调用结果、Composer 选择进入真实模型请求。避免只测试内部 manifest 解析。
- Skill 样例必须含 scripts/references/assets 与相对引用；检查项目和全局安装、同 ID 冲突、拒绝越界路径、禁用后不可见、升级回退后字节/版本一致。
- MCP 用官方 SDK fake server 覆盖 tools/list、tools/call、环境凭据缺失、风险映射、审批、单次执行、失败降级和重启后装配；OAuth-only 预检明确不支持。
- 外来包预检覆盖源 commit、manifest、许可、依赖、宿主 API 版本、必要贡献缺失；任何一项不满足时不得标‘完整兼容’或启用。
- 原生适配样例用目标滑条与 #865 的模型目录/Provider 合同验收；键盘、双主题、reduced-motion、窄宽度及不支持档位走现有前端验收。
- 不可信输入检查命令、路径、依赖脚本、凭据泄漏、权限扩大和 UI 边界；工具副作用仍要有 Operation Ledger / SessionEvent 可观察证据。

## Out of Scope

- 任意市场插件无需适配便原样运行；内嵌 DSH Cordis/Pi TypeScript 宿主；第一版通用 Python SPI 或通用 UI 插件框架。
- 应用内市场搜索、一键购买、公开插件发布平台；自动运行安装脚本；运行中无重启热插拔。
- 首版 MCP resources/prompts 与浏览器 OAuth 登录；OAuth-only server 单独排后续票。
- 未经选定的其他 DSH/Pi 插件和原生 Anthropic Provider；目标滑条 #865 之外的 Composer 重构。

## 方案依据与可行性

| 决策 | 成熟产品/技术依据 | 本项目契合与复用判定 | 可行性 |
| --- | --- | --- | --- |
| Skill 整目录导入 | [Agent Skills 规范](https://agentskills.io/specification)；[Pi packages](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/coding-agent/docs/packages.md) | 复用既有发现/按需加载；ADAPT 目录安装与资源生命周期 | 9/10 |
| MCP tools 导入 | [MCP Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)；[官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk) | REUSE 协议/SDK，ADAPT 配置与管理；保持唯一 Tool Runtime | 8.5/10 |
| 项目+全局安装与信任 | [Pi packages](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/coding-agent/docs/packages.md)；[VS Code Workspace Trust](https://code.visualstudio.com/docs/editing/workspaces/workspace-trust) | ADAPT 两级范围和项目显式启用；增加冲突管理成本 | 7/10 |
| 锁定版本/兼容预检/回退 | [DSH 插件管理器](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/boot/plugin-manager/README.md)；[VS Code 扩展清单](https://code.visualstudio.com/api/references/extension-manifest) | PORT DESIGN 包元数据与版本闸；不引入 DSH 包管理器 | 8.5/10 |
| DSH 滑条薄适配 | [目标插件](https://github.com/Microqian2th/dsh-codex-effort-slider)；[VS Code 扩展能力](https://code.visualstudio.com/api/extension-capabilities/overview) | PORT DESIGN 交互，REUSE 本项目 #865 模型/Composer 合同；不执行源 DOM 注入 | 9/10 |
| OAuth 后续 | [MCP 授权规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)；[官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 首版沿用 ADR-0012 环境凭据，另票评估官方授权能力 | 6/10 |

来源代码固定点与本仓 file:line 对账见 [跨市场插件调研](research/2026-10-08-cross-market-plugin-portability.md) 和 [目标插件调研](research/2026-10-08-plugin-adaptation-research.md)。DSH、Pi 与目标滑条仓库均为 MIT；如移植原代码，按具体版本保留许可与来源。

## Further Notes

Engineering Specification 08/09 与 ADR-0052 已按本 PRD 完成对齐（#869）。原 Phase 7/8 的完成状态仍仅表示其原始 Gate 达成，不代表包安装器已实现；本 PRD 的实现按 #868 子票逐票验收。实现继续遵守 Vision 的 Python Core、自有 Agent Runtime、单 Tool 路径、权限与恢复不变量。滑条 UI 由仍处于 in-progress 的 #865 所有；关联适配票复用其成果，不覆盖其在途工作。
