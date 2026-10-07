# V3.1-lite 全量门禁（阶段二-2）

- 分支：`codebuddy/365-w21-gate`；基线 `6728cf7e`；本分支为 **docs-only**（`docs/live_gate/w21/` 3 个 markdown），无产品代码改动
- 环境：`no_proxy=localhost,127.0.0.1` + `NO_PROXY=localhost,127.0.0.1`（大小写都设）；`PYTHONPATH=<repo>/src`；venv bin 在 PATH 首位；前端 `NODE_USE_ENV_PROXY=0`

## 已完成的车道

| 车道 | 命令 | 结果 |
|---|---|---|
| ruff | `ruff check .` | ✅ 全绿（All checks passed!） |
| gate0 | `python scripts/gate0.py` | ✅ **6/6**（diff-check/ruff/oxlint/tsc/guards/coverage 全 PASS；读数 `docs/gate/6728cf7eef9c3f3fe3414e3ed8fe942da0a00f51.json`） |
| tsc | `npx tsc -b`（web/） | ✅ exit 0 |
| oxlint | `npx oxlint`（web/） | ✅ 0 errors（45 warnings） |
| vite build | `npx vite build`（web/） | ✅ built in 1.72s |
| tests/challenge focused | `pytest tests/challenge/ -q -p no:randomly` | ✅ **6 passed** |
| 后端全量 pytest | 纯净 worktree `~/workspace/w21-work/wt-verify`（`6728cf7e`） | **6656 passed, 22 skipped, 51 deselected, 4 failed**（651s；4 红见下表，全部 base 已有） |

## 进行中的车道

| 车道 | 状态 |
|---|---|
| 后端全量 pytest（干净 worktree 重跑） | ✅ 完成：**6656 passed, 22 skipped, 51 deselected, 4 failed**（`~/workspace/w21-work/pytest-worktree.log`，651s）。注：worktree 首跑另有第 5 红 `test_exec_bit_matches_shebang.py::test_gate0_guards_lane_runs_this_guard`，系 worktree 缺 `.venv` 导致 `venv_python()` 取空（验证 setup artifact），补 symlink 后该文件 12/12 通过，不计入 |
| vitest | `npx vitest run`（worktree 的 web/，node_modules symlink） | ✅ **99 文件 / 1479 用例全过**（exit 0） |
| Playwright e2e `--workers=2` | 纯净 worktree，`npx playwright test --workers=2` | **490 passed, 16 failed**（46.1m；16 = 8 个用例 × 1280/1920 双宽度，见下表） |

## e2e 16 红分析（base 已有，本分支 docs-only 不可能引入）

| 用例 | 文件 | 定性 |
|---|---|---|
| AC9/AC10（菜单第一项入口）、AC11×2、AC12、AC9（占位区按钮） | `e2e/u-project-task.spec.ts` | **一致复现**（单独重跑 10/10 仍红）：点击"在此项目中新建任务"后 `.project-dialog[aria-label="在此项目中新建任务"]` 始终不出现。#367（W-23 任务创建入口）功能区的真问题，需另开 ticket 修，不属本票 |
| T12o/T12p/T12r（multiturn queue WS 时序） | `e2e/multiturn-queue.spec.ts` | 时序敏感（7–20s 超时），本 VM 高负载下跑的 46 分钟长任务，疑似抖动；未单独重跑证实 |
| AC8（memory pagination 上限） | `e2e/memory-management.spec.ts` | 1.5m 超时，同上疑似负载抖动 |

按 #365 票面"若关键工程测试仍有阻断，本 Gate 不可宣布发布完成"：e2e 在 base 上即有 16 红（含 #367 对话框打不开的一致失败），这是 **Gate 级别的阻断发现**，已如实记录；修复归属另票，不在本 docs-only 票内施工。

## 后端 4 红的隔离分析（已完成）

首轮全量：`4 failed, 6656 passed, 22 skipped, 51 deselected`。4 个失败已在**纯净 worktree**（`6728cf7e`，零改动）上逐个复现，全部复现 → 与本分支的 docs-only 改动无关（本分支 tracked 文件零修改，`git diff HEAD` 为空）。

| 失败用例 | 根因 | 定性 |
|---|---|---|
| `test_tracer_port.py::test_port_surface_is_closed_for_both_implementations` | `typing.get_protocol_members` 在 Python 3.12 不存在（3.13+ API） | base 已有，版本/环境问题 |
| `test_constraint_registration_kill.py::test_fact_durable_before_ledger_terminal_requires_manual_reconcile` | `_ConfirmSuccess` 缺 `source_for` 属性（`coordinator.py:683`） | base 已有真问题，另有分支 `origin/fix/i784-confirm-source-for` 在修（issue #784），不属本票 |
| `test_local_sandbox.py::TestExecBasic::test_timeout_returns_negative_exit_code` | 容器无 raw socket，`ping` 报 `Operation not permitted`，exit 2 ≠ -1 | base 已有，容器环境限制 |
| `test_progress_file.py::TestAtomicWrite::test_readonly_target_fails_explicitly` | root 绕过权限位（chmod 只读不生效） | 已知假红，AGENTS.md 2026-10-06 教训 |

按 #365 票面"按相应旧票规则处理"：以上 4 项均为 base main 已有问题（环境/他票），本票 docs-only 不修产品代码，不在本票开修。

## 树污染事件记录（诚实披露）

2026-10-07 13:48:31 CST（05:48 UTC），#368 worker 在共享主 checkout 上执行了 `git checkout origin/codebuddy/368-evidence-retention`，当时本任务的首轮全量 pytest（05:36–05:53）正在运行，工作树在测试中途被换掉。首轮数字（6656 passed / 4 failed）可能混入跨树 import，已**废弃**，改用独立 worktree 重跑。本分支与 #368 分支零文件交集；为避免互相污染，本任务后续验证全部在 `~/workspace/w21-work/wt-verify` 进行，不再碰主 checkout 的分支状态。

## 说明

- 本分支 docs-only，门禁数字即基线 `6728cf7e` 的数字；任何红色都将按测试铁律处理（修或隔离证伪），不 waive。
- 最终数字在 PR 描述中汇总。
