"""真实模型基线与可续跑评测框架（issue #1005）。

薄层：复用 `evaluation/` 既有能力（runner 的真实 AgentRuntime 装配、
assertions 的确定性断言、smoke 的 regression metadata、live_gate 的凭证扫描），
只新增**数据集 / 可续跑 JSONL 运行器 / Judge / 统计 / 报告**五块拼装。

设计来源：
- openai/evals（MIT）`evals/record.py` 的 LocalRecorder：JSONL 追加、
  首行=运行元数据、之后每条一行、写一条 flush 一条（中断不丢）。PORT DESIGN。
- confident-ai/deepeval（Apache-2.0）`deepeval/metrics/g_eval/g_eval.py`：
  结构化 score+reason 入库的 Judge 形状。ADAPT（本票用 3 票多数替代单票 GEval）。
"""
