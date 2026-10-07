# W-21 可行性评估（阶段一）

- 评估时间：2026-10-07；评估人：Muse（subagent）
- 环境：Linux 云电脑（Cloud Hypervisor，x86_64），无 Windows VM；Chromium 可用（Playwright 自带 chromium-1243；`/opt/meta-chromium/chrome` 拦本地网络访问，见 ~/AGENTS.md 2026-10-07 教训）
- 分支：`codebuddy/365-w21-gate`（基线 `6728cf7e` = origin/main）
- 五项启动检查：AGENTS.md §3/§8/§9/§14（`~/workspace/intelligence-agent/AGENTS.md`，已整读）✓；Issue #365 全文（`gh issue view 365`）✓；挑战全文（`docs/research/2026-09-27-long-task-resilience-challenge.md`）✓；W-20 判定器（`tools/challenge-fixture/judge.py`，6 项判定逻辑已读透）✓；本任务书 ✓

## §9.1.1 结论

Gate 设计本身合理，不触发停下线。#365 的验证项分为两层：

1. **Windows 真机层**（安装/桌面/TUI/两次真实模型长任务）—— 设计目标就是发布 Gate，必须在目标平台做，不可替代，不冗余。
2. **平台无关层**（W-20 判定器确定性链、真实浏览器操作、V3.1-lite 门禁、证据可重放性）—— 在 Linux 可完整执行，是 Gate 的必要非充分条件。

两层无冗余、无不可行设计。Linux 上能做的全部做、留证据；Windows 真机层如实记 blocked，不伪造。

## 逐项评估

| #365 要求 | 判定 | 说明 |
|---|---|---|
| 冻结代码树 SHA / 模型-provider ID / W-20 样例版本 / 权限预算 | [可做] | 已记录（见 10-w20-deterministic-evidence.md） |
| W-20 判定器 6 项判定完整跑通 | [可做] ✅ | 基线（未修复）已跑通：`overall=fail`，`missing=[r042_zero_writes, retry_conflict, ui_truthful]`；修复版真实模型施工中，完成后重跑 |
| 真实浏览器页面验证（R-042 状态页） | [可做] ✅ | Playwright 真实 Chromium 操作已验证：填表→点击→断言→截图（`browser-r042.png`） |
| kill/reconcile 语义演示 | [可做] ✅ | `FAULT_KILL_AFTER_COMMIT=1` 手动验证：kill 后 DB 40 条已提交、审计停留 `processing`、重启不盲重写 |
| V3.1-lite 全量门禁（ruff/pytest/tsc/vitest/oxlint/gate0/e2e） | [可做] ⏳ | Linux 可跑；pytest 全量与前端门禁后台运行中，数字见 30-gates.md |
| 证据可重放（evidence refs） | [可做] | 本目录 + `~/workspace/w21-work/` 可复跑脚本/命令 |
| Run A：Windows 桌面启动真实模型长任务（定位→修改→测试→浏览器验证→diff 审阅，含 compaction + 摘要失败各一次） | [阻塞] | 无 Windows VM，桌面客户端无法安装/启动 |
| Run B：TUI 冷启动 + DB 提交后 kill → 桌面打开 → reconcile → 手动续跑 | [阻塞] | 无 Windows；harness 层语义已在 Linux fixture 演示，但 TUI 真机流程不可做 |
| W-16 烟测：安装→启动→退出→更新→恢复 | [阻塞] | 无 Windows |
| 桌面/TUI 同时在场与单独退出规则 | [阻塞] | 无 Windows |
| 桌面/TUI 同一 Task 状态与 Event seq 一致 | [阻塞] | 无 Windows |
| 两次独立完整通过（Gate passed 判据） | [阻塞] | 依赖以上 Windows 项；Linux 侧只能给出确定性层证据 |
| 双轴独立审查（另起 subagent） | [部分阻塞] | 本 subagent depth=2/2、`can_spawn=no`，无法起独立审查 subagent，需 parent 另行安排；PR 标注 pending independent review |

## 与票面偏离

- 无。未修改 #365 票面、AC 或验收标准；本分支只产出证据文档（docs-only），不碰产品代码。
