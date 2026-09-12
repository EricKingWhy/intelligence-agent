# SDD Ticket Tracker

> **持久化活文档** — 跨 context window 追踪 SDD 循环进度。
> 每次进入新 context window 时，先读本文件恢复状态。

---

## 流程切换 + 批次记录（v2 批量审查循环）

> **自愈条款**：不确定当前在循环哪一步 / 不记得 fixed point 或批次边界 / 上下文刚被压缩过
> → **第一动作**：重读 `docs/SDD_WORKFLOW_PROTOCOL.md` + 本文件，禁止凭记忆猜流程继续施工。

| 项 | 值 |
| --- | --- |
| 协议版本 | `docs/SDD_WORKFLOW_PROTOCOL.md` **v2**（批量审查循环） |
| 切换日期 | 2026-09-12 |
| 切换生效 commit | `4a9f2dc`（切换时点）→ 旧循环遗留的 #158 于 `28c35e7` 收尾；首批批量审查 fixed point = **`28c35e7`** |
| 旧循环已完成票据 | 一律承认有效，**不补审、不重跑**（旧版每票 review 视为已覆盖） |
| 切换时的在途票据 | #158（MEM-3）：按旧规则把当前一步收尾（修复 + 测试全绿 + commit），落盘后即切 v2，**不为它开新 review** |

### 批次台账

| 批次 | 本批 tickets | fixed point（累计 diff 起点） | 审查结论 | 修复 commit（下一批 fixed point） |
| --- | --- | --- | --- | --- |
| B-1 | 待定（下一票 = #160 MEM-5 前端记忆管理 UI） | **`28c35e7`**（#158 收尾 commit——切换时的在途票据，其旧版两轴 review 已完成并修复，**不计入本批审查范围**；本批从 `28c35e7` 之后的新 ticket 起算） | 未审 | — |

**批次边界规则（v2 §1.2）**：每攒满 2–3 个 ticket（或遇到依赖链断点）即收批；收批时对
`git diff <fixed point>..HEAD` 跑一次两轴 `/code-review`（Standards + Spec，两个独立只读子代理）。

## 当前状态

| 项 | 值 |
| --- | --- |
| Worktree | `D:\intelligence-agent-frontend` |
| Branch | `feat/frontend` |
| 协议版本 | `docs/SDD_WORKFLOW_PROTOCOL.md` **v2**（批量审查循环；v1 的「每票一次 /code-review」已作废） |
| 后端交接手册 | 本轮：`D:\intelligence-agent-backend\docs\HANDOFF_FRONTEND_RECOVER_FORK_SCROLL.md`（A/B/C/D） |
| 集成交接提示词 | 本轮：`docs/integration/FRONTEND_REFRESH_PERSIST_INTEGRATION_PROMPT.md`（**集成 AI 的唯一入口**，§0 是可执行摘要）；上一批：`docs/integration/FRONTEND_RECOVER_FORK_SCROLL_INTEGRATION_PROMPT.md` |
| 本批交接手册 | `docs/HANDOFF_APPROVAL_CARD_COVERAGE.md`（做了什么 + 8 个坑点 + 未决项 + 复核命令） |
| 下一批提示词 | `docs/PROMPT_FRONTEND_NEXT_BATCH.md`（可直接复制给前端 Agent：OBS-015 修复为主） |

**禁止推送远程**（AGENTS.md §13.2/§14.4）：本地 commit 已完成，push 归集成 AI。

---

## ⚠️ 流程切换 + 批次记录（v2 批量审查循环，2026-09-12）

**自愈条款（先读这段）**：任何时候你发现自己（a）不确定当前在循环哪一步，（b）不记得批量审查的
fixed point 或批次边界，（c）上下文刚被压缩 / 摘要过 —— **第一动作 = 立即重读
`docs/SDD_WORKFLOW_PROTOCOL.md` + 本文件恢复状态；禁止凭记忆猜测流程继续施工。**

| 项 | 值 |
| --- | --- |
| 协议版本 | `docs/SDD_WORKFLOW_PROTOCOL.md` **v2**（本 worktree 已同步为 v2；v1 作废） |
| 切换日期 | 2026-09-12（切换动作发生在后端 worktree；v2 协议文件随本批同步到本 worktree） |
| 本 worktree 的批次起点 | `637bc89`（`feat/frontend` HEAD）= **B-1 的 fixed point** |
| 过渡条款 | 该点之前已完成并 commit 的前端批次**一律承认有效**（旧循环的每票 / 每批 review 视为已覆盖），不补审、不重跑 |

### 批次台账

| 批次 | 本批 tickets | fixed point | 审查结论 | 修复 commit |
| --- | --- | --- | --- | --- |
| B-1 | #160（MEM-5 前端半） | `637bc89` | 两轴各一 subagent；Spec 6 + Standards 7 findings → 9 修 / 2 说明不改 / 1 只登记（详见第十二轮「批次审查」） | `45227dc` |

### 最终全量 review（v2 §1.3）的处置——**已披露的偏离**

v2 §1.3 要求「全部 ticket 完成后对整条分支跑一次最终全量 /code-review，fixed point = main」。
本 worktree 的处置与理由如下（**不是静默跳过，是显式记录**）：

1. 本分支相对 `main` **落后 13 个 commit / ahead 14**（见 `docs/integration/BRANCH_TOPOLOGY_AUDIT.md`），
   且 `main` 侧的变更全在 `src/**`、`tests/**`（后端），与 `web/**` 无交集。以 `main` 为 fixed point
   的 diff 里，绝大多数是**别人的代码**，对前端审查没有信息量。
2. 本分支**切换 v2 之前**的 13 个前端 commit，已在各自轮次做过两轴 review（见本文件各「批次」小节 +
   `docs/FRONTEND_ISSUES_LOG.md` 各轮）。用户 2026-09-12 的过渡条款明确：「旧版循环下已完成并 commit
   的 tickets 一律承认有效，不再补审、不重跑」。
3. 因此**唯一未被审过的增量 = `637bc89..HEAD`**，已由 B-1 审查覆盖（新版本循环的 fixed point 语义）。

结论：本 worktree 认为 v2 §1.3 的**目的**（"切换后新增的代码全被审过"）已达成；字面执行
`main...HEAD` 只会重审已承认有效的内容。**留给集成 AI 决策**：若仍要跑，建议在 `main` 合入本分支后
再跑（那时 fixed point = 合并前的 main，diff 才等于本分支的真实增量）。

> 为什么单票成批：v2 §1.2 允许「遇到依赖链断点等自然分界提前收批」。#160 是 MEM-5 跨端票的
> 前端半、也是本轮唯一剩余票（MEM 链末端）→ 自然断点，单票即收批；
> 本批 diff = `637bc89..<#160 commit>`。
>
> 协议文件 §4「剩余 Ticket 清单」是旧内容（FE-T7/T8/T9 早已 done）——本轮**不动它**：
> 该文件跨 worktree 共用，保持与 feat/backend 侧逐字节一致可避免制造无谓的 merge 冲突。

### 最近一批：BUG-011 前端半——模型项双击不再发第二个 `POST /model`（2026-09-11）

| 项 | 值 |
| --- | --- |
| 本批 commit | `71e605b`（代码）+ `949d4ad`/`f06cfbd`（docs） |
| 门禁 | tsc ✓ / vitest **519 passed**（29 文件）/ oxlint **37w 0e**（基线持平）/ playwright **130 passed**（`--workers=2`）/ vite build ✓ |
| 触发 | 真机报障「续聊失败：Send failed: 404」，会话 `dd983104`（根因在后端，本批只做前端触发面 + 回归锁） |
| 交付 | ① `ModelPicker.tsx`：新增 `commitSelection`（默认链 + 目录项两处 `onSelect` 统一入口），**弹层已关（`!open`）即丢弃选中**——第一次选中后浮层进入 `--dur-out`(150ms) 退出动画，节点仍在 DOM 可命中，第二次 click 由此丢弃；② `e2e/fixtures.ts`：`onModelPost` 注入点 + 缺省 200 处理器（计数/延迟响应）；③ 新增 `e2e/q-model-dedupe.spec.ts`（3 条锁 × 2 视口）。 |
| 变异验证 | 去掉 `!open` 守卫 → 两例全红，失败信息即原始 bug 指纹（`Expected: 1 / Received: 2`；另一例 `Received length: 3` 且三个 payload 完全相同）。已还原。 |
| 取舍（实测驱动） | 最初设想在 `useSession.changeModel` 加「同目标在途复用 Promise」。探针实测该层**永不生效**：`dblclick()`（一次手势两下点击）与 `page.mouse.click` ×2 都是两次 click 之间 React 已提交 `open=false`，第二个请求到不了 hook——删掉该层前后探针结果完全相同（`dblclick=1` / `mouseclick_x2=1`）。故**只留入口一层**（等价于计划里的「弹层关闭后立即 `pointer-events:none`」），hook 恢复直通、仅留注释指明真正 seam，防止后人加错层。 |
| 跨端配对 | 后端半在 `D:\intelligence-agent-backend` `feat/backend`：`4b8eee4`（① `store.append_event` 每会话写锁 + seq 单调性守卫；② `SeqConflict → 409` 独立语义 + `change_model` 有界重试 3 次；删除 `except ValueError → SessionNotFound` 一刀切；`tests/web/test_web_seq_conflict.py` 4 例）。**后端门禁**：ruff ✓ / pytest **1596 passed**。**集成必须先后端后前端**（AGENTS.md §14.9），后端 seq 守卫是安全网，前端去重是堵源头。 |
| code-review | 两轴（Standards + Spec）各派 subagent：Spec 轴指出「永久 applied 幂等缓存超出计划且不可失效（外部改过模型后无法再选回）+ 依赖响应回显请求值」→ 已删除该设计；Standards 轴指出「测试无法证明第二次点击真的落到节点上（可能因节点已消失而假绿）」→ 已由变异验证补齐该证据并写进 spec 文件头。 |
| 未决/边界 | 跨进程并发写（多进程共享同一 JSONL）无文件锁——后端 `store.py` 文档已如实标注；历史遗留的损坏日志不自愈（读时 409）。 |

### 上一批：ARCH-4b 前端——SessionSummary.trace_url 契约锁 + e2e mock 同步（2026-09-11）

| 项 | 值 |
| --- | --- |
| 本批 commit | `4c38c69` |
| 门禁 | tsc ✓ / vitest **504 passed**（28 文件，+2）/ oxlint **35w 0e**（基线持平）/ playwright **118 passed**（`--workers=2`）/ vite build ✓ |
| 交付 | **类型声明零改动**（`types.ts` 本就正确声明 `trace_url: string \| null`）。① `src/lib/api.test.ts` 新增 `listSessions` 契约块：`trace_url` 原样透传（URL）+ 未追踪保持 null（不伪造，不变量 #21）；canonical fixture 用 `SessionSummary` **类型注解**锁**编译期**一致性——类型新增必填字段 → fixture 缺键 → `tsc -b` 红；fixture 多出未声明键 → 多余属性检查红。② `e2e/*.spec.ts`（6 文件 10 行）会话行 mock 补 `trace_url: null`——旧 mock 照抄了「后端不返回该键」的坏形状，会让前端永远看不到它。 |
| 变异验证 | 从 canonical fixture 删掉必填 `trace_url` → `tsc -b` 报 **TS2741** `Property 'trace_url' is missing ... but required in type 'SessionSummary'` 红（已还原）。 |
| 跨端配对 | 后端半在 `D:\intelligence-agent-backend` `feat/backend`：`a0f86a4`（`store.py` 的 `_terminal_trace_field` 两键共用守卫 + `web/app.py` 映射；`session/service.py` 零改动）。**后端侧权威锁**（断言**值**，能抓「键在但值是 null」的漏映射）：`tests/test_web_api.py::test_list_sessions_carries_terminal_trace_url`。 |
| code-review | 两轴：Standards 轴无 hard violation（采纳 `Literal` 键加固，不采纳 NamedTuple 返回对）；Spec 轴 AC1/2/3/5 满足、零 scope creep，指出 AC#3 值断言缺口已由后端补上。 |

### 上一批：OBS-016 前端同步——超长单行标记文案（2026-09-11）

| 项 | 值 |
| --- | --- |
| 本批 commit | `1fac807` |
| 门禁 | tsc ✓ / vitest **502 passed**（28 文件）/ oxlint **35w 0e**（基线持平）/ playwright **118 passed**（`--workers=2`）/ vite build ✓ |
| 交付 | 纯跨端同步，**解析逻辑零改动**（`LINE_TRUNCATED_RE` 的 `[^\]]*` 本就吞尾部）。① `web/src/lib/toolShapes.test.ts`：新增「新文案（OBS-016）」用例；原用例改标「旧文案（历史会话已落盘）」并**保留**——历史 JSONL 事件仍是旧文案，两种都要能解。② `docs/HANDOFF_FRONTEND_SYNC.md` §1.3：订正为「形状契约 + 措辞可变 + 历史文案兼容」。 |
| 变异验证 | 把 `LINE_TRUNCATED_RE` 改成仅匹配旧文案（追加 `\. Use bash`）→「新文案」用例变红、「旧文案」用例仍绿（已还原）。证明新增用例非空转，且旧用例仍锁住向后兼容。 |
| 跨端配对 | 后端半在 `D:\intelligence-agent-backend` `feat/backend`：`aa29562`（`read.py` 正文改点名真实工具标识符 bash/grep）。本 clone 是独立 clone，`web/` 与 `docs/HANDOFF_FRONTEND_SYNC.md` 相对 `origin/main` **零漂移**，故本批**未做 merge**（`feat/frontend` @`274afcf` 是 `origin/main` @`63db650` 的严格祖先，如需同步可 ff）。 |
| code-review | 本批为测试/文档同步，无解析逻辑改动；后端半的两轴 review 已发现并修复初版「the shell tool」指向不存在工具的问题。 |

### 上一批：OBS-015 修复——审批卡区分幂等已决(409)与真失败(5xx)（2026-09-11）

| 项 | 值 |
| --- | --- |
| 本批 commit | `cb0e008` |
| 门禁 | tsc ✓ / vitest **501 passed**（28 文件）/ oxlint **35w 0e**（基线持平）/ playwright **116 passed**（`--workers=2`）/ vite build ✓ |
| 交付 | ① `api.ts` 新增 `AlreadyResolvedError`；`postApproval` 在 HTTP 409 时抛它，其它非 ok 抛普通 `Error`。② `ApprovalCard.tsx` 修复 catch：`AlreadyResolvedError`(409) → 幂等成功翻卡片；其它错误 → 保持 pending + 显示可见错误(`role="alert"`) + 按钮重新可用可重试。③ 回归锁 `e2e/n-approval-card.spec.ts` +2 用例 ×2 视口 = 4 例。④ `.approval-error` CSS 规则（danger 淡染底 + 左侧 2px 实条）。⑤ 单测 `api.test.ts` +4 例（200 ok / 409 AlreadyResolvedError / 500 plain Error / 422 plain Error）。 |
| 变异验证 | 两处全部生效：① 还原 ApprovalCard 旧行为（任何错误都翻卡片）→ POST 500 用例变红（`Expected: "需要审批" / Received: "已批准"`）。② 禁用 AlreadyResolvedError 分支 → POST 409 用例变红（卡片不再翻「已批准」）。 |
| code-review | Standards 轴 0 hard violations、2 minor smells（均 acceptable）。Spec 轴发现 4 项：① 404 幂等语义未处理 → 经核实后端契约 404 = approval 不存在（不是「已解析」），409 才是幂等已决，当前代码正确。② 失败文案需更明确 → 已在 error message 中体现。③ `.approval-error` 无 CSS → 已补。④ tracker 未更新 → 本批更新。 |

#### 补记（2026-09-11 收尾）：404 语义订正 + 注释与代码对齐 + 404 fail-safe 锁

OBS-015 的既有 code-review 已判出「404 ≠ 幂等已决，当前代码正确」，但**只改了 tracker**，遗留了三处与代码矛盾的载体。本次收尾（**零产品行为改动**）：

| 项 | 内容 |
| --- | --- |
| 订正 1 | `web/src/lib/api.ts` 的 `postApproval` docstring 原写「404 with "already resolved" detail = same semantics → AlreadyResolvedError」，与代码（只有 409 抛 `AlreadyResolvedError`）相反，且会把「决策没生效」误显示成「已批准」。已按后端真实语义改写：404 的四个来源（session 不存在 / 审批队列缺失 / `approval_id` 不在队列 / 事件过期，`web/app.py:1157-1166`）无法与「已解析且已出队」区分 → 必须保持 pending；真已决由 `permission/resolved` 投影事件兜底。 |
| 订正 2 | `docs/PROMPT_FRONTEND_NEXT_BATCH.md` 原把「409/404 幂等语义走已决」写进任务步骤与验收标准（**该前提本身是错的**，会诱导后人实现 404-as-success）。已加 2026-09-11 订正块：保留原文 + 明确 **404 不走已决**。 |
| 新增锁 | `web/e2e/n-approval-card.spec.ts` +1 用例 ×2 视口：**POST 404 → 保持「需要审批」+ 错误文案含 404 + 按钮仍可重试**（此前该路径零覆盖）。 |
| 变异验证 | 注入「旧提示词推荐的错误实现」（`if (404) throw AlreadyResolvedError`）→ 新 404 用例**两视口变红**（2 failed / 12 passed）→ 还原后全绿。证明新锁非空洞。 |
| 未改 | `docs/FRONTEND_ISSUES_LOG.md` 的 OBS-015 条（证据记录，按 HANDOFF §8 不重写）；`ApprovalCard.tsx` / `api.ts` 的运行时行为零改动。 |
| 门禁 | tsc ✓ / vitest **501 passed**（28 文件）/ oxlint **35w 0e**（基线持平）/ playwright **118 passed**（`--workers=2`，116 + 新 404 用例 2）/ vite build ✓ |

### 上一批：瞬态三键 + 401 缝 + 审批卡两键（覆盖账目收口到 45/45，2026-09-11）

| 项 | 值 |
| --- | --- |
| 本批 commit | `35cd0a1`（401 缝单测 + `l-auth-banner.spec.ts`）、`8ed86f0`（瞬态三键 `m-stream-affordances.spec.ts`）、`c9dcf2a`（审批卡 `n-approval-card.spec.ts` + 联调车道） |
| 门禁 | tsc ✓ / vitest **497 passed**（28 文件）/ oxlint **35w 0e**（基线持平）/ playwright **112 passed**（`--workers=2`；104 + 审批卡 8）/ vite build ✓ |
| 交付 | ① 三个瞬态按钮（`tool-out-wrap-btn` / `tool-out-jump` / `reasoning-jump`）用 mock 流钉住窗口后**真实点击**；② 401 缝补 3 例单测 + 横幅 e2e；③ 审批卡「批准」「拒绝」**真机点击**（真实后端 + 真实模型） |
| 覆盖账目 | **45/45 全部已被点击**：38 真机 + 3 mock 流 + 1 mock 401 + 1 e2e 内激活 + **2 审批卡真机点击**（原记「产品不可达」已证伪） |
| 原「残余不可达」 | ~~审批卡「批准」「拒绝」：`auto_approve` 硬编码 → 永不渲染~~ **已证伪**：门是 `session/service.py:348` 的 `permission_mode_explicit`（显式选权限档位即开启交互式审批），与 `auto_approve` 无关。两键已真机点击，后端 JSONL 落库 `permission/resolved`（由测试自身轮询断言）；回归锁 `web/e2e/n-approval-card.spec.ts`；真机脚本走独立联调车道 `web/e2e-live/` + `playwright.live.config.ts` |
| 新登记问题 | **OBS-015（P2，前端，预存在，需产品决策）**：`ApprovalCard.tsx:26-33` 的 `catch` 对任何错误都翻成「已批准/已拒绝」，与注释「其它错误保持 pending」相反 → 审批 POST 失败时是乐观假象。本轮未改代码（§8） |
| 审查 | 两轮独立审查。`m-stream-affordances` **approve**（0 P0/P1/P2、6 项 P3 全修）；审批卡一轮发现 **2 项 P2**（URL 会话 id 未断言、联调车道分不清「后端已决」与「乐观 UI」）**均已修 + 变异验证**，P3 三项处置 |
| 自曝缺陷 | 新建联调车道时 `vitest.config.ts` 的 `exclude` 漏了 `e2e-live/**` → 单测车道被 Playwright 用例污染而变红；已修，并写入 HANDOFF §6 警示 |
| 关单 | 不适用（缺陷/覆盖批次，非 ticket 交付） |

### 最近一批：刷新一致性 BUG-005 / BUG-006 + 第二轮逐按钮巡检（2026-09-11）

| 项 | 值 |
| --- | --- |
| 起始 commit | `cf8f3a7` |
| 本批 commit | `138b056`（+ 后续小提交回填本 hash） |
| 门禁 | tsc ✓ / vitest **490 passed**（28 文件）/ oxlint **35w 0e**（基线持平）/ playwright **96 passed**（86 → +10）/ vite build ✓ |
| 交付 | BUG-005 刷新恢复选中会话；BUG-006 流式中刷新 → `?after_seq=` 接回流继续收事件；新增 `lib/sessionRestore.ts`(+test)、`api.ts` `NotFoundError`、`e2e/k-refresh-restore.spec.ts` |
| 真机验证 | BUG-006 **决定性取证**：真实后端 run 在途时 F5 → `GET /stream?after_seq=2 [200]`，零交互下事件 3 → 54 条直到 `run/completed`；与后端真值 54 条 / 0 重复 / 0 空洞。BUG-005 刷新前后正文指纹 `-271347586` / 4102 字符逐项一致 |
| 巡检 | 第二轮 66 行逐按钮表（密度/主题/Inspector 收起/Workspace/四选择器/五 Tab/命令面板/委派节点/分叉/令牌弹窗/preset chip/发送禁用/停止/请求量）全部通过 |
| 新发现 | BUG-007（命令面板 label 全英文，中文查询零命中）——**已修复**（label 本地化 + `keywords` 别名，英文仍可搜）；OBS-008（`glm-5.3-flash` `model/failed: RuntimeError`）、OBS-009（bash 工具 10s 超时且 `retryable:false`）均为**后端/provider**问题，前端渲染忠实 |
| 审查 | 第一轮 6 findings（3×P2 + 3×P3）：4 修 + 2 说明理由不改；第二轮见集成提示词 |
| 关单 | 不适用（缺陷修复批次，非 ticket 交付） |

### 上一批：恢复/分叉/滚动 三缺陷 + 真实浏览器逐按钮巡检（2026-09-11）

| 项 | 值 |
| --- | --- |
| 起始 commit | `8469a34`（另一 Agent 的 BUG-001 修复） |
| 本批 commit | `32356f4`（缺陷修复 + 巡检）、`0d6b82d`（架构扫描低风险项） |
| 门禁 | tsc ✓ / vitest **472 passed**（27 文件）/ oxlint **35w 0e** / playwright **86 passed** / vite build ✓ |
| 交付 | 交接手册 A/B/C/D；额外 BUG-004（Copy Run ID）+ 分叉 30s 超时反馈 |
| 真机验证 | A/B/D 三项在真实浏览器 + 真实后端复验（回执见 `FRONTEND_ISSUES_LOG.md` OBS-003/OBS-004）；43 行逐按钮巡检表 |
| 遗留 | OBS-007（中断会话绿色「已完成」脉冲与中断横幅矛盾，**预存在、故意未修**，§8）；覆盖缺口清单见集成提示词 §4 |
| 关单 | 不适用（缺陷修复批次，非 ticket 交付） |
| 架构扫描 | `/improve-codebase-architecture` 已完成。已修：候选 3（Inspector Run 摘要收归 `runState`，`0d6b82d`）+ 候选 4 字段级文档。**未做（按扫描结论 + §8）**：候选 1 `StreamOrchestrator`（最热路径，需监督 + 测试先行）、候选 2 `useFollowLatest`（代码库已显式推迟，ADR-0016）。报告：`%TEMP%rchitecture-review-20260911-0345.html` |

---

## Ticket 状态总览

| Ticket | 描述 | 状态 | Commit |
| --- | --- | --- | --- |
| FE-T7 | 会话级模型切换 + Fork UI（#137） | `done` | `71c01dd` + review 修复 `c6e6fab` |
| FE-T8 | 崩溃恢复 UI — run/interrupted + 409 守卫（#138） | `done` | `c137a23` |
| FE-T9 | 轮次标签（turn_index 显示）（#139） | `done` | `d2bfbc8`（类型/投影层）+ `cddea36`（UI 层） |
| 前置 | 重新生成 event-types（MODEL_CHANGED + RUN_INTERRUPTED） | `done` | `6012414` |
| 深化 C4 | api 层成为唯一归一化点 | `done` | `9b2234f` |
| 深化 C3 | Composer 档位 → 提交字段的单一构造器 | `done` | `9b2234f` |
| 深化 C2 | projection 事件语义注册表（编译期穷尽） | `done` | `f481ea5` |
| 深化 C1 | StreamOrchestrator 流式编排深化 | **`partial`** | `ae341e2`（第一刀） |
| 深化 review 修复 | 字段表编译期锁 + 时间参数集中 + 记录修订 | `done` | `2735422` |
| 深化 C5 | ConversationState 拆分 | `rejected` | —（YAGNI + 参考实现反证，见 `docs/ARCHITECTURE_REVIEW.md`） |

### 全部完成后的步骤

| 步骤 | 状态 |
| --- | --- |
| /improve-codebase-architecture | `done` → `docs/ARCHITECTURE_REVIEW.md`（`3e71b33`） |
| 深化批次实施 | `partial`（C1 剩余部分见下） |
| 写集成 AI 交接提示词 | `done`（`25917c1` 初版 → 本轮补深化批次 + 拓扑重测 + AGENTS.md 冲突预判） |

---

## 门禁基线（本轮全绿）

| 门禁 | 结果 |
| --- | --- |
| `npx tsc -b` | 0 error |
| `npx vitest run` | 408 passed / 27 files |
| `npx oxlint` | 0 error / 35 warning（全部既有，非本批引入） |
| `npx playwright test --workers=2` | 46 passed |
| `npx vite build` | OK（仅既有 chunk-size 提示） |

---

## 已完成批次：FE-T7 / T8 / T9（#137–#139）

### FE-T7 会话级模型切换 + Fork UI

- API：`changeSessionModel(sessionId, provider, modelId)` → `POST /api/sessions/{id}/model`；
  `forkSession(sessionId, fromSeq)` → `POST /api/sessions/{id}/forks`。
- 投影：`model/changed` → 更新 `conversation.model`。
- UI：ModelPicker 走 POST 并以**响应回传的规范 model_id**更新本地状态（不回显请求值）；
  用户消息上的「分叉」入口（`Conversation.tsx` + `.fork-btn`）。
- review 修复（`c6e6fab`）：CSS 变量改正、响应 model_id 采用、移除越出 T8 范围的改动。

### FE-T8 崩溃恢复 UI

- 投影：`run/interrupted` → `finalizeRun` + `ConversationState.run_interrupted`
  `{ step_id, interrupted_seq, reason }`。
- `sendFollowUp` 捕获 **409**：解析 detail，抛「存在需要人工裁决的高风险操作」，
  **不伪造结果继续**（后端硬拒绝，不变量 #14）。
- UI：中断横幅（`.interrupt-banner`）。

### FE-T9 轮次标签

- **数据层（`d2bfbc8`）**：`run/started` 提取 `data.turn_index`（后端 `session.begin_run`
  定义为「该 session 第几个 run，1-based」，每次 run 各自携带）→
  `ConversationState.turn_index`（会话级，供 Langfuse/turn 元数据）。
- **UI 层（`cddea36`）**：`Turn.turn_index`（per-turn 事实）→ `TurnView` 显示「第 N 轮」。
  ⚠️ 修正：原计划复用会话级 `state.turn_index` 传入 `TurnView`，但该字段被最新 run
  覆盖 → 所有历史轮次会显示同一个数字。改为把 RUN_STARTED 的值落到**当轮 turn** 上。

---

## 深化批次（架构评审 → 实施）

评审产物：`docs/ARCHITECTURE_REVIEW.md`（5 个候选 + 「选择的最佳路径」+ 「交付状态」+ 「已披露的行为变化」）。

**执行顺序（先低风险后深水）**：C4 → C3 → C2 → C1；C5 已否决。

### C4 + C3（`9b2234f`，一个 SDD 循环覆盖两个 candidate——偏离「每 candidate 一轮」，已记录）

- 新增 `lib/amend.ts`：`toAmendFields` / `toCreateControls`——Composer 档位（camelCase）
  → 契约字段名的**单一映射点**；明确**不做**空值丢弃（丢弃归 api 层）。
- `lib/api.ts`：`startSession` 成为与 `sendMessage` 同款的「有值才带键」执行点；
  空值 / 空数组不发键 = 后端默认。
- 新增 `lib/amend.test.ts`（5 例）锁字段集边界与「映射层不判空」契约。

### C2（`f481ea5`）事件语义注册表

- `applyEvent` 与 `summarizeEvent` 两个并行 switch 收敛为
  `EVENT_SEMANTICS: Record<EventTypeValue, EventSemantics>`（`{ apply, summarize }`）。
- **穷尽性经实验证伪**：注入 `FUTURE_THING` → `tsc` 报 TS2741；
  还原后 `git diff --stat` 干净。生成物新增事件类型而忘记登记 → 编译失败。
- 词汇表内但前端零处理的 7 个类型（`artifact/externalized`、`context/compaction_start|end`、
  `message/queued`、`queue/cancelled`、`steer/requested|applied`）**显式登记**为
  `unhandledProjection`（保持既有兜底行为进 `unknown_events`），使缺口可见而非静默。

### C1（`ae341e2`）第一刀——`ReconnectController`

- 新增 `lib/reconnect.ts`：**无 React / 无定时器 / 无 I/O** 的重连策略状态机。
  调用方拿 `decision` + `delayMs` 后自行调度（参考 deepseek-harness `BlockStreamer`
  的 injectable clock、pi-mono `lane.ts` 把 operation 生命周期从编排循环剥出）。
- 契约原语一并迁入 `decideStreamEnd` / `reconnectDelayMs` / `MAX_RECONNECT_ATTEMPTS`；
  时间常量 `RECONNECT_STALL_MS` / `RECONNECT_BANNER_DELAY_MS` 在 review 修复 `2735422` 随迁
  （评审「速度」目标：退避 / 停摆阈值 / banner 延迟集中为一处）。`useSession.ts` 以 re-export
  保持既有导入路径不破。
- 行为逐点对齐旧闭包：准入即占单飞并递增额度；`observeProgress` 只在 seq **严格超过**
  重连起点游标时复位额度（重放旧帧不是真进展，否则额度永不耗尽 → `give-up` 不可达 →
  悬空 run 无限重连）；`release` 放单飞但不动额度；`hold` 是 truncated 全量重建的占位；
  `reset` 是流的生命周期边界。
- 新增 `lib/reconnect.test.ts`（19 例）锁此前无测试的状态迁移。

---

## 剩余工作

### 1. C1 深水部分：`StreamOrchestrator`（未做，风险最高，需完整回归）

`attachLiveStream` 仍是约 200 行嵌套闭包，以下四类降级路径与合帧提交仍在 hook 内：

| 路径 | 现状位置 |
| --- | --- |
| 合帧批量提交（性能正向路径） | `coalescer` / `coalescerRef` |
| 停摆心跳 + visibilitychange | `stallCheckRef` / `stallCheck` |
| truncated 全量重建 | `doTruncatedRebuild` |
| seq-gap 分流 | `onEvent` 内 `isSeqGap` 分支 |

目标形状（评审「主路径」）：`lib/stream-orchestrator.ts`，构造注入
`fetchStream` / `scheduleTimer` / `clock`，内部按 pi-mono `drive/` 的状态分派
拆成 recovery / rebuild / stall 子模块；生产（SSE 解析）与消费（投影应用）分离
（pi-mono `EventStream<T,R>`）。

**为何本轮不做**：该段处于每帧热路径与全部降级路径的交汇处，无监督长会话收尾阶段
风险过高。先剥出重连策略状态机并锁单测，是后续提取的安全网前置条件。

### 2. 已登记待定项（不在本批范围）

- `session/forked`：已知类型但 Timeline 摘要仍落「未知事件」文案（pre-existing，
  文案变更未经确认）。C2 注册表按等价迁移保留，已登记。
- 上述 7 个未接线类型：需产品确认是否显示。
- `api.ts` 中 `START_SESSION_FIELDS` 与 `SEND_MESSAGE_FIELDS` 的 amend 四项各自登记
  （编译期各自穷尽，但同一字段集两处书写）——是否抽公共表待定，当前按
  「简单优先」保留显式重复。

### 3. 集成交接提示词（已更新）

`docs/integration/FRONTEND_INTEGRATION_PROMPT.md` 已重写：14 个 commit 清单、
新 HEAD `d5a8dca`、深化批次新增模块、两处已披露行为变化、C1 未完成范围，
以及**重测后的拓扑与冲突预判**。

#### ⚠ 拓扑已变，冲突预判与初版相反

初版写「ahead/behind 6/0，预期 merge 干净」。重测：merge-base `5c07fff`，
`main` `c5149ad`，**14 / 13**（main 在 merge-base 后又前进 13 个 commit）。

`git merge-tree --write-tree --name-only main HEAD` → **`AGENTS.md` 必然冲突**：
两侧都在 §15 之后追加同号 §16（`main` 版是后端/Primary 口径，本分支版是前端口径）。
按 §14.7 已给出 9 项分析与推荐统一语义（保留 main 版为唯一 §16，前端内容折叠为
`## 16.6 前端 worktree 补充`）——**留给集成 AI + 用户决策，本分支不自行解决**。

`web/src/generated/event-types.ts` 两侧都改过但**逐字节相同**，auto-merge 零差异。
其余 `main` 侧变更全在 `src/**` / `tests/**` / `docs/**`，与 `web/**` 无交集。

#### 已识别但未做的协议项

`main` 的 AGENTS.md §16.4 要求「每个 ticket 完成后更新 `docs/PHASE_STATUS.md`」。
本分支按前端协议记在 `docs/SDD_TICKET_TRACKER.md`，**未写 PHASE_STATUS.md**——该文件既有
条目均为「合入 main 后回填」，故在交接触提示词 §6 提供了建议条目文本，由集成 AI 在
merge 后追加。这是本批的协议偏离，记录在案。

---

## 4. 真机验收批次（2026-09-11）：逐按钮巡检 + 刷新一致性

用户要求「用真实浏览器把每个功能按钮都点一遍，问题实时写进文档，并检查刷新后会话是否与刷新前一致」。本批不新增 ticket，交付物是**问题登记簿 + 修复 + 回归锁**。

- 登记簿（单一事实源）：`docs/FRONTEND_ISSUES_LOG.md`——含 74 行逐按钮巡检表、BUG-005/006/007、OBS-007～010、前后端归因、三轮 code-review 处置。
- 交接提示词：`docs/integration/FRONTEND_REFRESH_PERSIST_INTEGRATION_PROMPT.md`（§1 改了什么 / §2 真机取证 / §8 OBS-007）。

| commit | 内容 |
| --- | --- |
| `138b056` | BUG-005 刷新恢复选中会话 + BUG-006 在途 run 接回流（`after_seq=N` 重放+续流） |
| `21fb004` | 文档回填 138b056 |
| `03d6a70` | BUG-007 命令面板本地化 + 可搜索英文别名 |
| `b4181ad` | OBS-007 中断脉冲第四态 + 清 `run_interrupted` 标记（含审查 P3 处置） |

**已修**：BUG-005、BUG-006、BUG-007、OBS-007。
**判定为后端/非前端**（仅记录，未改）：OBS-008（`glm-5.3-flash` 工具成功后 `model/failed`）、OBS-009（bash 工具 10s 超时上限与 `retryable` 语义）、OBS-010（`GET /api/sessions` 的 `trace_id` 恒为 `null`，但会话详情事件里的 `trace_id` 正常，故 UI 的 Trace 命令实际可用——**原登记曾误判为「命令不出现」，已订正**）。
**已知覆盖缺口**：~~OBS-006 审批卡不可达（`auto_approve` 硬编码）~~ **已证伪并闭合（第五轮真机点击）**；`已中断` 脉冲态真实语料不可达（仅单测）；`pulse-interrupted` 类名字符串与 CSS 选择器无测试绑定。

**子会话刷新一致性（追加真机验证）**：委派 child `2515a128`（列表点击 / 「打开子会话」两条入口）与分叉 child `1fdac9b9`（410 事件）刷新前后正文指纹**逐字节相同**（日志见登记簿对应章节）。新增回归锁 1 例（×2 视口）——首版播种式被变异验证证伪（只覆盖读路径），已改为真实点击写入路径 + 按 id 区分事件。

**第三轮控制面清点（可核对方法）**：从源码枚举全部 **45 个 `<button>`**（17 文件）逐个核对。初版**关键词比对**不可靠（两个方向都会错：`保存`/`清除` 命中的是无关散文 → 令牌弹窗两键实际没点过却判 OK；`滚动到最新`/`恢复会话` 其实有覆盖却判缺失），**故改为逐个真机点击**。最终 **45 = 38 点过 + 1 补 e2e + 3 按设计不可达 + 3 瞬态窗口不可达**：

- **38 个真机点击通过**（本轮新验含：Inspector 5 tab、加载更早 200→410、时间线行跳转、终端行→工具焦点、io-tabs ×4、JSON 展开、返回父会话、代码块换行、**推理块展开**、**Inspect chip**、**Inspector 工具行**、**令牌保存/清除**、空态示例 chip）；
- `auth-banner-close` 本地不可达（后端仅配 `jwt_secret` 时 401）→ 新增 `web/e2e/l-auth-banner.spec.ts`（含变异验证）；
- **按设计不可达 3**：审批卡「批准」「拒绝」（`auto_approve` 硬编码，OBS-006）、`ContextProviderPicker`（本部署后端目录为空 → 正确不渲染）；
- **瞬态窗口 3 已在第四轮补齐**：`tool-out-wrap-btn`、`tool-out-jump`、`reasoning-jump` 原需「流式中 + 用户上滚」才渲染（cmd 缓冲输出使尾窗仅存毫秒级），已用 **mock 流钉住窗口**（不发 `tool/result` / `reasoning/completed` → 投影状态恒为 running/streaming）后**真实点击**并逐一变异验证，见 `web/e2e/m-stream-affordances.spec.ts`。

**最终覆盖账目（第五轮后）**：**45/45 全部已被点击**，其中审批卡「批准」「拒绝」为**真机点击**（真实后端 + 真实模型）。原记的「2 产品不可达」是**误判**并被证伪——审批卡由 `tool/approval-requested` 事件驱动（`projection.ts:514`），门在 `session/service.py:348`（`permission_mode_explicit`），与 `auto_approve` 无关；显式选「只读」后任何 workspace-write 工具都会触发。真机证据 + 回归锁见登记簿 OBS-006 订正条。**不再有「未验证」按钮。**

**新发现的后端问题（含根因行号，需后端修复）**：OBS-011 子进程输出按 UTF-8 解码而 cmd.exe 输出 GBK → **乱码固化进 JSONL**（`sandbox/local.py:166-167`，铁证：原始字节中 U+FFFD 与侥幸合法的 GBK 双字节混杂）；OBS-012 `bash` 工具在 Windows 实为 cmd.exe（`shell=True`，`local.py:161`）→ bash 语法 41ms 失败；OBS-013 provider 退化重复（2,868 delta / 186,507 字符的同句循环，3.5 分钟无工具调用，另见多次 `model/fallback … InternalServerError`）；OBS-014 bash 工具 10.0s 硬超时且 `retryable:false`。

**本批最终门禁（实跑）**：tsc ✓ · vitest **497 passed**（28 文件）· oxlint **35 warnings / 0 errors** · playwright **112 passed**（`--workers=2`：瞬态三键 4 例 + 审批卡 8 例）· vite build ✓。联调车道（`e2e-live/`）用例不计入主车道（已核验计数 0）。
**第四轮新 spec 的独立审查**：0 个 P0/P1/P2，6 项 P3 **全部已修**（头注释挂载条件、合成滚动划界、显式 `aria-expanded`、数值化可滚动断言、`toHaveCSS` 断生效样式、作废指针），并按修改后版本**重跑三处变异**（均红）。详见登记簿「第四轮收尾」。

**本轮审查（`l-auth-banner.spec.ts`）**：0 个 P0/P1，1 项 **P2** + 4 项 P3，**全部已处置**。P2 是**注释谎报覆盖**——我写「`api.test.ts` 测 401 分类」，实则全 `src` 测试树零个 401 引用（该缝当时**无单测**）。已把谎报改成事实：`api.test.ts` 新增 3 例（401→`UnauthorizedError`、广播 detail、**body 非 JSON 的回退文案**）。P3 中一项揭示了**真实行为被我注释说反**：关闭**不是**永久忽略（`App.tsx:148` 每次广播都会重新显示），故 e2e 改为走「配置令牌」真实路径断言**横幅重新出现**（变异验证：删掉 `refreshSessions()` → 两视口都红）。


---

## 第六轮（2026-09-11）：真实浏览器全量验收 + BUG-008

**环境前提（本轮结论必须带这条读）**：:8000 = **`feat/backend` worktree 的后端**（其 `.env` 的
`CAPABILITIES` **只启用 `websearch`**），5173 = 前端 dev server。前几轮跑的是 **main worktree 的后端**
（`.env` 里 `websearch` + **`multiagent`** 都开），所以两轮看到的语料与能力集**本来就不同**。

**刷新一致性复验（新增证据）**：会话 `3b35b83d`（35 事件·中断态）在 balanced 与 detailed 两档下，
刷新前后 `document.body.innerText` 指纹**逐字节相同**（`2235:1645848761` / `2309:2105849635`），
按钮指纹（74 键 `2032:3401835054`）、滚动位、tab 选中态、`ahi.selectedSession` 全部一致。
**视图状态**（tab / 滚动 / 折叠）刷新不保留——与既有冻结边界（Inspector 属视图状态、DSH 语义）一致，
**非缺陷**；本轮为「内容一致 + 视图状态不保留」补了并存的实测证据。

**BUG-008（已修，commit `365fbee`）**：后端序列化省略值为 null 的字段 → 无步号事件的 `step_id`
是**键缺失**（`'step_id' in e === false`），而三处消费点用严格 `!== null`：
- `StepDetail.tsx::formatEventTooltip` → hover 浮层渲染字面量 `step undefined`
- `StepDetail.tsx` 事件详情 Overview → 空值 `step` 幽灵行
- `eventKind.ts::streamKeyFromEvent` → 伪造 key `step:undefined`（违背它自己的「无 step → null」契约）
三处统一改宽松 `!= null`（与 `projection.ts::resolveStep` 既有口径一致）；明确否决「改后端」与
「在 `eventValidate` 归一化」。两文件各补「键缺失」红灯用例（TDD 先红后绿）。
**真机复验**：无 step 行浮层只剩时间戳、带 step 行仍显 `step 1`；幽灵行消失；跳转 pulse 正/负对照 1 / 0。

**本轮新增真机通过**：事件详情 io-tabs ×4 + `复制 JSON`/`复制 JSON`/`复制 Raw`（剪贴板 165/165/490 字，
均为 JSON）；工具详情 4 tabs（默认 Output）+ `复制 JSON`(32)/`复制输出`(85)/**两个 `复制 Raw`**(446 call,
685 result)；Timeline hover 浮层；**全局 Esc 取消**（fetch 记录器捕获 `POST …/cancel`，脉冲 `已取消`
中性通道，非红色失败）；代码块 `自动换行`↔`不换行` 往返 + `复制代码`（22 字与渲染正文**逐字相等**）；
中断会话的工具详情**零伪造**（无 tool/result 时不渲染 Output tab、Raw 只 1 个复制键）。

**本轮不可达（配置/语料原因，非产品缺陷）**：委派/子会话 5 项、`加载更早 N 条`、Trace 三件套、
四个 picker 搜索框、Context picker。其中委派 5 项与 `加载更早` **已在其他轮次真机点过**（第二轮
第 38/61 项；第三轮「加载更早 200→410」用的是 410 事件的 fork child `1fdac9b9`，属 main 后端语料）。
**真正的产品不可达只有 Trace 三件套**（需 Langfuse 启用）。详见登记簿 OBS-016。

**两条过程自查（值得记住）**：① **图标按钮必须按 `aria-label` 定位**——`CopyButton` 的 `innerText`
为空，按可见文本 `^复制` 找会得出「按钮不存在」的**误报**；② **门禁链路 `| tail` 会吞掉退出码**——
本轮一次 `1 failed` 被掩盖成「成功」，改用 `set -o pipefail` 后复跑 5 次全绿（那次失败**不可复现**，
已如实登记，不计为通过）。

**门禁（实跑）**：tsc 0 · vitest **507 passed / 0 failed**（28 文件）· oxlint **35 warnings / 0 errors**
· playwright **118 passed**（`--workers=2`）· vite build 0。

**观察（后端/provider，本轮未改）**：真实 run 中默认链 `deepseek-v4-flash-0731` **停顿 50s+ 且 token
零增长**，随后**模型回退按设计生效**（不变量 #9），由 `glm-4.5-air` 完成（时间线 `model/completed
glm-4.5-air · 6907 tok` → `run/completed 13873 tok`）。停顿期间 UI 全程只显示诚实的 `思考中 · Ns`
（不伪造进度、不假报错），但**没有任何「正在等待模型/即将回退」的中间态提示，且阈值偏长**——
建议后端更早发 fallback 事件（前端已有渲染通道）。

---

## 第七轮（2026-09-11）：类型诚实化（#147）+ 停顿提示（#148）+ `加载更早` 覆盖锁

| commit | 内容 |
| --- | --- |
| `ec2a961` | BUG-010/#147：`AgentEvent.step_id`/`run_id` 放宽为可选——类型不再对「键缺失」撒谎 |
| `9517e5d` | FE-01/#148：停顿提示（展示层旁注，不新增 SessionEvent） |
| `051aff6` | e2e：`加载更早 N 条` 真实点击锁（唯一零自动化覆盖的控件） |

**grill 轮的四个决策用户未作答** → 按各题推荐项执行（放宽类型 / `step_id`+`run_id` 同票 /
前端本地观察式 / 超时默认一律不改），已在 issue 正文与 commit message 里标注为
「未获用户确认的默认值」，用户可事后否决。

**门禁（末次实跑）**：tsc 0 · vitest **519 passed**（29 文件）· oxlint **0 errors**（37 warnings）
· playwright **126 passed**（`--workers=2`）· vite build ✓。

**真机证据**：真实后端 + 真实模型（Reasoning Effort=Deep）在一次 31s 首 token 等待上验证
停顿提示（出现时机、秒数语义、give-up 让位、重连后累计），完整时间线见
`docs/FRONTEND_ISSUES_LOG.md` 第七轮。

**关单**：#147、#148 均已关闭（comment 内含 AC 逐条证据与残留观察）。**未 push**。

**过程自查（值得记住）**：门禁的 playwright 与我另外两次 ad-hoc e2e **并发**跑，两个进程
写同一个 `test-results/` → `ENOENT ... .playwright-artifacts-*` → 门禁假失败 4 例
（其中 3 例是无关用例）。清掉并发、`rm -rf test-results` 后重跑全绿。**同一 worktree 里
不要并行跑两个 playwright。**

**集成提示词**：`docs/INTEGRATION_PROMPT_TYPE_HONESTY_AND_WAIT_HINT.md`。

## 第八轮（2026-09-12）：WS-5 #155 项目分组 UI（跨端票的前端半）

**背景**：用户诉求「一个项目下多个会话，和 zcode 一样」。后端 #152/#153/#154（feat/backend）
已交付 Workspace 实体、列表 `workspace` 契约与 9 个项目 CRUD 端点；本票做**可见性**。

| 交付 | 位置 |
| --- | --- |
| 契约层：`Project` / `ProjectStatus` / `ProjectDeleted` + 7 个端点 + `ProjectError` | `src/types.ts` / `src/lib/api.ts` |
| 纯函数：分组投影 `buildRailModel` + 重排锚点 `moveAnchor` / `dropAnchor` | `src/lib/projects.ts` |
| 数据与动作：`useProjects`（写后重拉，不维护影子名单） | `src/hooks/useProjects.ts` |
| 侧栏改版：项目块（折叠/行内重命名/菜单）+ 行（点选/菜单/拖拽）+ 未分组区 | `src/components/SessionList.tsx` |
| 三个浮层：新建 / 删除确认（明示只解除分组）/ 加入项目 + 行内重命名 | `src/components/ProjectDialogs.tsx` |
| 样式：层级导轨、菜单、拖放落点、错误条、浮层 | `src/styles/app.css`（纯新增 562 行） |
| e2e：6 用例 × 2 视口 + 有状态项目 mock | `e2e/r-project-groups.spec.ts` / `e2e/fixtures.ts` |
| 真机：无 mock 的真后端流程 + 基线逐字段比对 | `e2e-live/project-groups-live.spec.ts` |

**门禁（末次实跑，最终 revision）**：tsc 0 · vitest **561 passed**（30 文件）· oxlint
**0 errors**（37 warnings，均为既有规则；本票 10 个文件 0 warning）· playwright
**144 passed**（`--workers=2`，本票新增 14 例）· vite build ✓。

**两轴 code-review 后修复（commit `8db0e5f`，零 finding 后才收）**：独立 Spec / Standards
两轴 review 的发现里，有两条是**会在真机或流式下真实发生**的，值得记住：

- **mock 与真机语义相反**：e2e 的 attach 推队尾，真实后端写的是 `[session_id, *kept]`
  （**前插**，`workspace/index.py` + `tests/web/test_projects_api.py` 都锁着）。也就是说
  这条 AC4 用例在**真机后端下必然失败**——围栏里的绿灯不能证明契约一致，只能证明
  "前端与我的假后端一致"。已把 mock 与断言都改成前插。
- **memo 被内联箭头破功**：`onRetryProjects` 每次渲染新建 → 流式期间整片 Session Rail
  跟着每个 delta 重渲染（本仓库明文规则，`handleSelect` 一列同样处理）。已 useCallback。

其余修复：`SessionSummary.workspace` 从"只在注释里被消费"变成真的消费（项目不在列表里
时未分组行带 `staleProject` 注解，AC6 的字面要求）；Pydantic **422 的 detail 是数组**，
只认字符串会把"path must be an absolute path"降级成"注册项目失败（422）"；`useProjects`
补乱序响应守卫与 in-flight 合并；拖拽落点只在来源项目内亮；Enter 提交补 pending 守卫；
删掉 rail/选择列表上不成立的 `role=list`、折叠时的悬空 `aria-controls`、`<p>` 里塞 `<ul>`
的非法 HTML；空项目提示去掉一条**不可达**的引导（单段 workspace 建不出项目内会话）；
mock 的 order 端点补自锚点 no-op（缺它会插错位置）；新增 409 用例（cwd 不一致 → 后端原因
就地显示、归属不变）；AC3 断言改为打在**后端原始串**（`WinError 3` + 路径）上以证明透传。
`playwright.config.ts` 固定 `workers: 2`（§16.6 硬要求，避免"门禁绿、本地红"）。
`gui-test-screenshots/` 进 `.gitignore`（真机截图是本地证据，不进版本库）。

**真机**：真 uvicorn 8000 + 真 `.env`/`harness.db` + 真浏览器 5173，无 mock；注册/改名/attach/
detach/重排/软删除全走通，**结束时会话归属与项目账本与开测前逐字段相等**（状态已还原）。
截图 `web/gui-test-screenshots/ws5/`（本地留存、未入库：运行时产物，与既有各轮一致）。

**视觉检查逮到 1 个 e2e 抓不到的 bug**：`.rail-menu` 复用 `palette-in` 关键帧（含
`translateX(-50%)`）→ 菜单永久左移半宽；Playwright 点真实位置所以全绿，人眼一看就歪。
已改为独立 `rail-menu-in`（opacity + scale，配 Radix 的 transform-origin 变量）。

**关单**：#155 **不关**（跨端票：后端半在 feat/backend 尚未合入 main；按 §14.12 以 comment
记录已完成部分与剩余项）。

**未做 / 交后续票**（详见 `docs/FRONTEND_ISSUES_LOG.md` 第十轮）：
①`POST /api/sessions` 仍只接受单段 workspace 名 → **任意目录的项目拿不到"新建会话"入口**
（用户原始诉求的最后一块缺口，需后端契约变更）；
②注册项目不批量回溯 attach（契约不暴露会话 cwd，前端无法判定，不猜）；
③项目端点之外的 CORS `*`（后端既有）；
④窄屏 56px 折叠轨看不见会话行（既有规则，本票只追加了项目 chrome 到同一 hide 列表）。


---

## 第十二轮（2026-09-12）：MEM-5 #160 前端记忆管理 UI（跨端票的前端半）

**本批 commit**：`5eb4fed`（`feat/frontend`；父 commit `637bc89` = B-1 的 fixed point）。

**背景**：MEM-4（#159，后端半，已关单）给了用户侧两个入口（`GET /api/memories` 列表 +
`DELETE /api/memories/{id}` 硬删），但没有界面。用户看不到记忆库里有什么，也就无从判断该删哪条
——所以「用户入口」若只有 API 就等于没有（issue #160 的原话）。

| 交付 | 位置 |
| --- | --- |
| 契约层：`MemoryScope` / `MemorySummary` / `MemoryDeleted` + 2 个端点 + `MemoryError` / `isMemoryDisabled` / `describeMemoryError` + 窄化 `parseMemory` | `src/types.ts` / `src/lib/api.ts` |
| 纯展示逻辑：`scopeLabel` / `formatMemoryTime`（解析失败原样返回，不伪造）/ `hasMoreAfter` / `withoutIds` | `src/lib/memory.ts`（+ `memory.test.ts` 7 例） |
| 数据与动作：`useMemories`（打开时拉取 / 分页 / 乐观删除 + 两结局都重拉权威 / 503 降级与错误分流） | `src/hooks/useMemories.ts` |
| 浮层：列表（content + scope + 创建时间 + 长正文展开）+ 行内二次确认（「删除不可恢复」）+ 降级 / 空 / 错误 / 分页四态 | `src/components/MemoryPanel.tsx` |
| 入口 ×2：顶栏 Brain 按钮 + 命令面板「管理记忆」 | `src/components/TopBar.tsx` / `src/App.tsx` |
| 样式：浮层材质与项目浮层共用 + 记忆专属 26 条规则（纯新增） | `src/styles/app.css` |
| 契约测试：canonical fixture **类型注解**（ARCH-4b）+ 11 例（分页参数 / 畸形行剔除 / 404·403·503 分流 / 回执缺字段） | `src/lib/api.test.ts` |
| e2e：7 用例 × 2 视口 = **16 例**（列表渲染与翻页 / 长正文展开 / 二次确认 + 后端权威 / 失败回滚 / 503 降级 / 真空态 / 500 错误重试 / 命令面板入口） | `e2e/s-memories.spec.ts` + `e2e/fixtures.ts`（**有状态** memory mock） |

**门禁（实跑）**：`npx tsc -b` ✅ · `npx vitest run` **580 passed**（31 文件，+19）·
`npx oxlint` **0 errors**（38 warnings：37 既有 + 1 条本票与既有同类的 `set-state-in-effect`）·
`npx playwright test --workers=2` **160 passed**（+16 例）· `npx vite build` ✅。

**e2e 逮到的真 bug（值得记住）**：列表读取失败（500/网络）时，`visible.length === 0` 分支先命中
→ 面板会**同时**显示「还没有记忆」与错误条——正是 AC4 禁止的「把读不到伪装成没有」。
首版实现的判空顺序漏了 `loadError === null` 这一项；`s-memories.spec.ts` 的 500 用例
（`.memory-empty` 必须 count 0）把它钉死。修复：空态只在「确实读到空列表」时渲染，
「0 行 + 读取失败」只留错误条 + 重试。

**真机验收（真 .env / 真模型 / 真 Zilliz / 真 sqlite / 真浏览器）**：

| 步骤 | 结果 |
| --- | --- |
| 种子数据 | `.scratch/seed_real_memories.py`（后端 worktree，**不入库**）：走生产同一条 `build_builtin_memory_components` + `capability.consolidate()` 写入 3 条真实记忆。**实测 3 条都 `degraded=consolidation_failed: VectorStoreError`** —— 当时 Zilliz/embedding 不健康，按 #158 的「不丢写」设计降级成无条件 insert（记录行照样落盘，所以列表有内容） |
| 列表 | 真 `GET /api/memories` 返回 3 行；浏览器点开面板渲染 content / `用户` chip / 本地化时间 |
| 二次确认 | 真实点击：行内出现「这是硬删除，**删除不可恢复**——没有回收站，删掉后模型不会再想起这条。」+ 取消 / 确认删除 |
| 删除成功 | 真实点击确认 → 行消失；`curl` 复查后端只剩 2 行；后端日志 `memory forget via api: forgotten`（MEM-4 的结构化审计）；**整页刷新后再打开面板**该条依然不在（证明不是本地隐藏） |
| 删除失败回滚 | **杀掉真后端**后在界面上点确认删除 → 行**回到列表**（回滚）+ 该行确认条显示「删除记忆失败（502）」+ 面板错误条「加载记忆失败（502）」+ 重试；重启后端点「重试」→ 列表恢复一致（2 行） |
| 真空态 | 经界面把两条种子记忆都真删掉 → 面板显示「还没有记忆」（**不是**降级态、无错误条）；`curl` 复查后端 0 行 |
| 降级契约 | 另起一个 `CAPABILITIES={}` 的实例（:8001）：真 `GET`/`DELETE /api/memories` 都回 **503** + `memory capability 未启用：请在 CAPABILITIES 中配置 memory。` —— 与 e2e mock 里那句**逐字一致**（mock 的降级文案不是编的） |
| 视觉 | 暗 / 亮两色 + 行内确认条各截图复核：scope chip、时间、危险色确认条、按钮对比度在亮色下均可读（新增样式全部复用既有 token，无 §15 双份同步问题） |
| 环境还原 | 种子记忆**已全部经界面删掉**（真记忆库里不留假事实——否则模型会把「用户使用 Windows 11」当真的召回）；两个 dev server 已停（避免残留 uvicorn 占 `.instance.lock`，那是已知会打红 `test_web_lifespan_flushes_on_shutdown` 的坑） |

**关单**：#160 **不关**（见 integration prompt §5 / issue comment）：按票面「跨端 ticket 的前端半」
+ §14.12，用 comment 记录已完成部分与剩余项（剩余 = 合入 `main`，由集成 AI 执行）。
本 worktree 与 #155 同一处置。

### 批次审查（B-1，v2 §1.2；fixed point `637bc89`）

两轴（Spec + Standards）各派一个 read-only subagent，各审 `git diff 637bc89...decc7be` 全量
（16 文件 / +1780 行）。**Spec 轴 6 finding + Standards 轴 7 finding**，逐条处置：

| # | 轴 | finding | 处置 |
| --- | --- | --- | --- |
| 1 | 两轴一致 | **重拉的 `limit` 会越过后端硬上界**：`Math.max(rows.length, 50)` 在加载 >200 条后送 `limit=250` → 后端 422 → **删除失败的回滚重拉与"重试"永久失败**（AC3 直接破） | 修：`lib/memory.ts` 新增 `MEMORY_MAX_LIMIT=200` + `refetchLimit(loaded)`（`min(max(loaded,50),200)`），hook 三处调用点改用它；+3 单测（0/3/50 → 50；120 → 120；250/10000 → 200）。已登记的代价：>200 条时重拉只带回前 200，需再点"加载更多"（比永久失败诚实） |
| 2 | Spec | **404 删除失败被界面吞掉**：`deleteError` 按行 id 渲染，而 404（这条已被别处删掉）后重拉里那一行不在 → 错误无处渲染（AC3「失败要报错」不成立） | 修：`MemoryPanel` 增面板级错误条（同一错误唯一来源：行在 → 行内；行不在 → 面板级 + 「知道了」）；e2e 新增 `memoryVanishedIds` 接缝 + 1 用例 ×2 视口锁住 |
| 3 | Standards | **e2e 的 403 detail 是我编的中文**，真后端是 `PermissionError("Memory belongs to a different namespace")`（`sqlite_record_store.py:195` → `str(exc)` 直通）；AC3 的失败用例因此是自我实现 | 修：mock 与断言都改成真后端原文；404 同样照抄 `记忆不存在：<id>`；`api.test.ts` 两处 detail 也换真实原文（顺带当契约文档）。**这就是 #155 轮「mock 语义与真机相反」同一类坑** |
| 4 | Spec | **DELETE 挂死无超时**：行已乐观隐藏、确认条两按钮都 disabled → 行"点了删除就消失"，界面再也点不动（AC3 的失败路径缺失） | 修：新增 `lib/timeout.ts`（`withTimeout`，App.tsx 原先的私有实现迁入共用，fork 行为不变）+ `DELETE_TIMEOUT_MS=30s`；超时走与失败**相同**的回滚/对账路径；+4 单测（含"落地后清定时器"）。语义提醒写在模块头：超时 ≠ 对端没执行 |
| 5 | Spec | ARCH-4b 的说法**过强**（fixture 只能锁前端类型，锁不住后端加字段） | 修文档：集成提示词 §2 改写为「前端侧漂移 → tsc 红；后端侧权威锁 = `tests/web/test_memory_api.py`」 |
| 6 | Spec | AC4 的"检索不可用"在后端契约里**没有独立状态**（列表读权威记录；检索故障会走 500） | 只登记不改代码：503 = 未装配（降级态）；5xx = 读取失败（错误条 + 重试 = 事实上的"暂不可用"）。AC4 的两个词组分别由这两条通道承担 |
| 7 | Standards | `MemoriesState.rows` 导出但无消费方 | 修：去掉该导出（只留 `visible`），并在接口注释里写明为何不导出原始列表 |
| 8 | Standards | `remove` 未复用 `useProjects` 的 `after()` 包装，且未说明 | 修：加注释说明语义不同（`after` = 跑写 → 重拉 → 抛错；这里要"乐观隐藏 → 两个结局都重拉 → 失败先取消隐藏再抛"），**不**硬套 |
| 9 | Standards | `.project-dialog-*` 类名被记忆面板复用（Mysterious Name） | **不改**：改名要连带动 ProjectDialogs + app.css + 既有 e2e 选择器，属跨模块审美重构（§8 Scope Lock），已在 app.css 注明共用材质 |
| 10 | Standards | `describeMemoryError` 与 `describeProjectError` 同形（Duplicated Code） | **不改**：注释已声明刻意分开（两个能力各自演进，共用会让一侧语义渗到另一侧）；属判断项 |
| 11 | Standards | 协议 v2 §1.1 与 `AGENTS.md` §16.1（每票 review）并存矛盾 | **不改 AGENTS.md**（跨 worktree 共用文件，改了徒增 §16 那条已知冲突面）；以用户 2026-09-12 指令为最高优先级，v2 覆盖 §16.1，本工作树以本文件 + 协议文件为准 |
| 12 | Standards | `tsc` / `oxlint` 基线核对 | 实测：tsc 0；oxlint 38 warnings / 0 errors，**新增 1 条**（`useMemories.ts:99` 的 `set-state-in-effect`，与既有 37 条同类；挂载即拉取不可避免） |

**修复后门禁（实跑）**：tsc ✅ · vitest **587 passed**（+7：refetchLimit 3 + withTimeout 4）·
oxlint 0 errors（38 warnings）· playwright **162 passed**（+2：404 用例 ×2 视口）· vite build ✅。
**修复后真机复验**（真后端 + 真 Zilliz + 真浏览器）：种 3 条 → 经界面真删 1 条 → 后端 `curl` 剩 2 条
（DELETE 已走新的 `withTimeout` 包装，零错误条）→ 再删 2 条 → 后端 0 条 + 面板显示「还没有记忆」；
dev server 已停、真记忆库已清空（不留假事实）。

**未做 / 交后续**（Scope Lock，只登记不顺手做）：
① 记忆**编辑** UI（票面非目标；后端入口本票也没有）；② 批量清空 / 回收站 / 恢复（非目标）；
③ 冲突可视化（LLM 决策只体现在最终条目上，非目标）；④ 记忆条目的**检索/搜索**（本票只做分页列表，
用户要的是"看得见 + 删得掉"）；⑤ 真机侧没法自然构造 `403`（需要一条 SESSION scope 记忆，而 HTTP
入口只列 USER 行）——该路径由 e2e 的 `memoryDeniedIds` 拦截口覆盖（伪造的是**真后端会回的那句话**）。

---

## 关单补记（2026-09-12，用户指示「完成了就关闭」）

| 票 | 处置 | 证据 |
| --- | --- | --- |
| #160（MEM-5 前端半） | **已关**（reason=completed） | comment：完整 AC 证据（上一条）+ 关单核实（分支/commit/门禁/真机/移交项）；后端半 #159 已关并随 `165fe9d` 入 `main` |
| #155（WS-5 前端半） | **已关**（reason=completed） | comment：AC1–AC8 逐条证据 + commit 链（`f015a60`→`8db0e5f`→`637bc89`/`dca3ede`）+ 4 条 scope 外剩余项登记；后端半 #153/#154 已关并入 `main` |

核实要点：`main` 已由集成方合入后端批次（`165fe9d`）并回合 `feat/frontend`（`2b51914`，**未动 `web/**`**，
故本文件上方的门禁数字对合并后 HEAD 仍有效）；仓库 **OPEN issue 归零**。
移交集成 AI：`feat/frontend` → `main` 合并 + push + `docs/PHASE_STATUS.md` 回填。
此前各节写的「本票不关单」是当时的 §14.12 处置（跨端票只完成一端），随两端齐备 + 用户指示而更新，
历史小节按「当时事实」保留不改。
