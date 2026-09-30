# GUI-R5 修复批次深度检查底账（2026-09-30）

> **本文用途**：给独立深度检查者的完整底账。实现者（本会话 Agent）声明了什么、证据在哪、
> 自知哪里薄弱——全部写在本文。**台账是声明式输入，不是真相**；真相在代码、测试与可复跑的
> 命令里。请逐条对账，不要信任任何一条未亲自验证的声明。
>
> 对账范围：`git log ff64228d..7caa5ddd`（main 上本批次全部提交）。当前 `main = origin/main = 7caa5ddd`。

---

## 1. 交付通道

| 环节 | 值 |
| --- | --- |
| 基线 main | `ff64228d` |
| 分支 | `fix/r5-issues`（远端未删） |
| PR | [#436](https://github.com/EricKingWhy/intelligence-agent/pull/436)（merge commit `5728d94e`，gate0 绿后合并） |
| 收口 PR | [#437](https://github.com/EricKingWhy/intelligence-agent/pull/437)（tracker 终态，docs-only，merge `7caa5ddd`） |
| 中途合并 | `98f8241d` = 先回后正 merge origin/main（W-31 收口，#433/#434），审计行 `ci-58e7b621-98f8241d.tsv` |
| 关票 | #420/#421/#422/#423/#424/#427/#428 关闭（自动关+证据评论）；#425 早前核实关闭；**#426 open**（设计票 #435 跟进） |

主要提交：
`20e272c3`（8 项修复，29 文件 +889/−121）→ `ce08cc8c`（#420 AC3 回显，7 文件 +436）→
`497b5952`（GUI 报告/截图入库）→ `73b87693`（CI gate0 修复）→ `98f8241d`（先回后正 merge）→
`a6257995`/`00dd08f1`/`58e7b621`/`c1bb3f73`/`367378dd`（台账/落盘记账）。

## 2. 逐票声明

### #420 审批等待 10s 断流（4 AC）
| AC | 声明 | 证据落点 |
| --- | --- | --- |
| AC1 服务帧活性 | `wsStream.ts` 加 `onServerFrame`——任何服务端帧（含心跳）喂活性计时 | `web/src/lib/wsStream.ts` + `wsStream.test.ts`（`20e272c3`） |
| AC2 决策后对账 | `resyncAfterDecision`：审批 POST 成功后 resync 一次 | `web/src/hooks/useSession.ts`、`web/src/App.tsx` |
| AC3 结果回显 | `ce08cc8c`：`lib/approvalEcho.ts` seen 锚定（渲染期登记挂起→决出后消费投影 `approval_decisions`）+ `ApprovalEchoCard.tsx` + `Conversation.tsx` 接线 + CSS | 测试：`lib/approvalEcho.test.ts`(6)、`ApprovalEchoCard.test.tsx`(4)、`Conversation.approvalEcho.test.tsx`(4, jsdom) |
| AC4 模态不断流 | 真浏览器：模态开 36s+ 连接不断（旧 bug 10s 断） | 后端事件流取证：seq8 `tool/approval-requested` → seq9 `permission/resolved`；e2e 会话 `4a5bc9a2` 留存本机 |

**已声明偏差（AC3）**：回显锚定"本观看窗见过挂起"——刷新/换会话不回显，历史决策不平铺；
与票面"持久显示"的偏差记录在 issue #420 评论。锚定理由：回显是活交互痕迹，永久真相在时间线
（不变量 #22 单一事实源）。**检查点：这个语义取舍是否可接受，还是票面本意就是持久化。**

### #421 审批队列消失
`web/src/components/ApprovalModal.tsx`（新）：首个**非失效**待决审批进 Radix Dialog；open 受控 +
`onOpenChange` noop；ESC/点外/关闭按钮全无（决策前不可关）。失效卡（stale/404）绝不进模态
（APR-01 关闭路径保留）。`ApprovalCard.tsx` 配合（submitted 态、autoFocus 收敛到唯一卡）。

### #422 launch=false 带预算应 422
`src/agent_harness/web/app.py`（`BudgetRequest.run` 字段描述）+ `tests/web/test_budget_local_fuse_api.py`
新增 `test_run_budget_any_dimension_on_launch_false_rejected`：六维参数化
（turns/requests/tokens/cost/deadline_at/tool_call_limits）+ `_assert_rejected_without_side_effects`。

### #423 显式 auto_approve=false 弹审批
`src/agent_harness/session/service.py`（`_build_approval_callback` 判据）+ `session/approval.py` +
`assembly.py`（deny 兜底 reason 用户向措辞，退役 "manual approval not yet wired"）+ `CHANGELOG.md`
Unreleased 条目。测试：`tests/session/test_permission_mode_persistence.py`、
`tests/web/test_web_phase5_permission.py`。真浏览器三路径：approve / deny / 300s fail-closed
（seq8→seq9 间隔 300.01s = config 默认 `approval_timeout_seconds: float = 300.0` 精确吻合）。

### #424 fork --from-message 序数
`src/agent_harness/cli.py`：N = 第 N 条**用户消息**（非事件 seq）；越界双标注报错（序数范围 +
底层 seq 清单）。测试 `tests/test_cli_fork.py` +59（含反例锁：seq 2 是 model/completed 时旧语义
会抛错/错锚）。

### #426 预算 UI 入口（**open**，部分交付）
`web/src/components/Composer.tsx`：只暴露**一维** `max_agent_turns_total`；`api.ts`/`amend.ts`
映射（`toCreateBudget`）。真浏览器验证预算 50 原样落库 `run/started.data.budget`。
一维取舍声明在 issue #426 评论；三维设计票 **#435**（max_total_tokens / deadline_at 形态、
用量呈现、锁定状态机）。**检查点：一维最小交付是否满足票面"常用三项"的最低验收。**

### #427 等待审批 TopBar 文案
`web/src/components/TopBar.tsx` + `lib/runState.ts`：等待审批例外分支——
"已 Ns 没有新进展，正在等待你的审批决定"。真浏览器实显验证（70s 时）。

### #428 截断显示自相矛盾
`web/src/lib/amend.ts` + `web/src/components/ArtifactViewer.tsx`：`truncated` = 行数截断 ∪
字符截断；字符截断（行数没少）不再报"显示 N / 共 N 行（已截断）"。

### #425 / #435
#425：白盒核实 HEAD 已修复 → 关闭（零代码）。#435：新建设计票（零代码）。

## 3. 随批次工程资产

- **GUI 证据入库**（`497b5952`，12 文件）：`GUI_BUG_LIST_2026-09-28.md`、
  `GUI_BUG_LIST_2026-09-28_R5.md`、`GUI_TEST_FINDINGS_2026-09-27.md`、
  `GUI_TEST_REPORT_2026-09-28.md`、`GUI_TEST_REPORT_2026-09-28_R5.md`、`gui-test-screenshots/`×7。
  凭证扫描（sk-/api_key/Bearer/env 键赋值）：唯一命中是 "task-only" 误撞 `sk-` 正则，零真实凭证。
  **`SDD_FLOW_REVIEW_2026-09-27.md` 未获用户指示，保持未跟踪。**
- **CI 修复**（`73b87693`）：服务端 gate0 首跑红 = ruff F841（`test_cli_fork.py:145` 未用变量）
  + guards 车道（`test_verification_map.py::test_every_tracked_file_is_covered_by_at_least_one_row`
  抓到 12 个新文件无 map 行）→ `docs/agents/verification.map.tsv` 新增 `gui-test-evidence` 行。
- **审查台账**（`docs/review_ledger.d/`）5 条新行：`420-ff64228d-20e272c3`（主批次双轴独立审查）、
  `420-20e272c3-ce08cc8c`（回显增量，**实现者自审**）、`gui-r5-ce08cc8c-497b5952`（证据入库核查）、
  `ci-a6257995-73b87693`（CI 修复）、`ci-58e7b621-98f8241d`（先回后正合并审计：同改 5 文件
  assembly.py/service.py/web/app.py/api.ts/tracker 两侧改动均存活）。
- **Gate 读数落盘**：`docs/gate/0e465b26*.json`（6/6 PASS）、`docs/gate/c1bb3f73*.json`（合并树 6/6）。
- **门禁读数**：后端 pytest 全量 4739 passed / 2 skipped；前端 vitest 1187 passed；tsc 0 错；
  oxlint 41 warnings（= main 基线）；coverage exit 0。

## 4. 复核命令（可直接跑）

```bash
# 逐笔对账（每条 fix 源码内带 #42x 注释锚点）
git log --oneline ff64228d..7caa5ddd
git show 20e272c3 --stat
grep -rn "#42[0-8]" src/ web/src/ tests/ --include="*.py" --include="*.ts" --include="*.tsx" -l

# 后端全量（⚠ dev 后端必须先停：workspace InstanceLock 冲突会让 flush_lifecycle 假红）
.venv/Scripts/python.exe -m pytest -q

# 前端（必须 cd web；tsc 用 ./node_modules/.bin/tsc，npx tsc 会解析到坏 stub）
cd web && npx vitest run && ./node_modules/.bin/tsc --noEmit && npm run lint

# 机械门禁（用项目 venv 的 python！shell 的 python 可能解析到仓外 venv）
.venv/Scripts/python.exe scripts/gate0.py
.venv/Scripts/python.exe scripts/check_review_coverage.py

# 回显语义独立验证（可选）：initConversation+applyEvent 构造 pending→resolved
# 见 web/src/lib/approvalEcho.test.ts 与 Conversation.approvalEcho.test.tsx
```

## 5. 自知薄弱处（深度检查优先打这里）

1. **两个负载敏感 flake 的归因靠隔离复跑**：`tests/model/test_stall_watchdog.py`（全量唯一
   failed，隔离 16/16 过）、`web/src/components/StepDetail.window.test.tsx`（隔离 6/6 过）。
   归因"负载时序"未做跨机器复现。
2. **CI gate0 首跑红暴露的流程疏漏**：实现者此前只单跑已知车道、没在最终树跑 gate0 本体；
   shell `python` 解析到仓外 Hermes venv 导致读数分叉——"本地绿"当时是环境性假绿。
   该根因已登记台账，但"以后每次跑 gate0 本体"只是纪律不是机制。
3. **#420 AC3 回显是实现者自审**（非独立 subagent 双轴）——主批次 `20e272c3` 是双轴独立审查，
   回显增量只有自查 + 台账声明。
4. **#426 一维交付**：票面"常用三项"只交付 turns 一维；tokens/deadline 的 UI 完全不存在
   （设计票 #435 只是计划，未实现）。关票条件上 #426 保持 open，但"部分交付不关票"的判断
   本身值得复核。
5. **e2e 证据在本机**：审批链 e2e 会话 `4a5bc9a2` 留存本地后端数据，未入库——不可复现于
   检查者环境，只能重跑。
6. **合并审计是自检**：`98f8241d` 的 5 个两侧同改文件核对是实现者做的（非独立审计）。
7. **未跟踪残留**：`SDD_FLOW_REVIEW_2026-09-27.md`（用户未表态）+ 合并前中间树的 FAIL gate
   读数 json（记录审计行落行前的 coverage 红，入库会误导故只留本地）。
8. **远端分支未删**：`fix/r5-issues`、`docs/r5-batch-epilogue`（删除需用户批准）。

## 6. 检查者安全约束（本项目红线，违反即事故）

- `.env` 的值**绝不**打印/提交/复制进文档或命令输出；可列 key 名，不可列 key 值。
- 端口 8000 是用户自己的 Docker Desktop（com.docker.backend.exe / wslrelay.exe）——**不许杀**。
  前端测试用后端静态托管：`.venv/Scripts/uvicorn.exe agent_harness.web.app:create_prod_app
  --factory --host 127.0.0.1 --port 8001`。
- 不许把探针供应商写进全局 `model-providers.json`（会打破 3 条内置目录断言）。
- 只删/改自己的测试数据（会话/记忆）。
- 读中文大文件用 Read/Grep，不要 cat/tail/sed（GBK 控制台乱码）。
- 跨 clone 比较比 git 对象，不比工作树字节（行尾配置不同）。
- 若检查后要提交修复：遵守 AGENTS.md §14（main 服务端保护，只能走 PR）；
  代码提交必须过 `check_review_coverage.py`（补台账行）。
