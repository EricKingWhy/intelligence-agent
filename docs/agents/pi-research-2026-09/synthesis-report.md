# Pi 调研汇总报告（A1/A2/B 三路 subagent + 主 agent 跨源交叉验证）

- **日期**：2026-09-30。**上游实测基準**：`D:\reference\pi` @ `1b347794e2a630e4359f2584f4eea388145d0ddf`（v0.99.1）；笔记 `D:\reference\dg-ai-notes` 基于 **v0.80.2**（系统性版本漂移，见 §6）。
- **子报告**：`a1-chapters-1-5.md`（29✅/15⚠️/0❓）、`a2-chapters-6-10.md`（38✅/9⚠️/2❓）、`b-consumption-map.md`（消费面普查）。合计 67✅/24⚠️/2❓——**零捏造**，全部 ⚠ 为版本漂移或简化表述。
- **入库**：2026-09-30 经用户批准入库 `docs/agents/pi-research-2026-09/`（四份同内容；后续票 #447/#448/#449 已开）。
- **跨源来源（本次全部实测抓取）**：
  - Manus《Context Engineering for AI Agents》：https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
  - Anthropic《Effective Context Engineering》：https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
  - Anthropic《Building Effective Agents》：https://www.anthropic.com/engineering/building-effective-agents
  - Cline Checkpoints 文档：https://docs.cline.bot/features/checkpoints
  - OpenHands Context Condensation 博客：https://www.openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents（slug 原文即含拼写）+ OpenHands 论文/SDK 论文（arXiv 2511.03690，经检索）
  - 本仓库实测：`compactor.py` / `runtime.py` / `fork.py` 等 file:line 见正文。

---

## 1. 总结论（三条）

1. **B 的三个深读问题全部有了跨源答案，且两个不需要动代码**：会话树（Q①）——ADR-0017 的否决被跨源证据**加强**（Cline 明确选截断、其余各家根本无会话树）；压缩（Q②）——我们的 8-section 结构化摘要 + 程序化抽取 + plan 交叉校验**已达跨源收敛的同一代甚至更严**，Pi/OpenHands 反而是纯 LLM 摘要；扩展钩子（Q③）——确有差距（我们只有装配期注入），但属 08 spec 下一版的架构级决策，且我们的权限已内建于 ToolExecutor，**不能**照抄 Pi 的钩子补权限路径。
2. **跨源交叉验证的核心价值是印证而非纠偏**：我们 9 项关键设计（loop 判据、错误即消息、ref 化截断、append-only+投影、权限=runtime 等）与 Pi/Anthropic/Manus/OpenHands 逐条同构；唯一「别人有我们无」的实质项就是 Q③ 的运行期事件缝。
3. **引用纪律被证明必要**：笔记零捏造但 24 处漂移全部源于 v0.80.2→v0.99.1；A1/A2 卡片的所有 file:line 已换算为 HEAD `1b34779` 实测，可直接引用。

---

## 2. Q① 会话树 / Fork / 原地探索 —— ADR-0017 否决维持，不立 ticket

| 维度 | Pi（HEAD 实测） | 我们（实测） | 跨源 |
| --- | --- | --- | --- |
| 存储形态 | 单 JSONL 内 id/parentId 树 + byId + leafId 单指针（session-manager.ts:1191-1196, 1579-1581） | **file-per-lineage**（ADR-0017 决策 1 明确否决 tree-in-file）；`fork.py:162` seed=前缀逐字复制重编 seq + tail 摘要 | Cline：无会话分支，恢复=**删除其后消息**（truncate/replay 而非 branch）+ shadow git 只管文件快照（docs.cline.bot 三 Restore 选项中两个删消息）；Anthropic/Manus/OpenHands 均无会话树概念 |
| 放弃分支的信息 | branchWithSummary() 追加 BranchSummaryEntry（parentId=分叉点，session-manager.ts:1600-1622）+ 固定 preamble "The user explored a different conversation branch before returning here." | fork tail 摘要（同构，fork.py:162） | Manus 反向论点："Erasing failure removes evidence"——保留错误路径是特性不是垃圾 |
| HEAD 新增 | **ContextEditEntry**：append-only 地修饰早前 entry 的投影（替换内容/整体隐藏，原始 entry 不动；白名单限 user/assistant/toolResult/custom 四种消息） | 无对应 | 映射我们「完整保存 ≠ 完整注入」；对敏感信息事后隐去有价值 |

**判定**：① ADR-0017 否决理由**维持且被加强**——树状对话分支作为存储形态业界无第二例；② 我们的 fork+lineage 只读视图+尾摘要已覆盖 Pi 树 UX 的等价消费面；③ 可移植增量只有 ContextEditEntry（投影层纠错），记 backlog 备忘，不立 ticket。可选文档动作：ADR-0017 补一段跨源注记（Cline 截断模式、Manus 保留错误证据、ContextEditEntry 备忘）。

---

## 3. Q② 压缩 —— 我们已达跨源收敛，剩余两个检查项

| 维度 | Pi（HEAD 实测） | 我们（实测） | 跨源 |
| --- | --- | --- | --- |
| 触发 | threshold（contextWindow−reserveTokens）+ overflow 应急；reason 枚举 manual/threshold/overflow | 比例制 0.70 + hard guard，三档 fallback（ADR-0007） | OpenHands：按 size 阈值触发以**摊销 prompt-cache 重建成本**（与我们 W-31.4 KV-cache 工作同向） |
| 切点/配对 | 合法切点白名单：user/assistant 可切、**toolResult 永不切**；split turn → 第二份轻量前缀摘要 | 06 §5 配对保持已落 | 三源一致（append-only 历史不动：Pi CompactionEntry × OpenHands 持久 EventLog × 我们 pruner.py「一字不动」） |
| 摘要结构 | 6-section（Goal/Constraints/Progress/Key Decisions/Next Steps/Critical Context）+ UPDATE 增量 prompt + 文件账目 `<read-files>/<modified-files>` **跨压缩累积** | **8-section 已存在**（compactor.py:33-42）：原始目标与用户约束/保护事实表/已完成工作与关键决策/**失败方案**/当前进行中状态/Next Step/精确标识清单/**文件清单**；section 0,1,6,7 **程序化正则抽取**（:45-57），模型只填 2:6；plan section 与 derive_plan 交叉校验（:253）；previous-summary 输入已有（:451-453） | OpenHands 摘要保留 goals/progress/todo + critical files & failing tests——字段集为我们 section 的子集；**我们独有**：「失败方案」section 正是 Manus "keep the wrong stuff in" 的唯一显式实现；程序化抽取比 Pi/OpenHands 纯 LLM 生成更确定 |
| 估算 | chars/4 保守启发式（A2 警告：中文低估） | 0.70 比例制；W-29/#416 已有真实 usage 计数链路 | Anthropic："先最大化召回再调精确"，并警告激进压缩丢关键上下文 |
| 实测背书 | — | — | OpenHands：54% vs 53%（SWE-bench Verified 子集，零性能损失）、每轮成本减半、二次方→线性 |

**判定**：不立增强 ticket。两个检查项去向（2026-09-30 代码级取证后更新）：① compaction 触发计数确认为 **tiktoken 估算**（`builder.py:336-405`、`tokens.py:1-14`），真实 usage 只进账目（`runtime.py:174-206`）——已开票 **#448**（偏差实测 + 安全边际数值推导）；② 跨压缩文件清单累积**已实现且有专用回归测试**（`compactor.py:448-463`、`test_compaction_bracket.py:405`）——无需开票。另：压缩参数已与 Pi 同代收敛（0.70/0.85 + reserve max(15%,16k) + keep recent 20k，`test_compaction_bracket.py:460` 参数融合声明）。

---

## 4. Q③ 扩展运行期钩子 —— 唯一实质差距，建议立项讨论而非直接动手

- **Pi（HEAD 实测）**：两管道判据「Agent 要不要读返回值」——决策钩子 await+读返回值（block/cancel/transform），通知钩子同步广播不收集；**fail-closed 例外显式化**（emitToolCall 是唯一不包 try-catch 的派发，扩展抛错即 block，runner.ts:1233-1251）；扩展拿 ReadonlySessionManager 只读门面、写走显式 action；context 钩子可整表改写投影（structuredClone 隔离，不回写 SessionEvent）。
- **我们（实测）**：CapabilityWiring 仅**装配期**注入（ContributesTools + 7 能力，wiring.py:632）；权限/审批**已是** ToolExecutor 必经路径（04 §8 落地）。
- **跨源**：Anthropic《Building Effective Agents》——"Agents can then pause for human feedback at checkpoints or when encountering blockers" + max-iterations 停止条件（与我们 02 §5.1 local fuse 同构）；Manus——**"mask, don't remove"**：运行期行为调制靠 logit 掩码、不动态增删工具表，理由=KV-cache 前缀稳定 + 防已引用工具消失导致 schema 违规/幻觉；Anthropic context-eng——工具集取 "smallest set of high-signal tokens"，bloated tool set 是常见失败。

**判定与边界**：
1. 值得补的是**运行期只读事件缝**（Capability 订阅通知型事件，fire-and-forget，不变量 21 友好），不是用钩子补权限——我们的权限在 Executor 必经，Pi 用钩子做权限是因为它 core 无权限；若把决策钩子做成权限的第二条路径，违反不变量 7（单一执行路径）/11（权限是 runtime 边界）。
2. **直接启示**：不运行期动态增删工具集（我们 7 能力装配期定死恰好符合 Manus 原则，应把这一条写成设计理由）。
3. 动作建议：以「08 spec 下一版 PORT DESIGN 候选」立项讨论（方案依据块引用本报告 §4 + 13§2:35），范围先限通知型订阅；决策钩子（tool 拦截/prompt 干预）若做，单独立项并先过 §7 不变量审查。

---

## 5. 跨源交叉验证明细（论点 × 来源 × 结论）

| # | 论点 | Pi | 我们 | 其他来源 | 结论 |
| --- | --- | --- | --- | --- | --- |
| 1 | Loop 判据看 tool_calls 有无、不看 stop 字符串 | agent-loop.ts:259-278 | runtime.py:947,1633 | Anthropic："LLMs using tools based on environmental feedback in a loop" + max-iterations | 三源一致 |
| 2 | 错误即消息 + 具体化让模型自纠 | ch5 isError ToolResultMessage | ToolResult ok/message/error_code | Anthropic ACI（poka-yoke、absolute filepath 案例）；Manus 保留错误证据 | 四源一致 |
| 3 | 截断诚实 + 大内容 ref 化 | "[Showing lines x-y of z. Full output: /tmp/…]" | 不变量 15 / ArtifactStore | Manus 只在可还原时压缩；Anthropic just-in-time identifiers | 四源一致 |
| 4 | 压缩 = 近窗 + 结构化摘要 + 历史不动 | ch9 | pruner.py + 8-section | OpenHands（54% vs 53%）；Anthropic（清 tool result 是最安全轻压缩） | 三源一致 |
| 5 | KV-cache：append-only + 稳定前缀 | HEAD transcript 化（system prompt 进首条 system message，字节稳定） | W-31.4/#416 已修 recency 锚 | Manus：单 token 差异杀缓存、时间戳勿入 prompt | 印证 W-31.4 方向 |
| 6 | 会话分支 | 树 + leafId | ADR-0017 file-per-lineage | Cline=截断；其余无 | 否决维持 |
| 7 | 权限 = runtime 不是 prompt | beforeToolCall 钩子位（可选） | 04 §8 Executor 必经（更强） | Anthropic：pause for human feedback | 一致；我们比 Pi 强 |
| 8 | 子代理蒸馏回传 1–2K | — | #415（1500 字符头 + ref） | Anthropic："1,000–2,000 tokens" | 同代 |
| 9 | 运行期事件缝 | 两管道 + fail-closed | 仅装配期 | Manus mask-don't-remove（反面：别动工具表） | **唯一实质差距** |

---

## 6. 版本漂移与引用纪律（引用笔记时必读）

- 笔记基 **v0.80.2**，上游 HEAD **v0.99.1**（`1b34779`）；零捏造、24 处漂移全为版本演进或简化。
- HEAD 重大变化：① transcript 化（SystemMessage 入 Message 联合，system prompt/工具声明由首条 system message 携带）；② `shouldStopAfterTurn` → `finishTurn({action})` + 新 `prepareRequest`；③ **length 截断消息的 toolCall 一律判错**（防流式 JSON salvage 产出"看似合法实则残缺"参数）；④ agent-core 会话层重构（Storage/JsonlStorage + seq/lane，EntryType 收敛 4 种；coding-agent Entry 11 种，新增 usage/context_edit）；⑤ 13 packages（pi-mcp/durable/codemode/session-backends 等）——笔记「Pi 不做 MCP」已过时；⑥ 无 orchestrator 包。
- A1/A2 卡片 file:line 全部为 HEAD 实测，可直接引用；笔记行号一律**不要**直接引用。

---

## 7. 后续动作（2026-09-30 用户决策后更新；修复型工作一律先开票）

- **已开票（先开票后修，正文含修复依据）**：
  1. **#447（P2 · 设计评估）T1 · Capability 运行期事件缝评估**——08 spec 下一版 PORT DESIGN 候选，范围先限通知型订阅；产出评估报告 + go/no-go，不实施。
  2. **#448（P3 · 检查项）compaction 触发计数的 token 来源确认**（tiktoken 估算 vs 真实 usage）——含偏差实测与安全边际数值推导；确认暴露才按最小修复方向修。
  3. **#449（P3 · 检查项）length 截断响应的 toolCalls 暴露面确认与判错防线**——先实测（含 langchain-core `invalid_tool_calls` 落点）；确认暴露才按 Pi HEAD 语义判错。
- **已核实、无需开票（原 T2b）**：跨压缩文件清单累积**已实现且有专用回归测试**——实现 `compactor.py:448-463`（上一版摘要的 user messages / identifiers / file paths / protected facts 经 `add_once` 并入）；测试 `tests/context/test_compaction_bracket.py:405`（`test_second_compaction_merges_previous_summary_after_reload`：两次压缩后摘要单份、原始用户消息与精确标识跨压缩存活、source range 扩张、原始事件逐字不动）。
- **已核实、无需开票（本轮新增核查）**：① 压缩参数与 Pi 同代收敛（见 §3）；② 我们无 Anthropic 原生 provider（`anthropic` 在 `src/` 零命中），Pi「prompt cache 三打点」不适用；③ steering / interrupt 已有实现（`session/interrupt.py`、`session/queue.py`），非差距。
- **文档动作（用户已批准）**：四份报告入库 `docs/agents/pi-research-2026-09/`；`reference-sources.md` 补版本漂移注记；ADR-0017 补跨源注记（§2）。分支 `docs/pi-research-import`。
- **长期残余**：`SPEC_ROOT/13` §7 旧 Pi 链接（冻结规格，仅报告）。
