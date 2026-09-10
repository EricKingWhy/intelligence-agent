# SDD 工作流协议（防指令漂移）

> 本文件是**跨上下文窗口的持久化指令**。任何 Agent 进入新 context window 时，
> 必须先读本文件 + `docs/SDD_TICKET_TRACKER.md`，恢复完整 SDD 上下文。
>
> 创建原因：长任务（多 ticket × SDD 循环）会跨越多个 context window。
> 没有持久化协议，每个新窗口会丢失前序工作状态，导致：
> - 指令漂移：忘记用 `/implement`、忘记 `/code-review`
> - 误差累积：跳过修复步骤、ticket 状态混乱
> - 重复工作：不知道哪些 ticket 已完成

---

## 1. 核心规则

### 1.1 每个 Ticket 的 SDD 循环

```
┌─────────────────────────────────────────────────────┐
│  Ticket SDD 循环（不可跳过任何步骤）                  │
├─────────────────────────────────────────────────────┤
│                                                       │
│  1. /implement  ← 用 implement skill 实现 ticket     │
│       ↓                                               │
│  2. /code-review ← 用 code-review skill 审查         │
│       ↓                                               │
│  3. 有 bug？ → 修复 → 回到步骤 2                      │
│       ↓                                               │
│  4. 无 bug → ticket 完成 → 更新 Tracker              │
│       ↓                                               │
│  5. 进入下一个 ticket                                 │
│                                                       │
└─────────────────────────────────────────────────────┘
```

### 1.2 全部 Ticket 完成后

```
1. /improve-codebase-architecture ← 扫描深化机会
2. 出现问题 → 修复 → /code-review
3. 无问题 → 写集成 AI 交接提示词
```

### 1.3 遇到不确定时

- 使用 `/ask-matt` skill 提问
- 不要猜测、不要自行决定架构方向

### 1.4 推送规则

- **禁止推送到远程 GitHub**
- 集成 AI 负责推送
- 完成后写提示词给集成 AI

---

## 2. Context Window 恢复协议

任何新 context window 启动时，按以下顺序恢复：

1. 读 `docs/SDD_WORKFLOW_PROTOCOL.md`（本文件）
2. 读 `docs/SDD_TICKET_TRACKER.md`（当前进度）
3. 读 `AGENTS.md` §16（SDD 工作流引用）
4. 根据 Tracker 中「下一个待处理 ticket」继续工作

### 2.1 恢复时的自检清单

- [ ] 当前在哪个 worktree？（`git worktree list`）
- [ ] 当前在哪个分支？（`git branch --show-current`）
- [ ] HEAD 是哪个 commit？（`git log --oneline -1`）
- [ ] 工作树是否干净？（`git status --short`）
- [ ] 上一个完成的 ticket 是哪个？（查 Tracker）
- [ ] 下一个要做的 ticket 是哪个？（查 Tracker）
- [ ] 是否有未完成的 `/code-review` 修复循环？（查 Tracker）

---

## 3. Ticket Tracker 格式

`docs/SDD_TICKET_TRACKER.md` 记录每个 ticket 的状态：

| 字段 | 说明 |
| --- | --- |
| Ticket ID | 如 FE-T7 |
| 描述 | 一句话说明 |
| 状态 | `pending` / `in_progress` / `code_review` / `fixing` / `done` |
| 实现方式 | 用了什么 skill、改了哪些文件 |
| Code Review 结果 | 通过/不通过 + 发现的问题 |
| 修复记录 | 每轮修复的内容 |
| Commit SHA | 完成时的 commit hash |
| 门禁结果 | tsc/vitest/oxlint/playwright/build |

---

## 4. 剩余 Ticket 清单

根据后端交接手册 `HANDOFF_FRONTEND_T7_T9.md`，剩余前端 ticket：

| Ticket | 描述 | 后端依赖 |
| --- | --- | --- |
| FE-T7 | 会话级模型切换 + Fork UI | T7 #137（POST /model, POST /forks, model/changed 事件） |
| FE-T8 | 崩溃恢复 UI — run/interrupted + 409 守卫 | T8 #138（run/interrupted 事件, 409 UNKNOWN 守卫） |
| FE-T9 | 轮次标签（turn_index 显示） | T9 #139（RUN_STARTED data.turn_index） |

### 处理顺序

FE-T7 → FE-T8 → FE-T9 → /improve-codebase-architecture → 写集成 AI 交接

---

## 5. 禁止事项

1. **禁止跳过 /code-review**：即使代码看起来没问题，也必须走 review 流程
2. **禁止跳过 /implement**：即使 ticket 很小，也必须用 implement skill
3. **禁止推送到远程**：本地 commit 可以，push 不行
4. **禁止自行决定架构**：遇到架构决策，用 /ask-matt
5. **禁止修改后端仓库**：后端归 feat/backend 会话
6. **禁止跳过门禁**：每个 ticket 完成后必须跑 tsc + vitest + oxlint + playwright + build
