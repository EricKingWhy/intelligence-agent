# W-03 · 只裁剪可回读的旧 Tool Result 投影
**目标仓库**：intelligence-agent-backend（Python ContextBuilder / ArtifactStore）。

**类型/优先级**：P1 Context。**依赖**：W-01；可与 W-05 并行。**范围**：`context/builder.py`、`tooling/overflow.py`、既有 ArtifactStore/inspect 工具与 context/tool 测试。原始 SessionEvent 和 Artifact 不得因裁剪删除。

## 工作指令

1. 在 LLM 摘要前，对已被新结果取代、重复检索或超长旧 Tool Result 做**确定性**裁剪。仅当原文已经经既有 ArtifactStore 保存、ref 校验且可读回时执行。保留同一 `tool_call_id` 的 call/result 逻辑配对、状态码/错误原因、必要结论、精确 ID、原 Event seq 与 artifact ref。
2. 定义可判定的 supersession：例如同一文件同一版本的重复读取或同一查询相同结果 hash；无法证明同源/可替代时不剪。只改 Runtime Context 投影，不改 durable 事件。
3. 裁剪后重新估算 token；若无法读回或存储失败，保持原结果并把压力交给 W-04 硬护栏，不能制造假引用。

**验收**：测试重复旧输出、不同版本文件、失败 ToolResult、call/result pair、ArtifactStore 写成功读失败、超大 Unicode 输出；裁剪前后可通过 inspect_artifact 找回原始内容；重启后裁剪决策可重建/解释；不出现新的 Tool retry。focused context/artifact/tool 测试与至少一组真实大输出测量，记录裁剪前后 token 估值和原件 ref。**不做**：替换 ArtifactStore、删除历史、把 TS pruner 引入 Core。

**成熟参考/复用**：[DeepSeek Tool Result Pruner](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-tool-result-pruner/README.md)保留 source event 与头尾信息；[oh-my-pi compaction](https://github.com/can1357/oh-my-pi/blob/main/docs/compaction.md)有旧结果裁剪。二者为 `PORT DESIGN`；本仓 ArtifactStore 是 `REUSE`。上游 TS/可变 SessionStore 不复制到 Python Core。
