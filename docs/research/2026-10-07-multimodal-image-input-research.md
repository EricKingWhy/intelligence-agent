# 多模态图片输入（Web + CLI）调研报告（2026-10-07）

> 调研问题：本项目（Python Agent Harness，CLI + FastAPI SSE + Web Inspector）如何让用户在
> Web 端与 CLI 端发送图片（多模态输入）。本文是 `AGENTS.md` §6.1 / SDD 协议 §1.3 要求的
> 方案先行调研产物，供后续设计/选型票（to-spec → to-tickets）引用。
>
> **证据来源说明（如实声明）**：本仓规格/协议/代码现状由本会话主代理直接实读；外部产品证据由
> 4 个调研子代理按「≥2 独立来源交叉验证 + file:line/commit/读取日期」标准采集，主代理未逐一
> 重开克隆文件复核，引用时以各来源登记的 commit 与 URL 为准。单源项与待核实项在 §6 明确登记。

---

## 0. 结论先行

1. **成熟产品已收敛出两条互补的通用模式**：
   - **Web 聊天 UI（Open WebUI / LibreChat / LobeChat 一致）**：上传与消息解耦——图片字节先传
     独立附件端点拿到 file_id/URL，**消息里只存引用**，发给 Provider 前一刻才物化成 base64；
     前端预检 + 后端权威强制的双层限制；显示走带鉴权的内容端点。
   - **CLI 编码 agent（Claude Code / Codex / Gemini CLI / Aider 一致）**：入口 = 剪贴板粘贴 +
     路径引用（@文件/参数），像素以 base64 content block **内联**进消息；部分产品发送前缩放、
     非视觉模型显式降级。CLI 主流把 base64 内联进会话 JSONL（resume 后图仍在）。
2. **本项目推荐取「Web UI 共识 + Pi 的能力降级」路线**：事件流与 JSONL 只存
   `artifact ref`（本仓不变量 #15 与 event-sourced 事实源的自然延伸），Provider 请求时在
   derive/Context 侧物化为 base64 content part。好处：JSONL 不膨胀、fallback 到非视觉模型时
   天然可按请求降级、fork/复用走既有 Artifact Ref 语义（spec 03 §7）。
3. **Provider 侧无硬阻塞**：三个默认 Provider 方向（OpenAI-compatible / Qwen / DeepSeek）当前
   都有视觉模型且都支持 `data:` base64 URL 的 OpenAI 风格 `image_url` part。注意 DeepSeek 的
   `deepseek-flash` 有视觉但**图片只允许出现在 user 消息**（system/assistant → 400）。
4. **本仓现状是全链路纯文本假设**，需要动 6 层（数据模型 / Artifact 存储 / Web API / 前端 /
   CLI / Provider 降级），无一处现成多模态代码；`supports_vision` 能力位已存在但是死元数据。

---

## 1. 本仓现状与 Gap（实读证据）

| # | 层 | 现状 | 证据 |
| --- | --- | --- | --- |
| 1 | 事件模型 | `USER_MESSAGE`（`user/message`）持久化，`data.content` 纯 str；消费方普遍 `isinstance(content, str)` 硬校验 | `src/agent_harness/session/event.py:49`、`agent/runtime.py:1598-1613`、`session/derive.py:232,247` |
| 2 | 投影 | `derive_messages` 产 `HumanMessage(content=str)`；token 估算 `model_dump_json`+tiktoken、压缩摘要、tool-result 裁剪全按文本假设 | `session/derive.py:1304-1305`、`context/builder.py:1237,1275,738+` |
| 3 | Provider | 单一 OpenAI-compatible 适配器（langchain-openai `ChatOpenAI` 子类），deepseek/qwen 只是 base_url 预设；`supports_vision` 能力位存在但运行时零消费 | `model/provider.py:46-78,92-160`、`model/config.py:27-84,114`、`web/catalog.py:127-132` |
| 4 | CLI | 一次性 argparse 子命令（无 REPL），无附件/@file 机制；唯一文件读取是 `ingest` 的 UTF-8 文本 | `cli.py:848,930-957,1071,1093` |
| 5 | Web API | 请求 schema 纯 JSON str 且 `extra="forbid"`；零上传/multipart 端点；`BODY_MAX_BYTES = 1 MiB` 中间件会拦大 body | `web/app.py:529-546,727-765,1941,2844`、`web/wire_safety.py:196,242-317` |
| 6 | 前端 | Composer 纯 textarea；`sendMessage` 仅 JSON；Markdown 渲染器明确不做图片；CSP `img-src 'self' data:` 已放行（有利条件） | `web/src/components/Composer.tsx:426-438`、`web/src/lib/api.ts:594-600`、`web/src/lib/markdown.tsx:7`、`web/app.py:1449` |
| 7 | Artifact | ArtifactStore 按构造是**文本**存储：hash=`sha256(content.encode("utf-8"))`，三个 Provider 全部 UTF-8 编解码，非 UTF-8 load 即失败；`artifact/created` 有词汇无生产发射点；Web 读 API 是行切片 JSON 文本，无二进制端点 | `storage/artifact.py:81-120,214`、`storage/local_artifact.py:104,150-157`、`storage/minio_artifact.py:90`、`tooling/overflow.py:162-166`、`web/app.py:2592-2683`、`web/artifacts.py:27-28` |
| 8 | 配置 | 无任何 upload 大小 / MIME 白名单 / 图片尺寸配置键；现有限制仅 1 MiB body、100k 字符、`artifact_overflow_chars=2000` | `config.py:102,140-144,158` |

**Gap 汇总**：① 事件 payload 无附件位且派生/记忆/队列消费方需加法式兼容；② ArtifactStore 不支持
二进制；③ Web 无上传端点、无二进制取回端点、1 MiB body 上限封死 base64-in-JSON 路线；④ 前端
无上传交互与附件渲染；⑤ CLI 无附图入口；⑥ Provider 层无 vision 能力判定 / 降级 / per-model
缩放策略；⑦ 图片 token 计费口径缺失（tiktoken 不计图）。

---

## 2. 成熟产品机制（交叉验证）

### 2.1 CLI 编码 agent（4 家）

| 产品 | 入口 | 进模型 | 持久化 | 降级/限制 |
| --- | --- | --- | --- | --- |
| Claude Code（闭源） | 拖拽；粘贴 Ctrl+V（Win/WSL Alt+V）插 `[Image #N]` chip；prompt 内路径；无斜杠命令 | Anthropic base64 image block 内联 | `~/.claude/projects/**.jsonl` 内联 base64（本机实测） | 无官方降级文档（待核实）；API 层单图 ≤10MB、自动降采样 1568px、token=28px patch |
| Codex CLI @ `8e23d183` | 粘贴 Ctrl+V/Ctrl+Alt+V → 剪贴板重编码 PNG 临时文件；粘贴路径自动转附件；`@` 补全选图；`codex exec -i/--image` | `UserInput::LocalImage{path,detail}` → 序列化时 data URL 内联；默认 ResizeToFit 2048px，detail 分档 6000px；1 GiB 护栏、32 MiB 传输预算 | Rollout JSONL 内联 base64 + 保留 `local_images` 路径供 UI 重挂 | `view_image` 工具对非视觉模型拒绝；读取失败插文本占位符 |
| Gemini CLI @ `fb972b2f` | 粘贴 Ctrl+V → 剪贴板写 PNG 临时文件 → 插 `@path`（粘贴统一进路径管线）；`@` 命令 read_many_files 透传 `inlineData` | Gemini `inlineData{data,mimeType}` | 会话 JSON 内联 inlineData；独有的 BlobDegradationProcessor 把旧图降级为「落盘文件+文本占位」控上下文 | 读取上限 20MB；无客户端缩放 |
| Aider @ `5dc9490b` | `/add <image>`；`/paste`（ImageGrab→临时文件）；启动参数 | OpenAI `image_url` data URL（detail=high）+ 前置文本标签 | **不持久化**：历史只记文本行，图片留内存文件集每轮从磁盘重读 | 最显式：`/add` 按 `supports_vision` 直接拒绝；消息构建层无 vision 则整体不发 |

共性：① 全部「像素内联」，绝不只给路径；② 粘贴+路径是普遍双入口；③ 发送前缩放只有 Codex 做
（其余交服务端或不做）；④ Agent 侧主动读图工具（`view_image` / read_file）是普遍第二入口。

### 2.2 Web 聊天 UI（3 家，克隆实证）

| 环节 | Open WebUI @ `8bd8b4fa` | LibreChat @ `e1dfc104` | LobeChat @ `0b064e7c` |
| --- | --- | --- | --- |
| 前端上传 | file input + 粘贴 + 拖拽；可选 canvas 压缩；无 vision 模型时**禁止上传** | file input + 粘贴 + 拖拽；默认关闭的客户端缩放（1900px/q0.92，跳过动图） | file input；未见客户端压缩（单源待核实） |
| 后端 | `POST /api/v1/files/` multipart；后端落盘后校验大小→413；扩展名仅 process 时白名单 | multer `fileFilter` MIME 白名单 + `limits.fileSize`；业务层权限/合规 | tRPC 预签名直传 S3（≥64MB 分片）；zod 大小上限双层 |
| 存储 | Storage Provider 抽象 local/S3/GCS/Azure；files 表存引用 | FileStrategy 策略族（local/s3/firebase/…，可按 avatar/image/document 细分） | S3-only，files 表 + `messages_files` 连接表 |
| 消息模型 | `userMessage.files=[{type:'image',id,url,name,…}]` **只存引用** | message `content:[{type:'image_file',image_file:{file_id}}]` + attachments 元数据 | 连接表引用，消息无图片列 |
| 发送时物化 | middleware 每次补全 `convert_url_images_to_base64`（SSRF 校验+所有权检查） | `encodeAndFormatImages` 按 endpoint 输出 OpenAI/Anthropic/Google 原生块；anthropic/google/ollama/bedrock 强制拉回 base64 | context-engine 重建；vision 模型直接上行 S3 URL，非 vision 替换为**文本占位符** |
| 显示 | `/files/{id}/content` 鉴权端点 | `/images/` 静态 + secureImageLinks 可选校验；S3 签名 URL | `/f/{id}` **故意不鉴权**（`<img>` 无法带 cookie）——反面对照 |
| 历史重发 | 每轮全量重注入+重物化；Compaction 摘要时旧图随旧消息丢弃（每图按 1000 tokens 估） | 默认 `resendFiles=true` 每轮重发 + 历史附件限额 | 每轮重建保留 imageList |

**三家共识**：上传与消息解耦、消息只存引用、发送时物化、双层限制（后端为权威并把配置下发前端）、
显示走受控端点、vision 门禁前置。

### 2.3 Provider API 格式（全部官方文档核实，读取日期 2026-10-07）

- **OpenAI Chat Completions**：`image_url` part 同时接受 http(s) URL 与 `data:` base64 URL；
  `detail ∈ {auto,low,high,original}`；SDK `chat_completion_content_part_image_param.py:10-21`。
- **DeepSeek（重点纠偏）**：**已有视觉**——`deepseek-flash`（旧名 deepseek-v4-flash-vision-exp 退役）。
  OpenAI 兼容 `image_url`（base64 data URL 支持）；限制：单图 ≤32MiB、请求 ≤48MiB、≤600 张；
  **图片只允许出现在 user 消息**（system/assistant → 400），tool 结果图需转 user 消息重发；
  另有 Anthropic 兼容端点与 Responses API。来源：api-docs.deepseek.com/guides/vision + API reference。
- **Qwen / DashScope**：OpenAI 兼容 `image_url`，base64 data URL 明确支持；base64 ≤250 张、
  请求 ≤64MB；`enable_thinking`/`vl_high_resolution_images` 走 `extra_body`。
- **GLM（智谱）**：同 OpenAI 形态；现行系列 ≤50 张/5MB/6000px；注意 GLM-4V-Flash 不支持 base64
  且限 1 张（老模型坑）。
- **Anthropic**：image block `source ∈ {base64,url,file}`；Bedrock/GCP 仅 base64；单图 ≤10MB
  （Bedrock 5MB）；>20 张/请求触发严格尺寸；image block 可挂 `cache_control` 进 prompt cache。
- **Gemini**：`inline_data`（base64）与 `file_data`（Files API URI）二选一；inline 整请求 ≤20MB。
- **LangChain**：v1 标准 content blocks（`langchain_core/messages/content.py:498-546`
  `ImageContentBlock`，@ `225ec1e8`）与 Pi 的 `ImageContent{data,mimeType}` 近乎同构；**请求方向
  的「标准块→provider 载荷」翻译在各 partner 集成包内，core 不代做**。

### 2.4 Pi harness（`D:\reference\pi` @ `1b347794`，本地浅克隆实读）

- `packages/ai/src/types.ts:411-414`：`ImageContent = { type:"image"; data: base64; mimeType }`；
  `UserMessage.content: string | (TextContent|ImageContent)[]`；模型目录带
  `input: ("text"|"image")[]` + `ModelImageInputLimits`（每模型缩放档与张数上限）。
- Provider 翻译：Anthropic → base64 source（:128-170）；OpenAI → data URL（:1267-1275），
  **tool 结果里的图抽出来作为独立 user 消息**（:1399-1457，与 DeepSeek 约束互相印证）；
  Gemini → inlineData。
- 降级：`api/transform-messages.ts:35-51` `downgradeUnsupportedImages`——模型 `input` 不含
  "image" 时请求前统一替换为文本占位符 `"(image omitted: model does not support images)"`，
  不是丢弃、不是报错、不靠 prompt。
- 持久化：`packages/durable` JSONL **内联 base64**（`session-manager.ts:1124-1129`）；
  靠发送前 resize + 每模型 limits 控体积；resume 后图仍在。
- CLI：入口是 `@文件路径`（MIME magic bytes 探测 jpeg/png/gif/webp/bmp → 自动缩放 → base64）；
  未发现剪贴板粘贴入口。

---

## 3. 推荐方案（方向级，待 to-spec 细化）

### 3.1 总体形态：引用存储 + 请求时物化（Web UI 共识 × 本仓不变量）

```text
用户附图（Web multipart / CLI --image）
→ 校验（magic bytes MIME 白名单 + 大小/数量上限，双层：入口预检 + 服务端权威）
→ ArtifactStore 二进制 save（Local/MinIO，sha256(bytes)）
→ user/message 事件 payload 增加 attachments 引用数组（content 保持 str）
→ derive_messages/Context 构建时按「当前请求模型的能力」物化：
     支持 vision → HumanMessage(content=[text, image_url(data:base64,…, detail=…)])
     不支持 vision → 替换为文本占位符 + 发 warning 事件（Pi 模式）
→ Provider 请求
```

与不变量的对齐：

- **#15 Artifact 优先 / #7 完整保存≠完整注入**：JSONL 与事件只存 ref，字节进 ArtifactStore；
  事件流不膨胀（相对 CLI 主流内联 base64 的关键差异，也是本仓 JSONL 作为事实源的必然选择）。
- **#3 append-only / #4 模型可见输入可追溯**：attachments 引用是持久化对话事实；物化发生在
  模型可见投影层，可从事件完整重建。
- **spec 03 §7**：fork 时「Artifact Ref 可以按权限复用」已有语义；compaction summary 本就要求
  保留 `artifact_refs`（spec 06 §5）——旧图被压缩时引用仍可回溯。
- **无第二条 Tool 路径 / Loop 不特判**：图片是消息内容的一部分，不新增工具、不改 Agent Loop；
  能力判定与降级在 derive/Context 层与 adapter 层。

### 3.2 分层要点

1. **数据模型**：`user/message` payload 加法式扩展 `attachments: [{kind:"image",
   artifact_id, mime_type, size, name?, detail?}]`，`content` 保持 str（消费方 isinstance 校验
   不破坏）；上传本身是否需要独立持久化事件（扩展 `artifact/created` 加 `origin=user_upload`
   vs 新事件）→ 设计票决定。派生/记忆/队列消费方同步兼容（记忆抽取对图降级为文本摘要）。
2. **Artifact 二进制化**：`save/load/hash` 字节化 + mime 感知；Local 先行、MinIO 同步；存量
   文本 artifact 行为不变（加法扩展）。配套配置：上传大小/单消息数量/MIME 白名单/可选发送前
   缩放档（默认建议 2048px，Codex 默认档，兼容 Anthropic 1568 服务端降采样）。
3. **Web API**：`POST /api/sessions/{id}/attachments`（FastAPI `UploadFile` multipart，框架
   原生 REUSE）；`GET .../artifacts/{id}/content` 二进制端点（Content-Type 感知 + 所有权校验，
   供 `<img>` 显示）；`SendMessageRequest`/`CreateSessionRequest` 增加附件字段；
   `wire_safety` 的 1 MiB body 上限对上传路由单独放宽（否则 multipart 直接被拦）。
4. **前端**：Composer 加文件选择/粘贴/拖拽三入口 + 客户端预检（可选用 LibreChat 式默认缩放）；
   用户消息渲染附件位（`<img src=受控端点>`，CSP 已放行 `'self'`）；无 vision 模型时上传禁用/
   警示（Open WebUI 模式）。**不做 LobeChat 式无鉴权图片路由**（反面对照）。
5. **CLI**：`--image <path>`（可多次/逗号分隔，Codex `exec -i` 模式）+ magic bytes MIME 探测
   （Pi `detectSupportedImageMimeType` 同款判据）；`@路径` 语法可作为后续交互式 CLI 的第二入口。
6. **Provider/降级**：`supports_vision` 从死元数据接入运行时：入口拦截（Aider 模式）+ 请求前
   占位符降级（Pi 模式）双保险；per-model 缩放档（Pi `ModelImageInputLimits` 模式）；
   `detail` 可配默认 auto；图片 token 计费按 Provider 公式近似计入预算（Open WebUI 的
   1000 tokens/图是现成下界参照）。DeepSeek 约束天然满足（我们只往 user 消息放图）。
7. **明确 DEFER**：tool 结果图（sandbox 截图回喂，需 user 消息重包，Pi/Codex `view_image`
   先例）；图片**生成**（Pi `packages/ai/images.ts` 是另一领域）；剪贴板粘贴进终端 REPL
   （本仓 CLI 目前无 REPL，属独立前提）。

### 3.3 风险与设计票必须回答的问题

- `derive.py` 7 处 `isinstance(content, str)` 消费方（含 memory v2、queue/steer 路径）的加法
  兼容面；`EVENT_VOCABULARY` 生成物与守卫测试同步。
- token 估算口径（`model_dump_json`+tiktoken 对图片 content 会失真）与 hard guard 的交互。
- 附件的保留策略（artifact 生命周期 vs session 生命周期；fork 跨 session 复用权限）。
- fallback 到非视觉模型时的降级事件与 UI 提示语义（占位符是模型可见事实，需可对账）。

---

## 4. 方案依据块（SDD 协议 §1.3）

**来源（≥2 独立来源/机制项）**：

| 来源 | 类型 | 登记 |
| --- | --- | --- |
| Open WebUI @ `8bd8b4fa` / LibreChat @ `e1dfc104` / LobeChat @ `0b064e7c` | 源码浅克隆 | 2026-10-07，file:line 见 §2.2；交叉验证 docs.openwebui.com（env-configuration、S3 教程）、librechat.ai（file_config、S3/CDN）、lobehub.com 环境变量文档 |
| Codex @ `8e23d183` / Gemini CLI @ `fb972b2f` / Aider @ `5dc9490b` | 源码浅克隆 | 2026-10-07，file:line 见 §2.1；交叉验证 learn.chatgpt.com/docs/codex/cli、gemini-cli 仓库官方 docs、aider.chat/docs/usage/images-urls.html（旧 images.html 已 404，已核实） |
| Claude Code | 官方文档 + 本机 transcript 实证 | code.claude.com/docs（common-workflows / interactive-mode / data-usage / slash-commands），2026-10-07 |
| Anthropic / OpenAI / Gemini / DeepSeek / Qwen / GLM 官方 API 文档 | 官方文档 | 2026-10-07，URL 见 §2.3；SDK 佐证：anthropic-sdk-python @ `18f25547`、openai-python @ `bd74c4ec`、python-genai @ `6e36aaae` |
| LangChain core @ `225ec1e8` | 源码 + docs.langchain.com | `content.py:498-546`；standard content blocks v1 |
| Pi @ `1b347794`（本地克隆） | 源码实读 | file:line 见 §2.4 |

**机制摘要**：见 §0–§2（Web UI 的「引用+物化」共识；CLI 的「内联+缩放+降级」；Pi 的 per-model
能力/限额/占位符降级；各 Provider 的 base64 data URL 兼容面）。

**契合点**：引用存储与 #15/#7/#3/#4 及 spec 03 §7 / 06 §5 对齐（§3.1 逐条）；与 CLI 主流
「JSONL 内联 base64」的差异是**有意的偏离**，由本仓 JSONL 事实源 + Artifact 层既有投资正当化；
不新增第二条工具路径、不改 Agent Loop、不引入新框架。DeepSeek「图片仅 user 消息」约束与本项目
形态天然兼容。

**判定（口径 = Reuse Matrix §4/§5）**：
- Artifact 二进制化扩展：**BUILD**（项目自有 Contract 的最小扩展，无上游可复用此层 glue）；
- Web 上传端点：**REUSE**（FastAPI UploadFile/multipart 框架原生，懒惰阶梯第 4 级）+ 两层校验
  模式 **PORT DESIGN**；
- 消息模型附件引用：**PORT DESIGN**（Pi `ImageContent` / LangChain `ImageContentBlock` 的中立
  三字段形态 {base64, mime_type, url|file_id} 作参考，落成 Pydantic DTO）；
- Provider 载荷与降级：**REUSE** langchain-openai 既有多模态消息透传（设计票需先验证
  `ChatOpenAI` 对 content 列表的 data URL 处理）+ 降级/限额策略 **PORT DESIGN**（Pi）；
- 前端上传/粘贴/拖拽与附件渲染：**BUILD** 最小实现（React 侧项目自有，模式参考 LibreChat/
  Open WebUI）。

**License**：本轮全部为 PORT DESIGN / 模式借鉴，无实质复制；若后续实质复制上游代码，按矩阵 §1
流程先查 License 并保留来源（LibreChat MIT、Pi 待查、Open WebUI License 近期有变更条款，复制前
必须重查——本条本身不依赖其结论）。

---

## 5. 启动检查表（AGENTS.md §3）

| 前置项 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision 相关原则 | `SPEC_ROOT/00_PROJECT_VISION.md` 全文（§2.5 Reuse First、§3 冻结原则 4/7/15/17、§4 V1 形态） | READY |
| 当前任务规格 | `SPEC_ROOT/02_AGENT_RUNTIME.md` §3；`03_SESSION_EVENT_MODEL.md` §3.1/§4/§7；`06_CONTEXT_ARTIFACT_MEMORY.md` §3/§8；`11_STREAMING_API_WEB_UI.md` §4/§5/§7 | READY |
| Reuse 相关判定 | `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md` 全文；`docs/agents/reference-sources.md` 全文 | READY |
| Phase 依据 | `SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md` 全文——多模态不在既有 16 个 Phase 内，属新能力票（走 to-spec/to-tickets），不新增 Roadmap 阶段 | READY |
| 本任务触发细则 | `docs/SDD_WORKFLOW_PROTOCOL.md` 全文（§1.3 方案依据、§8.9 写盘纪律）；`reference-sources.md` §3.1 | READY（有一处偏差，见下） |

**偏差登记（已闭合）**：`reference-sources.md` §3.1 要求「出现新领域先补本清单再调研」；首轮调研
开始时该文件有他人未提交改动（IMP 批次补录），为避免缠绕在途工作，本轮**先完成调研、后补清单**。
该文件他线落地后已补录「多模态图片输入 / 附件」领域条目（含本地克隆 deepseek-harness / oh-my-pi /
codex 与 2026-10-07 实测 HEAD），偏差披露也写在该条目内。本偏差不改变调研证据标准
（所有来源仍按 ≥2 独立来源执行）。

---

## 6. 待核实项汇总（不得作为方案依据引用）

- Anthropic/OpenAI/Gemini/Qwen/GLM 的具体数字限制多为官方单页来源（JSON 形态均为双源）；
- Open WebUI 后端对非视觉模型无载荷级拦截、LibreChat 无自动降级、LobeChat 无客户端压缩与
  历史截断 token 预算——均为反向 grep 未命中，非肯定结论；
- Claude Code 客户端侧 resize/降级行为（闭源无文档）；Gemini Files API 单文件上限；
- Aider resume 后图片是否随文件列表恢复（文档与代码未见专门机制）。

---

## 7. 第二轮调研（2026-10-07）：按用户指定上游的深度提取

用户 2026-10-07 裁决：**Web 端与桌面端以 DeepSeek Harness 为主要复用源；CLI/TUI 端以
Pi、oh-my-pi、Claude Code、Cline 为复用源；能抄就不自写。** 本节记录该轮提取结果与复制清单，
§8 给出合并后的三端方案，§9 记录新增约束。

### 7.1 DeepSeek Harness（`D:\reference\deepseek-harness` @ `5badb150`，MIT，2026-10-03）

**领域模型（与目标项目同构）**：`packages/attachment` 定义内容寻址附件——`AttachmentId` 是不透明
id（永不暴露路径/URL）、`ImageAttachmentRef{attachmentId, mediaType, bytes, width, height, name?}`、
`FileAttachmentRef{attachmentId=sha256, name, bytes}`、`ImageAttachmentLimits{maxImageBytes,
maxImagesPerMessage, maxMessageImageBytes, maxImagePixels, maxImageDimension, mediaTypes}`；
`PromptContentPart`（浏览器提交的 text/image-base64）→ `AdmittedPromptContentPart`（落库的
text/image(ref)/file(ref)）。证据：`packages/attachment/attachment/src/types.ts:8-163`、
`attachment/src/index.ts:53-263`、`attachment-local/src/index.ts:34-58,147-201`；
双源：`docs/subsystems/attachment.md:5,7,15-57`。

**关键不变量（逐字）**：`docs/subsystems/attachment.md:5` — "Session events and model-visible
attachment blocks contain that reference and metadata, never a browser object URL, host temporary
path, provider URL, or base64 payload."；`:7` — 图片字节在 user 事件 append **之前**已落到
`<DSH_HOME>/attachments/v1`（persist-before-event）。

**Web 链路**：前端 intake 分散在 `packages/client/ui-conversation`（编排/校验）与
`packages/client/ui-attachment`（展示）——隐藏 `<input type=file multiple>`（`InputBar.tsx:434-441`）、
文档级拖拽监听（`ui-attachment/src/client/drop-events.ts:30-87`）、Lexical `PASTE_COMMAND`
（`keymap.ts:159-185`）；**客户端不压缩不缩放**（`ui-conversation/src/client/service.ts:124-137`），
归一化全在 host 侧用 sharp（`attachment-local/src/normalization.ts:109-137`）；前端预检镜像 host
限制（`InputBar.tsx:211-233`）。图片走 prompt JSON（base64 随 body），通用文件走流式端点
`POST /api/session/uploadFileBinary`（`packages/client/file-upload/src/http-route.ts:22-67`，
`requestBody:'streaming'`）。

**历史重放**：受控端点 + session 授权（`session.readAttachment(attachmentId)` →
`ui-conversation/src/client/conversation/historical-images.ts:103-131`），host 端先证明该 id 被本
Session 事件引用（`session-controller/src/commands.ts:391-425`）。

**降级**：prompt admission 按 `model.inputModalities` 拦截 →
`MODEL_DOES_NOT_SUPPORT_IMAGES`（`session-controller/src/commands.ts:336-348`）；另有文本化投影与
超预算 `IMAGE_OFFLOAD_REQUIRED` + `compaction-image-offload` 重试。

**桌面链路**：桌面窗口直接加载同一 Web 应用（`apps/desktop/src/web-document.ts:18-36`），
**没有独立附件实现**；桌面特有只有 `__DSH_HOST_PATHS__` 路径桥
（`apps/desktop/src/preload-app.ts:79-84`，`webUtils.getPathForFile`）→ 有真实路径的非图片文件转
`@path` 引用而不上传（`ui-conversation/src/client/apply.ts:457-498`）。

**⚠ 与本仓的硬冲突**：DSH 图片 base64 随 JSON prompt 提交，默认 body 上限 **300 MiB** 且启动断言
`maxRequestBodyBytes ≥ maxMessageImageBytes*4/3 + 1 MiB`（`packages/client/connection/src/http-bridge.ts:14`、
`connection/src/index.ts:71-86`）；本仓 `BODY_MAX_BYTES = 1 MiB`（`web/wire_safety.py:196`）。
单张归一化图（4 MiB）base64 后 ≈5.3 MiB ⇒ **必须二选一**：(a) 按 DSH 公式放宽本仓 body 上限；
(b) 图片也走流式上传、prompt 只带 ref。**推荐 (b)**（与通用文件同路，不动 1 MiB 约束）。

### 7.2 CLI / TUI 侧（Pi / oh-my-pi / Cline / Claude Code）

**关键发现：目标 TUI 已依赖 `@earendil-works/pi-tui@^1.0.4`（`tui/package.json:20`，唯一 runtime
依赖），该包已自带**：终端图片协议层（`terminal-image.ts`，kitty/iterm2，无 sixel）、`Image` 组件、
`getNativeClipboard()` 原生剪贴板（含 win32 x64/arm64 预编译 `win32-platform.node`）、编辑器大粘贴
折叠。已用 npm tarball 1.0.4 核对导出存在。目标 TUI 现状：`tui/src/app.ts:97,107,112` 用 pi-tui
`Editor`，`interceptKeys`（`app.ts:115-131`）只处理 Ctrl+C 与审批，无粘贴分支；
`tui/src/api.ts:78-81` 发送体 `{content, mode}` 纯文本。

**各上游机制要点**：

| 上游 | 附图入口 | 表示 | 持久化 | 降级 |
| --- | --- | --- | --- | --- |
| Pi @ `1b34779`（MIT） | 粘贴（键位 `alt+v`/Windows，`ctrl+v` 其他；`keybindings.ts:142-145`）→ 临时文件 + 编辑器插**路径文本**；`@路径` 参数（`cli/file-processor.ts:26-70`）；拖拽=路径粘贴 | 纯路径文本（无 chip） | 交互路径只存路径文本；`ImageContent` 内联 base64 在编程路径（`session-manager.ts:162`） | `getNonVisionImageNote` 文本提示（`read.ts:59-64`） |
| oh-my-pi @ `1c0993c3`（MIT） | 粘贴（含 **OSC 5522 Kitty 剪贴板协议**增强，`enhanced-paste.ts:96-220`）；路径粘贴 `handleImagePathPaste`（`input-controller.ts:2288-2340`） | **`[Image #N]` chip + pendingImages**，缩略图卡片（`prompt/attachment-chips.ts`） | **内容寻址 blob 外置**：`blob:sha256:<64hex>`，会话只存引用（`session/blob-store.ts:9-12`，注释明确 "externalizing large binary data (images) from session JSONL files"）；`attachment://N` URI 定位原图 | 视觉模型描述后注入（`agent-session.ts:6818-6829`） |
| Cline @ `5b67631`（Apache-2.0，OpenTUI） | `handlePaste` 事件带 `bytes+mimeType`，空文本时读系统剪贴板；Ctrl+V 兜底（`input-bar.tsx:143-196`）；`image-paste.ts:150-217` 平台分支 | `[Image N]` marker + ref 数组（`use-prompt-input-controller.ts:149-153`） | 内存 data URL（JSONL 形态未确认） | 未找到拦截逻辑 |
| Claude Code（闭源） | `Ctrl+V`/`Alt+V`(Win/WSL)/`Cmd+V`(iTerm2)；拖拽；路径文本（`interactive-mode.md:28`、`common-workflows.md:305-310`） | `[Image #N]` chip，可位置引用 | 文档未披露 | 文档未披露 |
| codex @ `7f892275`（Apache-2.0，旁证） | 粘贴 `paste_image` → 临时 PNG；WSL PowerShell 回退（`clipboard_paste.rs:125-230`） | `[Image #N]` 占位 + 隐藏 path 标签（`models.rs:1681-1689`） | rollout 存**路径引用** `UserInput::LocalImage{path}` | 占位符（`image_preparation.rs:36-43`） |

**Windows/WSL 硬事实（三方一致，可作依据）**：① Windows 上 `Ctrl+V` 常被终端截获 ⇒ 用 `Alt+V`；
② WSL 下 `wl-paste`/`xclip` **拿不到 Windows 截图**，必须经 `powershell.exe`
（Pi `clipboard-image.ts:110-165`、codex `clipboard_paste.rs:157-196`、Cline `image-paste.ts:213-217`）；
③ **Windows Terminal 不渲染终端图片**：Pi 显式 `if (process.env.WT_SESSION) return { images: null }`
（`terminal-image.ts:108-110`），win32 console 同（`:119-124`）；oh-my-pi 记录 WT 需 1.22+/1.24+
且仅 SIXEL（`terminal-capabilities.ts:333-350,382-385`），而 pi-tui 不支持 sixel ⇒ **Windows 上只应
承诺文本占位 + 原图路径，不承诺缩略图**。

### 7.3 复制清单（合并两轮，按落点）

**A. 后端 `src/agent_harness/`（DSH 是 TS，只移植契约与算法）**

| 上游（DSH @ `5badb150`，MIT） | 处置 | 落点 |
| --- | --- | --- |
| `packages/attachment/attachment/src/types.ts`（164 行） | ADAPT→Python 类型（brand→NewType） | `attachments/types.py` |
| `.../attachment/src/error.ts`（87） | ADAPT（稳定错误码） | `attachments/errors.py` |
| `.../attachment/src/admission.ts:15-24`（canonical base64 校验） | ADAPT | 同上 |
| `.../attachment/src/request-projection.ts`（63，纯函数） | COPY（算法） | `attachments/projection.py` |
| `.../attachment-local/src/file-store.ts:45-56`（文件名消毒/Windows 设备名） | COPY（算法） | `storage/attachment_local.py` |
| `.../attachment-local/src/store.ts:214-388,431-458`（原子内容寻址发布 + 读校验） | ADAPT | 同上 |
| `.../attachment-local/src/normalization.ts`+`image.ts`+`encoding.ts` | ADAPT（sharp→Pillow） | `attachments/normalize.py` |
| `.../attachment-local/src/compression-limiter.ts`（53） | COPY（算法） | 同上 |
| oh-my-pi `session/blob-store.ts`（426） | PORT DESIGN 范本（内容寻址外置） | 同上 |

**B. Web 前端 `web/src/`（DSH MIT）**

| 上游 | 处置 | 落点 |
| --- | --- | --- |
| `ui-attachment/src/client/drop-events.ts`（88） | **COPY** | `web/src/components/attachments/drop-events.ts` |
| `ui-attachment/src/DropOverlay.tsx`（77） | **COPY** | 同上 |
| `ui-attachment/src/AttachmentRail.tsx`（171） | ADAPT（图标/CSS） | 同上 |
| `ui-attachment/src/FileCard.tsx`（81） | ADAPT | 同上 |
| `ui-attachment/src/MessageImage.tsx`（192） | ADAPT（lightbox→Radix） | 同上 |
| `ui-attachment/src/client/MessageImages.tsx`（17） | COPY（薄） | 同上 |
| `ui-attachment/src/client/ComposerAttachments.tsx`（108） | ADAPT（props） | 同上 |
| `ui-conversation/src/client/service.ts`（609） | ADAPT（重写 hook；Cordis 依赖剥离） | `web/src/hooks/useAttachments.ts` |
| `ui-conversation/.../keymap.ts:159-185`（粘贴取 File） | ADAPT | `web/src/lib/paste-files.ts` |
| `ui-conversation/.../InputBar.tsx:205-243,434-441`（预检+file input） | ADAPT | `web/src/components/Composer.tsx` |
| `ui-conversation/.../historical-images.ts`（182） | ADAPT（重写 hook） | `web/src/hooks/useImageUrls.ts` |
| `ui-conversation/.../image-labels.ts`（59） | COPY（映射表） | 随 Composer |
| `file-upload/src/http-route.ts`+`protocol.ts` | ADAPT（FastAPI 流式端点参照） | `src/agent_harness/web/upload.py` |
| **不可复制**：所有 Cordis `Service`/slot/`@Remote`/`session-format-*`/`llm-*` adapter/`connection/*` | NO | — |

**C. 桌面 `desktop/src/`（DSH MIT）**

| 上游 | 处置 | 落点 |
| --- | --- | --- |
| `apps/desktop/src/preload-app.ts:79-84`（`__DSH_HOST_PATHS__`） | ADAPT（新增 preload 桥） | `desktop/src/preload.ts` |
| `ui-conversation/src/client/apply.ts:457-498`（路径分流→`@path`） | ADAPT（与 Web 共用） | `web/src/components/Composer.tsx` |
| 目录选择/静态页反代 | 目标已存在，**无需新增** | 已存在 |

**D. TUI `tui/`（Pi MIT / Cline Apache-2.0）**

| 上游 | 处置 | 落点 |
| --- | --- | --- |
| `@earendil-works/pi-tui` 的 `Image`/`terminal-image`/`getNativeClipboard` | **REUSE（已依赖，直接用）** | `tui/src/views/` 渲染层 |
| Pi `packages/coding-agent/src/utils/mime.ts`（116，零依赖 magic bytes） | **COPY** | `tui/src/lib/mime.ts` |
| Pi `utils/clipboard-image.ts`（240） | COPY/ADAPT | `tui/src/lib/clipboard-image.ts` |
| Pi `utils/clipboard-command.ts`（44）+ `utils/wsl.ts`（15） | COPY | 同上 |
| Cline `apps/cli/src/tui/utils/image-paste.ts`（285） | COPY（换掉 `PasteEvent` 类型） | `tui/src/lib/image-paste.ts` |
| Cline `apps/cli/src/utils/image-attachments.ts`（58） | COPY（换 1 个 import） | 同上 |
| Pi `interactive-mode.ts:3030-3076` / oh-my-pi `input-controller.ts:2007-2035` 的 chip 交互 | PORT DESIGN | `tui/src/app.ts` 键位钩子 |
| **不可复制**：oh-my-pi `@oh-my-pi/pi-tui` 整包（绑 omp 运行时）、Cline `input-bar.tsx`/`use-prompt-input-controller.ts`（OpenTUI/React）、codex 全部 Rust | NO | — |

**E. Python CLI `src/agent_harness/cli.py`**：四个上游均非 Python（Aider 是旁证，未核实）。
最小形态：`--image <path>`（`action="append"`）→ 读字节 → magic bytes 探测 MIME → 上传附件 →
事件只放 `artifact_id`。MIME 探测按 Pi `mime.ts` 的签名表对译为标准库实现（不引新依赖）。

---

## 8. 合并后的三端方案（用户裁决版）

1. **服务端先行（唯一 Contract，三端共用）**：① `ArtifactStore` 从 UTF-8 文本扩为 bytes + mime
   （内容寻址 sha256，Local 先行、MinIO 同步）；② 新增流式上传接缝（DSH
   `uploadFileBinary` 模式，`requestBody:'streaming'` 等价物）与受控读取端点（证明附件被本 Session
   引用才返回，DSH `ATTACHMENT_NOT_REFERENCED` 语义）；③ `user/message` 事件增加 image/file 引用块
   （content 仍兼容 str 消费方）；④ derive/Context 侧按**当前请求模型能力**物化（支持→image part；
   不支持→文本占位，Pi/DSH 双先例）。
2. **Web**：COPY/ADAPT §7.3-B 的 DSH 组件与 hook；图片走**流式上传**（避开 1 MiB body 上限，
   不采用 DSH 的 300 MiB 放宽）。
3. **桌面**：复用同一 Web 组件；按 §7.3-C 新增 `__DSH_HOST_PATHS__` 等价 preload 桥，实现
   「有真实路径的文件→`@path` 引用、粘贴字节→上传」分流。该桥是 preload 边界的一次小扩展，
   需在票面明确（`webUtils` 仅对用户已选/拖入的 File 返回路径，不构成任意 fs 读取）。
4. **TUI**：REUSE 既有 pi-tui（已依赖）做渲染；COPY Pi/Cline 的 mime/clipboard/image-paste 纯函数；
   chip 交互 PORT DESIGN。**Windows 只承诺文本占位 + 原图路径，不承诺缩略图**；键位 `Alt+V`，
   WSL 走 `powershell.exe`。
5. **CLI**：`--image` 参数 + 服务端上传。
6. **降级与门禁**：入口拦截（无 vision 模型禁用上传/报错）+ 请求前占位符（双保险）；per-model
   缩放档；图片 token 计费近似入预算。

## 9. 第二轮新增约束与风险

- **1 MiB body 上限**：决定图片走流式上传而非 base64-in-prompt（§7.1 冲突条）。
- **Windows Terminal 无图片渲染**：pi-tui 在 `WT_SESSION` 下返回 `images: null`；不得承诺缩略图。
- **preload 边界扩展**：`__DSH_HOST_PATHS__` 桥是新的渲染层能力面，需独立票 + 安全审查。
- **框架版本**：DSH 前端 React 18.2 vs 目标 React 19.2.8——所涉组件只用通用 hook 与
  `createPortal`，不阻碍复制；成本在 UI 原语（图标/lightbox/CSS）与 i18n。
- **归属义务**：所有 COPY/ADAPT 文件按既有惯例加文件头（上游文件 + commit + MIT/Apache-2.0）
  并更新 `THIRD_PARTY_NOTICES.md`（`desktop/` 已有先例；`web/`、`tui/` 需新建对应 notices）。
- **待核实（第二轮）**：DSH 官方文档站本环境不可访问（以仓库内 `docs/` 源文件双源替代）；
  Cline JSONL 是否内联 data URL 未确认；Aider 源码本轮未核实（仅旁证）。
