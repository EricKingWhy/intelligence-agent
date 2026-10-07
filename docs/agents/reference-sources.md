# 参考源清单（Reuse First 的第一站）

> 承接 `AGENTS.md` §6 / §6.1：设计 / 选型类工作动手前先来本清单查对应领域，看成熟实现怎么做。
> 本文件只回答「**去哪里查、看什么**」；复用**判定**（REUSE / ADAPT / PORT DESIGN / BUILD /
> DEFER）唯一维护在 `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md`，架构边界以 `AGENTS.md` §7 为准。
> 建立于 2026-09-30。来源条目必须可复现；来源失效 / 改名时更新本文件（近期一例：Pi 由
> `badlogic/pi-mono` 更名为 `earendil-works/pi`，旧链接 GitHub 自动重定向，仍可用）。

## 1. 本地浅克隆（代码来源优先用它们 grep 对照）

| 来源 | 本地路径（每台机器手动准备，**不进任何仓库**） | 上游 |
| --- | --- | --- |
| Pi（即更名前的 badlogic/pi-mono） | `D:\reference\pi` | `https://github.com/earendil-works/pi` |
| dg-ai-notes（Pi 源码精读笔记，第三方） | `D:\reference\dg-ai-notes` | `https://github.com/buchidonggua/dg-ai-notes` |
| DeepSeek Harness（DSH） | `D:\reference\deepseek-harness` | `https://github.com/deepseek-ai/deepseek-harness` |
| oh-my-pi | `D:\reference\oh-my-pi` | `https://github.com/can1357/oh-my-pi` |
| Codex CLI | `D:\reference\codex` | `https://github.com/openai/codex` |

重建命令（新机器一次即可）：

```bash
git clone --depth 1 https://github.com/earendil-works/pi.git D:\reference\pi
git clone --depth 1 https://github.com/buchidonggua/dg-ai-notes.git D:\reference\dg-ai-notes
git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness.git D:\reference\deepseek-harness
git clone --depth 1 https://github.com/can1357/oh-my-pi.git D:\reference\oh-my-pi
git clone --depth 1 https://github.com/openai/codex.git D:\reference\codex
```

（`--depth 1` 浅克隆取的是**当时**的 HEAD；上表后三个克隆在 2026-10-07 实测分别为
`5badb150` / `1c0993c3` / `7f89227`。Pi 同日实测 `1b347794`。引用时按下方纪律记 commit。）

**引用纪律**：

- 代码来源给出克隆内的 `file:line`，并在方案依据里记录所读 commit（`git -C <路径> rev-parse HEAD`）——
  上游持续变化，不带 commit 的引用无法复核；
- 第三方笔记 / 文章是二手来源：**先对照上游源码或官方文档核实再引用**，并记下核实结果。
  （dg-ai-notes 的 TypeScript 章节对上游真实实现；Python 章节是作者转写，两版不一致时以上游 / TS 为准。）
- **版本漂移实测（2026-09-30，Pi 调研批次）**：dg-ai-notes 基于 **v0.80.2**，上游 HEAD 已到 **v0.99.1**
  （实测 `1b34779`）——逐章核对 67 处吻合 / 24 处漂移 / 2 处存疑、零捏造；笔记的行号与「包清单 / 不做清单」
  类章节可能整体过时（如「Pi 不做 MCP」已不成立），引用行号一律以上游实测为准。核对明细：
  `docs/agents/pi-research-2026-09/`。
- 文章类来源记链接 + 读取日期；实质复制 / Port 前查 License 并保留来源（`AGENTS.md` §6 末段）。

## 2. 领域 → 来源

### Agent Harness / Agent Loop / 会话 / 工具运行时

| 来源 | 是什么 | 看什么 |
| --- | --- | --- |
| Pi（本地克隆） | 生产级 agent toolkit（TypeScript）；Reuse Matrix §2 的主参考 | `packages/agent`（agent runtime / tool calling / state）、`packages/durable`（会话持久化）、`packages/session-backends`、`packages/ai`（多 provider 抽象）、`packages/coding-agent`（CLI 与容器化文档） |
| dg-ai-notes（本地克隆） | 上述 Pi 的逐章精读，10 章 + 配图，TS / Python 双版 | `pi-agent/pi_source_dive/typescript/` 各章：Agent Loop、工具系统、消息系统、事件驱动、上下文工程 / 压缩、会话管理（存储恢复与分叉） |
| Anthropic 工程博客 | 设计模式（文章，非代码） | [Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)（workflow vs agent、模式选型）、[Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)（工具接口设计）、[Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)（上下文策略） |
| Cline（`cline/cline`） | 生产编码 agent（IDE / CLI） | 计划-执行分离、checkpoint 与回滚、MCP 客户端实践 |
| OpenHands（`OpenHands/OpenHands`） | 开源软件工程 agent | 沙箱化执行（Docker runtime）、事件流设计、基准（SWE-bench 类）工程化 |
| Manus 工程博客 | 设计模式（文章，非代码） | [Context Engineering for AI Agents](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)：KV-cache 友好、工具结果卸载、注意力管理 |

（DeepSeek Harness 的核查链接已在 Reuse Matrix §3 / §7，此处不重复。）

### Coding Agent 工具链 / 执行期检查

| 来源 | 是什么 | 看什么 |
| --- | --- | --- |
| [ZCode Hooks 官方文档](https://zcode.z.ai/en/docs/hooks) | ZCode CLI 事件钩子规范 | `PreToolUse` 拒绝、`PostToolUse` 工具结果、`Stop`、Hook 启用和配置作用域；确认当前版本的项目配置是否执行 |
| [腾讯云 CodeBuddy Code Hooks](https://cloud.tencent.com/document/product/1831/137030) | WorkBuddy 随附 CodeBuddy Code CLI 官方 Hook 规范；文档标注 Beta | `PreToolUse`／`PostToolUse`／`Stop`、转录路径、权限行为、配置作用域、错误与超时；区分 CLI 能力与 WorkBuddy 桌面实际接线 |
| [CodeBuddy CLI 设置](https://www.workbuddy.ai/docs/cli/settings)；[WorkBuddy v2.48.0 配置分离说明](https://www.workbuddy.ai/docs/cli/release-notes/v2.48.0) | CLI 分层配置与 WorkBuddy 独立 `.workbuddy/` 路径的一手依据 | CLI 通用 `.codebuddy/` 配置和 WorkBuddy 桌面配置不能互相推定；核实具体应用的运行器与 Hook 接线 |

以上是一手工具文档，不表示本项目已启用或验收。调研结论与边界见 `docs/agents/agent-tool-read-gates-research-2026-10.md`。

### Memory

| 来源 | 是什么 | 看什么 |
| --- | --- | --- |
| LangMem（`langchain-ai/langmem`） | LangChain 官方记忆 SDK；本仓默认 Provider 候选（不变量 17，判定见矩阵 §4） | 记忆抽取 / 更新 / 检索的工具化封装 |
| Mem0（`mem0ai/mem0`） | 生产级「记忆层」实现 | 增量抽取管线、向量 + 图混合存储 |
| Graphiti（`getzep/graphiti`） | 时序知识图谱 | bi-temporal 建模、事实失效 / 矛盾处理 |
| Letta（`letta-ai/letta`，原 MemGPT） | 有状态 agent 平台 | core / archival memory 分页、agent 自我编辑记忆 |
| AWS Prescriptive Guidance | 官方架构模式文档 | [Transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)：双写原子性、重复投递与幂等消费者 |
| Milvus 官方文档 | 向量索引适配器行为依据 | [Upsert Entities](https://milvus.io/docs/upsert-entities.md) 的主键插入/更新语义；[Consistency](https://milvus.io/docs/consistency.md) 的可见性级别 |
| SQLite 官方文档 | 权威本地存储与备份语义 | [Online Backup API](https://www.sqlite.org/backup.html)：一致快照及在线备份边界 |

### WebSocket / ASGI transport

| Source | What it is | What to check |
|---|---|---|
| [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455.html) | IETF WebSocket standard | §§5.2/5.4 payload length and fragmentation; §7.4.1 close code 1009; §10.4 resource limits. |
| [ASGI HTTP+WebSocket specification](https://asgi.readthedocs.io/en/latest/specs/www.html) | Application/server protocol | `websocket.receive` delivers a text or binary message to the app; the ASGI app boundary does not expose wire-frame fragments. |
| [Uvicorn settings](https://www.uvicorn.org/settings/) | ASGI server configuration | `--ws-max-size` and `--ws` backend applicability; compare transport-level bounds with app-level checks. |
| [websockets memory guide](https://websockets.readthedocs.io/en/stable/topics/memory.html) | WebSocket implementation behavior | `max_size` / `max_queue` bound queued message memory; implementation-specific message-size handling. |

### Workbench 桌面 / 宿主 / 在场 / 发布（2026-10-04 补强批次）

| 来源 | 是什么 | 看什么 |
| --- | --- | --- |
| [Microsoft Windows Sandbox overview](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-overview) | 官方文档（2026-10-04 读取） | 显式启用的可选功能与版本前提（Home 不支持）、"doesn't allow multiple instances to run simultaneously"、网络默认开启且可用配置文件关闭 → W-19 显式选择与缺依赖提示 |
| [ReFS overview（block cloning）](https://learn.microsoft.com/en-us/windows-server/storage/refs/refs-overview) | 官方文档（2026-10-04 读取） | block cloning / file-level snapshots 为 ReFS 独有、NTFS 不支持 → #527 工作区快照 / CoW 判定（用户目录通常 NTFS ⇒ DEFER 至验证） |
| [GitHub Codespaces idle timeout](https://docs.github.com/en/codespaces/setting-your-user-preferences/setting-your-timeout-period-for-github-codespaces) | 官方文档（2026-10-04 读取） | 不活动一段时间后停止；个人交互 / 终端活动重置 idle → W-22 在场/缺席语义类比（resume 契约仍以已批准 Spec 变更为准） |
| [Codex app 介绍](https://openai.com/index/introducing-the-codex-app/) | 官方博客（2026-10-04 读取） | threads 按项目组织、隔离代码副本、后台执行汇入 review queue、线程内 diff 审阅 → W-23 创建入口与队列、W-09 审阅分离 |
| [Cline CLI README](https://github.com/cline/cline/blob/main/apps/cli/README.md) | 上游仓库（2026-10-04 读取） | OpenTUI 流式 TUI、plan/act 切换、markdown / 语法高亮 diff / 可滚动聊天 → W-28 / W-17 TUI 渲染形态佐证 |
| [FastAPI handling-errors](https://fastapi.tiangolo.com/tutorial/handling-errors/) | 官方文档（2026-10-04 读取） | HTTPException detail 接受任意 JSON-able 值、自定义 handler 定义错误体 → #596 409 detail 结构化 |

**待核实候选（2026-10-04 WebFetch 超时 / 404 未取到正文；按 §6.1 核实完成前不得作为方案依据引用，不得编造读取日期）**：Jupyter Server security（loopback 默认 + token 鉴权）、Docker Desktop architecture（单后端附着）、Electron `app.requestSingleInstanceLock`、systemd `journalctl --verify` / SEQNUM、`git fsck` 连通性校验、RFC 7807 problem details、Podman Desktop Windows 前提、Devin / Cursor background agent 入口。`pg_waldump` 同日实测**不适用**（文档明示无 gap / 损坏段处理描述），不得引用。

### 原子文件写 / 跨平台文件锁（2026-10-04 新增，i660 调研）

| 来源 | 是什么 | 看什么 |
| --- | --- | --- |
| POSIX man pages（man7.org：`rename(2)` / `flock(2)`） | 内核语义权威 | rename 只查目录权限（不查目标文件位/句柄）；flock 为 advisory、进程退出自动释放 |
| `filelock`（tox-dev/py-filelock） | Python 跨平台文件锁事实标准 | lockfile + flock/msvcrt 平台分支、超时/计数语义 |
| 本仓 `src/agent_harness/instance_lock.py` | 已有跨平台 advisory 锁协议（#150） | `_take_os_lock` 双平台分支、advisory 边界声明——仓内先例 |

（2026-10-04 i660 调研首查时本领域缺失，按本文件 §3.1 规则补录；外部来源当时因宿主权限未实读核实，判定与核实状态见 `docs/research/i660-posix-guard-research.md` §4。）

### 多模态图片输入 / 附件（2026-10-07 新增，i821 调研）

| 来源 | 是什么 | 看什么 |
| --- | --- | --- |
| DSH（本地克隆，MIT） | 与目标项目**同构**的附件域（事件存引用、字节外置）；Web / 桌面复用主源 | `packages/attachment`（`AttachmentId` 不透明 id、`ImageAttachmentRef`、`ImageAttachmentLimits`）、`attachment-local`（staging→fsync→原子发布、`normalization.ts` 归一化、文件名消毒含 Windows 保留名）、`docs/subsystems/attachment.md`（persist-before-event 不变量逐字）、`ui-attachment`（`drop-events` / `DropOverlay`）、`ui-conversation`（`historical-images` 受控读取）、`apps/desktop/src/preload-app.ts`（`__DSH_HOST_PATHS__` 路径桥） |
| Pi（本地克隆，MIT） | CLI / TUI 复用主源；**降级文案的逐字来源** | `packages/ai/src/types.ts`（`ImageContent`）、`api/transform-messages.ts`（`downgradeUnsupportedImages` 的文本占位符）、`packages/tui`（终端图片协议层、`Image` 组件、`getNativeClipboard`） |
| oh-my-pi（本地克隆，MIT） | **内容寻址外置**先例（与本仓不变量 15 同构） | `session/blob-store.ts`（`blob:sha256:` 外置，注释明写 "externalizing large binary data (images) from session JSONL files"）、`prompt/attachment-chips.ts`（`[Image #N]` chip + pendingImages）、`input-controller.ts` 的路径粘贴 |
| Cline（`cline/cline`，Apache-2.0） | CLI 粘贴的平台分支与 data-url 构造 | `apps/cli` 的 `image-paste.ts`；其 OpenTUI / React 组件**不可复制** |
| Claude Code（闭源） | 交互语义参考（**非代码来源**） | `[Image #N]` chip、Windows 用 `Alt+V`（`Ctrl+V` 常被终端截获）、API 层自动降采样 1568px |
| Codex CLI（本地克隆） | 粘贴 / 路径双入口与缩放档 | `UserInput::LocalImage{path,detail}`、ResizeToFit 2048px、`view_image` 对非视觉模型拒绝、读取失败插文本占位符 |
| Provider 官方文档（2026-10-07 读取） | 请求载荷形状的权威 | OpenAI Chat Completions（`image_url` 同时接受 http(s) 与 `data:`，`detail` 档位）；**DeepSeek 已有视觉**（`deepseek-flash`；图片**只允许出现在 user 消息**，system/assistant → 400）；Anthropic image block（`source ∈ {base64,url,file}`，单图 ≤10MB）；Gemini（`inline_data` / `file_data`）；Qwen/DashScope 与 GLM 的 base64 支持与张数上限差异 |
| Web 聊天 UI：Open WebUI / LibreChat / LobeChat（克隆实证） | 「上传与消息解耦、消息只存引用、发送时物化」的共识 | Open WebUI 的 `convert_url_images_to_base64`（SSRF + 所有权校验）、LibreChat 的 `encodeAndFormatImages`（按 endpoint 输出原生块）、LobeChat 的非 vision 文本占位符与**故意不鉴权**的 `/f/{id}`（反面对照）；后端为权威并把配置下发前端 |
| LangChain core content blocks | 标准内容块形状 | `langchain_core/messages/content.py` 的 `ImageContentBlock`；请求方向的「标准块 → provider 载荷」翻译在 partner 集成包内，**core 不代做** |

**⚠ 已知硬冲突（不得直接照搬 DSH 的上传形态）**：DSH 把图片 base64 随 JSON prompt 提交，默认 body 上限
**300 MiB** 且启动断言 `maxRequestBodyBytes ≥ maxMessageImageBytes*4/3 + 1 MiB`；本仓
`BODY_MAX_BYTES = 1 MiB`。结论与取舍见 `docs/PRD_MULTIMODAL_IMAGE_INPUT.md`（选流式上传 + prompt 只带 ref）。

**补录说明（§3.1 偏差披露）**：本领域在 i821 调研开始时尚未在清单中，属**事后补录**——首轮调研
按用户指定上游直接开展，未先补本清单。逐文件复制清单、引用 commit 与核实状态见
`docs/research/2026-10-07-multimodal-image-input-research.md`；票面见
`docs/tickets/multimodal-2026-10-07/`（#821–#830）。

## 3. 怎么用（与流程的挂钩）

1. 出现新领域 / 新来源：**先补本清单再调研**——"去哪查"只在这里维护一处；
2. 调研与「方案依据」块的字段要求：`AGENTS.md` §6.1 + `docs/SDD_WORKFLOW_PROTOCOL.md` §1.3；
3. review 侧检查项：`docs/agents/review-debug-playbook.md` Independent Review；
4. 本清单**不记判定结论**（哪个能力用 REUSE 还是 PORT DESIGN）——判定只在 Reuse Matrix 维护，
   两处都写必然漂移。
