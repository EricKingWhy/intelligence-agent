# 集成提示词：全分支结构轴复审 + 修复（feat/backend **与** feat/frontend，一篇含两端）

> **交付对象：Git Integrator**（最终在 `D:\intelligence-agent` 的 `main` 上执行合并 + push）。
> 日期：2026-09-15 ｜ 两端工作已完成、门禁全绿、**均未 push、未创建 PR**。
> 按 AGENTS.md §13/§14 的集成纪律：合并与 push 由 Integrator 执行；本 Agent 只做本地 commit。
> **本文件已合并取代**原先的前后端两篇（`INTEGRATION_PROMPT_REVIEW_STRUCTURAL_BACKEND.md` / `..._FRONTEND.md`，均已删除）。

---

## 0. 先看这一段：仓库拓扑（本次集成最容易踩的坑）

三个目录是**独立 clone**，不是同一个 worktree。**`D:\intelligence-agent` 里本地的
`feat/backend` / `feat/frontend` 是过期的**，真实成果在这两个 clone 的本地分支上：

| 目录 | branch | 说明 |
| --- | --- | --- |
| `D:\intelligence-agent-backend` | `feat/backend` | **真实后端成果**（本批 + #194–#204 全部） |
| `D:\intelligence-agent-frontend` | `feat/frontend` | **真实前端成果**（本批 + #194–#204 全部） |
| `D:\intelligence-agent` | `main` | 集成目标；其**本地** `feat/*` 陈旧，**别用来合并** |

**HEAD 请现场解析，不要照抄本文件的数字**（本 Agent 每次补文档都会推进 HEAD）：

```bash
git -C D:/intelligence-agent-backend  rev-parse --short HEAD   # 后端真实 HEAD
git -C D:/intelligence-agent-frontend rev-parse --short HEAD   # 前端真实 HEAD
```

**已核验的关系（本次实测）**：

- main 仓库本地 `feat/backend`(`80d49e1`) / `feat/frontend`(`2b51914`) **都是** 各自 clone HEAD 的祖先
  ⇒ 把 clone HEAD 拉进 main 仓库这两个分支指针是 fast-forward（仅仅是**指针更新**，不是"合进 main"）。
- 同理当前 `origin/feat/backend`(`9727344`) / `origin/feat/frontend`(`3706e9b`) 也都是各自 clone HEAD 的祖先。

⇒ **不要复用 main worktree 里的旧 `feat/*` 分支去 merge**（会漏掉全部新成果）。

### 0.1 ⚠ 合进 main 是**真 merge，不是 fast-forward**

clone HEAD **不是** 当前 `origin/main`(`9ce0b47`) 的子孙：main 上有 backend clone **缺的 11 个 commit**
（#171/#181/#191/#193 的集成记录等）、frontend clone 缺 2 个。所以无论怎么合，都是**三路 merge**。

**已实测预演冲突**（用 `git merge-tree` + 临时仓库完整走了一遍，结论可直接采信）：

| 合并 | 结果 |
| --- | --- |
| `feat/backend` → main | 1 处冲突：`docs/PHASE_STATUS.md` |
| `feat/frontend` → main（**backend 已合入后**） | 2 处冲突：`docs/design/CONTEXT_CAPACITY_DASHBOARD.md`、`docs/design/WEB_UI_BATCH_REDESIGN.md` |

> ⚠ 注意第二行：`feat/frontend` 单独对旧 main 是零冲突，**但 backend 一旦先进 main 就变成 2 处冲突**。
> 这正是 §14.9「backend 合入后，之前对 frontend 的冲突判断全部过期」的实例——不要凭"先前预演过零冲突"就放心。

**全部 3 处冲突都在 `docs/`，没有一处落在代码上**（实测：两侧源码文件全部自动合并成功）。三处解法如下：

1. **`docs/PHASE_STATUS.md`（backend 合入时）**——纯追加日志的 EOF 冲突，也不是语义冲突：
   main 追加了 #171 那条、本分支追加了 #194–#204 与本次复审各条，两侧都在文件尾部追加。
   **解法 = 两段都保留**（main 的 #171 条目 + 本分支的 #194…#204/复审条目），不是二选一，
   **不要**用 `ours`/`theirs` 一把梭。

2. **`docs/design/CONTEXT_CAPACITY_DASHBOARD.md`（frontend 合入时，3 个冲突块）**——两侧都在改 §3.2 桶表与风险表。
   **以 backend 侧为准**：backend 的副本 (a) 已按本批复审的真实成本口径改写，
   (b) 时间在后（`d2aa803`），而 frontend 的副本是 `c00604e` 一次性镜像的**旧快照**。
   已核对：frontend 侧唯一独有的一句"技能与工具 schema 不可精确分割"，backend 侧**已有等价表述**
   （第 31 行"技能目录注入的文本折进 system prompt（与 dsh 归档 'Not separable here' 同一个病"），
   所以取 backend 侧**不丢信息**。

3. **`docs/design/WEB_UI_BATCH_REDESIGN.md`（frontend 合入时，1 个冲突块）**——`launch=False` 的响应形状描述。
   **以 backend 侧为准**：backend 写的是"刻意小形状 `{session_id, permission_mode}`，仓库里不存在
   `GET /api/sessions/{sid}` 单会话路由，不伪造"；frontend 旧快照写的是"同 `GET /api/sessions/{sid}` 的形状"。
   已核验：backend 的路由表里**确实没有**单会话 GET 路由（只有 `/events`、`/stream`、`/context-usage`…），
   所以 backend 侧才是事实，frontend 那句是镜像时对不上的描述。

⇒ 这正是 §14.6「先回后正」的场景：**先在 feature 分支上把 main 合进来、把冲突解掉并复跑门禁**，
再回到 main 做干净的 down-merge。§2 已按此改写；上面三处的裁决就是 §14.7 要求的"逐文件分析"结论。

---

## 1. 本批交付内容

### 1.1 commit 清单

**本批代码 commit（内容锚点，SHA 稳定不变）**：

| 端 | commit | 说明 |
| --- | --- | --- |
| 后端 | `d2aa803` | `refactor(review)`：结构复审修复（used_tokens 漏报 + 去重 + 单一接缝） |
| 前端 | `f766848` | `refactor(review)`：供应商多模型编辑 + 队列条动作带 queue_id |

其后每端还有若干 **docs-only commit**（进度登记 + 本集成提示词），只改 `docs/`，不碰代码。

clone HEAD 相对 `origin/main` 的**完整**增量：backend **22 个 commit**（#196/#195、#200、#202、#198、#203、#204、终审修复、本批复审修复），frontend **28 个 commit**（同批次前端半 + 本批）。即本批是压在这些之上的**增量**，前面各批此前已关单、证据在 `docs/PHASE_STATUS.md` / `docs/SDD_TICKET_TRACKER.md`。

### 1.2 本批性质

复审轴 = **代码整洁度**（重复、死代码、抽象泄漏、注释噪音、命名漂移、真 bug），刻意**不**重复"票面/规格是否满足"（上一轮终审已覆盖）。因此本批**不新增票、不关单**。

---

## 2. 建议的集成流程（§14.6 先回后正；§14.9 一次一个分支；§13.3 先本地 main 验证再 push）

> 每一步的 `merge` / `push` 都需要**用户明确批准**（§14.4）。冲突处理见 §14.7（逐文件分析，禁止机械 `ours`/`theirs`）。

```bash
MAIN=D:/intelligence-agent
BE=D:/intelligence-agent-backend
FE=D:/intelligence-agent-frontend

# ---------- 0) 同步与核查 ----------
git -C $MAIN fetch origin
git -C $MAIN status                                   # 必须 clean，不 clean 就停
git -C $BE   status ; git -C $FE status               # 两个 clone 也必须 clean
# 起始点：origin/main 与 $MAIN 的 main 内容一致（本次实测同为 9ce0b47）

# ================= 后端（先做完，再碰前端；§14.9） =================
# 1) 「先回」：在 backend clone 上把 main 合进来，就地解 §0.1 的 PHASE_STATUS 冲突
cd $BE
git fetch origin
git merge origin/main                    # ← 需要批准；预期仅 docs/PHASE_STATUS.md 冲突
# 解法：两段都留（main 的 #171 条目 + 本分支 #194–#204/复审条目），再：
#   git add docs/PHASE_STATUS.md && git commit
.venv/Scripts/python.exe -m pytest tests/ -q          # 「回」之后必须复跑全量
ruff check .

# 2) 「后正」：把已解冲突的 backend 分支取进 main 仓库并合入
git -C $MAIN fetch $BE feat/backend:feat/backend-new  # 取 clone 最新（含上面的合并提交）
git -C $MAIN merge --no-ff feat/backend-new           # ← 需要批准（此时应无冲突）
git -C $MAIN log --oneline -1 main                    # 确认 backend 成果已进 main

# ================= 前端（backend 稳定后重新分析，§14.9） =================
# 3) 「先回」：⚠ 这里必须合 $MAIN 的 main（**本地**，含刚合入的 backend），
#    不是 origin/main —— 我们尚未 push，origin/main 还是旧的 9ce0b47。
cd $FE
git fetch $MAIN main                     # 把本地 main 取进 FETCH_HEAD
git merge FETCH_HEAD                     # ← 需要批准；预演为零冲突
git diff FETCH_HEAD...feat/frontend --stat | tail -3   # 基于新 main 重看差异
cd $FE/web && npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build

# 4) 「后正」：把前端合进 main
git -C $MAIN fetch $FE feat/frontend:feat/frontend-new
git -C $MAIN merge --no-ff feat/frontend-new          # ← 需要批准

# ---------- 5) 集成后总门禁（在 main 上） ----------
cd $MAIN    # 按仓库既有方式启动，至少跑后端 pytest + 前端构建

# ---------- 6) 通过后才 push ----------
git -C $MAIN push origin main                         # ← 需要批准
```

> 说明：上面用 `feat/backend-new` / `feat/frontend-new` 两个**临时分支名**接收 clone 的最新 HEAD，
> 避免与 main 仓库里已存在、且内容不同的同名旧 `feat/*` 混淆。合并验证完成后可删除临时分支（删分支同样需批准）。

### 2.1 备选（若 Integrator 希望只更新已有分支名）

```bash
# 让 main 仓库的 feat/backend 指向 backend clone 的 HEAD（FF，已核验；逐条需批准）
git -C $MAIN fetch D:/intelligence-agent-backend  feat/backend
git -C $MAIN branch -f feat/backend FETCH_HEAD
# frontend 同理：
git -C $MAIN fetch D:/intelligence-agent-frontend feat/frontend
git -C $MAIN branch -f feat/frontend FETCH_HEAD
```

若 Integrator 更希望走 GitHub PR 流程，则先在两个 clone 里 `push origin <branch>`，再开 PR——
**但 §13.3 的默认是不走这条路**：先本地 `main` 验证，最后才 push。

---

## 3. 冲突仲裁锚点（若 main 上出现冲突，按这些语义裁决）

**后端**：
- `ContextBuilder.usage_snapshot(session)` **只接受 session 一个参数**（本批删掉了旧签名里的 `skills_tokens` 入参）。六个桶各有真实来源，`used_tokens = Σ各桶`——**不要再引入"用总量减别的桶"的残差扣法**。
- `web/context_usage.py::skills_provider_tokens()` **已被删除**（它是复制 `select()` 文本、访问私有面、且跳过预算截断的漂移实现）。若 main 上还有调用方，以删除本函数、改用 `builder.usage_snapshot` 为准。
- `ProviderStore.for_settings(settings)` 是**唯一构造入口**（`builtin_ids` 由 `PROVIDER_PRESETS` 统一推导）。任何直接 `ProviderStore(path, cred, builtin_ids=...)` 的旧写法都应被替代——旧写法有 2 处漏传 presets 会导致 `kind` 派生错误。
- `utc_now_iso()` 是时间戳唯一出口（`model_providers._now_iso` 已删）。
- `web/app.py` 的 SSE 生成统一走模块级 `_run_stream_response(...)`；`STREAM_REPLAY_MAX_EVENTS = 1000` 必须保留。

**前端**：
- 供应商表单的 `models` 是 **`ProviderModelRow[]`（每行 `model_id` + `label`）**，提交前经 `normalizeProviderModels()` 规整（trim、丢空行、按首个去重）。若 main 上还是"整表替换为一行"的旧逻辑，以本分支为准（旧逻辑会**静默删掉用户其余模型和全部 label**）。
- 队列条「立即发送」发 `amend:{mode:'steer', queue_id}`；「编辑」走**行内编辑态**提交 `{content, queue_id}`，**不回填主输入框**；三个操作按钮**仅对 `kind==='queue'` 渲染**（steer 项渲染出来是静默 404）。

**文档合并（§0.1 第 2/3 处冲突专用裁决）**：
- `docs/design/CONTEXT_CAPACITY_DASHBOARD.md` → **取 backend 侧**（backend 已按本批复审口径重写，frontend 是旧镜像快照；frontend 独有的一句在 backend 第 31 行有等价表述，不丢信息）。
- `docs/design/WEB_UI_BATCH_REDESIGN.md` → **取 backend 侧**（frontend 旧快照引用了一个**不存在**的 `GET /api/sessions/{sid}` 路由，backend 侧的描述与真实路由表一致）。

以上均对应 ADR-0030 §5.2 / ADR-0032 §8.1；设计稿 `docs/design/CONTEXT_CAPACITY_DASHBOARD.md` §3.2 的桶表已按新口径重写。

---

## 4. 已验证的门禁（本次实测，均为修复后状态）

| 端 | 命令 | 结果 |
| --- | --- | --- |
| 后端 | `ruff check` | clean |
| 后端 | `pytest tests/`（全量） | **2382 passed / 10 skipped / 42 deselected**（末次全跑 0 失败） |
| 前端 | `npx tsc -b` | clean |
| 前端 | `npx vitest run` | **848 passed**（较上批 +9） |
| 前端 | `npx oxlint` | **0 error / 43 warnings**（全部既有，无一来自本批改动文件） |
| 前端 | `npx playwright test --workers=2` | **346 passed**（含新增 T12c/T12d） |
| 前端 | `npx vite build` | 绿 |

**本批新增回归测试**（旧口径下必红）：
- 后端 `tests/web/test_context_usage.py::test_t4_snapshot_counts_provider_injection` / `::test_t4_snapshot_attributes_skills_separately`（锁"桶 = 真实注入成本、总量 = Σ桶"）；
- 前端 `web/src/lib/providerModels.test.ts` 6 例 + Composer 队列条 3 例（含"steer 项不渲染这三个操作"）+ e2e `T12c`（立即发送请求体带 `queue_id`）/ `T12d`（就地编辑不回填主输入框）。

---

## 5. 已知环境注意点

- **依赖**：`pyproject.toml` 需 `keyring>=24`。backend venv 已装；main 合并后若缺：
  `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY all_proxy= uv pip install --python .venv/Scripts/python.exe "keyring>=24"`
- **代理对 gh 的影响**：`gh` 需 `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy gh ...` 前缀。
- **测试禁忌（安全）**：任何 provider / 凭据测试**必须**注入 `MemoryCredentialStore`（fixture 注入）。曾实测把假 key 写进**真实 Windows 凭据管理器**并跨用例泄漏（已清理）。合并后跑测试若见凭据管理器有新条目，说明该注入丢失，需停下排查。
- **e2e 必须 `--workers=2`**（4 worker 全量并行有资源竞争抖动）。

---

## 6. 已知 flake（既有，**非本批引入**，合入时按 flake 重跑处理）

`tests/web/test_multiturn_queue_http.py::test_get_queue_and_flush_roundtrip` 约 1/5 概率失败，
两种表现：flush 未返回 SSE 流 / 期望 `200` 得到 `409`。

**归因证据**：本批改动了 flush 的 SSE 路径，为排除嫌疑，特在**修复前 commit `552a5e5`**
建临时 worktree 复现（8 跑 1 败）后删除 → 证明是**既有 flake**，与本批无关。

其余既有抖动：`test_multiturn_queue_http.py::test_edit_queued_item_cancel_old_then_queue_new`、
e2e `r-project-groups.spec.ts:139`（单跑恒绿，全量偶发）。

---

## 7. 遗留债务（复审明确记录、**未修**，不阻塞集成；§8 Scope Lock）

- 前端 `web/src/hooks/useSession.ts`：流前置代码 4 处（≈715/786/845/1123 行）——逐处比对**语义实质不同**，非机械重复，轻率合并会改变重放/取消语义，故不动。
- 前端 `getContextProviders`：导出后无调用方（端点仍在），属**既有显式决定**，保留不删。
- 后端 `SystemCredentialStore.available()` 的 backend 类名字符串匹配（对 keyring 版本敏感）；`_classify_failure` 的状态码子串匹配可被 URL/模型名误命中。
- 后端 `X-Permission-Mode` 头只在 create SSE 有，`/messages`、`/queue/flush` 的 SSE 没有（pill 契约只锚创建响应，语义成立但不对称）——文档化不对称，刻意未改。
- 本批**未新增 CSS token**，`§15` 的 `:root` / `[data-theme='light']` 双块同步规则不涉及。

---

## 8. 安全 / 边界确认（供 Integrator 复核）

- 本批**未触碰任何密钥写入路径**；未新增/修改凭据相关代码（仅 `ProviderStore` 构造收敛，不改密钥存储语义）。
- 本批**未改变** `api_key` 相关契约：响应/错误/日志仍不含 `api_key` 值字段或 `sk-` 子串，`has_api_key` 仍是唯一状态通道（#203/ADR-0032 约束保持）。
- 本批为**纯复审修复**，无架构级改动、无规格变化。

---

## 9. 我（本 Agent）明确未做、留给 Integrator 的动作

`git merge` / merge 冲突解决 / `git push` / 创建 PR / 删除分支或 worktree —— **全部未执行**（AGENTS.md §13.2 / §14.4）。上述 §2 流程即交付路线，需用户逐步批准。
