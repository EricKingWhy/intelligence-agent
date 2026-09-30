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

### Memory

| 来源 | 是什么 | 看什么 |
| --- | --- | --- |
| LangMem（`langchain-ai/langmem`） | LangChain 官方记忆 SDK；本仓默认 Provider 候选（不变量 17，判定见矩阵 §4） | 记忆抽取 / 更新 / 检索的工具化封装 |
| Mem0（`mem0ai/mem0`） | 生产级「记忆层」实现 | 增量抽取管线、向量 + 图混合存储 |
| Graphiti（`getzep/graphiti`） | 时序知识图谱 | bi-temporal 建模、事实失效 / 矛盾处理 |
| Letta（`letta-ai/letta`，原 MemGPT） | 有状态 agent 平台 | core / archival memory 分页、agent 自我编辑记忆 |

## 3. 怎么用（与流程的挂钩）

1. 出现新领域 / 新来源：**先补本清单再调研**——"去哪查"只在这里维护一处；
2. 调研与「方案依据」块的字段要求：`AGENTS.md` §6.1 + `docs/SDD_WORKFLOW_PROTOCOL.md` §1.3；
3. review 侧检查项：`docs/agents/review-debug-playbook.md` Independent Review；
4. 本清单**不记判定结论**（哪个能力用 REUSE 还是 PORT DESIGN）——判定只在 Reuse Matrix 维护，
   两处都写必然漂移。
