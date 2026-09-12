# 集成提示词 — WS-6 / WS-7：目录会话 + 宿主目录选择器（#169 / #170，**只做完后端半**）

> **给集成 AI 的可执行摘要**：本批在 `D:\intelligence-agent-backend`（分支 `feat/backend`）只完成了
> **两张跨端票的后端半**。两张 issue **都还 OPEN**（前端半未做，§14.12 纪律：只完成一端不关单）。
> 请按 §0 顺序操作；§5 是给前端 AI 的契约坑点清单（**不要漏**）。
> 来源文档：`docs/PRD_WS6_WS7_DIR_ROOTED_SESSION_AND_DIR_PICKER.md`（契约矩阵）、
> `docs/adr/0027-session-cwd-arbitrary-directory.md`、`docs/adr/0028-host-directory-listing-endpoint.md`、
> 根目录 `CONTEXT.md`（Workspace / Project / Directory-rooted Session 正名）。
> 进度单一事实源：`docs/SDD_TICKET_TRACKER.md` 的「B-2」批次记录（含两轴审查 findings 与处置）。

---

## 0. 一句话与操作顺序

本批让**会话可以落在任意已存在的真实目录**（`POST /api/sessions { cwd }`，自动注册为项目并归组），
并新增一个**只读的宿主目录列举端点**（`GET /api/host/dirs`）驱动前端目录选择器。

```text
1) feat/backend → main（先合，§14.9 一次只合一条分支）
2) main → 启动完整项目 + 全量 pytest（§2 的门禁命令）
3) main → feat/frontend（把本批 docs 带过去：PRD / ADR / CONTEXT / 本文件）
4) 前端 AI 做 #169 AC9–AC14、#170 AC8–AC13（§5 的坑点必须先读）
5) 前端半各自门禁 + 真机（真浏览器点选）通过后，**两端齐备才关单** #169 / #170
```

**两个不要做**：① 不要因为后端半完成就关单（§14.12）；② 不要在前端参数里"为了显式"传默认权限档（§5.1）。

---

## 1. 后端半交付了什么

| Ticket | Commits | 内容 |
| --- | --- | --- |
| #169 WS-6 | `50e96a4`（文档）→ `561b553`（实现）→ 收口见 `9c158c9` | `POST /api/sessions` 新增可选 `cwd`（与 `workspace` 互斥）；合法 `cwd` → 会话 root 与 `session/started.data.cwd` = realpath；自动注册项目（title = 目录末段名）+ 归组（幂等）；`POST /api/projects` 响应新增 `sessions_attached` 并补齐 cwd 匹配的既有会话（软删除 → 重注册闭环） |
| #170 WS-7 | `21c0b06` → 收口见 `9c158c9` | 新模块 `src/agent_harness/web/host_dirs.py` + `GET /api/host/dirs`：`path` 缺省 → 盘符/根列表（可注入 `ROOTS_PROVIDER`）；给定 `path` → 一层直接子目录（仅目录、depth=1、按名排序、`parent` 供向上、盘根 `parent=null`）；错误矩阵 422/404/422/403 不冒 500；条目上限 500 + `truncated`；symlink 照列一个条目不展开 |
| 收口 | `9c158c9` | B-2 批次两轴审查 findings 的最小修复（跨平台绝对路径 / OSError 不冒 500 / 账本剪枝保命 / NUL / 文档一致性）+ 测试补强 + 变异验证 |

变更文件（后端，供 diff 复核）：
`src/agent_harness/sandbox/paths.py`（新 `is_absolute_path`）、
`src/agent_harness/session/{errors,projects,service}.py`、
`src/agent_harness/web/{app,domain_errors,projects,host_dirs}.py`、
`src/agent_harness/workspace/index.py`、
`tests/web/{test_web_session_cwd,test_host_dirs_api,test_domain_error_mapping}.py`、
`tests/workspace/test_workspace_index.py`、`tests/sandbox/test_paths.py`。

---

## 2. 门禁证据（后端，已实跑）

```bash
cd D:\intelligence-agent-backend
.venv/Scripts/python.exe -m ruff check src/ tests/      # All checks passed
.venv/Scripts/python.exe -m pytest -q                   # 2109 passed / 10 skipped / 42 deselected / 0 failed
```

**已知既有 flaky（非本批引入，不要当成回归）**：
`tests/test_web_api.py::test_disconnect_leaves_run_running_and_cancel_stops_it`
（SSE 断连时序竞争，5s 预算随机器负载偶发悬挂；已用 `git stash` 在 pristine HEAD 复现；OBS-9.3 与
`PHASE_STATUS` #152/#154 已三次登记）。它在本批最后一次全量中**通过**。

**真机证据**（真 uvicorn + 真模型，`WORKSPACE_DIR` 指向隔离临时目录，未触碰仓库真 `harness.db`；
临时目录已清理）：
- WS-6：`cwd=D:\_b2real\proj` 建会话 → 模型用 `read` 工具以**相对路径** `hello.txt` 读到真实文件并逐字复述
  （相对路径能成功 = cwd 真的换过去了）；`started.cwd=D:\_b2real\proj`；项目自动注册 `title=proj` 且会话归组；
  校验矩阵 6 条 detail 逐字一致；软删除 → 重注册 `sessions_attached=2`、幂等重放 `0`，目录与文件原样。
- WS-7：根模式返回真实 `C:\` / `D:\`（`path=null`、`parent=null`）；一层列举排除文件与嵌套、按名排序、
  `parent` 正确；501 个目录 → `truncated=true` 且 500 条（排序后前缀）；非绝对 422 / 不存在 404 / 是文件 422 /
  含 NUL 422 逐字一致；跨源 `Origin` → 403、`localhost` → 200。

---

## 3. 后端 API 契约（前端要消费的形状）

### 3.1 `POST /api/sessions`（新增 `cwd`）

```jsonc
{ "task": "…", "cwd": "D:\\repos\\my-project", "max_steps": 5 }   // workspace 与 cwd 互斥
```

- `workspace`（旧，单段目录名）与 `cwd`（新，**已存在**的绝对目录）**只能二选一** → 同时非空 422
  `workspace 与 cwd 只能二选一`。
- `cwd` 非绝对 → 422 `cwd 必须是绝对路径：'<原值>'`；不存在 → 422 `目录不存在：<规范路径>`（**不代创建**）；
  是文件 → 422 `不是目录：<规范路径>`；含 NUL → 422 `cwd 含非法字符（NUL）：'<原值>'`。
- 成功：SSE 流照旧（**注意 `session/started` 不在 create 的 SSE 流里**——它在订阅建立前已落盘；
  前端要拿 cwd 走 `GET /api/sessions/{id}/events`，这也是重连时的同一条路）。

### 3.2 `POST /api/projects` 响应新增字段

```jsonc
{ "id": "…", "path": "D:\\repos\\my-project", "title": "my-project", "status": "ok",
  "session_ids": ["…"], "created_at": "…", "updated_at": "…",
  "sessions_attached": 2 }   // 本次调用**新**归入的会话数（幂等重放 → 0）
```

注册（含幂等命中既有项目）后，所有 `header cwd == 项目路径` 的既有会话会被补进账本——
`DELETE /api/projects/{id}`（软删除）→ 重新注册即可恢复分组。

### 3.3 `GET /api/host/dirs`

```jsonc
// ?path 缺省 → 根模式
{ "path": null, "parent": null, "truncated": false,
  "entries": [ { "name": "C:\\", "path": "C:\\" }, { "name": "D:\\", "path": "D:\\" } ] }

// ?path=D:\repos → 一层直接子目录（仅目录；按 name 大小写不敏感排序；盘根 parent=null）
{ "path": "D:\\repos", "parent": "D:\\", "truncated": false,
  "entries": [ { "name": "alpha", "path": "D:\\repos\\alpha" } ] }
```

- 前端据 `path === null` 判定"当前在根"（据此禁用「向上」）。
- 条目 `path` 是**父目录 + 名字**拼出的链接本身（symlink 不展开；进入时后端按真实目标解析，仍是一层）。
- 错误 detail（**原样显示，不要翻译**）：422 `path 必须是绝对路径` / 404 `目录不存在：<规范路径>` /
  422 `不是目录：<规范路径>` / 403 `无权限访问：<规范路径>` / 422 `path 含非法字符（NUL）` / 422 `路径不可用：<规范路径>`。
- 超过 500 个子目录 → `truncated: true` + 截断后的前 500 条（排序前缀）→ **UI 必须显示"已截断"提示**。
- 闸：`require_trusted_origin`（与项目端点同一份）——本地信任模式下只接受本机 `Origin`。

---

## 4. 两轴审查的结论与处置（B-2，fixed point `80d49e1`）

- **Spec 轴**：`NEEDS-FIX` → 1×P1（跨平台绝对路径判定：三处都用 `PureWindowsPath(...).is_absolute()`，
  POSIX 上拒掉一切合法绝对路径）+ 5×P3（空白 `cwd` 语义、403 文案变更、`max_length` 造出矩阵外 list 形状
  detail、ADR 与 PRD 对条目 `path` 措辞冲突、两处测试假绿）。**全部已修 / 已文档化**。
- **Standards 轴**：`NEEDS-FIX` → 3×P2（`attach_matching_sessions` 剪枝会永久删掉 header 暂时读不到的成员 /
  `host_dirs` 只抓 `PermissionError` 其余 OSError 冒 500 / 根模式在事件循环上跑同步 I/O）+ 3×P3（NUL、
  `exists`/`isdir` 吞权限错误、测试缺口）。**全部已修**，其中账本剪枝一条有**变异验证**
  （改回旧行为 → 新锁 1 failed；源码 sha256 逐字节还原）。
- **有据不改**（审查方也认可）：`MAX_ENTRIES` 全量物化后才截断（截断契约要求"排序后的前缀"）；
  `PureWindowsPath` 在 Windows 侧的既有口径；`ROOTS_PROVIDER` 作为测试 seam。

---

## 5. 给前端 AI 的契约坑点（**先读这段再动手**）

1. **默认权限档必须不发 `permission_mode`**（后端语义：显式传非 DANGER 档 = 切**交互式审批**，
   每个工具调用都会弹审批卡）。项目内新建任务的确认面在用户**留在默认档**时 payload **不含**
   `permission_mode`；只有用户主动改档才发。既有 `web/src/lib/api.ts` 的映射
   （`p.permission_mode ? ['permission_mode', p.permission_mode] : null`）已是这个形状，照用即可。
2. **确认面的路径明示必须逐字**：`Agent 将直接读写该目录：<项目规范路径>`（#169 AC10，e2e 断言原文）。
3. **目录浏览器不要试图取浏览器的真实绝对路径**：`<input webkitdirectory>` 只给 fakepath、
   `showDirectoryPicker()` 句柄没有路径——只能走 `GET /api/host/dirs` 自绘（ADR-0028 Context）。
4. **错误留在原地**：#169 AC12 要求 422 留在确认面可重试；#170 AC9 要求 403/404/422 用后端
   `detail` **原文**（不翻译、不改写）。
5. **项目 `status=missing-dir` 时入口照常出现**，点击后由后端 422 明示（前端不预判）。
6. **手填路径保留为完整后备**（双向同步：浏览器点选 → 回填输入框；手改输入框回车 → 浏览器跳转）。
7. **e2e 用状态 mock**（先例 `e2e/r-project-groups.spec.ts` / `s-memories.spec.ts`）：为
   `/api/host/dirs` 增加可编程假目录树分支；playwright 必须 `--workers=2`。
8. 前端门禁（§16.6）：
   `cd web && npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build`。

---

## 6. 合入后的验证清单（集成侧）

- [ ] `git diff main...feat/backend` 复核：只含上表文件 + `docs/`；无 `.env` / secrets / 未相关清理。
- [ ] 本地 `main` 合并后：`ruff check src/ tests/` + 全量 `pytest` → 0 failed（忽略 §2 的既有 flaky）。
- [ ] 启动完整项目（真 `.env`）跑一遍真机：项目行「在此项目中新建任务」→ 任务落到项目真实目录
      （会话内 `read` 一个只有该目录才有的相对路径文件）；「新建项目」用目录浏览器点选注册成功。
- [ ] 两张 issue 的 comment 是否已写清"后端半 + 前端半"的当前状态；**两端齐备后**才 `gh issue close`。
- [ ] 更新 `docs/PHASE_STATUS.md`（合入 `main` 后的进度单一事实源）。

## 7. 未决 / 仅报告（不在本批修）

- 前端半未做（#169 AC9–AC14、#170 AC8–AC13）——两票保持 OPEN。
- `GET /api/host/dirs` 的暴露面 = "这台机器有哪些目录"，与"本地信任模式下 UI 本就可注册任意目录"
  同阶；**若后端不在用户本机部署，本端点必须随部署形态重估**（ADR-0028 Consequences 已记）。
- 存量 `workspaces_root/<uuid>` 会话**不迁移**（ADR-0027 D4）：它们继续 Ungrouped，用户可用既有的
  「加入项目…」手动归组。
- 既有 CORS `*` 漏洞（#154 已列为独立 ticket 候选）与本批的来源闸是两套机制，本批未动。
