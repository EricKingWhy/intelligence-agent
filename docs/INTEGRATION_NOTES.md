# 前端 feat/frontend → main 集成备忘

> 本文件供**集成 AI**在合并 `feat/frontend` → `main` 时参考。
> ZCode（本会话）**不执行 merge**——按 AGENTS.md §13，并行开发工作树不互相合并，
> main 集成由集成 AI 在用户批准后执行。

## 1. 分支状态（2026-09-04 收尾）

- **分支**：`feat/frontend`（worktree `D:\intelligence-agent-frontend`）
- **HEAD**：待提交的 Phase 1-7 收尾 fix（本次会话产出的 5 个 Standards/Spec 修复 + 本文档）→ 推送后的新 HEAD 见 `git log -1`
- **七阶段提交链**：`6183d3f`（Phase 1）→ `05a1baa`（Phase 2）→ `f7684c6`（Phase 3）→ `e49d6b8`（Phase 4）→ `3ad4a37`（Phase 5）→ `a68a380`（Phase 6）→ `38fc775`（Phase 7）→ 本次收尾 commit
- **质量门**：`tsc --noEmit` clean、`vitest run` 73/73 passed、`npm run build` 通过
- **冻结决策来源**：`docs/UI_DESIGN_DECISIONS.md`（七阶段 UI 重设计单一事实源）

## 2. 合并策略建议

```
git checkout main
git pull origin main
git merge --no-ff feat/frontend
# 或 rebase（视后端 main 进展而定——若后端有新 commits，先 rebase feat/frontend 到最新 main 再合并）
```

**冲突预期**：

- `web/` 目录是 Phase 9-10 新增，main 已存在精简版 → 本次七阶段是大改写，预期 `web/` 全量替换（以 `feat/frontend` 为准）
- `docs/PHASE_STATUS.md` 两边都在追加时间线 → 保留双方记录，冲突段手动合并
- 后端工作目录 `D:\intelligence-agent-backend` 不受影响（本分支从未触碰）

## 3. 集成后必须验证

```bash
# 后端工作树（D:\intelligence-agent 或 D:\intelligence-agent-backend）
python -X utf8 -m pytest tests/ -q --tb=short

# 前端（合并到 main 后）
cd web
npx tsc --noEmit
npx vitest run
npm run build

# 集成联调（后端 :8000 + 前端 dev :5199 proxy）
cd web && npm run dev
# 浏览器打开 http://localhost:5199
# 手动验证：
#   1. 启动新 session → SSE 流式 token 渲染
#   2. 工具卡片 bash/diff 渲染
#   3. Run Inspector 五 tab 切换
#   4. Trace Density 四档切换 + 刷新后 localStorage 持久
#   5. 历史 session 回放（点 SessionList 行 → 投影重建）
#   6. 主题切换（暗/亮）+ 焦点环 Tab 键可达
```

## 4. 后端集成 Gap（必须由后端 Phase 9 扩展，否则前端有 graceful 空槽）

详见 [`docs/BACKEND_GAP_PROMPT.md`](./BACKEND_GAP_PROMPT.md)——以下三个扩展是前端 UI 重设计的最后一块拼图，缺它们前端会显示"后端未暴露"空槽（不崩溃，但产品体验不完整）：

1. **`usage` / `model` / `cost` 字段**：随 `run/completed` 或 `model/completed` 事件携带，前端 StepDetail MODEL 空槽消费
2. **`trace_id`**：随 session 元数据返回，前端可链到 Langfuse trace（当前前端无渲染点，集成阶段补）
3. **历史 session 列表 payload 携带首条用户消息**：`GET /api/sessions` 返回每行带上 `first_user_message` 字段，前端 SessionList 渲染标题；当前前端通过投影 `getSessionEvents` 提取首条 user/message 作为标题缓存（events 头部扫描），后端直接返回可省一次 events 拉取

## 5. 不变量守护（合并前必读）

前端七阶段严守这些不变量，集成 AI 若发现后端新改动违反，应报告而非迁就：

- **#22 Web UI 不维护第二套不可对账 Session 真相**——前端 `lib/projection.ts` 是单一投影源，事件 → 视图模型唯一路径
- **#4 Event ≠ Diagnostic Log**——前端只消费 `SessionEvent`（后端映射为 `AgentEvent`），不读日志
- **#8 Tool Retry 只有 ToolExecutor 一个责任域**——前端 ToolCard 只渲染投影出的 `status: running|success|failed|stopped`，不重试
- **完整保存 ≠ 完整注入**——Artifact 大内容前端只持 ref（`artifact_id` + size + mime），不内联 payload
