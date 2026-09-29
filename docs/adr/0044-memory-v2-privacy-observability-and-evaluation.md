# ADR-0044 — Memory V2 隐私观测与质量评测

- **Status**: Accepted（实现、验证与真实证据已随 PR #339 集成；GitHub issue #302 已关闭）
- **Date**: 2026-09-26
- **Deciders**: 用户（#302 范围、真实服务授权、LoCoMo 非商业内部评测边界）+ 本 Agent（实现方案）
- **Related**:
  - Issue **#302 / MEM-V2-6**，`docs/tickets/mem-v2-6-privacy-observability-quality-evaluation.md`
  - PRD §6.5、§8.1–§8.4
  - ADR-0018（Langfuse 旁路观测与评测）
  - ADR-0042、ADR-0043（Memory V2 信封、证据来源）
- **Refines**: ADR-0018 D6，仅限 Memory V2 挂载的真实运行；非 V2 trace 行为不变
- **Supersedes**: 无

---

## 1. Context

ADR-0018 的全局内容模式允许 `full` / `redacted`。Memory V2 的输入和输出包含用户原话、记忆、
证据摘录及工具结果；把这些内容按全局 `full` 发送到 Langfuse 与 PRD §6.5 / #302 R1 冲突。
另一方面，Memory V2 的写入精度、安全、回退、幂等与跨会话召回不能只靠测试总数证明。

本 ADR 为 V2 确定专用的内容边界、阻塞门禁、公共基准产物与许可证边界。Langfuse 仍是可选旁路，
SQLite 与派生索引仍是业务数据所有者。

## 2. Decision

### D1 — Memory V2 runtime trace 恒为 metadata-only

当 `AgentRuntime` 挂载 Memory V2 formation 时，trace 根、model generation、tool observation 与
最终 run observation 都省略 `input` / `output` 内容字段。该规则优先于 `LANGFUSE_TRACE_CONTENT=full`；
未挂载 Memory V2 的运行继续遵守 ADR-0018 的全局配置。

### D2 — 内容只在进程内计算哈希；发送字段走 allowlist

Memory V2 observation 只发送稳定的标识、阶段、模型别名、动作、kind、scope、计数、reason code、
attempt/fallback、schema/safety 结果、输出失败类别、延迟、token 数、成本及 SHA-256 哈希。输入、输出、候选内容、
证据、provider response、秘密凭证和自由文本错误不发送。官方 SDK 会自动把项目 `public_key`
附在 OTel `scope.attributes.public_key`；实际 trace API 回读将该 instrumentation scope 投影为
`metadata.scope.attributes.public_key`（trace 与 observation 都可能出现）。按用户批准，它是非秘密
路由标识，只允许出现在直接 scope 字段或该精确 API 投影，不视为应用内容或秘密凭证。`secret_key`
及 public key 在其他字段中的出现仍由真实 trace Gate 拦截。哈希在进程内由规范化内容计算后，只将
摘要交给 Langfuse；结构化字段通过 sink allowlist 和有限 token 格式过滤。

模型调用的输入 token 是本地估算值，并标注 `input_tokens_estimated`；输出 token 如可估算也标注来源。
当前 invoker 不提供定价来源，所以按用户批准将 `cost_usd` 如实记为 `null`，不伪造费用；只有可信费率
或 provider 实际计费数据可用时才记录金额。

### D3 — 观测故障不能改写 Core 结果

Langfuse 通过现有官方 SDK sink 接口接入，不新建存储或 job owner。sink 只在配置启用时装配；
SDK 错误使用脱敏本地诊断并被 sink / executor 边界吸收。Memory Job 的提交、Formation、Adjudication、
Recall 和 AgentRuntime 事件事实不依赖 Langfuse。

### D4 — 阻塞指标来自冻结、合成的项目金集

版本化金集 `evaluation/datasets/memory_v2_project_gold_v1.json` 标注为 `synthetic: true`，每项声明
eligibility、action、kind、scope、source authority、recall target、禁止结果及适用的 fallback、replay、
contradiction 标签。项目自己的冻结语料是 release gate；公开基准只提供非阻塞外部参照。
语料 v1.2.0 更正了三项把“run-end 触发资格”与“候选策略拒绝”混在一起的期望值：assistant 来源、secret、
未获同意的敏感候选仍属于可入队的合格 run，是否写入由后续候选策略与动作准确率检查。质量阈值保持不变。

机器报告计算 PRD §8.2 全部阈值：secret write、越权 recall/mutation、ineligible write 和 replay duplicate
为零；NOOP、write precision、kind、contradiction、Recall@6 与 transient-primary fallback 达到各自阈值。
fallback safety 按 PRD 接受“成功切换”或“降级且零写入”；#304 另要求 fallback 模型调用成功，单独用
`fallback_model_success` 阻塞指标证明。尝试 fallback 不等于调用成功。
每项带 numerator / denominator / threshold / verdict。缺观测、缺分母、错误类型、漏跑、失败、跳过、
未 await、重复 case 或 trace 都不能形成绿色 Gate。

The 2026-09-28 #304 Gate identified one fixture expectation that contradicted PRD §4.3: the synthetic episodic memory about a release postponed due to failing integration tests is project-scoped. The expected scope was corrected from user_global to project and the corpus advanced to 1.5.0; blocking thresholds remain unchanged. Per-case diagnostics retain only allowlisted vector-store error categories, never provider exception text.

### D5 — 报告只保存聚合指标与可复现身份

报告 schema v3 保存 corpus 版本 / digest、code SHA / tree SHA、配置别名（不含配置值）、case 计数、阻塞指标、
安全 job reason 与输出失败类别的直方图、累计 latency / token / cost、run ID 和重复运行的 `repeat_of`。
输出失败类别仅为 `invalid_response_type` / `empty_output` / `invalid_json` / `contract_violation`；
未知值折叠为 `other`。报告不保存题面、答案、会话、模型响应或
记忆正文。每 case 结果只保留固定 case ID、状态、白名单内的评估字段、模型别名与调用阶段、错误类型名；不会保留
自由文本异常、题面、提示词、模型响应或记忆正文。fallback“已尝试”与“调用成功”分别计量，只有成功的 fallback
调用且作业正常终结才满足 `fallback_model_success`。身份来自运行前固定的已提交 HEAD/tree；Gate-0 的工作树检查同时拒绝追踪文件偏离、隐藏索引位和
未跟踪车道输入，运行后再次验证 HEAD/tree 与工作树，变化或无法验证时报告失败。相同 report path 以独占创建
拒绝覆盖；重复的 case / trace 身份会失败，明确的重复实验用新 run ID 并记录 `repeat_of`。

### D6 — LoCoMo / LongMemEval 适配器不 vendoring 数据或完整上游运行时

运行者从外部路径提供官方数据文件；适配器只将上游 schema 映射到 runner 输入，并只写不含内容的
汇总报告。LoCoMo 的上游 `LICENSE.txt` 是 **CC BY-NC 4.0**；用户确认只用于**非商业内部评测**，
不用于商业发布或营销结果。该数据及衍生的逐条问答不提交到仓库。LongMemEval cleaned 数据按上游
Hugging Face 数据集卡标注 **MIT**。适配器没有 vendoring 数据、完整基准运行时或官方 scorer；公开 smoke 可在注明来源与许可证的前提下复用 reader 方法。

来源与许可证：

- LoCoMo 数据与结构：<https://github.com/snap-research/locomo>；许可证：
  <https://raw.githubusercontent.com/snap-research/locomo/main/LICENSE.txt>
- LongMemEval schema：<https://github.com/xiaowu0162/LongMemEval>；cleaned 数据许可证：
  <https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned>
- 可复用基准实践：<https://github.com/mem0ai/memory-benchmarks>（只参考运行与比较方式，未复制代码）

首个完整且有效的公开基准运行可以独占写入冻结 baseline；后续报告不能覆盖它。公开基准报告永久
标记 `blocking: false`，不影响 PRD §8.2 项目门禁。

### D7 — 首次公开数据运行可以是单样本 smoke，不冻结正式 baseline

用户批准 LoCoMo 与 LongMemEval 各执行一个真实样本，用实际 Memory V2 formation、Milvus recall
与模型回答链验证接线。该 smoke 只写非阻塞内容脱敏证据，标记 `run_mode: smoke`，不传
`freeze_path`，不代表正式基准分数。smoke 回答质量使用归一化 token F1 ≥ 0.5 的本地判别器，
证据中同时记录 F1 与阈值；它不是上游正式 scorer。当前选例规则：LoCoMo 只接受标注相关 turn
中由 user persona 提供的证据；LongMemEval 只选 `single-session-user` 类，并要求 gold
`answer_session_ids` 中的 user turn 提供证据。预期答案的规范化词序列必须连续出现在该 user
turn；满足条件后按样例大小、再按 case ID 稳定选最小者，无合格样例则失败关闭。选例用 gold
只判断来源是否真正提供答案，不进入形成、检索排序或回答提示；真实回答仍须独立达到 F1 ≥0.5。
LoCoMo 单样本 smoke 进一步限于上游数据 category `4` 的单跳事实题：此前不设类别时最小合格题
落在多跳类，答案还需要非权威 assistant 的经历，超出了本项目从 user 形成记忆的权限边界。
用户同日批准此单样本限制，正式全量基准仍保留所有类别；上游评测器把 category `1` 按多跳题
单独计分（<https://github.com/snap-research/locomo/blob/main/task_eval/evaluation.py>）。
此前用户批准过 user-turn F1≥0.5 的选例规则，但其选中 `single-session-assistant` 样例，user
turn 只是重复问题，根本未给出答案。2026-09-26 用户批准以当前更严格规则替换该规则。
扩展后的真实 LongMemEval smoke 选中 user-evidence F1=0.631579 的样例，但本次未通过：一个
formation job 因 `embedding_unavailable` 降级、没有提交记忆，Recall@6=0、注入数为 0、回答 F1 为
0.421053，`chain_verified=false`。runner 报告及随后独立 Milvus 查询都确认临时 collection 已清理；
报告没有原始问题、答案或对话文本，脱敏检查也未发现这些原文。报告保存在
`docs/evidence/memory-v2-public-smoke-longmemeval-6224d862.json`。独立 embedding 探针曾成功返回
1024 维向量，但 smoke 运行期间再次失败；因此 #302 的真实链路验收仍未满足，不能按通过处理。
随后用另一份已授权的本机 embedding 配置重试，Milvus 初始化探针仍以 `embedding_unavailable`
失败且未生成报告。独立 Milvus 查询再次确认没有 `memv2pub_*` 临时 collection。
LoCoMo 仍只用于非商业内部评测。
为适配 harness 的 user/assistant 来源权威，LoCoMo 将对话中首位参与者映射为评测 user persona，
其余参与者映射为非权威 assistant evidence；LongMemEval 保留数据集提供的角色。报告记录这一映射，
烟测选例只接受预期答案规范化词序列连续出现在权威 user turn 中的可回答样例；assistant-only 证据样例不作为烟测目标，
因为 Memory V2 应对它们拒绝形成持久用户记忆。用户于 2026-09-26 批准了 user evidence 来源限制。
烟测通过还要求至少一个非空且带标注用户证据的 session 形成已提交记忆，并由 Milvus 命中后实际注入回答上下文。
烟测的 Recall Provider 使用与生产 wiring 相同的 `memory_search_timeout_seconds` 配置。
形成作业最多排空 1,200 秒；报告记录该预算、active memory 的 tier/kind 计数及相关来源会话计数，不记录记忆文本。
同日的真实模型探针发现形成提示词没有写明 discriminated payload 必须含 `payload.kind`；已批准在
#298 运行时提示词中补全三个 payload 的精确键集合，同时保留严格解析与 fail-closed 行为。

2026-09-26 使用 `Pro/BAAI/bge-m3` 的 LongMemEval smoke 已验证完整形成、Milvus 命中与上下文注入链：
Recall@6=1.0、`chain_verified=true`、命中并注入 1 条 Collection 记忆。真实答案 token F1=0.222222，
未达到 0.5 阈值；样例只有一个召回候选，因此尚不能证明额外重排会改善答案质量。报告为
`docs/evidence/memory-v2-public-smoke-longmemeval-11db90a5.json`，清理与内容脱敏检查通过。

2026-09-27 在已提交、干净代码树 `aff3e46f438d0d11ec6e4439f1e85f739f26bda7`（tree
`045b396cab5feaaef1e4dce26f2a5cf109f25a76`）上分别运行两个获批样例。LoCoMo category 4 与
LongMemEval `single-session-user` 均选择到权威 user turn，答案 token F1 分别为 **0.666667** 与
**0.5**（门槛均为 0.5）；两者 Recall@6 均为 **1.0**，相关 hit rank 为 1，形成记忆被实际注入，
`chain_verified=true`，runner 均确认临时 Milvus collection 已不存在。报告为
`docs/evidence/memory-v2-public-smoke-locomo-f22ae031.json` 与
`docs/evidence/memory-v2-public-smoke-longmemeval-6374b2d9.json`，只含脱敏指标和哈希，不含问题、答案、
记忆正文或凭证；`cost_usd=null` 符合无可信费率来源时的既定口径。两份均为非阻塞单样本 smoke，
不冻结正式 baseline。完整项目门禁与集成尚未完成。

### D8 — BGE reranker 试验未证明收益，不纳入运行时

用户于 2026-09-26 批准评估 SiliconFlow `BAAI/bge-reranker-v2-m3`，前提是实际质量有改善。LoCoMo rerank 与
确定性排序控制组使用同一选例 hash，且两条 Memory V2 端到端链路均验证成功。Rerank run 在同一批 14 个授权
候选上的 Recall@6 与 deterministic hybrid 都为 1.0，增量为 0。生成答案 F1 分别为 0.1 与 0.2；两次形成的
活动记录数为 19 与 15，故不能把答案差异归因于 rerank。两者均未达到 0.5 的 smoke 答案门槛。

本次样例没有显示检索收益，也没有证明答案质量改善，因此不把 reranker provider、配置或运行时接线纳入交付；
PRD 保留原 deterministic hybrid 与“不调用在线 LLM reranker”约束。两份脱敏 smoke 报告留作实验记录：
`docs/evidence/memory-v2-public-smoke-locomo-41b256f4.json` 与
`docs/evidence/memory-v2-public-smoke-locomo-control-79ab6ce8.json`。后续若重新评估，需固定形成后语料，
对同一候选记录做排序和答案 A/B 对照，并达到既有答案质量门槛后再提议修改规格。

### D9 — 公开 smoke 复用官方 reader 方法，按答题质量修复

2026-09-26 的真实 smoke 已证明形成、Milvus 召回与上下文注入链路通过，Recall@6=1.0；答案 token
F1 仍低于 0.5。rerank 没有召回增益，因此先改读题与答题方式，不调排序或放宽门槛。

采用 `ADAPT`，不引入新依赖或整套基准运行时：

- LoCoMo 的答题指令采用官方评测器的短语式回答策略，尽量复用上下文原词；本地指令继续明确记忆文本
  是不可信数据。来源：<https://github.com/snap-research/locomo/blob/main/task_eval/gpt_utils.py>，
  **CC BY-NC 4.0**。LoCoMo 及此适配仅用于用户批准的非商业内部评测，且适配与来源有明确记录。
- LongMemEval smoke 采用官方 `CoN` reader 方法：先从实际注入的记忆中提取与问题相关的 notes，再生成
  答案；最终答题仍以原始注入记忆为证据，notes 视作不可信派生内容并逐项核对。由于本项目注入的是按
  tier 合并的 Memory V2 记录而非原始 session，提取按当前检索上下文执行。来源：
  <https://github.com/xiaowu0162/LongMemEval/blob/main/src/generation/run_generation.py>，MIT。
- 不改 smoke 的 `normalized_token_f1` 指标与 0.5 门槛；LongMemEval CoN 两次模型调用的 token 与总延迟
  都计入报告，报告不保存问题、记忆、notes 或模型回答。

此处只借用公开基准的 reader 方法；不改变 Memory V2 生产运行时、持久记忆契约或确定性检索排序。

首次 reader 适配的真实烟测未达门槛：LoCoMo Recall@6=1.0、链路通过、答案 F1=0；LongMemEval
Recall@6=0、链路未通过、答案 F1=0.45。两份报告均确认临时 Milvus collection 已清理：
`docs/evidence/memory-v2-public-smoke-locomo-711d4b5c.json` 与
`docs/evidence/memory-v2-public-smoke-longmemeval-511bd604.json`。审查发现 CoN notes 被放在最终问题
之后，与上游顺序相反，现改为 notes 在前、原问题最后；答案阶段温度固定为 0，与两个上游评测器一致，
并添加只含数值的相关来源作业结果和答案 token 覆盖率，定位形成或注入丢失。上述修正还需重新跑真实
烟测，不能用旧报告宣称质量通过。

### D10 — 保留明确事实值，并分别验证形成、召回和注入

严格 user-evidence 选例后的真实 LoCoMo smoke 已确认相关来源会话的形成作业提交成功，但唯一相关活动记忆
没有保留标准答案词，答案 F1=0。直接用同一用户证据答题的模型探针 F1=0.667，说明当前失败点在记忆形成的信息保留，
而不是 rerank 或放宽评分门槛。报告：`docs/evidence/memory-v2-public-smoke-locomo-8d08dfab.json`。

采用最小 prompt 约束：持久化用户事实与偏好要在记忆正文和类型化 payload 中保留证据明确给出的名称、值、数量、日期
及限定词；每项仍须由有效证据引用支持。此处借鉴 LangMem 对独立、清楚表达的记忆单元的设计，未复制其实现或引入依赖：
<https://github.com/langchain-ai/langmem/blob/main/src/langmem/knowledge/extraction.py>。

烟测分别记录相关来源的活动记忆、该记忆答案 token 覆盖率，以及真正被上下文提供器选中注入的相关记忆覆盖率。
注入判定通过实际 `list_profiles` / `hybrid_search` 返回项与运行时注入 ID 求交，只把确实进入 prompt 的活动记录计入；
profile 注入与前六条 hybrid hit 分开计数。原始记忆和模型输出不写进报告。F1 ≥0.5 与 Recall@6 仍独立保留为质量门槛。
若来源记忆形成成功但答案词在活动记忆中丢失，应归因于形成；若活动记忆保留答案词但注入覆盖率为 0，应归因于检索或上下文选择。

## 3. Consequences and open verification

### D11 — Preserve official question dates and expose content-free retrieval diagnostics

后续代码审查发现，旧 `chain_verified` 只分别要求存在 Milvus 命中和相关已注入记忆，却没有要求二者是同一条记忆；相关 profile 注入配合无关命中也可能误报成功。判定现要求相关且活动中的记忆同时出现在 top-6 Milvus 命中和最终注入中。Profile 指标仍单独报告，不能单独满足链路验收。

2026-09-27 的真实 smoke 仍未达回答门槛：LoCoMo hybrid Recall@6=0、答案 F1=0、`chain_verified=true`；
相关答案词已保留在并注入的 profile（相关 profile=1、注入答案词覆盖率=1.0），但最终回答没有答案 token。
LongMemEval Recall@6=0、答案 F1=0、`chain_verified=false`；权威来源记忆保留了答案词，但没有进入注入上下文。
两份 content-free 报告均确认临时 Milvus collection 已清理：
`docs/evidence/memory-v2-public-smoke-locomo-071fbde5.json` 与
`docs/evidence/memory-v2-public-smoke-longmemeval-5daa9284.json`。因此现有证据分别指向 LoCoMo reader
输出与 LongMemEval 检索/上下文选择，不能把它们合并归因于 formation。

复核官方 reader 实现后发现 LongMemEval adapter 丢弃了 `question_date`，而官方 CoN 和最终回答都使用该日期；
LoCoMo 已筛选为带权威用户证据的正向样例，但 reader 仍额外指示证据不足时拒答。适配器现保留并传入
LongMemEval 日期，LoCoMo 正向问答改为上游短语式回答指令；真实 smoke 需在干净提交树上重跑确认。
报告新增相关 hybrid 候选排名、profile 候选/注入计数和答案 token 数，不包含记忆、问题、标准答案或模型输出；
这些诊断只用于区分候选召回、上下文选择和最终回答，不改变生产检索排序或 #302 质量阈值。

- 全局 trace 内容配置不再能覆盖 Memory V2 的隐私边界；旧版非 V2 tracing 保持原状。
- 如果 SDK 未提供真实 token / 费用，报告保留估算标记或 `null`，不得把估算伪装成 provider 计量。
- 数据集文件由运行者在本地保管；LoCoMo 结果仅用于已批准的非商业内部评测。
- 自动化已验证门禁计算、变异失败、内容脱敏与适配器 schema。每种公开数据各一条真实 smoke
  不会冻结正式 baseline；完整 LoCoMo / LongMemEval 正式基线仍待单独运行。真实 Milvus / Langfuse
  run 及 Milvus / Knowledge / Qiniu 清理证明须在可复现的干净树上完成。

### D11 — Latest-tip smoke and real-service cleanup evidence

On clean tip `12966e9fb3e53433be51808a9480a2ca332dba60` / tree
`757e6bc4b7e8a3037526b9bfec2b48e22ab4c541`, the stricter LoCoMo category-4 smoke
passed (answer F1=1.0, Recall@6=1.0, verified relevant memory in top-six and final
injection, temporary collection absent). Two current-tip LongMemEval reruns reached the
reading-notes answer call after formation and recall, then received provider HTTP 500
before scoring or report emission. Runner cleanup completed after both attempts, and an
independent Milvus query confirmed zero `memv2pub_` collections. The prior completed
LongMemEval report on the full official cleaned dataset remains the AC9 score evidence
(strict `single-session-user` selection, F1=0.5, Recall@6=1.0); current rerun failures
are recorded without assigning a score or changing the selector/threshold. Evidence:
`docs/evidence/memory-v2-public-smoke-locomo-64bd21ec.json`,
`docs/evidence/memory-v2-public-smoke-longmemeval-6374b2d9.json`, and
`docs/evidence/memory-v2-real-gate-cleanup-20260927.json`.

The latest real cleanup gates passed 6/6. Milvus smoke collections, Knowledge tenant
records, and Qiniu test objects were verified absent; the dedicated Knowledge collection
pre-existed and was neither created nor dropped by the gate. Synthetic Langfuse trace,
dataset, and experiment evidence was retained with content fields omitted.

### D12 — #304 uses an approved gate-local Mimo fallback

On 2026-09-28 the user approved changing only #304's real-gate fallback from the
original Qwen choice to the locally configured `mimo` provider (`mimo-v2.6-flash`).
The primary remains the locally configured SenseAudio model. The gate resolves the
existing `memory.fallback` alias to Mimo for this release run; the frozen PRD §5.3 and
the default Memory V2 role mapping remain unchanged. Evidence records aliases and
safe attempt metadata, never endpoint credentials or key values.

### D13 — #304 recall evidence follows a real two-session lifecycle

The project gold corpus v1.8 keeps each recall query separate from the source user
statement. The real Gate forms the source in conversation A through the production
run-end notifier and job executor, then selects and injects it in conversation B
through `MemoryV2ContextProvider`. The required lifecycle case also reads the
authorized why-recalled API, edits through the public memory API and verifies version
history, deletes through that API, verifies the tombstone, and confirms the old recall
explanation is no longer returned. Recall@6 credits only records containing their
gold fact anchor. NOOP accuracy includes only eligible candidate decisions; ineligible
trigger behavior stays measured by its dedicated gates. The lifecycle evidence also
verifies that SQLite no longer contains the logical record chain and Milvus no longer
contains any version vector after deletion.

### D14 — #304 binds to the approved current primary model

On 2026-09-28 the user approved changing #304's primary model contract to the current
local primary: provider `qwen`, model `cline-pass/glm-5.3-flash`. The real-gate runner
resolves the configured primary chain for this ticket and pins the approved provider
and model before any live calls; its configured Mimo fallback remains pinned to
`mimo-v2.6-flash`. This is a #304-only evidence-gate override. The frozen PRD §5.3 and
the default production Memory V2 role mapping remain unchanged. Endpoint and credential
values are neither recorded nor emitted.

### D15 — #304 records the untrusted-recall permission boundary and collection ownership

The gold gate's adversarial recall probe verifies two independent runtime properties:
the malicious synthetic memory is recalled only inside the fixed, untrusted `HumanMessage`
data envelope, and a simulated privileged `bash` request is denied by the production
`ToolExecutor` under `WORKSPACE_WRITE` without an approval callback. The report labels
the request as simulated; it does not claim that a live model independently chose to
issue it. The blocking `non_privileged_recall` gate requires the complete evidence tuple
and fails closed if a component is missing, inconsistent, or false.

The temporary Milvus collection is considered owned only when the client confirms that
this run created it. A lost or ambiguous create acknowledgement does not establish
ownership from a matching name or schema; cleanup refuses to drop such a collection
and reports unverifiable cleanup instead. This avoids deleting a same-name collection
created by another actor during the create race.

### D16 — #304 temporarily promotes Mimo to the gate-local primary (superseded by D17)

On 2026-09-29, after the qwen primary produced zero successful calls across 23 attempts,
the user approved promoting the configured `mimo` / `mimo-v2.6-flash` model to the #304
real-gate `memory.primary`. The former configured qwen model
(`qwen` / `cline-pass/glm-5.3-flash`) becomes the gate-local `memory.fallback`, so AC4
continues to require a successful fallback call and completed job. The runner swaps the
two already configured `ModelConfig` roles in memory and validates both identities before
live calls; `.env` is not rewritten. This decision supersedes D12 and D14 only for the
#304 evidence gate. Frozen PRD §5.3 role defaults, runtime wiring, quality thresholds,
and credential handling remain unchanged. The failed source run is preserved at
`docs/evidence/memory-v2-real-gold-v1.8.0-1d8d3cd3d9b4-d25667d2.json`.

### D17 — #304 uses the configured Cline gateway primary and Mimo fallback

On 2026-09-29, after the D16 Mimo-primary run failed blocking quality thresholds and its Qwen fallback did not complete, the user approved replacing both #304 gate models. The configured Cline gateway model `cline-pass/deepseek-v4.1-flash` is the gate-local `memory.primary`; `mimo` / `mimo-v2.6-flash` is `memory.fallback`.

Cline is not a production provider preset. The real-gate runner therefore maps the configured `cline` provider id to the existing generic OpenAI-compatible adapter slot only while constructing the gate model. It preserves the configured model name, Cline base URL, and credential. A live probe found Cline's non-streaming response wraps the Chat Completion under `{\"success\": true, \"data\": ...}`; the documented streaming response uses standard OpenAI SSE. The runner enables streaming only for this Cline primary. This gate-local mapping reuses the existing transport without adding a production provider preset or changing PRD §5.3 aliases, runtime defaults, or quality thresholds. The observed non-streaming mismatch is tracked in [Cline issue #12647](https://github.com/cline/cline/issues/12647). Before any live call, the runner also requires the primary and fallback base URLs to match the user-approved Cline and Mimo endpoints (allowing one trailing slash); endpoint mismatches fail closed without logging the configured URL. D17 supersedes D16 for subsequent #304 evidence runs.

### D18 — Memory V2 formation and adjudication use deterministic sampling

The #304 real gate showed that the same configured models could intermittently change fact kind and source authority at the default interactive temperature. The user approved fixing gate-discovered blockers without changing frozen quality thresholds. Memory V2 therefore constructs its model invokers from a shallow copy of the configured model and sets that copy's temperature to `0.0` for formation and adjudication. The shared configuration remains unchanged, so interactive agent responses retain their configured sampling temperature. This applies to Memory V2 production aliases as well as the gate-local Cline/Mimo mapping; it does not change provider selection or the PRD role aliases. The focused regression is `tests/memory/v2/test_v2_runner.py::test_memory_calls_do_not_inherit_the_agent_sampling_temperature`.

## 4. Verification contract

落实证据位于 `tests/observability/test_tracer_port.py`、`tests/memory/v2/test_v2_executor.py`、
`tests/evaluation/test_memory_v2_quality.py` 与 `tests/evaluation/test_memory_v2_public_benchmarks.py`。
实现分支集成前仍须通过 #302 要求的双轴审查、review coverage、全量后端门禁及真实服务清理验证；
本 ADR 不把尚未运行的真实 Gate 记作已通过。
