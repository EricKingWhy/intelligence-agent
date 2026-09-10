# 集成 AI 提示词 — feat/frontend → main

> **给集成 AI（Git Integrator）的执行提示词。**
> 按 AGENTS.md §14 集成规则执行；merge / push 需用户明确批准。

---

## 0. 任务

将 `feat/frontend` 分支合入 `main`。

**内容**：T7 #137 + T8 #138 + T9 #139 前端实现

| commit | 内容 |
| --- | --- |
| `6012414` | chore(web): 重新生成 event-types.ts——新增 MODEL_CHANGED + RUN_INTERRUPTED |
| `71c01dd` | feat(web): T7 会话级模型切换 + Fork UI（#137） |
| `c6e6fab` | fix(web): code-review 修复——CSS 变量 + 响应 model_id + 移除 T8 范围 |
| `c137a23` | feat(web): T8 崩溃恢复 UI — run/interrupted + 409 守卫（#138） |
| `d2bfbc8` | feat(web): T9 轮次标签——turn_index 显示（#139） |
| `3e71b33` | docs: 架构评审——5 个深化候选 + Top Recommendation |

### 变更文件清单

```
web/src/generated/event-types.ts  — 新增 MODEL_CHANGED + RUN_INTERRUPTED
web/src/lib/api.ts                — 新增 changeSessionModel() + forkSession()
web/src/lib/projection.ts         — MODEL_CHANGED 投影 + RUN_INTERRUPTED 终态
web/src/hooks/useSession.ts       — useSession 暴露 changeModel/fork 操作
web/src/App.tsx                   — handleModelChange 触发 POST /model；handleFork 跳转 child
web/src/components/Conversation.tsx — 用户消息上添加「分叉」按钮
web/src/styles/app.css            — fork-btn + interrupt-banner 样式
web/src/types.ts                  — ConversationState 新增 run_interrupted + turn_index
docs/ARCHITECTURE_REVIEW.md       — 架构评审文档（5 个深化候选）
```

---

## 1. Git 拓扑

| 项 | 值 |
| --- | --- |
| Worktree | `D:\intelligence-agent-frontend` |
| Branch | `feat/frontend` |
| HEAD | `3e71b33` |
| `main` | `c5149ad`（已含后端 T7+T8+T9 批次） |
| merge-base(`HEAD`, `main`) | `46990fb` |
| ahead / behind | **6 / 0**（main 已包含所有后端依赖） |

### 冲突预判：无

对 merge-base 两侧的变更路径取交集：

```
后端侧：src/agent_harness/**, tests/web/**, docs/**
前端侧：web/**, docs/ARCHITECTURE_REVIEW.md
交集：空集
```

**预期 merge 干净。**

---

## 2. 门禁证据

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| Type check | `npx tsc -b` | exit 0，无输出 |
| 单元测试 | `npx vitest run` | **25 files / 377 tests passed** |
| Lint | `npx oxlint` | **0 errors / 35 warnings**（全是既有） |
| e2e | `npx playwright test --workers=2` | **46 passed** |
| 生产构建 | `npx vite build` | ✓ built in 844ms |

---

## 3. 契约接触面

### 3.1 新增 API 函数

```typescript
// POST /api/sessions/{id}/model
changeSessionModel(sessionId, provider, modelId): Promise<ModelChangeResult>

// POST /api/sessions/{id}/forks
forkSession(sessionId, fromSeq): Promise<ForkResult>
```

### 3.2 新增事件投影

| 事件 | 投影行为 |
| --- | --- |
| `model/changed` | 更新 `conversation.model` 为 `data.to_model_id` |
| `run/interrupted` | `finalizeRun` 标记为终态；存储 `run_interrupted` 信息 |

### 3.3 新增 UI

- ModelPicker 选择后调 `POST /api/sessions/{id}/model`（而非仅本地状态更新）
- 用响应里的 `model_id` 更新本地状态
- 监听 SSE 事件流中的 `model/changed` 事件，更新 UI 显示当前模型
- Fork 选择器：在历史用户消息上提供 fork 入口
- 调 `POST /api/sessions/{id}/forks` body `{from_seq}`
- 成功后跳转到 child session
- 中断横幅：显示「上次运行在第 N 步中断」

### 3.4 明确未动的部分

- SSE 帧形状 / seq 投影 / 消费机器**零改动**
- 未迁 WS
- `permission_mode` 不在 `/messages` 的 amend 契约内，未传

---

## 4. 集成步骤

```text
1. 前置检查
   git worktree list --porcelain
   git -C D:/intelligence-agent-frontend status --short
   git -C D:/intelligence-agent-frontend log --oneline -1

2. 先回后正（§14.6，需用户批准）
   git -C D:/intelligence-agent-frontend fetch origin --prune
   git -C D:/intelligence-agent-frontend merge main
   # 预期无冲突（§1）；若冲突 → 立即停止，按 §14.7 逐文件分析

3. 在 feat/frontend 上复跑门禁（§2 五条命令）

4. 合入 main（§14.4，需用户批准）
   git -C D:/intelligence-agent merge feat/frontend

5. main 上验证
   复跑门禁 + 在 D:\intelligence-agent 起完整项目做前后端联调

6. push（§14.4，需用户批准）
   git -C D:/intelligence-agent push origin main
```

**禁止**：`git pull`、在 dirty worktree 上 merge、`reset --hard`、`rebase`、`push --force`、未经批准删分支/worktree。

---

## 5. 后端依赖确认

本批前端实现依赖以下后端已完成的功能：

| 后端功能 | 后端 commit | 前端消费方式 |
| --- | --- | --- |
| `POST /api/sessions/{id}/model` | `ae553ad` (T7) | `changeSessionModel()` |
| `POST /api/sessions/{id}/forks` | `ae553ad` (T7) | `forkSession()` |
| `model/changed` 事件 | `ae553ad` (T7) | projection.ts MODEL_CHANGED case |
| `run/interrupted` 事件 | `ccebf9a` (T8) | projection.ts RUN_INTERRUPTED case |
| `RUN_STARTED.data.turn_index` | `c438a1e` (T9) | projection.ts RUN_STARTED case |

所有后端依赖已在 `main` 中（`c5149ad`）。

---

## 6. 未完成项 & 后续 ticket

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| T7 前端 UI | ✅ 完成 | 模型选择器、fork 选择器、model/changed 事件监听 |
| T8 前端 UI | ✅ 完成 | 中断提示 UI、409 人工裁决守卫 |
| T9 前端 UI | ✅ 完成 | 轮次标签（turn_index 显示） |
| 架构深化候选 | 📋 已记录 | 见 `docs/ARCHITECTURE_REVIEW.md`，Top Recommendation: Candidate #2 |
| A.4 compaction 子 span | ⬜ 后续迭代 | 既有 context_build_completed 已传 compacted_turn_count；独立 compaction 子 span 未加 |
| 架构深化候选 | ⬜ out-of-scope | `_drive` 分解、`SessionService` 拆分、Recovery 流程整合（§8 Scope Lock） |

---

## 7. 集成 AI 注意事项

1. **`web/src/generated/event-types.ts` 是生成物**——合入后需确认前端 worktree 同步到最新版本
2. **`AGENTS.md` §16 SDD 工作流协议**——本次新增的防指令漂移机制，请勿删除
3. **`CONTEXT.md` 更新**——`run/interrupted` 术语、Run 边界更新已写入
4. **未推送远程**——等你交给集成 AI 处理
