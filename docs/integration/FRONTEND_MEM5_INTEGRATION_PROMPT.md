# 集成提示词：MEM-5 #160 —— 前端记忆管理 UI（跨端票的前端半）

> **给集成 AI**：本文件是这一批的**唯一入口**，§0 是可执行摘要。
> 本批**不推送远程**（AGENTS.md §13.2/§14.4）——push 由你执行。
> 本批是**跨端票的前端半**：#160 **不关单**，用 comment 记录（见 §5）。

---

## 0. 可执行摘要

| 项 | 值 |
| --- | --- |
| Worktree | `D:\intelligence-agent-frontend` |
| 分支 | `feat/frontend` |
| 本批 commit | `<本文件落盘后回填>`（HEAD，父 commit = `637bc89`） |
| 后端依赖 | **已就绪且已关单**：MEM-4 #159（`feat/backend`，`GET /api/memories` + `DELETE /api/memories/{id}`）。本批**没有**任何后端改动——无需等后端 |
| 集成动作 | ① 把 `feat/frontend` 合入本地 `main`（§14.9：一次只合一条分支；后端已先行完成）② 真机跑一遍 §3 的 5 步 ③ `main` 上跑 §4 门禁 ④ 关 #160（comment 已写好，见 §5）⑤ push |
| 冲突预判 | 本批只动 `web/**` + 两个 docs 文件；与后端 `src/**`/`tests/**` 无交集。**唯一需注意**：`docs/SDD_WORKFLOW_PROTOCOL.md` 本批把它同步成了 v2（与 `feat/backend` 侧**逐字节相同**）——若 `feat/backend` 已合入 `main`，该文件在第二次 merge 时是 identical-change，**不构成冲突**；`docs/SDD_TICKET_TRACKER.md` 两侧都追加了各自章节，**可能冲突**（按 §14.7 分析，两边内容可同时保留：后端记后端批次、前端记前端批次） |
| 关单 | **#160 不关**（跨端票的前端半 + 代码未合入 `main`），comment 见 §5 |

---

## 1. 这批做了什么

用户诉求（issue #160）：用户能**看见**系统记住了什么，并能把它**删掉**。
只有 API 的"用户入口"等于没有——看不见就无从判断该删哪条。

| 层 | 交付 | 文件 |
| --- | --- | --- |
| 契约 | `MemoryScope` / `MemorySummary` / `MemoryDeleted`（逐字段对齐后端 `web/memory.py`） | `web/src/types.ts` |
| API | `listMemories(limit, offset)` / `deleteMemory(id)` / `MemoryError` / `isMemoryDisabled` / `describeMemoryError` / 窄化 `parseMemory` | `web/src/lib/api.ts` |
| 纯逻辑 | `scopeLabel` / `formatMemoryTime`（解析失败**原样返回**，不伪造）/ `hasMoreAfter` / `withoutIds` | `web/src/lib/memory.ts` |
| 数据 | `useMemories`：打开即拉取（不缓存旧列表）/ offset 分页 + 按 id 去重追加 / 乐观删除 + **两个结局都重拉权威** / 503 与真故障分流 | `web/src/hooks/useMemories.ts` |
| UI | `MemoryPanel`：列表（content + scope chip + 创建时间 + 长正文展开）+ 行内二次确认 + 降级/空/错误/分页四态 | `web/src/components/MemoryPanel.tsx` |
| 入口 | 顶栏 Brain 按钮（`aria-label="记忆管理"`）+ 命令面板「管理记忆」 | `web/src/components/TopBar.tsx` / `web/src/App.tsx` |
| 样式 | 浮层材质与项目浮层共用选择器；记忆专属规则纯新增 | `web/src/styles/app.css` |

### 1.1 三条**故意的**设计选择（评审时请重点看这里）

1. **乐观删除 + 无条件对账**（AC3 的字面要求是"立即反映 + 失败回滚"，不变量 #22 要求"不维护第二套真相"）。
   实现：点确认 → 先把该行藏起来（`hiddenIds`）→ `DELETE` → **无论成功失败都重拉权威列表**：
   - 成功 → 该行由后端的缺席消失（本地集合只是让它在往返期间先不可见）；
   - 失败 → 重拉让该行回来（这就是 AC3 说的"回滚"）+ 错误留在**那一行**的确认条里。
   所以列表的**静止态**永远等于最近一次后端响应。e2e 用"关掉面板再打开 / 整页刷新"证明它不是本地隐藏。
2. **503 是配置状态，不是故障**：`isMemoryDisabled` 把它分流到独立降级态（「记忆未启用」+ 后端原文 +
   `CAPABILITIES` 指引 + **不给重试按钮**），与「读取失败（错误条 + 重试）」「真的没有（空态）」三分。
   不变量 #21 的落地。
3. **`created_at` 零解析**：`types.ts` 保持后端 ISO 原值（`<time datetime>` 原样透传），
   只在展示层 `formatMemoryTime` 本地化；解析失败**原样返回字符串**（显示 `Invalid Date` 或补当前时间
   都是在"记忆是何时记的"上撒谎）。

### 1.2 e2e 逮到并修掉的一个真 product bug（AC4）

列表读取失败且当前 0 行时，分支链漏了 `loadError === null` → 面板**同时**显示
「还没有记忆」与错误条（= 把"读不到"伪装成"没有"，AC4 明令禁止）。
已修为互斥三态；`e2e/s-memories.spec.ts` 的 500 用例（`.memory-empty` count 必须 0）
就是钉它的锁——**首版两视口都红**，不是事后补的测试。

---

## 2. 契约同步点（与后端逐字段）

| 前端 | 后端（`src/agent_harness/web/memory.py`） |
| --- | --- |
| `MemorySummary {id, content, scope, metadata, created_at}` | `MemorySummary`（`public_metadata` 已剥 provider 内部载荷） |
| `MemoryDeleted {id, deleted}` | `MemoryDeleted` |
| `GET /api/memories?limit=50&offset=0` → 数组（按 `created_at` 倒序） | `list_memories`（`limit` 1..200 由后端夹） |
| `DELETE /api/memories/{id}` → 200 `{id, deleted:true}` / **404** 不存在 / **403** 不属于当前入口（含 SESSION 行）/ **503** 未装配 | `forget_memory`（含 `record_forget` 审计：`memory forget via api: forgotten`） |
| 编译期锁：`api.test.ts` 的 `CANONICAL_MEMORY: MemorySummary` 类型注解 fixture（ARCH-4b 套路）——后端加必填字段 → `tsc -b` 红 | 后端侧权威锁（断言**值**）：`tests/web/test_memory_api.py` |

**实测核对的 503 文案**（e2e mock 里那句不是编的）：
`memory capability 未启用：请在 CAPABILITIES 中配置 memory。`

---

## 3. 真机验收（已跑过一遍，集成后请复跑 —— 5 步）

前置：后端 `D:\intelligence-agent-backend`（真 `.env`，`CAPABILITIES` 含 `memory`）起 uvicorn :8000；
前端 `D:\intelligence-agent-frontend\web` 起 `npx vite --port 5173`。

1. **列表**：点顶栏 Brain 图标 → 面板列出后端 `GET /api/memories` 的内容
   （若库里为空，先跑 `.scratch/seed_real_memories.py` 种 3 条；脚本走生产装配，见 §6）。
2. **二次确认**：点某行「删除」→ 行内出现「这是硬删除，**删除不可恢复**——没有回收站…」+
   取消 / 确认删除；点取消 → 行还在（**不发** DELETE）。
3. **删除成功 + 后端权威**：再点删除 → 确认 → 行消失；
   `curl http://127.0.0.1:8000/api/memories` 复查该条不在；后端日志出现 `memory forget via api: forgotten`；
   **整页刷新（F5）后重开面板** → 该条依然不在。
4. **失败回滚**：杀掉后端 → 点确认删除 → 该行**回到列表** + 该行确认条出现错误 + 面板错误条 + 重试；
   重启后端 → 点「重试」→ 列表恢复一致。
5. **降级契约**（可选，需另起实例）：
   `CAPABILITIES={} WORKSPACE_DIR=<tmp> python -m uvicorn agent_harness.web.app:create_prod_app --port 8001`
   → `GET /api/memories` 回 **503** + 上述原文。前端 503 渲染由 e2e 锁定（`memoryDisabled` 分支）。

**验收后请还原环境**：种子记忆是**假事实**，验证完请经界面删掉（否则真模型会把它当真的召回）；
停掉临时 uvicorn（残留进程会占 `.agent/workspace/.instance.lock`，那是已知会把
`test_web_lifespan_flushes_on_shutdown` 打红的坑）。

---

## 4. 门禁（本批实跑结果 + 合并后复跑命令）

```bash
cd web && npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build
```

| 门禁 | 本批结果 |
| --- | --- |
| `npx tsc -b` | 0 error |
| `npx vitest run` | **580 passed**（31 文件；本批 +19：api 11 + memory 7 + 既有基线） |
| `npx oxlint` | **0 error**（38 warnings：37 既有 + 1 条本批与既有同类的 `set-state-in-effect`） |
| `npx playwright test --workers=2` | **160 passed**（本批 +16 = 8 用例 × 2 视口） |
| `npx vite build` | OK（仅既有 chunk-size 提示） |

---

## 5. #160 的 comment 与关单

**本批不关单**（票面明写「跨端 ticket 的前端半 → 按 §14.12 只完成一端不关单，用 comment 记录已完成
部分与剩余项」；本 worktree 与 #155 同一处置）。建议 comment：

```text
前端半已在 feat/frontend 完成（commit <SHA>，父 637bc89），未 push。后端半 MEM-4 #159 已关单。

AC 逐条：
1. 列表视图（content + scope + 创建时间 + 分页"加载更多"）：✅ MemoryPanel + useMemories（offset 分页，
   后端 limit/offset 切片；e2e 55 条 fixture 验证首屏 50 → 加载更多 offset=50 → "已全部加载"）
2. 单条删除必须二次确认且写明"删除不可恢复"：✅ 行内确认条原文含该字样（真机点击复核）
3. 删除后立即反映且与后端权威一致（不得只做本地隐藏）+ 失败回滚：✅ 乐观删除 + 两个结局都重拉权威；
   e2e 用"关掉再打开"、真机用"整页刷新"证明；失败路径真机用杀后端复现（行回来 + 报错）
4. 空态与降级：✅ 三分（503"记忆未启用"不给重试 / 读取失败错误条+重试 / 真空态"还没有记忆"）；
   真机核对 503 原文与 mock 逐字一致；e2e 锁定"500 时不得显示空态"
5. 契约同步：✅ types.ts + api.test.ts 的 CANONICAL_MEMORY 类型注解 fixture（ARCH-4b）
6. e2e 回归锁（--workers=2）：✅ 8 用例 × 2 视口 = 16 例（列表/展开/二次确认+后端权威/失败回滚/
   503 降级/真空态/500 重试/命令面板入口）
7. 门禁：✅ tsc 0 · vitest 580 · oxlint 0 error · playwright 160 · vite build OK

剩余项：合入 main（由集成 AI 执行，见 docs/integration/FRONTEND_MEM5_INTEGRATION_PROMPT.md）。
按 §14.12 本票不自行关闭。
```

---

## 6. 残留 / 未决（只登记，不顺手做）

| 项 | 说明 |
| --- | --- |
| 记忆**编辑** UI | 票面非目标；后端 MEM-4 也没有 update 入口 |
| 批量清空 / 回收站 / 恢复 | 票面非目标（用户已选硬删） |
| 冲突可视化 | 票面非目标（LLM 决策只体现在最终条目上） |
| 记忆搜索 | 本票只做分页列表；"看得见 + 删得掉"是 AC 的全部 |
| 真机 403 路径 | HTTP 入口只列 USER 行，SESSION scope 记忆不可达 → 由 e2e 的 `memoryDeniedIds` 拦截口覆盖（伪造的是真后端会回的那句话） |
| 种子脚本 | `.scratch/seed_real_memories.py`（后端 worktree，`.scratch/` 不入库）；种的是**假事实**，验收后须删 |
| 实测环境观察 | 种子写入时 Zilliz/embedding 不健康 → 3 条都 `degraded=consolidation_failed: VectorStoreError`（按 #158 设计降级为无条件 insert，记录行照落）——**外部依赖问题，非本票缺陷**，记录在案 |
| 协议文件同步 | 本批把 `docs/SDD_WORKFLOW_PROTOCOL.md` 同步为 v2（与 feat/backend 逐字节相同）；其 §4「剩余 Ticket 清单」仍是旧内容（FE-T7/T8/T9，早已 done）——不在本批范围 |
