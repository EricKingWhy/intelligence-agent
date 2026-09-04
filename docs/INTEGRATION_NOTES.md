# 前端 feat/frontend → main 集成备忘

> 本文件供**集成 AI**在合并 `feat/frontend` → `main` 时参考。
> ZCode（本会话）**不执行 merge**——按 AGENTS.md §13，并行开发工作树不互相合并，
> main 集成由集成 AI 在用户批准后执行。

## 1. 分支状态（2026-09-05 更新）

- **分支**：`feat/frontend`（worktree `D:\intelligence-agent-frontend`）
- **HEAD**：`4592fa2` —— **P0 bug 修复，main 需补并**（见下）
- **七阶段提交链**：`6183d3f`（Phase 1）→ `05a1baa`（Phase 2）→ `f7684c6`（Phase 3）→ `e49d6b8`（Phase 4）→ `3ad4a37`（Phase 5）→ `a68a380`（Phase 6）→ `38fc775`（Phase 7）→ 收尾 fix → Gap 消费 `e1689c6` → **bug 修复 `4592fa2`**
- **质量门**：`tsc --noEmit` clean、`vitest run` 86/86 passed、`npm run build` 通过
- **冻结决策来源**：`docs/UI_DESIGN_DECISIONS.md`（七阶段 UI 重设计单一事实源）

> **⚠️ main 补并提示（集成 AI）**：`4592fa2` 修复「模型输出不渲染」P0 bug（用户实测踩中）。
> 根因两层：①`resolveStep` 用 `!== null` 漏掉 JSON 缺键产生的 `undefined` → user/message 与
> model/completed 落进不同 turn；②后端不持久化 `model/started`（stream-only），无工具会话只有
> `model/completed`，此前该事件不回填 segment/activity → `activities.length > 0` 渲染条件不满足
> → 模型文本整块不渲染。带 2 个回归测试。main 上若不补并，**所有无工具纯对话会话的模型回复都不显示**。
> 补并方式：`git checkout main && git merge feat/frontend`（fast-forward 到 `4592fa2`）或
> `git cherry-pick 4592fa2`。补并后重跑 §3 六项验证（2026-09-05 已在 feat/frontend 全绿，见 PHASE_STATUS 时间线）。

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

## 4. 后端集成 Gap（✅ 已落地——feat/backend `db8ea96..8e950e6`，前端已完成消费）

三个 Gap 的字段形状按 [`docs/BACKEND_GAP_PROMPT.md`](./BACKEND_GAP_PROMPT.md) 契约交付，前端消费同批落地（见 git log）：

1. **`usage` / `model` / `cost` 字段** ✅ —— `model/completed.data` 增 `model?` / `usage?`；`run/completed.data` 增 `usage_total?`（权威聚合，覆盖前端对 model/completed 的累计）/ `cost_usd`（费率表未定义，恒 null → 显示「—」）。前端：projection 捕获 + StepDetail MODEL 区块渲染，全缺失时保留空槽提示
2. **`trace_id`** ✅ —— `run/completed.data.trace_id` + `GET /api/sessions` 行级 `trace_id`。当前恒 null（Langfuse Phase 15 才接入）→ StepDetail Trace 行显示「未追踪」灰字（预期降级，非故障）；跳转链接待 Phase 15
3. **首条用户消息** ✅ —— `GET /api/sessions` 行级 `first_user_message`（截断 128）。前端 useSession 零额外请求预填 titlesById；events 扫描保留为 fallback（后端未返回时）

**集成 AI 注意**：验证数据路径前先重启后端进程加载 Gap 代码（此前 :8000 上跑的旧进程 payload 无新字段——前端对此完全优雅降级，已目检确认）。剩余未落地项：checkpoint 元数据 API（StepDetail CHECKPOINT 空槽保留中）、Langfuse 接入（Phase 15）。

## 4.1 安全加固建议（2026-09-05 前端安全审查产出，供集成 AI 参考落地）

前端已完成两项加固：不可信工具输出渲染截断（`truncateForDisplay`，20k 字符上限，防 MB 级 stdout/JSON 冻结 UI）+ 生产 sourcemap 关闭（`vite.config.ts`，不再向静态资源暴露 1.03MB 源码映射）。审查同时确认：**零 XSS sink**（全 src 无 dangerouslySetInnerHTML/innerHTML/eval，markdown 白名单渲染声明属实）、URL 构造已 encodeURIComponent、localStorage 仅两个非敏感枚举键、dist 产物无任何密钥。

**留给后端的一项（Scope Lock：后端 worktree 不归前端会话改）**：建议 FastAPI 静态服务（`src/agent_harness/web/app.py`）为 HTML 响应加 `Content-Security-Policy: default-src 'self'; img-src 'self' data:` 响应头——比 meta 标签可靠，且不影响 Vite dev。当前无注入 sink 故不可利用，属纵深防御。

## 5. 不变量守护（合并前必读）

前端七阶段严守这些不变量，集成 AI 若发现后端新改动违反，应报告而非迁就：

- **#22 Web UI 不维护第二套不可对账 Session 真相**——前端 `lib/projection.ts` 是单一投影源，事件 → 视图模型唯一路径
- **#4 Event ≠ Diagnostic Log**——前端只消费 `SessionEvent`（后端映射为 `AgentEvent`），不读日志
- **#8 Tool Retry 只有 ToolExecutor 一个责任域**——前端 ToolCard 只渲染投影出的 `status: running|success|failed|stopped`，不重试
- **完整保存 ≠ 完整注入**——Artifact 大内容前端只持 ref（`artifact_id` + size + mime），不内联 payload
