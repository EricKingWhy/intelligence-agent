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

## 状态
- [x] 可达性调查（replace 可达；input_request_id 不可分叉）
- [ ] TDD 红 → 修 → 绿
- [ ] 本机 focused + ruff
- [ ] 沙箱全量
- [ ] 台账
- [ ] 两轴审查
- [ ] Gate0 + Push + PR
