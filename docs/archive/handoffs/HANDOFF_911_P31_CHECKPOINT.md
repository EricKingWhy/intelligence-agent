<!-- 断点交接（一次性）：#911 P3-1 施工中断用的续跑断点，随本票落进版本控制。 -->
# #911 断点

- 基线：`origin/main` = `8edade996c294e9560bed1717edc7cbe6113010e`（开工时 `git fetch` 后 `rev-parse` 复核）
- 分支：`claude/911-p31-gate-exclusions`；worktree：`~/workspace/intelligence-agent-wt/911-p31`
- 规则已读：`~/workspace/system/agent-workflow-prompt.md`（全文 106 行）、`AGENTS.md`（545 行全文）、`CLAUDE.md`（16 行）、Issue #911 全文、`~/workspace/system/dispatch/862-run.log`（00:20:43 节）
- 触发细则已读：`docs/agents/review-debug-playbook.md`、`docs/agents/implementation-discipline.md`、`docs/agents/issue-tracker.md`、`docs/agents/git-workflow.md`、规格 00/03/13/14 相关章节

## 探针（仓库外 `~/pytest-911/`）
- `probe_gate.py` — replace 污染形状（supersede 在途）→ 闸门选中 replace 事件 seq=4，`is_direct`=False ⇒ **分叉成立**
- `probe_bracket.py` — replace 落在 compaction bracket 内 → 替身投影成 `(s,s)`，闸门选中 ⇒ **分叉成立**
- `probe_input_request.py` — 真生产点 `_constraint_input_answer_data` 产出四种答复 → `latest`=None 且 `is_direct`=False ⇒ **一致，无分叉**

## 状态（收尾更新 2026-10-10；SPECKIT 上游已停用本文件作状态载体 ⇒ 状态以 `docs/SDD_TICKET_TRACKER.md` / `docs/phase_status/2026-10.md` 为准，此处只留断点续跑所需的最小事实）

- [x] 可达性调查 —— ⚠ **下条结论已被后续实证推翻，保留原文以留痕**：「input_request_id 不可分叉」**错**：
  澄清答复之后又有 user 消息、而该后续消息被 `message/superseded` 整轮 shadow 时，投影里 seq 最高的可见
  `HumanMessage` 正是答复本身（早退守卫只在答复**就是末条**时触发）⇒ 分叉可达。上面
  `probe_input_request.py` 只试了「答复就是末条」的形状，故得出相反结论；正确形状的复现见
  `tests/session/test_derive_direct_user_input.py` 的两条 input_request_id 用例。
  两条排除**均已**补齐（`replace` 虽无生产写点，但不可达理由不是被 shadow）。
- [x] TDD 红 → 修 → 绿（红证：`8edade99` 实现 + 新用例 = 4 failed / 9 passed）
- [x] 本机 focused + ruff
- [x] 沙箱全量（见归档节的读数与其唯一红的归因）
- [x] 台账（`docs/review_ledger.d/911-p31-*.tsv`）
- [x] 两轴审查（发现阶段 + 修后重审 + 两轮窄复验，零未闭合 finding）
- [ ] Gate0 终态收据已落；Push + PR 待授权（§14.4）
