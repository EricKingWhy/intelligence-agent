# SDD 工作流协议（防指令漂移）

> ## ⚠️ 自愈条款（最高优先级，先读这段）
>
> **任何时候你发现自己**：
> （a）不确定当前在循环哪一步；
> （b）不记得批量审查的 fixed point 或批次边界；
> （c）上下文刚被压缩 / 摘要过；
>
> **第一动作 = 立即重读 `docs/SDD_WORKFLOW_PROTOCOL.md` + `docs/SDD_TICKET_TRACKER.md` 恢复状态。
> 禁止凭记忆猜测流程继续施工。**

> 本文件是**跨上下文窗口的持久化指令**。任何 Agent 进入新 context window 时，
> 必须先读本文件 + `docs/SDD_TICKET_TRACKER.md`，恢复完整 SDD 上下文。
>
> 创建原因：长任务（多 ticket × SDD 循环）会跨越多个 context window。
> 没有持久化协议，每个新窗口会丢失前序工作状态，导致：
> - 指令漂移：忘记用 `/implement`、忘记批量审查
> - 误差累积：跳过修复步骤、ticket 状态混乱
> - 重复工作：不知道哪些 ticket 已完成
>
> **协议版本**：v2（新版批量审查循环，2026-09-12 切换；v1 的"每票一次 `/code-review`"已作废）。
> 切换记录与 fixed point 见 `docs/SDD_TICKET_TRACKER.md` 的「流程切换 + 批次记录」章节。
>
> **与根规则文件的关系（2026-09-16 明确）**：本文件是 SDD 执行流程的**唯一权威**。
> `AGENTS.md` §16 只是入口与触发表述，**不复制流程细节**。两者若出现不一致，**以本文件为准**。
> （补写原因：v1→v2 切换时本文件只声明了"v1 已作废"，从未声明它取代 `AGENTS.md` §16，
> 导致根文件里那份 v1 副本长期与新协议并存、且因为根文件是自动加载的而实际胜出。）

---

## 1. 核心规则（v2：批量审查循环）

```
┌──────────────────────────────────────────────────────────────┐
│  新版批量审查循环（每个 ticket）                              │
├──────────────────────────────────────────────────────────────┤
│  A. 单 ticket：/implement → 门禁全绿 → commit → 记 Tracker    │
│      （**跳过**该票自带的 /code-review，改为批量审）           │
│       ↓                                                       │
│  B. 每 2–3 个 ticket：对本批累计 diff 跑一次 /code-review     │
│      fixed point = 上一批审查结束时的 commit                  │
│       ↓                                                       │
│  C. findings：能定位的直接最小修复 + 跑测试；疑难才 diagnose   │
│      修复后**不重跑全量 review**（跑测试 + 自查 diff 即可）    │
│      仅当改动触及架构/契约 → 只对上一轮 findings 做增量复查    │
│       ↓                                                       │
│  下一批                                                        │
│       ↓                                                       │
│  D. 全部 ticket 完成：对整条分支跑最终全量 /code-review        │
│      fixed point = main → findings 按 C 的方式修完 = 任务结束  │
└──────────────────────────────────────────────────────────────┘
```

### 1.1 单 ticket 执行

1. `/implement` 完成当前 ticket。**跳过其收尾自带的 `/code-review`**（本流程改为批量审查，避免同票双审）。
2. 每个 ticket 完成后自行 commit（**门禁全绿才 commit**——后端 `ruff check` + 全量 `pytest`；
   前端五件套，命令见 §5 第 6 条。commit message 描述工程事实），并在
   `docs/SDD_TICKET_TRACKER.md` 追加记录。

### 1.2 批量审查（每 2–3 个 ticket 一次）

3. 每完成 2–3 个 ticket（批大小按 ticket 体量自定；遇到依赖链断点等自然分界可提前收批），对这批
   tickets 的**累计 diff** 跑一次 `/code-review`；**fixed point = 上一批审查结束时的 commit**。
4. findings 分级处理：
   - **一眼能定位的** → 直接最小修复 + 跑测试，不停顿；
   - **真正疑难的**（无法稳定复现 / 间歇性 / 回归）→ 才用 `/diagnosing-bugs` 定位根因后修复。
5. 修复后**不重跑全量 review**：跑测试 + 自查 diff 确认 finding 消除即可；**仅当修复触及架构或契约时**，
   才做**增量复查**（只复查上一轮 findings）。确认无误后 commit 修复，并把 Tracker 中本批审查状态推进到
   修复 commit，进入下一批。

### 1.3 总门禁（全部 ticket 完成后）

6. 对整条分支跑一次**最终全量** `/code-review`；**fixed point = `main`**。
7. findings 全部按 §1.2 第 4、5 条的方式修复并验证后，任务才算结束。

### 1.4 过渡条款（切换时正在施工的票）

- 旧版循环下**已完成并 commit** 的 tickets 一律承认有效：不再补审、不重跑。
- 切换时正处于中间步骤的票（做到一半 / 旧循环遗留未修 findings）：**按旧规则把当前这一步收尾**
  （修复、测试全绿、commit），落盘后立即切到 v2；**不要**为已收尾的工作开新的 review。
- 每个 ticket 的**第一个动作** = 重读本文件 + Tracker（把重读变成强制步骤，不靠自觉）。

### 1.5 遇到不确定时

- 使用 `/ask-matt` skill 提问
- 不要猜测、不要自行决定架构方向
- 仅当出现**规格实质冲突**或**架构分叉**时才停下来问用户

### 1.6 推送规则

- 实现线默认只做**本地 commit**，不在 feature 分支上 `git push`；
- **集成**与 **`push origin main`** 由**当前主开发**执行（用户 2026-09-16 常设授权，
  不必每次重新批准）。前置条件：集成后门禁全绿（本文件 §1.3 + `AGENTS.md` §14.10）；
- 集成完成后必须通知另一条线（见 `AGENTS.md` §14.9），否则"三方一致"会当场破功。

---

## 2. Context Window 恢复协议

任何新 context window 启动时，按以下顺序恢复：

0. **先执行上面的自愈条款**（不确定就重读，不猜）
1. 读 `docs/SDD_WORKFLOW_PROTOCOL.md`（本文件）
2. 读 `docs/SDD_TICKET_TRACKER.md`（当前进度 + 批次 fixed point）
3. 读 `AGENTS.md` §16（SDD 工作流引用）
4. 根据 Tracker 中「下一个待处理 ticket」继续工作

### 2.1 恢复时的自检清单

- [ ] 当前在哪个 worktree？（`git worktree list`）
- [ ] 当前在哪个分支？（`git branch --show-current`）
- [ ] HEAD 是哪个 commit？（`git log --oneline -1`）
- [ ] 工作树是否干净？（`git status --short`）
- [ ] 上一个完成的 ticket 是哪个？（查 Tracker）
- [ ] 下一个要做的 ticket 是哪个？（查 Tracker）
- [ ] **本批**已攒了几个 ticket？**上一批审查结束时的 commit（fixed point）**是哪个？（查 Tracker）
- [ ] 是否有**未修完的批次 findings**？（查 Tracker）

---

## 3. Ticket Tracker 格式

`docs/SDD_TICKET_TRACKER.md` 记录每个 ticket 的状态：

| 字段 | 说明 |
| --- | --- |
| Ticket ID | 如 FE-T7 |
| 描述 | 一句话说明 |
| 状态 | `pending` / `in_progress` / `done` / `batched`（已进批待审）/ `reviewed` |
| 实现方式 | 用了什么 skill、改了哪些文件 |
| Commit SHA | 完成时的 commit hash |
| 门禁结果 | 前端：tsc/vitest/oxlint/playwright/build；后端：ruff/pytest（+ 真机 gate/变异，若有） |

**批次记录**（v2 新增，防漂移的关键）：

| 字段 | 说明 |
| --- | --- |
| 批次号 | 如 `B1`、`B2` |
| 本批 tickets | 这批包含哪几个 ticket |
| fixed point | 本批累计 diff 的起点 commit（= 上一批审查结束时的 commit） |
| 审查结论 | 轮次 + findings 分级 + 是否零 finding |
| 修复 commit | findings 修完后的 commit（下一批的 fixed point） |

---

## 4. 剩余 Ticket 清单

**不在此维护静态清单**——它会在每个 ticket 完成后立刻过期（本文件 2026-09-10 曾在此写下
FE-T7/T8/T9 三张票，该阶段早已结束，而清单留在这里一直被当成"当前剩余工作"）。

当前剩余工作唯一来源：

- `docs/SDD_TICKET_TRACKER.md` —— 在途 ticket、批次、fixed point、审查结论；
- `docs/PHASE_STATUS.md` —— Phase 状态与集成证据；
- GitHub Issues（`EricKingWhy/intelligence-agent`）—— 票面与验收标准。

---

## 5. 禁止事项

1. **禁止跳过批量审查**：每 2–3 票必须对累计 diff 跑一次 `/code-review`；最终还必须跑一次全量
   （fixed point = main）。（单票内的收尾 review 按 v2 §1.1 跳过，不算"跳过审查"。）
2. **禁止跳过 `/implement`**：即使 ticket 很小，也必须用 implement skill
3. **禁止在 feature 分支上推送**：本地 commit 可以；`push origin main` 属集成动作，按 §1.6 执行。
4. **禁止自行决定架构**：遇到架构决策，用 `/ask-matt`
5. **禁止跨仓库无授权写入**：`D:\intelligence-agent`（main）、`D:\intelligence-agent-backend`、
   `D:\intelligence-agent-frontend` 是**三个独立 clone**（不是 worktree），各有自己的工作树与分支。
   用户可以授权任意一条线做另一端的活，但**动手前必须先读 `AGENTS.md` §13 的仓库模型**——
   "三方一致"靠"谁集成谁通知、另一条线开工前先把 main 合回来"维持，不靠目录名分工。
6. **禁止跳过门禁**：后端 `ruff check` + 全量 `pytest`；前端（在 `web/` 下）
   `npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build`。
   e2e 必须 `--workers=2`（4 worker 全量并行存在资源竞争型抖动）。
7. **禁止凭记忆猜流程**：不确定就执行自愈条款（重读本文件 + Tracker）
