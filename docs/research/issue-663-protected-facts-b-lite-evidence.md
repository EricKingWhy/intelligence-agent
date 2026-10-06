# #663 B-lite：成熟产品机制与可复用实现证据

调研日期：2026-10-06。范围：一次性会话后约束提取、原文来源核验、既有 protected facts 复用。ZCode 指 Z.ai ZCode。此笔记是实现调研，不修改 #663 票面。

## 结论

B-lite 可借鉴成熟产品“让模型挑选值得长期保留的内容”的判断方式，同时把持久化约束收窄为：主 run 成功结束后至多做一次结构化候选提取；输入只取原始用户消息；每个候选必须带稳定的源事件 ID/序号与原文片段；运行时逐条核验来源后调用本仓既有 protected-fact 登记路径。无候选、输出不合 schema、来源不匹配或抽取失败时跳过写入，不影响已完成的主 run。提取器不负责更正/冲突解决，不覆盖旧事实，也不再加 consolidation、向量库或第二套记忆存储。

这是把 Claude Code / ZCode 的筛选机制与 oh-my-pi 的成功轮次钩子、用户来源过滤结合后的最小方案；成熟产品没有提供可直接替换本仓 API 的 protected-fact 实现。

## 一手产品证据

| 来源 | 已核实机制 | 对 B-lite 的借鉴 / 边界 |
|---|---|---|
| [Claude Code memory](https://code.claude.com/docs/en/memory)（2026-10-06） | 模型按未来会话价值选择偏好、纠正、项目上下文等；不是每轮都保存。auto memory 是可编辑 Markdown，前 200 行或 25KB 自动加载。文档明确说明记忆是上下文指导，不是强制配置。 | 支持“模型筛选而非把每句话入库”；不能把 prompt 当来源或写权限校验。Markdown 存储不是 protected fact 的替代品。 |
| [Z.ai ZCode memory](https://zcode.z.ai/en/docs/memory)（2026-10-06） | 开启后，在每个成功完成的对话轮次后台判断偏好、纠正、目标、约束和外部引用是否值得长期保留；用户可直接要求 remember/forget。默认关闭，文档提示会额外消耗模型请求/token。 | 最直接支持“成功轮次后一次后台判断”的触发方式；明确预算/故障边界，且不把后台抽取混入用户可见回答。 |
| [DeepSeek Harness memory guide](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/user/guide/mcp-memory.md)（本地 D:/reference/deepseek-harness，commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc） | shipped composition 不含 memory server；三种 MCP 配置默认关闭。随附指引只建议“用户要求记住时”调用 memory write tool。DSH 将 memory 接作可选外部能力。 | 支持 optional capability 故障不拖垮主 Agent；其显式 remember 策略比本次批准的自动筛选更保守，不能照搬为唯一触发条件。 |
| [Pi sessions](https://pi.dev/docs/latest/sessions)（2026-10-06；另见 D:/reference/pi commit 1b347794e2a630e4359f2584f4eea388145d0ddf 的 packages/coding-agent/docs/sessions.md:97-117） | session entries 以树保存，切分支不擦除原分支；模型只读活动分支；compaction 留存原始 entries，只压缩注入上下文。 | 借鉴持久化历史与模型上下文分离：候选应引用原事件 ID，而非以 compaction 摘要代替用户原文。Pi 本身不是语义事实抽取器。 |
| [OpenAI Agents API sessions](https://developers.openai.com/api/docs/guides/agents-api/sessions)（2026-10-06） | session 保存 agent 配置、conversation、工作；复用 session 可继续对话。空闲 session 收到输入开启新 turn，活动 turn 收到输入则 steer 当前 turn。 | 证明同 session 后续输入/steer 的基本语义；没有证明等待中的自定义 run 能暂停并以相同 run_id 恢复，也没有规定等待期的预算预留。 |

## 可移植源码（oh-my-pi）

上游：D:/reference/oh-my-pi，HEAD 1c0993c3d12e70042169a951663bb2702e2c0a9e（2026-10-04），根 LICENSE 为 MIT。已逐行核对：

上游固定源码链接：[Mnemopi retention](https://github.com/can1357/oh-my-pi/blob/1c0993c3d12e70042169a951663bb2702e2c0a9e/packages/coding-agent/src/mnemopi/state.ts#L533-L588)、[user-only transcript](https://github.com/can1357/oh-my-pi/blob/1c0993c3d12e70042169a951663bb2702e2c0a9e/packages/coding-agent/src/hindsight/content.ts#L274-L277)、[extract prompt](https://github.com/can1357/oh-my-pi/blob/1c0993c3d12e70042169a951663bb2702e2c0a9e/packages/mnemopi/src/core/extraction/prompts.ts#L1-L31)、[extract client](https://github.com/can1357/oh-my-pi/blob/1c0993c3d12e70042169a951663bb2702e2c0a9e/packages/mnemopi/src/core/extraction/client.ts#L134-L181)、[autolearn controller](https://github.com/can1357/oh-my-pi/blob/1c0993c3d12e70042169a951663bb2702e2c0a9e/packages/coding-agent/src/autolearn/controller.ts#L90-L151)、[MIT license](https://github.com/can1357/oh-my-pi/blob/1c0993c3d12e70042169a951663bb2702e2c0a9e/LICENSE)。

- packages/coding-agent/src/mnemopi/state.ts:533-588,610-625：订阅 agent_end；按 retainEveryNTurns 和已保留 user-turn cursor 仅处理未留存后缀；把完整对话保留为 episode，但单独将 prepareUserRetentionTranscript(messages) 的结果用于 fact extraction。
- packages/coding-agent/src/hindsight/content.ts:274-277：用户来源过滤就是 messages.filter(message => message.role === "user")。这是最值得适配的小设计；本仓还应保留 SessionEvent ID/序号并在写入前核对事件确为直接 USER_MESSAGE，单纯 role 标签不足以建立强来源保证。
- packages/mnemopi/src/core/extraction/prompts.ts:1-31 与 packages/mnemopi/src/core/extraction/client.ts:134-181：独立抽取器要求结构化 JSON，支持按消息索引标来源；但提示同时要求更正时提取旧值和新值，解析只做 JSON 数组解析并把条目强转类型，没有逐项 schema/事件来源验证。因此只借鉴结构化候选形式，不照搬自动冲突行为与宽松解析。
- packages/coding-agent/src/autolearn/controller.ts:90-151、autolearn/settings.ts:7-38：可选的停止后 capture 会跳过 aborted/短任务/plan/goal run；single-flight 防重叠并合并一个 pending capture；但该 experimental 功能及私有 capture 默认均关闭，capture 使用额外 tokens。适合借鉴“只处理成功边界、失败不打断主 run、避免重叠”的控制思路，不应直接移植其额外 agent turn/skill 生成功能。

代码策略：ADAPT 成功轮次触发与 user-role 输入过滤；PORT DESIGN 借用结构化候选与稳定来源思想；在本仓 REUSE 现有 protected-fact 校验/追加存储和工具处理器；不要移植 oh-my-pi 的 TypeScript runtime、Mnemopi 数据库/embedding/consolidation 栈。若实质复制 MIT 代码，随副本保留 MIT 版权与许可文本；一行过滤逻辑更适合按本仓 Python 风格重写并注明设计来源。

## 本仓代码复用边界（当前分支）

- `src/agent_harness/session/session.py:363-395` 的 `Session.register_protected_fact` 是现有 append/idempotency 写口；它调用 `build_protected_fact_data`。`src/agent_harness/session/derive.py:618-659` 校验 source event 存在、源序号匹配、约束值与用户原文匹配，再生成稳定 fact id。
- `src/agent_harness/tools/register_constraint.py:107-151` 的模型可调用工具还要求 active run 与本轮冻结的 `current_constraint_tool_context`，并要求候选是当前输入里的连续原文片段。因此，run 结束后若从多条旧消息抽取，不能直接调用这个 tool handler；应复用受校验的 Session 登记机制，并针对每个候选重新核验源事件。若需要共享模型工具与后台抽取写入规则，应收敛到同一个领域校验/登记路径，而非再造存储。

## 与本仓规格的契合

Vision §3 与 Session/Context 规格要求 append-only 事件、model-visible input 可追溯、完整历史不等于完整注入、Memory 是可替换 capability/context provider；#663 当前目标是复用 protected facts。故抽取只产生候选，写入边界由 runtime 验证并走现有注册路径；不能新增隐藏写入旁路。Memory V2 的 user/project 长期 scope 与 protected facts 不是同一存储契约；本票不应顺便扩展 scope 或引入新的 provider。ADR-0051 决定的用户澄清仍由既有交互流处理，后台抽取不替用户决定永久替换。

### 启动检查表

| 前置项 | 实际读取依据 | 状态 |
|---|---|---|
| Vision 相关原则 | SPEC_ROOT/00_PROJECT_VISION.md §3 | READY |
| 当前任务规格 | SPEC_ROOT/06_CONTEXT_ARTIFACT_MEMORY.md §§1、6–8；docs/adr/0051-protected-fact-conflict-user-input-resume.md | READY |
| Reuse 相关判定 | SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md §§2–6；docs/agents/reference-sources.md Memory / Agent Harness | READY |
| Phase 依据 | SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md Phase 5–6 | READY |
| 本任务触发细则 | docs/SDD_WORKFLOW_PROTOCOL.md §§1.3、8.9；纯调研，不写代码或操作 GitHub | READY |

本记录不改变现有 issue、标签、分支或验收标准。
