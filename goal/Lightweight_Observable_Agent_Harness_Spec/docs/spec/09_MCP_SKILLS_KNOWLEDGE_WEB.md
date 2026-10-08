# 09 — MCP / Skills / Knowledge / Web

## 1. MCP

主链：

```text
Remote MCP Server
→ official/mature MCP Python Client
→ Tool Discovery
→ schema/metadata
→ MCPToolAdapter
→ ToolRegistry
→ ToolExecutor
→ remote invoke
→ ToolResult
```

原则：
- Agent 是 MCP Client；
- 不自研 wire protocol；
- MCP Tool 必须走统一 Runtime；
- MCP SDK retry 与 ToolExecutor retry 不得叠加失控；
- remote side effect 不能靠 Tool Name 猜，必须映射 metadata/policy。

### 1.1 MCP 包导入后续范围（PRD #868）

原 Phase 8 的合同仍限 MCP Client 的 tools 原语。后续包导入只为显式配置现有 MCP 能力提供入口，不扩展 MCP 原语：仅支持当前 `MCPServerConfig` 能表达的 stdio / Streamable HTTP 和 `tools/list` / `tools/call`。MCP resources、prompts、SSE transport 与 OAuth 登录均不属于首版兼容面；需要 OAuth 的 server 在预检中明确报告 `oauth_required_unsupported`，不得显示完整兼容或启动后伪装成匿名访问。

导入描述必须映射到现有 `MCPServerConfig` 字段（server name、transport、command/args 或 URL、cwd、env/headers、timeout、enabled、tool permission overrides）。没有明确描述时只报告缺少适配描述；不得从 package name、README 或任意脚本猜启动命令。预检只解析静态文件，不启动子进程、不连接远端。

凭据继续使用 ADR-0012 的 `${VAR}` / `${VAR:-default}` 环境变量引用；值在运行时展开，明文 secret 不写入安装记录、包清单或诊断。环境变量不可用时预检列明缺项，不回显 secret。安装或启用不覆盖 MCP 权限映射：只读提示的解释、最严默认、配置覆写、审批、单次执行和错误语义继续按 ADR-0012。

MCP server 启动属于执行边界。未信任的 server 不得因导入成功而启动；启动后的每个模型工具调用仍走 `ToolRegistry → Validation → Permission → Scheduler → ToolExecutor → Operation Ledger（需要时）→ ToolResult → SessionEvent`，不增加独立 retry 或副作用路径。server 连接失败仍按 OPTIONAL_RUNTIME 降级并可观察，不能拖垮基础 Agent。管理配置允许重启后生效，不要求热插拔。

## 2. Skills

参考 Pi 的 progressive disclosure 思路。

Skill 采用 `SKILL.md`：

```text
discover
→ read name + description
→ expose catalog to model
→ task requires skill
→ load full SKILL.md on demand
→ inject into Context
```

原 Phase 7 的 Runtime 规则保持不变：
- 支持 global/project skill directories；
- 支持发现和解析；
- 支持按需 load；
- 支持手动指定 Skill；
- Skill 全文不默认永久进入 Context；
- Skill 是 Context Capability，不等于 Tool。

### 2.1 Skill 包导入后续范围（PRD #868）

本项目可以从用户明确选择的本地目录或固定 Git commit 导入整个 Skill 目录。源目录根必须含 `SKILL.md`；`scripts/`、`references/`、`assets/` 和其他包内相对资源随同目录保留，引用仍以 Skill 根为基准。安装到 project 或 global 目录后继续由当前 SkillDiscovery 发现；导入不改变其一层发现规则、frontmatter 校验和渐进披露行为。

导入器不执行 Skill 中的脚本。相对引用缺失、越过 Skill 根或依赖当前宿主不支持的运行时/命令时，预检应逐项报告；不得仅因 `SKILL.md` 可读就判完整。Skill 正文仍按需加载；当模型按 Skill 指引请求脚本能力时，由现有命令/工具入口执行并沿用现有 sandbox、permission 与 approval 行为。Skill 内容与脚本本身不能声明或提升权限。

project/global 安装、项目显式启用、同 ID scope 选择、版本锁定/升级/回退、信任及待重启状态由 spec 08 §6 与 ADR-0052 定义；不要在 SkillCatalog 或 `CAPABILITIES` 中复制第二份安装状态。
## 3. Knowledge Capability

Knowledge/RAG 是插件，不是固定 Runtime Pipeline。

### Ingestion

```text
source docs
→ parse/sections
→ chunk
→ embedding
→ VectorStore Provider (Milvus default)
→ metadata persistence
```

Ingestion 与 Agent Runtime MUST 分离。

Agent 启动不得每次重新扫描、重 embedding 全部文档。

### VectorStore 抽象

Milvus 是默认 Provider，不绑定接口。

## 4. Chunk / Metadata

基本要求：
- token-window chunk；
- `overlap < chunk_size`；
- 保留 `heading_path + content` 作为检索文本设计选项；
- metadata 保留 doc_id/source/heading/hash/version。

## 5. retrieve_knowledge Tool

至少：

```text
query
kb_id
top_k
```

返回：

```text
chunk_id
doc_id
source/file_name
heading_path
score
content or artifact_ref
sufficient
```

模型自己决定何时调用，禁止关键词 if/else 强制 RAG。

## 6. Citation

Citation 必须从 Tool Result 一路携带。

```text
Vector/Web result + source metadata
→ ToolResult
→ SessionEvent
→ model
→ final citation
```

不得把预训练知识伪装成检索证据。

## 7. Incremental Index

```text
scan
→ doc identity
→ mtime/hash
→ unchanged: skip
→ changed: new version
```

生产型更新 SHOULD：

```text
write N+1
→ embed/insert
→ verify
→ switch active
→ cleanup N
```

避免 `delete old → insert new` 窗口风险。

## 8. Web Search / Retrieval Fallback

Web Search MUST 是独立 Tool，不允许藏进 `retrieve_knowledge()`。

策略：

```text
retrieve_knowledge
→ sufficient=true: answer, no web
→ sufficient=false
→ rewrite one query
→ web_search
→ synthesis + Web Citation
```

这只是轻量 Retrieval Fallback Policy，不实现复杂学术式 CRAG。

## 9. Failure Semantics

- Embedding failure：metadata 不得标记 INDEXED；
- Milvus unavailable：Knowledge Tool 返回明确错误；
- KB 无证据：`sufficient=false`；
- KB 足够：不得无意义联网；
- Web provider failure：明确失败，不伪造来源；
- Citation 只能指向真实 Tool Result。

## 10. Acceptance Criteria

- MCP Tool 走统一 Executor；
- Skill 只在需要时加载完整内容；
- 重启后 Knowledge 可直接 search；
- KB sufficient 时不联网；
- insufficient 时可以 web fallback；
- Knowledge/Web Citation 可验证；
- VectorStore Provider 可替换；
- ingestion 与 chat runtime 独立。
