# W-25 · 压缩阈值对齐规格（0.80/0.90 → 0.70/0.85）
**目标仓库**：intelligence-agent-backend（Python Core）。

**类型/优先级**：P1 Bug（规格一致性 Gap）。**依赖**：无。**范围**：`src/agent_harness/context/compactor.py` 及对应 context 测试。实施前重读 Engineering Spec 06 §阈值（`auto_compact_threshold = 0.70`、`hard_guard_threshold = 0.85`，第 100-101 行冻结值）。

## 可复现症状

`compactor.py` 构造函数默认 `auto_compact_threshold: float = 0.80`、`hard_guard_threshold: float = 0.90`，与 Spec 06 冻结的 0.70/0.85 不一致。W-04（#348）票面引用的也是规格值。按 AGENTS.md §1.1，代码不能反向覆盖冻结规格。

## 工作指令

1. 把两个默认值改为 0.70 / 0.85；保留 `max_context_tokens <= 0 or not 0 < auto <= hard <= 1` 的既有校验不动。
2. 全仓 grep 这两个默认值的所有显式传参点（assembly、测试、demo），显式传参点**不改**（那是调用方选择），只改缺省；但要在测试里锁定「缺省 = 规格值」。
3. `reserve = max(int(max_context_tokens * 0.15), 16384)` 不动（与 Pi `reserveTokens=16384` 一致，PRD §4.1 已吸收为正式口径）。

## 验收

- 新增/修改测试：默认构造的 `Compactor` 其 `_auto_limit == max_context_tokens * 0.70`、`_hard_limit == max_context_tokens * 0.85`（可判定断言，不接受目测）。
- 若既有测试硬编码了 0.80/0.90 行为（如按旧阈值构造触发场景），逐处改为按配置注入阈值，不得通过放宽断言让灯变绿。
- 运行 focused context 测试、ruff，按 V3.1-lite 记录冻结树与 review。

**不做**：改触发算法结构、改 reserve 公式、改估算器、顺手重构 compactor。

**成熟参考/复用**：行业收敛区间 70–90%（Codex 90%、dsh `min(0.8W, W−O−64K)`、zcode ~73%、Cline `W−40K`，见 `docs/research/2026-09-27-long-task-context-management-research.md` §0）；规格 70/85 在区间内且对弱模型更保守，无需变更规格。
