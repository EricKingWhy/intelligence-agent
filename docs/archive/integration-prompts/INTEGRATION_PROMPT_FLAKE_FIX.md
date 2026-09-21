# 集成提示词（后续）：复用手册两个 commit 到 main —— 竞态修复 + 手册订正

> **交付对象：Git Integrator**。承接 `docs/archive/integration-prompts/INTEGRATION_PROMPT_REVIEW_STRUCTURAL.md`。
> 上一轮集成你已完成并 push（`9b95e4f`）——本文件是**之后的两个新 commit**。
> 日期：2026-09-15 ｜ 分支：`feat/backend` clone ｜ **未 push、未 merge**。

---

## 1. 背景：你报的两处偏差都成立，已据此修复

你在集成报告里指出手册两处不准，我独立复核后**确认你的判断全部正确**，并做了修复：

| 你指出的 | 复核结论 | 处置 |
| --- | --- | --- |
| 手册 §2 命令写 `--ours`，与正文"取 backend 侧"相反 | **成立**。frontend clone 里 `HEAD`=`feat/frontend`，`ours` 恰是旧镜像快照，`theirs` 才是含 backend 的 main。你按 stage 内容实证后取 `--theirs` 是对的 | 手册已改为 `--theirs` 并附注缘由 |
| `test_get_queue_and_flush_roundtrip` 隔离 5 跑 4 败，远超手册"约 1/5" | **成立**。我复测到同样量级（且不固定：10 跑批次间在 1/10 ↔ 9/10 之间摆动），说明它根本不是概率问题 | 已定位为**确定性竞态 + 一处假通过**，修复见 §2 |

你对那个 flake 的归因方法（用 `merge-tree` 预演 + 核对合并对 `src/`、`tests/` 改动面）是对的；
唯一的偏差是那句"**0 个文件**"——它是相对**分支 tip**量的（同义反复，必为 0）。
相对**集成前的 main**（`9ce0b47`），后端合并实际改了 `src/`+`tests/` **45 个文件**。
结论不变（该用例的失败机制与合并无关），但口径要更正，否则日后有人照此推断会踩坑。

## 2. 本次新增两个 commit

| commit | 文件 | 说明 |
| --- | --- | --- |
| `5fb016d` | `tests/web/test_multiturn_queue_http.py` | 修掉队列 HTTP 用例的收尾竞态（**只动测试，产品代码零改动**） |
| `12fa471` | `docs/archive/integration-prompts/INTEGRATION_PROMPT_REVIEW_STRUCTURAL.md` | 手册 §2 `--ours` 订正 + §6 改写为真实根因 |

### 修复内容（点成）

1. **起点竞态**：`_wait_idle` **不是只读的**（它 POST `/queue/flush`），且其 idle 回执只意味着
   "没有待投递输入"——`deliver_next_undelivered` 在 pending 为空时直接返回，**根本不看 `get_active`**。
   所以"`run/completed` 已落盘"与"`_drive` finally 调 `run.finish()` 摘掉 active"之间存在窗口，
   该 run 的终态回调会把测试刚 append 的 queued 项**接力投递**掉 → flush 正确返回
   `{"status":"idle"}`，断言却期望 SSE 流。
   **修法**：新增 `_empty_session()`（`POST /api/sessions?launch=false`）作确定性起点。
2. **假通过**：`test_supersede_with_queue_id_still_validated` 与 `..._injected_message_409` 只断言
   `status == 409`，而 `ActiveRunConflict` 与 `SupersedeTargetInvalid` **同为 409**——
   竞态触发的在途冲突会让这两条"验证取代校验"的用例假通过。**修法**：补 detail 断言钉住原因。

### 证据

- 两条目标用例隔离 **各 25 跑全绿**（修复前分别约 1/5、1/2 失败）。
- 全量门禁：`ruff check` clean + **2382 passed / 10 skipped / 0 failed**。
- **变异测试**：把 `deliver_next_undelivered` 改成永不投递 → 目标用例立刻红。
  证明修复后仍能捕获真实投递回归，不是放松断言换来的绿。

## 3. 集成方式（低风险——已核对可干净应用）

这两个 commit 只碰上面两个文件，且**当前 `origin/main` 上这两个文件与 clone 的修复前版本逐字节相同**
（两者 sha256 均为 `6e1745bd` / `eda1c376`）⇒ 不会与 main 上的 frontend 合并成果冲突。

```bash
MAIN=D:/intelligence-agent
BE=D:/intelligence-agent-backend

# 干净树检查（main 工作区有第三方未提交改动 CLAUDE.md——push 只发布 commit，不阻塞）
git -C $MAIN fetch origin
git -C $MAIN status --short

# 方式 A（推荐）：把 clone 的两个 commit 直接拿过来
git -C $MAIN cherry-pick 5fb016d 12fa471        # ← 需要用户批准

# 方式 B：若不想用 cherry-pick，走常规先回后正
#   cd $BE && git merge origin/main（clone 落后 main 的 frontend 合并，需先对齐）
#   再 git -C $MAIN merge <backend-tip>

# 验证
cd $BE && .venv/Scripts/python.exe -m pytest tests/web/test_multiturn_queue_http.py -q
cd $BE && .venv/Scripts/python.exe -m pytest tests/ -q        # 期望 2382 passed / 0 failed
.venv/Scripts/python.exe -m ruff check src/ tests/
git -C $MAIN push origin main                    # ← 需要用户批准
```

> 注意：clone 的 `feat/backend` 目前**不含** main 的 frontend 合并（`334de4b`）。
> 若走方式 B，clone 需先 `git merge origin/main` 对齐；方式 A 直接在 main 上 cherry-pick 更省事，
> 因为只有两个文件、且 pre-image 完全一致。

## 4. 残留项（如实记录，**未修**，不阻塞集成）

共享事件循环下偶发的**整文件级联**：一次 20 跑里出现 1 次，表现为多个用例
`RemoteProtocolError: peer closed connection`（从 `test_supersede_non_latest_user_message_409`
起连锁）。已确认两点：① 它始于**本次未改动**的用例；② 在本次改动前就出现过（旧日志 `fz10.log`）。
⇒ 属测试基础设施的资源竞争，与本修复无关，也不影响全量门禁（集成后 main 全量 0 failed）。

## 5. 落地后请在 `docs/PHASE_STATUS.md` 追加一条记录

`docs/PHASE_STATUS.md` 的唯一事实源在 **main**（你上一轮的集成记录 `9b95e4f` 就在那里）；
clone 上的副本是集成前版本，**不要**从 clone 带 PHASE_STATUS，以免与 main 的记录打架。
请在 main 上追加（格式 §16.5）：

```markdown
- 2026-09-15：**队列 HTTP 用例收尾竞态修复（既有 flake，非产品 bug）**。commit `<cherry-pick sha>`。
  `test_get_queue_and_flush_roundtrip` 原被登记为"约 1/5 既有 flake"，集成 AI 复测 5 跑 4 败
  提出质疑——复核确认是**确定性竞态**（`_wait_idle` 非只读，其 idle 回执只证明"无待投递输入"，
  不证明 run 已收口；测试手工 append 的 queued 被 run 终态回调接力投递）；另发现两条 supersede
  用例只断言 `status==409`，与 ActiveRunConflict 不可区分（假通过）。修法：`_empty_session()`
  作确定性起点 + 补 detail 断言。验证：目标用例隔离各 25 跑全绿；全量 2382 passed / 0 failed；
  变异测试（永不投递）目标用例变红。手册 §2 `--ours` 错写与 §6 描述一并订正。
```

## 6. 我未做、留给你的动作

`git cherry-pick` / `git merge` / `git push` —— **全部未执行**（AGENTS.md §13.2 / §14.4）。
