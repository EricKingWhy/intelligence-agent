# 验收车道环境（真实浏览器验收的**前提**）

> 起因：2026-09-11 第六轮验收时，同一批控件在前几轮「能点」、这一轮「不可达」。
> 根因不是代码，而是 **`:8000` 上跑着哪个 worktree 的后端**决定了语料与能力集。
> 本文把这条车道的前提写死，避免下次再有人对着「不可达」误判为产品缺陷。

## 1. 车道构成

| 角色 | 值 |
| --- | --- |
| 前端 | `D:\intelligence-agent-frontend`（branch `feat/frontend`），dev server `:5173` |
| 后端 | `:8000`——**由启动者决定是哪个 worktree**（见 §3） |
| 代理关系 | `web/vite.config.*` 把 `/api` **硬编码**代理到 `http://127.0.0.1:8000`，不读环境变量 |

推论：**`:8000` 上是谁，这条车道就在验谁。** 前端只是如实渲染后端给的事件与能力。

## 2. 期望能力集（`/api/capabilities` 是唯一判据）

启动后先查一次，期望至少：

```bash
curl -s http://127.0.0.1:8000/api/capabilities
# 期望 count=2: websearch + multiagent
```

- **`websearch`**：`web_search` 工具、Context Provider 目录等。
- **`multiagent`**：**`delegate` 工具**。缺它则委派/子会话整块前端 UI **按设计不渲染**
  （零伪造，符合不变量 #21），于是这 5 个控件不可达：委派行展开、`复制子会话 ID`、
  `Inspect 子会话`、`打开子会话`、child 视图 `Run` 返回。

## 3. 两个 worktree 的 `.env` 差异（本文件存在的原因）

`.env` 按 AGENTS.md §13.1.6 属**不在 worktree 之间同步**的本地文件，所以能力集天然可能不同。
2026-09-11 实测：

| worktree | `CAPABILITIES` |
| --- | --- |
| `D:\intelligence-agent`（main） | `websearch` + `multiagent`（enabled） |
| `D:\intelligence-agent-backend`（feat/backend） | **已对齐为** `websearch` + `multiagent`（2026-09-11 改，原为仅 `websearch`） |

**已做**：把 `feat/backend` 的 `CAPABILITIES` 逐字节对齐到 main 的值（取 main 的那一行原样写入，
不用 sed 拼 JSON）。理由是**车道必须验当前分支的代码**——本会话已有实证：main 的后端**没有**
`feat/backend` 的 `trace_url` 改动（`/api/sessions` 不返回该字段），当时必须杀掉它换成本分支后端，
否则 ARCH-4b 的前端契约锁在真机上根本验不了。用 main 后端还有一个更隐蔽的代价：
**main 落后于本分支的修复时会「真机通过」，制造假阴性**。

## 4. 想复现前几轮的语料怎么办

前几轮的部分结论建立在 **main 后端**的语料上（例如第三轮「加载更早 200→410」用的是
410 事件的 fork child `1fdac9b9`；委派/子会话的真机结论同理）。feat/backend 的语料是另一套
（2026-09-11 时为 11 个会话、最大 35 事件）。

**切换步骤**（任选一侧，注意一次只能有一个进程占 `:8000`）：

```bash
# 1) 停掉当前占用者
PID=$(netstat -ano | grep LISTENING | grep ":8000" | head -1 | awk '{print $NF}'); taskkill //F //PID $PID

# 2A) 换 main 后端（大语料：70+ 会话，含 410 事件 child）
cd /d/intelligence-agent && uv run uvicorn agent_harness.web.app:create_app --factory --host 127.0.0.1 --port 8000

# 2B) 换回本分支后端（**验当前分支代码时用这个**）
cd /d/intelligence-agent-backend && uv run uvicorn agent_harness.web.app:create_app --factory --host 127.0.0.1 --port 8000
```

⚠️ `.env` 改动与两个后端的差异**都不会进 Git**（`.env` 本地文件），所以**换后端后必须重新查
§2 的 `/api/capabilities`**，不能凭记忆假设能力集。

## 5. 验收前的三条检查

1. `:8000` 上跑的是**你想验的那个分支**吗？（验 feat/backend → 用 `D:\intelligence-agent-backend`）
2. `/api/capabilities` 是否符合 §2 期望？不符合就先修 `.env` 再启动，别把「配置缺能力」记成产品缺陷。
3. 会话语料够不够触发你要验的条件？（`加载更早 N 条` 需 >200 事件；委派需 `multiagent`；
   Trace 三件套需 Langfuse 启用——本部署未配，因此那三项**恒不渲染**且属正确行为。）

## 6. 相关记录

- `docs/FRONTEND_ISSUES_LOG.md` 第六轮：`OBS-016`（委派/子会话为配置不可达的完整证据链）、
  `BUG-008`（前端修复）、`BUG-009`（后端 P1：非流式 run 的回退在带闸时必崩）。
- `docs/ACCEPTANCE_CONTROL_INVENTORY.md`：110 控件清单（含各自的渲染条件）。
