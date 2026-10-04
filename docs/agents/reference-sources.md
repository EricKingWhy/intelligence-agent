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

重建命令（新机器一次即可）：

```bash
git clone --depth 1 https://github.com/earendil-works/pi.git D:\reference\pi
git clone --depth 1 https://github.com/buchidonggua/dg-ai-notes.git D:\reference\dg-ai-notes
```

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

### 原子文件写 / 跨平台文件锁（2026-10-04 新增，i660 调研）

| 来源 | 是什么 | 看什么 |
| --- | --- | --- |
| POSIX man pages（man7.org：`rename(2)` / `flock(2)`） | 内核语义权威 | rename 只查目录权限（不查目标文件位/句柄）；flock 为 advisory、进程退出自动释放 |
| `filelock`（tox-dev/py-filelock） | Python 跨平台文件锁事实标准 | lockfile + flock/msvcrt 平台分支、超时/计数语义 |
| 本仓 `src/agent_harness/instance_lock.py` | 已有跨平台 advisory 锁协议（#150） | `_take_os_lock` 双平台分支、advisory 边界声明——仓内先例 |

（2026-10-04 i660 调研首查时本领域缺失，按本文件 §3.1 规则补录；外部来源当时因宿主权限未实读核实，判定与核实状态见 `docs/research/i660-posix-guard-research.md` §4。）

## 3. 怎么用（与流程的挂钩）

1. 出现新领域 / 新来源：**先补本清单再调研**——"去哪查"只在这里维护一处；
2. 调研与「方案依据」块的字段要求：`AGENTS.md` §6.1 + `docs/SDD_WORKFLOW_PROTOCOL.md` §1.3；
3. review 侧检查项：`docs/agents/review-debug-playbook.md` Independent Review；
4. 本清单**不记判定结论**（哪个能力用 REUSE 还是 PORT DESIGN）——判定只在 Reuse Matrix 维护，
   两处都写必然漂移。
