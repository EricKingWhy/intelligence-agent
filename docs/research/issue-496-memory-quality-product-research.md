# Issue #496：Memory V2 质量层成熟产品调研

## 调研范围

目标是核实当前 memory extraction 的成熟做法，判断哪些机制适合 #496；不改变本项目的 R5 信任门槛、模型、gold v1.9.0、阈值或 #485 的 fail-closed 语义。本记录只引用机制，没有复制上游代码。

## 来源与核实

### Mem0

- 官方仓库浅克隆快照：b7ad69afda6b6ed030347c66d48a13e4de9dec08。
- 许可证：仓库根 LICENSE 为 Apache-2.0。
- 机制：mem0/configs/prompts.py:578 提示抽取器优先保留有用信息；:638-666 要求保留精确数量和具体名词，避免把具体值泛化。mem0/memory/main.py:942-964 显示 extraction 阶段使用 JSON object 响应格式。
- 来源：[prompts.py](https://github.com/mem0ai/mem0/blob/b7ad69afda6b6ed030347c66d48a13e4de9dec08/mem0/configs/prompts.py#L578-L666)，[main.py](https://github.com/mem0ai/mem0/blob/b7ad69afda6b6ed030347c66d48a13e4de9dec08/mem0/memory/main.py#L942-L964)，[LICENSE](https://github.com/mem0ai/mem0/blob/b7ad69afda6b6ed030347c66d48a13e4de9dec08/LICENSE)。

### LangMem

- 官方仓库浅克隆快照：48e3c11f5bb527282c7d5339c6a87a0b35abccfc。
- 许可证：仓库根 LICENSE 为 MIT。
- 机制：src/langmem/knowledge/extraction.py:185-206 要求结合输入与既有记忆提取、引用支持信息、压缩重复项，同时避免虚假记忆；:217-235 暴露 schema、自定义指令以及是否允许 insert/update/delete；:253-270 通过 create_extractor 和 schema tools 约束模型输出。
- 来源：[extraction.py](https://github.com/langchain-ai/langmem/blob/48e3c11f5bb527282c7d5339c6a87a0b35abccfc/src/langmem/knowledge/extraction.py#L185-L270)，[LICENSE](https://github.com/langchain-ai/langmem/blob/48e3c11f5bb527282c7d5339c6a87a0b35abccfc/LICENSE)。

## 对本项目的适配判断

- ADAPT：采用有证据引用、保留具体信息、明确输出结构的提示设计。这与本项目的证据引用和既有 runtime 校验相容，可用于小范围 prompt 调优。
- 不照搬 Mem0 的“有疑问也抽取”倾向：本项目有独立的 precision、授权、敏感信息与 R5 门槛，不能因追求 coverage 而放宽它们。
- DEFER：LangMem 的 schema/tool-calling 约束是成熟的结构化输出模式；但其实现依赖 LangGraph、Pydantic 与 trustcall。当前项目有 provider-neutral ModelInvoker 与自己的 contract 校验，直接移入依赖或更换抽取路径不符合当前小票范围。若要解决 schema failure，应先获取失败响应并核实当前 provider seam 是否支持等价的受约束输出。
- 本次没有复制上游代码，因此没有将其实现或依赖引入本仓库；如后续实质复制，须遵守对应 MIT/Apache-2.0 notice 要求。

## 与 run5 证据的关系

冻结报告 docs/evidence/memory-v2-real-gold-v1.9.0-dfc40b9d0006-bb29e3c5.json（gold v1.9.0）显示 positive_procedure 最终为 NOOP，selection_rejected_counts 中 procedural_threshold_not_met 为 1。这个事实与 issue #496 评论中“positive_procedure 本跑命中”的表述不一致。

报告还显示 primary_transient_fallback 的 formation 使用 fallback 成功，但随后 adjudication contract violation，最终 fail-closed 且没有写入。现有报告未保存无效响应体，因此无法基于此记录断言具体提示文本能修复它。不能因成熟产品使用 schema-constrained output 就擅自改变本项目的 #485 重试/失败语义。

## 当前结论

用户 2026-10-08 批准仅做原票面 prompt 调优；冻结 gold v1.9.0、模型、阈值及 R5 / #485 语义下，本轮修改的完整门禁仍失败（`docs/evidence/memory-v2-real-gold-v1.9.0-d0804a1be5dd-5a2ddcfb.json`）。补充单例诊断揭示 user/tool `source_authority` 与冻结 gold 预期存在差异；详见 `docs/phase_status/2026-10.md` 的 #496 条目。本轮未改 gold 或阈值，也不据单例诊断宣称达标。

## 可复核位置

- Mem0 clone：C:/Users/王浩宇/AppData/Local/Temp/issue496-research-mem0
- LangMem clone：C:/Users/王浩宇/AppData/Local/Temp/issue496-research-langmem
- 当前实现的 deterministic procedural gate：src/agent_harness/memory/v2/policy.py:419-445, 595-670
- Gold runner 的 positive_procedure event 构造：scripts/run_memory_v2_real_gold_gate.py:207-263
