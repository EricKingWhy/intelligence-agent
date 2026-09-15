# 集成提示词：WebSocket 实时流传输（WIP + 6 缺陷修复 + #205/#206 补全闭环）

> **交付对象：Git Integrator**（在 `D:\intelligence-agent` 的 `main` 上执行合并 + push）。
> 日期：2026-09-15 ｜ 前端工作已完成、门禁全绿、**未 merge、未 push、未建 PR**。
> 按 AGENTS.md §13/§14：合并与 push 由 Integrator 执行；本 Agent 只做本地 commit。
> 对应 issue：**#207**（集成票，本文件就是它的执行依据）；被集成的是 **#205** / **#206**。

---

## 0. 仓库拓扑（本次最容易踩的坑）

三个目录是**独立 clone**，不是同一个 repo 的 worktree。成果在**前端 clone 的本地分支
`integrate/ws-stream`** 上——它**没有**推到 origin：

| 目录 | branch | 说明 |
| --- | --- | --- |
| `D:\intelligence-agent-frontend` | **`integrate/ws-stream`** | **本批全部成果**（本文件所在处） |
| `D:\intelligence-agent` | `main` | 集成目标 |
| `D:\intelligence-agent-backend` | `feat/backend` | 本批**未动后端**，仅参考 |

**HEAD 请现场解析，不要照抄本文件**：

```bash
git -C D:/intelligence-agent-frontend rev-parse --short integrate/ws-stream   # 本批 HEAD
git -C D:/intelligence-agent          rev-parse --short main                  # 集成目标
```

本次写作时的实测：前端 `integrate/ws-stream` = `60da04c`（含 `2dfc5f9`），距 `origin/main`
**4 个 commit**；`origin/main` = `e4da691`。

---

## 1. 这批改了什么

本分支 = `origin/main` 之上的 **4 个 commit**（`git log origin/main..integrate/ws-stream` 的顺序）：

| # | commit | 内容 | 说明 |
| --- | --- | --- | --- |
| 1 | `907e10e` | WIP checkpoint：`lib/wsStream.ts`（+230）+ `useSession.ts` 三处接流点切 WS（+78/−22） | 交付层攒包导致 SSE 通道无法流式（实测响应头 44.2s / 41.6s 才到 ≈ run 全长）。原本是 main worktree 的在途未提交改动，被存成 WIP commit |
| 2 | `afb3254` | merge `ws-stream-fallback` → `integrate/ws-stream`；冲突仅 `useSession.ts`，解决后相对第二父 +167/−22 | **独立 review 的 6 处修复与新增的 `wsStream.test.ts`（+268）是在这一步落的**（游标 / 悬挂 promise / 空集基线 / 终态集合重复 / `liveSidRef` / 魔法数）——不是机械合并 |
| 3 | `2dfc5f9` | **#205 + #206** 主体（42 files，+833/−236） | 本批新增工作，见下 |
| 4 | `60da04c` | 两轴 review 收口（9 files，+396/−28） | 见 §3 |

> `907e10e` 的父是 `9ce0b47`（更早的 main），但 `9ce0b47` 已是本分支基点的祖先，所以第 1 条
> commit 实际只带入 `wsStream.ts` 与 `useSession.ts` 两个文件——旧 main 的差别没有被混进来。

**#205**（`flushQueue` / `scheduleReconnect` / `doTruncatedRebuild` 切 WS）：

- 「立即发送全部」不再 `await` 攒包的 SSE 响应体 —— 改为「攒包判别 + WS 接流」，
  与既有 `sendFollowUp` 同形；带**本会话对话的真实 max seq** 作游标（避免快照重放的
  旧终态把新 run 误判成"已收口"）。
- `wsStream.ts` 新增「零服务帧建连失败 → 自动降级 HTTP SSE」（WS 不可用时的显式行为）。
- 404 语义（会话已删）改由存在性探测 (`sessionExists`) 承担——WS 错误帧不带状态码。
- 顺带修掉传输切换引入的两个真实回归：重连横幅退化成永不出现、`exhaustive-deps`。

**#206**（e2e WS mock）：`#206` 正文里「Playwright 1.63 的 `routeWebSocket` 在本环境
拦不到 `/api/ws`」的结论**不成立**——真因是注册没被 `await`（`void page.routeWebSocket(...)`）。
已用 smoke 对照实验确认（awaited 拦得到、void 的拦不到），因此**不需要页内 shim**，
`routeApi` 改为 async 并全量 `await`（39 个 spec 各加一处 `await`）。

### 1.1 合并footprint（vs 当前 main，实测）

```
46 files changed, 1841 insertions(+), 255 deletions(-)   （4 A / 42 M）
  42 M  web/**   = web/e2e/** 40（39 个 spec 各加一处 await + fixtures.ts）
                 + web/src/hooks/useSession.ts + web/src/hooks/useSession.test.ts
   4 A  web/e2e/queue-flush.spec.ts
        web/e2e/stream-fallback.spec.ts
        web/src/lib/wsStream.ts
        web/src/lib/wsStream.test.ts
   2 M  docs/BACKEND_CONTRACT_STREAMING_UI.md   （新增 GET /api/ws 契约章节）
        docs/E2E_SCENARIO_MAP.md                （车道说明 + 计数 + 两条新 spec 行）
```

**没有触碰**：后端源码、`tests/`、`AGENTS.md`、`CLAUDE.md`、`docs/PHASE_STATUS.md`。

### 1.2 冲突预判：**零冲突**（已 dry-run，不是估计）

```bash
git -C D:/intelligence-agent-frontend merge-tree --write-tree --name-only origin/main integrate/ws-stream
# → 只输出 tree hash（e26f682…），无冲突文件名 ⇒ 干净合并
```

---

## 2. 合并步骤（§14.6 先回后正）

```bash
# 1) 把前端 clone 的分支取进 main 仓库（跨 clone，走路径 fetch；老办法，见 HANDOFF_PROMPT_REGISTRY）
git -C D:/intelligence-agent fetch D:/intelligence-agent-frontend \
    integrate/ws-stream:refs/remotes/fe-ws/ws-stream

# 2) 若 main 比本批基点更新：先在前端 clone 里 git merge main（§14.6）再重跑门禁；
#    本次 dry-run 为干净合并，直接走 3) 也可，但**以现场 `merge-tree` 结果为准重跑一次**。

# 3) 合并（保留合并点，便于回溯）
git -C D:/intelligence-agent merge --no-ff refs/remotes/fe-ws/ws-stream

# 4) 校验
git -C D:/intelligence-agent diff --check          # 无 whitespace / 冲突标记
git -C D:/intelligence-agent status --short        # 干净
```

---

## 3. 合并后必须跑的门禁

### 3.1 前端（在 `D:/intelligence-agent/web`）

```bash
cd D:/intelligence-agent/web
npx tsc -b
npx vitest run
npx oxlint
npx playwright test --workers=2      # 必须 --workers=2（4 worker 有资源竞争型抖动）
npx vite build
```

**期望值（本次在前端 clone 实测，逐条一致才算过）**：

| 命令 | 期望 |
| --- | --- |
| `tsc -b` | 0 错 |
| `vitest run` | **882 passed / 50 files** |
| `oxlint` | **0 error**（44 warning 为既有 React Compiler 类告警，与 main 同量级） |
| `playwright --workers=2` | **364 passed / 0 failed**（182 × 2 档，41 个 spec） |
| `vite build` | ✓ built |

### 3.2 后端（本批未动后端，作回归）

```bash
cd D:/intelligence-agent && ruff check . && python -m pytest -q
```

期望：与 main 集成前一致（本批不新增后端测试；后端 keepalive 改动是另一条线，见
`feat/backend` 的 `7bd6c4a`）。

---

## 4. 真机验证点（合 main 后、push 前建议扫一遍）

| 场景 | 观察点 | 判据 |
| --- | --- | --- |
| 提交任务 / 续聊 | 正文**阶梯增长**（不是一次性到齐） | 交付层攒不住 WS 帧 |
| 队列「立即发送全部」 | 点下去**立刻**开始出字 | 旧实现在这里静默卡到 run 结束（#205 P1） |
| 断线重连 | 「连接中断，正在重连…」条出现→数据回来即消失 | 传输切换后本条曾退化成永不出现，已修 |
| WS 被代理拒（若可构造） | 不静默：会降级到 HTTP 流，且超时如实报「连接中断」 | 见 `docs/BACKEND_CONTRACT_STREAMING_UI.md` 的降级说明 |

---

## 5. 风险与未尽事项（如实划界）

1. **WS 快照无 backlog 上限**（后端落差，已开票 **#208**）：切到 WS 后
   「backlog > 1000 → `stream/truncated` 全量重建」在 WS 主通道上**不可达**（只剩
   降级流会走到）。功能不丢（seq 幂等吸收重复），但超大会话一次握手塞整段日志。
   前端**未**单方面限流（会是第二套语义）。已在传输边界注释 + 契约文档记明。
2. **降级路径不是"能用"、是"不静默"**：攒包交付层下降级流的帧同样要到 run 结束才到，
   停摆检查（10s）会先判停摆 → 重连 3 次 → 如实报错。让停摆检查认识"已降级的传输"
   属传输策略改动，与 #208 一并留待决策（真机攒包本机造不出来，e2e 只锁到
   「降级接通 + truncated 重建」这一层）。
3. **后续前端 spec 必须 `await routeApi(...)`**：漏掉不会报错，只会让 WS mock 静默
   失效（spec 在"看起来跑了"的情况下考错路径）。若还有别的前端分支在写 e2e，合并前
   请检查。
4. **`onStreamGet` 语义已收窄**：它现在**只**服务降级兜底与 `stream/truncated`
   控制帧；实时流一律用 `onWs`。别按旧文档拿它驱动 live 流。
5. **`stream/truncated` 的 e2e 覆盖是经降级通道**取的（`stream-fallback.spec.ts`）：
   主通道（WS）本来就不发这个帧（见 1），所以这是**当下唯一忠实**的构造方式。

---

## 6. Ticket 状态

| issue | 状态 | 备注 |
| --- | --- | --- |
| #205 | 已关（comment 记录 commit + 证据） | 前端纯票；集成由 #207 执行 |
| #206 | 已关（comment 记录 commit + 证据） | 同上；正文里关于 `routeWebSocket` 的结论已更正 |
| #207 | **OPEN（留给 Integrator）** | 合 main + 门禁 + push（push 需用户批准）后关单 |
| #208 | OPEN（新开的后端票） | WS 快照加 cap / 复用 `stream/truncated`，见 §5.1 |

---

## 7. 本 Agent 明确**没做**的事

- 未 `git merge`、未 `push`、未建 PR、未删分支/worktree。
- 未改后端任何文件（#205 范围明确「不动后端」）。
- 未更新 `docs/PHASE_STATUS.md`（它是合入 main 后的进度单一事实源，由集成环节记录）。
