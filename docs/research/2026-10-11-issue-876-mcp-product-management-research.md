# Issue #876：MCP 包管理与 Runtime 接入产品调研

- 日期：2026-10-11
- 范围：官方一手产品文档、官方源码与 MCP 规范；不含实现步骤或代码修改。
- 样本：Claude Code、Codex、VS Code、Cursor、Zed、Cline、Goose、Pi、DeepSeek Harness，以及 MCP 官方资料。
- 判定：`REUSE / ADAPT / PORT DESIGN / BUILD / DEFER`。引用的产品文档于 2026-10-11 查阅；Pi、DeepSeek Harness 源码固定在所列 commit。

## 项目现状与边界

当前仓库已经有 MCP Python SDK 依赖（`pyproject.toml:19`，锁定版本见 `uv.lock:1654-1657`），配置模型为 `MCPServerConfig`（stdio / HTTP、命令或 URL、env/headers、超时、启用和工具权限，`src/agent_harness/mcp/config.py:33-67`）；密钥采用 `${VAR}` / `${VAR:-default}` 引用（同文件 70-90 行）。MCP discovery 按 server 隔离失败、记录错误，并对命名冲突做确定性处理（`src/agent_harness/mcp/capability.py:81-125`）。适配器实现项目的 `Tool` 合同并转发到 MCP connection（`src/agent_harness/mcp/adapter.py:112-191`）；实际调用由 `ToolExecutor` 统一完成 Registry 查询、参数校验、审批及执行（`src/agent_harness/tooling/executor.py:365-388,462-579`）。MCP 接线标为可选运行时能力（`src/agent_harness/capability/wiring.py:409-453`）。

本仓库的 MCP/Python SDK 决策是 **REUSE + ADAPT**：SDK 承担 transport、协议和 discovery，本项目拥有 adapter 与运行路径（工程规格 `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md:83-87`）。包导入合同已写在 `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/09_MCP_SKILLS_KNOWLEDGE_WEB.md:26-34` 与 ADR-0052 D1-D5：只把明确描述映射到现有 config；仅 stdio / Streamable HTTP 与 tools 原语；静态预检不启动进程或连接远端；未信任不启动；密钥不写入安装记录；启动后每次工具调用继续走统一 Runtime；重启生效可接受。这里是调研对既有合同的核对，不是新增方案。

## 可复用机制与判定

| 主题 | 一手产品证据 | 对本仓的判定 |
|---|---|---|
| 来源固定、更新和回退 | Pi 支持 npm、Git、本地目录；版本化 npm spec 与 Git tag/commit 固定，更新不移动已配置 ref。DeepSeek Harness (DSH) 在安装前检查来源；安装或 bundle 校验失败时恢复快照中的 package manifest 与 lockfile，但下载文件可能保留。Codex marketplace 也支持 Git ref/SHA 选择器。 | **PORT DESIGN**：借鉴固定来源、预检与失败事务语义；**BUILD**：当前 MCP config/runtime 不持有 package 来源记录或快照。ADR-0052 D1/D3 已要求 Git 完整 commit SHA、本地内容摘要、显式升级和保留快照回退。不要把包管理语义塞入 MCP adapter。 |
| 全局安装与逐项目启用 | Pi 把个人包声明放用户设置，项目声明放项目设置且要先信任；项目同包默认覆盖个人项。Codex 将 repo marketplace 可发现性与 `.codex/config.toml` 的 `enabled` 分开，禁用不等于卸载，项目设置只对可信项目生效。VS Code 也分别支持 user/workspace 配置，启停状态独立于共享的 `mcp.json`。 | **PORT DESIGN**：复用 scope 与 selection 两个独立概念。ADR-0052 D3 已规定 global 安装默认不启用、项目显式选择；同 ID 双 scope 不做隐式 shadow。**REUSE** 现有 capability 装配作为运行消费端。 |
| 可执行 stdio 的信任与审批 | MCP 官方 SEP-1024（状态为 final）要求一键配置本地 server 前展示完整命令、全部参数、危险提示并要求明确同意。VS Code 对不可信 workspace 阻止项目 MCP 启动，对其他来源在首次启动或配置变化时询问 trust。Claude Code 的 project server 会有 pending approval，但非交互 CLI / SDK 等上下文无法弹该提示；Codex 项目配置限可信项目。 | **ADAPT**：采用“信任独立于兼容”的产品状态和明确启动门槛；把信任绑定到实际来源/内容摘要，配置或字节变化重新核验（ADR-0052 D5）。工具调用 approval 不能代替 server 启动信任。产品文档只证明各自 UI 行为，不据此假定所有运行模式都有安全提示。 |
| 秘密引用 | Claude Code、Cursor 支持配置值引用环境变量；VS Code 还提供隐藏输入的 `inputs`；Cline 文档建议将 secret 放环境变量。 | **REUSE** 本仓已有 env reference 及缺失时显式报错；继续禁止明文写入 package manifest、安装记录和诊断。VS Code 的 secret-input 存储机制不扩入当前 MCP 初版合同。 |
| server / tool 命名冲突 | VS Code 要求每个 server 名唯一；Claude Code 会报告跨 scope 同名但端点不同，并明示 precedence；Zed profile 可以按需关闭冲突工具。当前仓库的工具名已使用 `mcp__{server}__{tool}` 命名空间，重名时记录冲突并按配置顺序确定胜出者。 | **REUSE** 现有 namespacing 和冲突诊断，不另建通用工具名注册层。安装记录必须让 server identity 明确；不能让同 ID 的 project/global 选择静默覆盖（ADR-0052 D3）。 |
| 待应用与运行态 | Codex 本地插件文件变化文档要求重启；DSH 区分已保存选择与是否成功加载，并在 startup-only profile 返回 `restart-required`。Zed 能消费 MCP 的 `tools/list_changed`，不重启 server 而刷新 tool list。 | **PORT DESIGN**：借鉴 DSH 的“保存态 / 已应用态”分离；**ADAPT** 当前规格允许重启生效并要求显示 pending selection 与当前运行版本。**DEFER** 热插拔；Zed 的动态 tool list 是协议可选能力，不是安装生命周期必需项。 |
| 单 server 故障隔离 | Cursor 文档明确一个 MCP server 失败不会影响其他 server；Codex 暴露 optional startup grace 与 `required` 开关。当前仓库的 `build_mcp_capability` 已逐 server 捕获 connection/discovery/adapter 错误，保留其他 server；全部失败时 capability 可选降级。 | **REUSE** 现有隔离和 observable errors。不要让包预检、server 加载失败拖垮基础 Agent；包状态（未信任/不兼容/待重启）与 Runtime 的 `init_failed`、连接失败分开呈现。 |
| MCP 调用入口 | MCP stdio transport 定义由 client 启动子进程并通过 stdin/stdout 传 JSON-RPC；协议本身不提供本项目权限层。仓库的 MCPTool 挂在 Tool 合同上，ToolExecutor 执行校验/审批后调用 adapter。 | **REUSE** 现有 SDK、adapter、Registry、Scheduler/ToolExecutor、Ledger 与 SessionEvent 路径。禁止 package manager 或 MCP adapter 自建执行、重试或副作用旁路。 |

### 消融后的最小边界

真正影响安全、可复现和可解释状态的维度是：**来源解析版本/摘要、信任、项目选择、当前生效态**。仓库已有协议调用、工具权限、单 server 隔离和碰撞规则，研究没有找到需要重造这些模块的依据。市场搜索/自动升级、任意生态 ABI、热插拔、OAuth、MCP resources/prompts/SSE 不属于当前首发合同，按现有规格 **DEFER**。这里的“消融”是对照产品机制与仓库已有能力后的范围判断，没有扩写验收步骤。

产品资料只支持机制借鉴；本报告没有复制上游实现代码。若后续需移植实现，仍应按 Reuse Matrix 核对对应来源 commit、许可证及必要声明。

## 来源索引

| ID | 官方一手来源与本次核实范围 |
|---|---|
| P1 | [Pi Packages 文档](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/coding-agent/docs/packages.md#L196-L224)，commit `1b347794e2a630e4359f2584f4eea388145d0ddf`，来源、scope、信任和 pin。 |
| P2 | [DeepSeek Harness Plugin Manager](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/boot/plugin-manager/README.md#L202-L229)，[pending/restart 与失败处理](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/boot/plugin-manager/README.md#L268-L276)，[failure behavior](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/boot/plugin-manager/README.md#L306-L317)，commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`。 |
| P3 | [Codex MCP 官方文档](https://developers.openai.com/codex/mcp)：共享配置、用户/项目配置、重启、env 与 per-tool approval/optional server 状态（Connect / STDIO / Other configuration sections）。 |
| P4 | [Codex Plugins 官方文档](https://developers.openai.com/plugins/build/plugins#L1004-L1018)：local/Git marketplace 来源与 ref/SHA；[repo enablement 与 trusted project](https://developers.openai.com/plugins/build/plugins#L1104-L1112)。 |
| P5 | [Claude Code MCP 官方文档](https://code.claude.com/docs/en/mcp)：安装作用域 (MCP installation scopes)、workspace trust、scope precedence、env interpolation、冲突提示。 |
| P6 | [VS Code MCP 管理文档](https://code.visualstudio.com/docs/agent-customization/mcp-servers#L423-L449)：workspace/server trust 和诊断；[配置参考](https://code.visualstudio.com/docs/agents/reference/mcp-configuration#L214-L239) 与 [sensitive inputs/server names](https://code.visualstudio.com/docs/agents/reference/mcp-configuration#L360-L423)。 |
| P7 | [Cursor MCP 官方文档](https://cursor.com/docs/mcp)：config scope 与环境变量；tool approval、server failure isolation 和 restart/update (Using MCP in chat / FAQ)。 |
| P8 | [Zed MCP 官方文档](https://zed.dev/docs/ai/mcp)：local/remote config、状态指示、profiles 和 `tools/list_changed`。 |
| P9 | [Cline MCP 官方项目文档](https://github.com/cline/cline/blob/main/docs/mcp/mcp-overview.mdx#L207-L325)：全局配置、CLI 管理、enable/disable、server restart、secret 与 tool approval 建议；此页为官方 repo 的 `main` 文档，按访问日记录。 |
| P10 | [Goose Extensions 官方文档](https://goose.vcorp.ai/docs/getting-started/using-extensions/#L25-L50)：MCP extension、启用前恶意软件检查和权限说明；[CLI plugin 文档](https://goose.vcorp.ai/docs/guides/goose-cli-commands/#L535-L553)核实 Git plugin 安装/显式更新行为。 |
| P11 | [MCP Python SDK 官方源码](https://github.com/modelcontextprotocol/python-sdk#L244-L317)：官方 Python client/server SDK，支持 stdio、Streamable HTTP、SSE；本仓库复用已安装 SDK，但初版兼容面按本项目 spec 收窄。 |
| P12 | [MCP transports 规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#stdio)：stdio 子进程/标准流语义。 |
| P13 | [SEP-1024](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/seps/1024-mcp-client-security-requirements-for-local-server-.md#L200-L233) 与 [官方 SEP Tracker](https://plan.modelcontextprotocol.io/seps)：本地 server 安装的预先明示同意要求与 final 状态。 |
| P14 | [MCP Tools 规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools#security-considerations)：tool 调用确认、敏感操作提示及审计建议。 |

## 本次本地核对锚点

本地实现与规格按仓库 HEAD `a63adb000b078deffb72eb35e44f25a1847a65e2` 核对：

- `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/09_MCP_SKILLS_KNOWLEDGE_WEB.md:26-34`
- `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md:83-87`
- `docs/adr/0052-cross-market-plugin-import-and-adaptation.md:16-50`
- `src/agent_harness/mcp/config.py:33-90`
- `src/agent_harness/mcp/capability.py:81-125`
- `src/agent_harness/mcp/adapter.py:112-191`
- `src/agent_harness/tooling/executor.py:365-388,462-579`
- `src/agent_harness/capability/wiring.py:409-453`

报告仅为研究产物；未修改代码、Issue、label 或既有 ticket/research 文件。
