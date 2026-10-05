# #528（IMP-11 工具定义延迟加载 / 曝光级别）方案依据

> 按 `AGENTS.md` §6.1 与 `docs/SDD_WORKFLOW_PROTOCOL.md` §1.3 落块。调研日期：2026-10-06。
> 本文件只记录来源核实与复用判定；判定口径 = `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md`。

## 方案依据（SDD §1.3）

### 来源（≥2 个独立来源）

| 来源 | 类型 / 版本 | 核实方式 |
| --- | --- | --- |
| Pi（`earendil-works/pi`，原 `badlogic/pi-mono`） | 上游代码，本地浅克隆 `/home/hatch/reference/pi`，commit `28dcce2ba45ce4a9efeb0f5b686f0be830fd89b9`（2026-10-06 克隆，HEAD） | 实读源码（file:line 见下） |
| Anthropic 官方文档：Tool search tool | `https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool`，读取日期 2026-10-06 | WebFetch 实读正文 |
| OpenAI 官方文档：Tool search | `https://developers.openai.com/api/docs/guides/tools-tool-search`，读取日期 2026-10-06 | WebFetch 实读正文 |

Pi 本地克隆路径符合 `docs/agents/reference-sources.md` §1（每台机器手动准备、不进仓库）。

### 机制摘要

**Pi — ToolExposure 五级 + tool_search（客户端实现，provider 无关）**

- `packages/coding-agent/src/core/extensions/types.ts:509`：
  `export type ToolExposure = "direct" | "model-only" | "codemode" | "deferred" | "hidden"`。
  语义（同文件 495–508 行注释）：`direct` 声明给模型且可调用；`model-only` 只声明不可调用；
  `codemode` / `deferred` 注册即可调用但**不声明给模型**（deferred 连 codemode 工具的清单也不进）；
  `hidden` 注册但不可达。声明集（active tools）与可调用集是两个集合。
- `packages/coding-agent/src/extensions/tool-search/tool.ts`（同 commit）：内置 `tool_search`
  工具 = 对工具元数据（name + description + schema 描述/属性名）的 Okapi BM25 排序器；
  搜索对象是**未声明给模型**的工具（codemode + deferred）；命中后经 active tool set
  （`setActiveTools`）把匹配工具**声明给下一轮模型调用**，并记录进 transcript（resume/fork 后仍在）。
- 关键点：**曝光级别只控制"模型看得到什么"，不控制"能调用什么"**——deferred 工具注册后
  本来就可调用（"callable whenever registered"），发现与否不影响执行权限。

**Anthropic — Tool search tool（服务端实现，provider 特性）**

- `defer_loading: true` 的工具"load only when Claude discovers them through search"；
  官方原话：**"defer_loading controls what enters the context window, not what you send in
  the request"**——完整定义仍随每个请求发送（服务端要用它跑搜索），省的是上下文暴露，不是传输。
- 搜索在 Anthropic 服务端执行（regex / BM25 两种变体），命中以 `tool_reference` 块内联展开，
  prefix 不动以保 prompt cache；发现 ≠ 授权：执行仍是标准客户端 tool_result 流程，
  客户端仍决定是否执行。
- 模型支持面：Claude Opus/Sonnet 4.5 起（"Opus 4.1 and earlier models don't support"）；
  官方场景判据：10+ 工具 / 定义 >10k tokens / MCP 聚合 200+ 工具才值得；少于 10 个工具、
  定义总 <100 tokens 不建议。

**OpenAI — Tool search（Responses/Agents API，provider 特性）**

- `tool_search` 作为工具加入 `tools` 数组 + 工具标 `defer_loading: true`；deferred 函数
  模型仍先看到 name + description，"in practice tool search is mostly deferring the
  parameter schema"。
- host 模式下完整定义仍声明在请求里（服务端搜索）；client 模式下应用返回
  `tool_search_output`，"On the next turn, the loaded tool is callable like a normal function"，
  未列入 output 的工具对模型不可用；加载注入在上下文末尾以保 cache。
- 模型支持面：Responses API 仅 `gpt-5.4` 及以后。

### 契合点（对 `AGENTS.md` §7 不变量）

- **兼容**：曝光级别是 Registry 导出面（不变量 #7/#18 的统一 Tool 路径不变）——`tool_search`
  本身是一个注册进 Registry 的普通 Tool，经统一 Executor 执行；激活只改"下一轮 bind_tools
  的定义集"，不碰 Agent Loop 结构、不产生第二条执行路径（不变量 #21 / No hidden second path）。
- **兼容**：发现不授予执行权（审计备注硬约束）——本仓执行权边界 = Registry 成员资格
  （profile 收窄在 Registry 层物理剔除，ADR-0015 决策 11）+ Permission/Approval。
  `tool_search` 搜索它被注册进的那个（已收窄）Registry，被权限剔除的工具物理不在册 ⇒
  搜不到、调不到；deferred 激活不改变权限判定。
- **冲突/边界**：Anthropic/OpenAI 的 defer_loading 是 **provider 服务端特性**，模型支持面
  受限（Anthropic Opus 4.5+ / OpenAI gpt-5.4+），且本仓 `bind_tools` 是客户端静态绑定
  （`agent/runtime.py` 构造期一次绑定）。直接 REUSE 会让工具可见性依赖 provider 能力，
  违反 provider-neutral（Reuse Matrix §5）。⇒ 采用 Pi 的**客户端**机制：任何 provider 可用，
  代价是需要客户端动态重绑（每轮定义集变化时重绑一次）。
- **与审计备注对齐**：不宣称"省掉传输"——deferred 定义仍在 Registry，只是不进
  `export_model_definitions()` 的模型菜单；上下文暴露每轮减少，传输 payload 是否减少取决于
  provider（客户端绑定路径上定义确实不再随请求发送，但这是实现效果而非宣称的合同）。

### 判定

**PORT DESIGN（主判，Pi 机制）+ 借鉴 Anthropic/OpenAI 的语义边界，不依赖其服务端特性。**

- 不是 REUSE：无可直接依赖的库（Pi 是 TypeScript 运行时，Reuse Matrix §2 已判 Pi TS runtime = DEFER）。
- 不是 ADAPT：Anthropic/OpenAI 的 defer_loading 绑定 provider 服务端与模型版本，
  无法薄适配成 provider-neutral 合同。
- PORT DESIGN：把 Pi 的曝光级别（本项目收缩为票面最小集 `direct / deferred / hidden`，
  `model-only` / `codemode` 属未来扩展）与 tool_search（收缩为 token 重叠排序，不搬 BM25）
  用 Python 在本项目 Contract 上重新实现，不复制上游代码。
- 不实质复制 / Port 上游代码（只借鉴机制），License 环节因此不触发；Pi 为 MIT
  （`LICENSE: Copyright (c) 2025 Mario Zechner`），若未来实质移植需保留来源声明。

### Token 成本量化（审计备注要求的 ROI 输入，2026-10-06 实测）

- 测法：按 `assembly._build_tooling` 的 main/默认路径重建 Registry
  （BUILTIN_LOCAL_TOOLS + update_plan，capability 未配置 = 缺席），
  `export_model_definitions()` 序列化后用本仓 `context/tokens.py`（cl100k_base）计数。
- **现状：10 个工具，全量定义 ≈ 2,837 tokens/轮（常驻）**——单工具 148–788 tokens。
  当前规模下全量注入成本可忽略（<3k tokens）。
- **MCP 接入后（合成 MCP 工具 ≈ 207 tokens/个，GitHub server 量级）**：
  - 1 server × 50 工具 ≈ +10,350 ⇒ **≈ 13.2k tokens/轮**；
  - 3 server × 50 工具 ≈ +31,050 ⇒ **≈ 33.9k tokens/轮**；
  - 5 server × 50 工具 ≈ +51,750 ⇒ **≈ 54.6k tokens/轮**（与 Anthropic 官方
    "~55k tokens in definitions" 的多 server 量级陈述吻合）。
- **ROI 结论**：现状（全部 direct）零收益、零回归风险；真正的收益点在 MCP 接入后——
  3+ server 时工具定义每轮吃 30k+ tokens 常驻上下文（典型 128k 窗口的 25%+，且挤压
  compaction 预算）。票面定位为 P2 提前量（提前把机制铺好、默认行为不变）成立：
  实现面小（元数据 + 导出过滤 + 一个内置搜索工具 + 构造期可选重绑缝），默认全 direct
  时行为逐字不变。**不构成"现在就省 token"的效果，不得以此宣称当前运行成本下降。**
