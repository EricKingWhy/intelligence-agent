# 08 — Plugin / Capability System

## 1. 目标

让 Harness 的业务能力可插拔，使未来增加 Finance 等领域能力时无需修改 Agent Core。

参考 DeepSeek Harness 的 capability seam 思想：

```text
Service Definition
→ Service Provider
→ Consumer
```

本项目采用 Python 化表达。

## 2. Capability Contract

每个 Capability 至少明确：

- Interface / Protocol
- Provider Registry
- Provider lifecycle
- Consumer
- capability descriptor
- health / availability（按需）
- permissions
- error vocabulary

## 3. Provider

Provider 负责具体实现，例如：

```text
MemoryCapability
  └─ LangMemProvider

VectorStoreCapability
  └─ MilvusProvider

ArtifactCapability
  ├─ LocalProvider
  └─ MinIOProvider

WebCapability
  └─ SearchProvider

SubAgentCapability
  ├─ in_process
  └─ future external provider
```

Provider 可多实例共存时使用命名 Registry。

## 4. Consumer

Consumer 可以是：
- 模型 Tool；
- Context Provider；
- AgentRuntime 内部服务；
- API/UI；
- another capability。

例如 Memory：
- `MemoryContextProvider` 是主要 Consumer；
- 可选 `memory_search` Tool 是另一个 Consumer。

## 5. Capability Descriptor

建议：

```text
name
version
provider_name
capabilities[]
risk
supports_streaming
supports_recovery
supports_concurrency
config_schema
```

Consumer 在使用前 SHOULD 检查 Capability 是否真的支持所需能力，不允许“接受但静默忽略”。

## 6. Capability Discovery and Package Import

### 6.1 Runtime Capability discovery（原 Phase 7 合同）

Runtime 仍通过显式 `CAPABILITIES` 配置选择已登记的 Capability / Provider，并由 `wire_capabilities` 中的受控 factory 装配。Python entry points / package discovery 仍为 DEFER；本节新增的本地/Git 导入是用户选择来源后的包管理流程，不是扫描已安装 Python distribution，不会让任意包自动进入 Registry。没有本项目明确实现的 Provider 或逐包适配器，包元数据本身不能产生可运行能力。

### 6.2 Phase 7/8 后续：跨市场包导入（PRD #868）

Phase 7 与 Phase 8 的原始交付和 Gate 已按 `docs/PHASE_STATUS.md` 记录为完成。下面是构建在其基础上的后续范围；它不回写或扩大原 Phase Gate。此范围提供显式的本地目录/Git 来源导入，不提供应用内市场搜索、排序、发布或自动发现。

支持的贡献类型与接入方式：

| 包类型 | V1 接入合同 | 不隐含支持的能力 |
| --- | --- | --- |
| Agent Skill | 按 spec 09 §2 导入完整 Skill 目录；继续由现有 SkillDiscovery 发现，全文按需加载 | 任意宿主 API、自动执行 scripts、未声明的工具依赖 |
| MCP server | 预检并映射到现有 `MCPServerConfig`；只启用当前支持的 tools / transport / credential 范围，见 spec 09 §1 | OAuth、resources、prompts、自定义 transport |
| DSH/Pi 原生插件 | 每个包由本项目侧适配清单逐项映射到现有 Capability、Tool、ContextProvider 或本项目原生 UI | DSH Cordis / Pi TypeScript runtime、私有 DOM、未知 hook 或隐式生命周期 |

原生适配由本项目维护并绑定明确的上游来源版本。清单声明用户可见贡献、依赖、权限、目标接缝和验证依据；每项贡献必须有目标实现。清单只描述与核验适配，不执行上游插件。首版不定义通用原生插件 SPI 或通用 UI contribution API；第二个同类原生插件或第二种 UI 需求出现后再评估抽象。

首版管理入口为 CLI `agent-harness plugins`。安装、升级、启停均是显式用户动作；本地来源快照其目录内容并保存摘要，Git 来源解析并锁定具体 commit 与资源摘要。检查、下载、解包和升级预检不得运行包内安装脚本、原生模块或 MCP server。升级必须指定新目标并完成预检后再切换；切换失败保留当前可用版本。旧版本作为回退快照保留，rollback 是显式动作。

每个安装记录至少能还原包 ID/类型、来源与解析版本、内容摘要、project/global scope、适配器身份与版本、信任状态、逐贡献兼容结果、显式项目启用选择、当前/待重启状态和可回退版本。具体文件格式可由实现票选择，但不能丢失这些语义。project 与 global 使用独立记录和内容；global 安装不会自动加入任何项目。每个项目必须显式启用包。项目版和 global 版同 ID 时，必须显式指定 scope；没有选择时拒绝启用，不采用优先级或静默 shadow。若项目选择 global 包，显式升级该 global 记录后，该项目在重启时使用该记录的新锁定版本；项目自己的同 ID 安装不变。

启用/停用、来源版本切换和 scope 选择持久化后允许重启生效。CLI 与现有状态面必须分别显示“已保存待重启”及当前运行中的 scope/版本；不能用客户端缓存推断实际运行版本。适配缺口或重启失败不得被显示为已生效。

### 6.3 兼容性、信任与统一执行

预检按包的必要贡献逐项列出直接兼容、适配后兼容、尚缺和不支持的功能，并显示来源版本/摘要、许可证声明、运行时依赖、凭据和所需权限。必要贡献清单由标准包格式或该包的项目侧适配清单提供；对 DSH/Pi 包，若没有已审阅的贡献清单，不得推断为完整兼容。

“完整兼容”仅在所有必要的用户可见功能和运行语义均映射到本项目实现并有验收依据时成立；必要 UI 交互也在清单内，允许用本项目原生 UI 提供等价行为。任一必要功能未映射、未实现或未验证时，报告为需要适配/不支持，并阻止把包以“完整兼容”启用。需要继续处理的包可以保留为禁用状态；不支持的贡献不得进入 Runtime。下载成功、文件可解析、Capability 能装配或单次工具调用成功都不足以证明完整兼容。

包来源信任与功能兼容是两个独立判据。检查/安装不会自动授予进程内执行权。只有用户明确核验并信任的本地原生代码版本才可进程内加载；信任绑定具体内容摘要，来源内容改变后须重新核验。未信任的 DSH/Pi 代码不得运行；只有本项目已实现并声明的薄适配，或经本项目既有隔离边界运行，才可继续。没有受控路径时保持禁用。MCP server 也是可执行外部程序，未信任来源不得启动。Skill 脚本不得由导入器自动运行；脚本被模型请求时仍经现有命令/工具权限。任何工具均不得因安装或适配声明提升权限，模型可调用工具必须走统一 Tool Runtime、Permission、ToolExecutor、Operation Ledger（需要时）、ToolResult 与 SessionEvent。导入包及其贡献是可选扩展：缺失、禁用、信任阻断、不兼容或初始化失败不得改变 Agent Core 启动条件；映射为 Runtime Capability 的贡献遵守 §7 的 degradation 分类及现有 CapabilityError 语义，管理状态（未安装/禁用/不兼容/等待重启）不得与运行时 init_failed 合并。
## 7. Graceful Degradation

Capability 分三类：

- REQUIRED_CORE：缺失则 Agent Core 无法启动；
- OPTIONAL_RUNTIME：缺失则功能不可用但基础 Agent 可运行；
- OPTIONAL_OBSERVABILITY：缺失不得影响业务执行。

Memory、Langfuse、Web、Knowledge 默认属于 OPTIONAL。

## 8. Finance 等未来扩展

未来 Finance 插件可以包含：

```text
Finance Capability
├─ tools/
├─ context providers/
├─ skills/
├─ agent profiles/
└─ provider adapters/
```

但 MUST NOT 要求修改 `core/agent_loop.py`。

## 9. Acceptance Criteria

- 切换 Memory Provider 不改 Core；
- 切换 Artifact Provider 不改 ContextBuilder Contract；
- MCP/Knowledge/Web 可以按配置启停；
- Provider 不支持能力时明确报错；
- Optional Provider 故障可以降级；
- 插件不能绕过 Tool Permission / Operation Ledger。
