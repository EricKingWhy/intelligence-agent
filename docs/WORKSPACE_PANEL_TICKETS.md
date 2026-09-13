# Tickets — Workspace Panel

> 母 PRD：`docs/WORKSPACE_PANEL_PRD.md`（待批准）。批准后按序 implement。
> 工作区：**前端票在 `D:\intelligence-agent-frontend`（feat/frontend）**；**T4 后端票在 `D:\intelligence-agent-backend`（feat/backend）**。
> 门禁：前端 `cd web && npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build`；后端 `uv run ruff check . && uv run pytest -q`（§16.6 / §16.1）。

---

## T1 · 中心列改为能力声明的工作面（删除 Split/Preview）（issue #182）

- **工作区**：前端
- **依赖**：无（`GET /api/capabilities` 已存在，`src/agent_harness/web/app.py:902-943`）
- **改动面**：`web/src/App.tsx`（`WORKSPACE_MODES` / `workspaceMode` 状态与按钮行）、`web/src/styles/app.css`（`.workspace-mode*`）、`web/src/lib/`（新增 capabilities 拉取与降级）、`web/e2e/workspace-modes.spec.ts`

**AC**

1. `Split` / `Preview` 两个模式名与相关 CSS 残留**全部删除**（含 `reserved` 分支与 `RESERVED_MODE_NOTE`）；`workspace-modes.spec.ts` 改写为"不再存在禁用占位"的守卫。
2. 中心列 tab 集 = `Chat`（**恒存在**）+ 当前能力 `surfaces` 为 true 的面；`terminal` 为 true 时渲染 Terminal 面（命令输出聚合，见 PRD §4 的 PTY 约束）。
3. **不消费到**能力数据时（请求失败/未就绪）降级为 PRD 缺省语义（`chat`+`timeline`），**Chat 永不因能力数据缺失而消失**。
4. 声明为 false 的面**不渲染**（PRD §6 开放决策 4）；若实现为禁用位，必须给出如实原因且**不存在"点了没事"的路径**。
5. `aria-*` 如实：选中的 tab `aria-selected="true"`，可 Tab 到达、方向键可在 tab 间移动。
6. 新增 e2e：capabilities mock 两组（`terminal:true` / `terminal:false`）各断言 tab 集**恰好**符合声明。
7. **不得**改动三区几何（`app.css:35` 的 `240px | 1fr | 320px`）与 Conversation/Composer 行为。

---

## T2 · Inspector 升级为"清单 + 详情 + peek 升级链"（issue #183）

- **工作区**：前端
- **依赖**：无
- **改动面**：`web/src/App.tsx`（`inspectorOpen` / `focus` / 既有 Main↔Inspector 联动）、`web/src/components/StepDetail.tsx`、`web/src/styles/app.css`、`web/e2e/`

**AC**

1. **清单与详情同框**：Inspector 内在 Timeline 清单中移动选中项时，详情区**实时跟随**（不重新挂载、不丢滚动位置）。
2. **键盘**：`↑`/`↓` 在清单条目间移动；`Esc` 关闭面板（关闭**不卸载**——保持既有冻结语义）；`Space` 快按=保持打开 / 按住=松手关闭（Linear peek 语义）。
3. **鼠标**：点击条目即选中即预览（默认可用，不要求键盘）。
4. **钉住（pin）**：钉住后切换会话或切换选中项**不自动收起**；钉住状态是视图状态，**不持久化**（不变量 #22）。
5. **整页打开（⤢）**：长 trace / 大 diff 可整页宽读；整页态可退回面板态。
6. **可拖宽**：320 → 480；**不持久化**；拖宽不得使中心列低于可用最小宽度。
7. 关闭/钉住/整页均**可 Tab 到达**且有 `aria-*` 状态如实（`aria-pressed`/`aria-expanded`）。
8. 新增 e2e：键位三条（↑↓/Esc/Space）+ 钉住跨会话保持 + 整页往返 + 拖宽不持久化（重载后回 320）。
9. **不得**让 Inspector 与中心列出现同一数据的**两份独立渲染**（PRD §3.3 分工；重复渲染需在此票内收敛，DiffBlock 的收敛在 T5）。

---

## T3 · 补齐 Inspector 的 PERMISSION 与 ARTIFACTS 段（PRD §12）（issue #184）

- **工作区**：前端
- **依赖**：无（数据供给已存在，见 PRD §3.3）
- **改动面**：`web/src/components/StepDetail.tsx`、`web/src/lib/projection.ts`（如需派生）、`web/src/styles/app.css`

**AC**

1. **PERMISSION 段**：显示权限档（来自 `/api/permission-modes` 与会话启动时的 `permission_mode`）、**待审批请求**（`tool/approval-requested`）、**裁决结果**（`permission/resolved`）。无待审批时如实显示"无待审批"，而不是整段消失（该段是运行状态，不是可选内容）。
2. **ARTIFACTS 段**：显示计数 + 最新一条（id/size/mime + 入口）；计数为 0 时如实显示"本会话未产生产物"。
3. **CHECKPOINT 段保持诚实占位**——不得为了"填满八段"而伪造或隐藏（PRD：No fake values）。
4. 段内数据缺失/不可得 → `—` / `Unavailable` / 整段省略，**不得**填 0 或占位数字。
5. 新增 vitest 单测（projection 派生）+ e2e 至少一条：有待审批时 PERMISSION 段出现且能点进审批面。
6. **不得**改动审批的实际提交语义（`POST /approve` 契约不变）。

---

## T4 · 后端：artifact 内容读取 HTTP 接口（issue #185）

- **工作区**：**后端**（`D:\intelligence-agent-backend`，feat/backend）
- **依赖**：无
- **改动面**：`src/agent_harness/web/app.py`、`src/agent_harness/storage/minio_artifact.py`、`tests/web/`（或 `tests/test_web_api.py`）
- **背景事实**（实测）：`ArtifactStore.inspect(artifact_id, *, start_line, end_line, keyword, max_lines, max_chars_per_line) -> ArtifactSlice` 是唯一读入口（`storage/artifact.py:75-90`，三种实现签名一致）；对象 key 为 `{session_id}/{artifact_id}`（`storage/minio_artifact.py:97`、`storage/s3_artifact.py:64`）；**`artifact_id` 是内容哈希**（`sha256(content)[:16]`，`storage/artifact.py:93-95`）→ **跨会话可重复，必须靠 URL 里的 `session_id` 做归属**。

**AC**

1. 新增只读路由 `GET /api/sessions/{session_id}/artifacts/{artifact_id}`，复用 `ArtifactStore.inspect`；支持可选 `start_line` / `end_line` / `keyword` 与**响应体积上限**，响应为 JSON（含是否截断、总行数等如实标记）。
2. **归属校验**：以 URL 的 `session_id` 构造/校验 store 命名空间；**非本会话的 artifact_id 不得可读**（因内容哈希可跨会话重复，这是硬要求）。给出 404/403 的分层语义与理由（参照 `DELETE /api/sessions/{session_id}` 的分层：`web/app.py:1196-1226` + `session/service.py:780-828`）。
3. **路径穿越防御**：`session_id` 走既有 `validate_session_id`（正则 `[A-Za-z0-9_-]+`，`session/service.py:131-144`）→ 422；`artifact_id` 形态校验**必须补齐**——`MinioArtifactStore.load` 当前**没有**形态校验（`storage/minio_artifact.py:103-108`），而 `S3ArtifactStore.load` 有 `[0-9a-f]{16}` 正则（`s3_artifact.py:72-73`）。**两个 provider 的行为必须一致**，并加对应测试。
4. **元数据如实**：`MinIO` 路径不持久化 `source_tool`/`tool_call_id`/`created_at`（`minio_artifact.py:118-127`），S3 路径有。接口**不得**把缺失字段伪造成空串或默认值；缺失即 `null` 或省略，并在响应里区分实现（PRD：No fake values）。
5. artifact 不存在 → 404；`session_id` 不存在 → 404；`session_id` 形态非法 → 422；**不得**把 `store` 的内部异常原样抛给客户端（复用 `http_error` 映射，`web/domain_errors.py:248-258`）。
6. **路由注册顺序**：新 API 必须在 `mount_static` 之前注册（静态资源挂在 `/`，`web/app.py:1448`），否则被静态吞掉。
7. 测试：跨会话读取被拒、非法 `session_id` 422、非法 `artifact_id` 422、不存在 404、正常读取返回内容与标记、超限截断、`keyword` 过滤；`MinIO`/`S3`/`Fake` 三实现的行为一致性（至少断言校验层一致）。
8. **不新增 SessionEvent**、不改 Tool 执行路径、不改 `ArtifactStore` 的既有语义（若必须改抽象，需在 PR 描述里说明理由）。

---

## T5 · 前端：artifact 内容可见 + diff 单一渲染器 + 文案不一致修复（issue #186）

- **工作区**：前端
- **依赖**：**T4**（没有接口就做不了内容）
- **改动面**：`web/src/components/StepDetail.tsx`（`ArtifactsTab`）、`web/src/components/DiffBlock.tsx`、`web/src/lib/toolShapes.ts`、`web/src/lib/projection.ts`、`web/src/types.ts`

**AC**

1. `ArtifactsTab` 从"只显示 id/size/mime"升级为**可点开看内容**（调 T4 的接口）；加载中/失败/不可得三态如实，失败显示后端 detail 原文。
2. `DiffBlock` 的 archived 占位从"复制一句命令"改为**真实可用的查看入口**（打开内容面板/内联展开），并保留复制能力。
3. **收敛 diff 渲染为唯一渲染器**：Inspector `ChangesTab` 内联的 `.diff-cols` 改为调用 `DiffBlock`（`StepDetail.tsx:699-708`），两处视觉与行为一致。
4. **修复文案/正则不一致**：后端外置时产的 marker 是 `use read_artifact(<id>)`（`tooling/overflow.py:115-123`），而前端正则抓的是 `inspect_artifact\(([^)]+)\)`（`lib/toolShapes.ts:105`）——两者**必须**统一（选定一个方向并在两端同步 + 加测试），否则将来的外置 diff 在前端提取不到 `artifactId`。
5. **类型如实**：`types.ts` 中 artifact 相关类型补齐内容接口所需字段（内容、截断标记、元数据可空）。
6. 测试：`StepDetail.test.tsx` / `projection.test.ts` 补内容面板与 marker 提取；**e2e 当前对 artifact/diff 零覆盖**（`e2e/` 全量 grep 无命中），至少新增一条"外置 diff → 打开内容 → 可见"的端到端用例。
7. 内容面板不得成为"第二真相"：渲染的数据必须来自接口/事件投影，不得在本地另存一份可变副本。

---

## 依赖图

```
T4 (后端接口) ──→ T5 (前端内容面板)
T1 (能力声明 tab) ─┐
T2 (peek 升级链)  ─┼─→ 可并行；T2 与 T1 都动 App.tsx / app.css，串行更稳
T3 (补段)        ─┘
```

建议落地顺序：**T4 → T2 → T3 → T1 → T5**（先把后端供给打穿，再动 Inspector，最后动中心列 tab 与内容面板）。
