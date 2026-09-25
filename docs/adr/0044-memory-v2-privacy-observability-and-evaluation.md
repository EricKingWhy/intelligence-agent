# ADR-0044 — Memory V2 隐私观测与质量评测

- **Status**: Proposed（实现与离线判别测试已完成；公开基准正式基线和真实服务 Gate 待运行）
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
attempt/fallback、schema/safety 结果、延迟、token 数、成本及 SHA-256 哈希。输入、输出、候选内容、
证据、provider response、凭证和自由文本错误不发送。哈希在进程内由规范化内容计算后，只将摘要交给
Langfuse；结构化字段通过 sink allowlist 和有限 token 格式过滤。

模型调用的输入 token 是本地估算值，并标注 `input_tokens_estimated`；输出 token 如可估算也标注来源。
当前 invoker 不提供定价来源，所以 `cost_usd` 如实为 `null`，不伪造费用。

### D3 — 观测故障不能改写 Core 结果

Langfuse 通过现有官方 SDK sink 接口接入，不新建存储或 job owner。sink 只在配置启用时装配；
SDK 错误使用脱敏本地诊断并被 sink / executor 边界吸收。Memory Job 的提交、Formation、Adjudication、
Recall 和 AgentRuntime 事件事实不依赖 Langfuse。

### D4 — 阻塞指标来自冻结、合成的项目金集

版本化金集 `evaluation/datasets/memory_v2_project_gold_v1.json` 标注为 `synthetic: true`，每项声明
eligibility、action、kind、scope、source authority、recall target、禁止结果及适用的 fallback、replay、
contradiction 标签。项目自己的冻结语料是 release gate；公开基准只提供非阻塞外部参照。

机器报告计算 PRD §8.2 全部阈值：secret write、越权 recall/mutation、ineligible write 和 replay duplicate
为零；NOOP、write precision、kind、contradiction、Recall@6 与 transient-primary fallback 达到各自阈值。
每项带 numerator / denominator / threshold / verdict。缺观测、缺分母、错误类型、漏跑、失败、跳过、
未 await、重复 case 或 trace 都不能形成绿色 Gate。

### D5 — 报告只保存聚合指标与可复现身份

报告保存 corpus 版本 / digest、code SHA / tree SHA、配置别名（不含配置值）、case 计数、阻塞指标、
累计 latency / token / cost、run ID 和重复运行的 `repeat_of`。报告不保存题面、答案、会话、模型响应或
记忆正文。相同 report path 以独占创建拒绝覆盖；重复的 case / trace 身份会失败，明确的重复实验用
新 run ID 并记录 `repeat_of`。

### D6 — LoCoMo / LongMemEval 适配器不 vendoring 数据或上游实现

运行者从外部路径提供官方数据文件；适配器只将上游 schema 映射到 runner 输入，并只写不含内容的
汇总报告。LoCoMo 的上游 `LICENSE.txt` 是 **CC BY-NC 4.0**；用户确认只用于**非商业内部评测**，
不用于商业发布或营销结果。该数据及衍生的逐条问答不提交到仓库。LongMemEval cleaned 数据按上游
Hugging Face 数据集卡标注 **MIT**。实现没有复制 Mem0 / LoCoMo / LongMemEval 的代码。

来源与许可证：

- LoCoMo 数据与结构：<https://github.com/snap-research/locomo>；许可证：
  <https://raw.githubusercontent.com/snap-research/locomo/main/LICENSE.txt>
- LongMemEval schema：<https://github.com/xiaowu0162/LongMemEval>；cleaned 数据许可证：
  <https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned>
- 可复用基准实践：<https://github.com/mem0ai/memory-benchmarks>（只参考运行与比较方式，未复制代码）

首个完整且有效的公开基准运行可以独占写入冻结 baseline；后续报告不能覆盖它。公开基准报告永久
标记 `blocking: false`，不影响 PRD §8.2 项目门禁。

## 3. Consequences and open verification

- 全局 trace 内容配置不再能覆盖 Memory V2 的隐私边界；旧版非 V2 tracing 保持原状。
- 如果 SDK 未提供真实 token / 费用，报告保留估算标记或 `null`，不得把估算伪装成 provider 计量。
- 数据集文件由运行者在本地保管；LoCoMo 结果仅用于已批准的非商业内部评测。
- 自动化已验证门禁计算、变异失败、内容脱敏与适配器 schema。首个真实 LoCoMo / LongMemEval baseline、
  real Milvus / Langfuse run 及 Milvus / Knowledge / Qiniu 清理证明仍须在可复现的干净树上完成。

## 4. Verification contract

落实证据位于 `tests/observability/test_tracer_port.py`、`tests/memory/v2/test_v2_executor.py`、
`tests/evaluation/test_memory_v2_quality.py` 与 `tests/evaluation/test_memory_v2_public_benchmarks.py`。
实现分支集成前仍须通过 #302 要求的双轴审查、review coverage、全量后端门禁及真实服务清理验证；
本 ADR 不把尚未运行的真实 Gate 记作已通过。
