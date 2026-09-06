# Phase 15 Gate — Langfuse 旁路观测 + 评测（#117-#125，ADR-0018）

> 日期：2026-09-07 ｜ 分支：feat/phase15（对 main 的 diff：40 文件 +3514/-109）
> 全量：**1173 passed / 9 skipped / 27 deselected / 0 failed**（基线 1136 + 新增 37）
> Lint：`ruff check src/ tests/ evaluation/` 全绿

## Gate 结果总表（5/5）

| # | Gate | 车道 | 结果 |
| --- | --- | --- | --- |
| 1 | Langfuse 关闭时 Core 正常（不变量 #21） | 默认 | ✅ PASS（trace_id=null、旁路零痕迹、事件流与 Phase 14 语义一致） |
| 2 | 真云链路：上传 → 回捞 → best-practices 审计 | integration（真云 jp） | ✅ PASS（两次真云全绿；一次因本地网络 10061 抖动失败后恢复复跑通过，见「过程记录」） |
| 3 | P0 Golden Cases 全绿（deterministic） | 默认 | ✅ PASS（tool_selection / recovery / kill_resume 三条 + 全局 dangling=0 断言） |
| 4 | 未配置云时 seed/experiment 优雅降级 | 默认 | ✅ PASS（skipped 语义，绝不触 SDK） |
| 5 | 真云 Dataset seed 幂等 + Experiment 上报 | integration（真云 jp） | ✅ PASS（重复 seed created=0；run_experiment 真实落云） |

`uv run pytest tests/integration/test_phase15_gate.py -m integration -v` → 2 passed（Gate 2/5）。
默认车道随全量：1173 passed 零失败。

## Gate 2 审计清单（官方 best-practices 固化断言，全部对真云回捞数据）

- trace name 描述性（`agent-run`）✓；session 聚合 = session_id（`propagate_attributes` 官方通道）✓
- trace metadata：run_id / agent_id / git_commit ✓；trace input=用户消息、output=最终回答 ✓
- GENERATION ×2（两次模型调用）：model 名在场、usage_details（input/output/total）如实上报（自动 cost 的前提）、response_model 元数据 ✓
- TOOL 观测：name=add、tool_call_id + session_id（Operation Ledger 对账键）✓
- context-build SPAN 在场 ✓；不发明第二套 trace identity ✓

## Eval 结果（P0 deterministic，可复现）

| Case | 断言 | 结果 |
| --- | --- | --- |
| tool_selection_add | 工具选择正确 + dangling=0 + completed | ✅ |
| recovery_guard_soft | 软熔断在场 + 恢复后 completed + dangling=0 | ✅ |
| kill_resume_confirm_success | session/resumed + 合成 tool/result 补齐（无悬空）+ 裁决 CONFIRM_SUCCESS | ✅ |

真实模型 smoke：入口 `evaluation/smoke.py`（结构性断言 + regression metadata）已就绪并经注入 fake runtime 验证管线；**真实模型调用未在本轮执行**（手动车道，烧 token）——集成后在有凭证环境运行 `python -m evaluation.smoke` 或等价入口。

## 过程记录与自审发现（code-review 双轴）

**Standards 轴（修复）**：
1. `evaluation/reports/*.json` 加入 .gitignore（运行产物不入库，数据集真相源入库）——本轮唯一需修复项。
2. ScriptedModel 升格 `src/agent_harness/model/scripted.py`（tests 同名文件变 shim）：eval runner 是项目代码，不能 import tests——语义/行为零变化（含 astream 分块与快照）。
3. 静默替换事故一次：runtime→execute_batch 的 `tracer=tracer` 参数首patch未命中（缩进漂移），真云 Gate 审计抓到（tool 观测缺席）——已修复并复跑。教训：脚本 patch 必须 assert 锚点。

**Spec 轴（ADR-0018 对照，已知边界如实登记）**：
1. D3 熔断语义澄清：wrapper 熔断覆盖**同步调用层**故障；网络层不可达由 SDK 后台队列隔离（热路径仍零阻塞、零异常泄漏）——两层各自兜底，主流程隔离保证不变。
2. D7 缺项（DEFER）：model_parameters（temperature 等）未随 generation 上报（runtime 不持温度配置）；`resumes` 恢复链 metadata 未实现（resume 语义在 web 层，本轮以 session 聚合承载恢复链）。
3. D8：tool 观测的 ledger 关联以 session_id+tool_call_id 承载（operation_id 是 Ledger 派生属性，T4 审计已记录口径）。
4. 真云审计过程发现官方语义：`trace.list` 返回观测 id、`trace.get` 返回完整对象；观测 type 为大写枚举（GENERATION/TOOL/SPAN）——Gate 断言已按真实形状固化。
5. 观测落库顺序：trace 先于子观测可见——Gate 回捞以「observations 到齐」为就绪条件（带退避）。

## 环境备注

- Langfuse 云（jp 区）密钥在 worktree `.env`（零泄漏）；集成时需同步 `LANGFUSE_*` 到 main worktree `.env`。
- 上游模型网关（senseaudio）与 Langfuse 云均出现过间歇抖动；Gate 全部按「退避复跑 + 结果登记」处理，无 FAIL 被掩盖。
