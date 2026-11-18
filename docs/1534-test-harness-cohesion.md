# #1534 测试基建收敛：逐组差异与语义变更登记

本文件是 #1534 的 AC1 落点：`tests/web/_harness.py` 里每个共享 helper 收容了哪些
等价实现、哪些差异被保留为薄 wrapper / 专属实现。原计划写在 PR 正文里，但本分支
没有 PR，故落到仓库内。

```text
范围：1cb3b8babacbfb1b5ca0994ee62ca59065df89c4..<本票 tip>
收窄口径（用户 2026-11-18 01:38 裁决 A）：只做 fixture 自身 teardown 路径的资源
回收；看门狗 os._exit(1) 的进程级回收属 #1702，本票不碰。
```

## `_start_server`（16 份 → 16 个薄 wrapper + 1 份共享实现）

AST 指纹显示 16 份里 13 个不同实现，差异全在"起服务前注入什么"，起停骨架一致。

| 组 | 签名（base） | 返回 | 授权 / 模型差异 | 处理 |
|---|---|---|---|---|
| A | `(tmp_path, monkeypatch)` | 3-tuple | 注入"被调用即失败"的模型工厂（`/metrics`、`/api/health` 不触模型） | `test_metrics` wrapper；本组那份本来就带 30s deadline + reap，**升为共享实现本体** |
| B | `(tmp_path, monkeypatch)` | 3-tuple | `_OneTurnModel` / `_SlowModel` 等不同工厂 | `test_sse_keepalive` / `test_multiturn_queue_http` / `test_web_send_message_stream` wrapper 各传 `model_factory` |
| C | `(tmp_path, monkeypatch, model)` | 4-tuple | `lambda: model()` | `test_web_cancel` / `test_web_stream` wrapper；`tests/test_sse_disconnect.py` 同形 3-tuple |
| D | `(tmp_path, monkeypatch, model_factory=None)` | 4-tuple | 默认 `_OneTurnModel` | `test_web_ws_relay` wrapper（`_SlowStreamModel` / `_BurstThenStallModel` 调用点不动） |
| E | `(tmp_path, monkeypatch, model=FastModel)` | 4-tuple | 默认 `FastModel` | `test_web_stream_follow` wrapper |
| F | `(tmp_path, monkeypatch, model_cls)` | 4-tuple | **原实现忽略 `model_cls`、硬用 `_BashThenTextImpl`** | `test_web_batch51_spec_contract` / `test_web_phase5_approval` wrapper（保持"忽略入参"的原语义，docstring 记明） |
| G | `(tmp_path, monkeypatch)` | 4-tuple | `_ContentTriggeredImpl` | `test_web_messages_interactive_binding` wrapper |
| H | `(tmp_path: Path, *, jwt_secret=None)` | 3-tuple | 自建 app（`_app`），不造模型 | `test_ws_auth` wrapper → `start_test_server(app=...)` |
| I | `(tmp_path, monkeypatch, factory, *, ask_user_timeout, tick_seconds)` | 4-tuple | 额外 monkeypatch（WS 心跳、ask-user ticker）+ Settings 档位 | `test_ask_user_timeout_online` wrapper（`settings_kwargs={"ask_user_question_timeout": ...}`） |
| J | `(tmp_path, monkeypatch, model_kwargs=None)` | 3-tuple | `_OneTurnModel(**kwargs)` | `test_context_usage` wrapper（返回 4-tuple） |
| K | `(tmp_path, monkeypatch)` | 4-tuple | **原实现不传 `allow_testserver`（默认 False）** | `test_web_stream_ordered_delivery` wrapper 显式 `allow_testserver=False` |

### Settings 口径

16 份原实现都写了 `Settings(..., enable_cors=False)`。`Settings` 是 pydantic
`extra="ignore"` 且**没有** `enable_cors` 字段（`hasattr` 实测 False），该实参
本来就被丢弃；`enable_cors` 只在 `create_app(..., enable_cors=...)` 上生效，而这
16 处都没往那里传。⇒ 共享 `make_settings` 不传 `enable_cors`，CORS 装配与 base
逐字一致（都是 `create_app` 默认 `True`），不是语义漂移。那 16 行死参数可以在
后续清理票据里删掉，与本票无关。

### 起服务失败的语义差异（本票唯一授权范围内的收紧）

- 原 15 份：`for _ in range(100): if server.started: break; await sleep(0.05)` +
  `assert server.started`。上界 5s（`test_ws_auth` 是 200 次 = 10s），失败类型
  `AssertionError`，**不 reap**。
- 共享实现：`asyncio.timeout(30.0)`，失败类型 `TimeoutError("Uvicorn did not start
  within 30s")`，抛出前 `should_exit` + cancel + gather serve_task 与 lifespan task
  + `_close_listening_sockets()`（deadline 可能落在「已绑定、未 started」的窗口里，
  那时 serve_task 被 cancel 掉、uvicorn 的 `serve()` 来不及关 `asyncio.Server`）。
- 任务书要求照 `test_metrics.py:39` 的 try/except + `serve_task.cancel()` 模式，
  本票即把它升为共享本体。`tests/web` 与 `tests/test_sse_disconnect.py` 内没有
  任何 `pytest.raises(AssertionError)` 包住 `_start_server`，故无用例依赖旧类型。
- reap 后抛什么（按实测）：deadline 到期 → `TimeoutError`；轮询发现 serve_task 先结束
  → 原样上抛它自己的异常（`test_start_server_propagates_startup_failure` 钉的就是
  `RuntimeError` 透传）；调用方自己被取消 → `CancelledError`。注意 uvicorn 的
  `startup()` 内部失败时通常先把 serve_task 取消掉，那时调用方看到的是
  `CancelledError`，原始异常只留在 uvicorn 的 traceback 里。
- 已知边界：真实 uvicorn 的 lifespan 启动失败会 `sys.exit(3)`，这个
  `SystemExit(3)` 由事件循环直接抛出（不经 helper 的 `except`），pytest 把它记在
  该用例上、进程继续。helper 的 reap 只管 `Exception` / `CancelledError`；这条
  边界写在 `start_test_server` docstring 里。

## `_shutdown`（15 份 → 1 份共享实现）

15 份 AST 指纹完全相同（12 份带 `-> None`、3 份不带，AST 等价）：
`server.should_exit = True; serve_task.cancel(); try: await serve_task except CancelledError: pass`。

**语义变更（登记）**：共享实现改为**先优雅停（grace 1.0s）再兜底 cancel**。

- 为什么：立即 `cancel()` 时 uvicorn 来不及关监听 socket、也跑不到 lifespan
  shutdown ⇒ watchtower scheduler / ask-user ticker 留在事件循环里，端口用例结束后
  仍不可重绑。这是 AC2 的实证缺口，不是纯重构。
- 实测对照（同一台机，有在途 SSE 流）：旧式 `0.00s / cancelled=True /
  port_free=False`；新式 `0.19s / cancelled=False / port_free=True`。
- 取消来源不变：uvicorn shutdown → `AppState.shutdown()` → `RunManager.aclose()`，
  只是时序推迟至多 `grace` 秒；可观察断言不变。
- 代价：带常驻 SSE/WS 连接的用例，teardown 多约 1s（常态 0.1–0.5s）。
- 已知边界：走 cancel 分支时只保证 serve_task 结束与监听 socket 关闭，不保证
  lifespan shutdown 跑完。`_close_listening_sockets()` 在取消/超时两条路径上补关
  监听 socket（`uvicorn.Server.serve()` 在 `main_loop()` 被 cancel 时不会关）。

## `_create_session`（21 份 → 19 份薄 wrapper + 2 份专属实现）

| 组 | 载荷 / 签名（base） | 文件 | 处理 |
|---|---|---|---|
| 1 | `(client)`，`task="hi"` + 默认 budget | `test_artifacts_api` / `test_workspace_export_api` / `test_workspace_files_api` | `create_session(client)`；后两者 `extra={"sandbox_backend": "local"}` |
| 2 | `(client)`，加 `model="vision-probe"` | `test_attachments_api` / `test_fork_attachment_d1` | `create_session(client, model="vision-probe")` |
| 3 | `(client, *, model="vision-probe")` | `test_send_message_attachments` | 传 `model=` |
| 4 | `(client, *, model=None)` | `test_mm03_gates_and_limits` | 传 `model=` |
| 5 | `(client, *, workspace=None)` | `test_projects_api` | `extra={"workspace": …}` |
| 6 | `(client, **payload)` | `test_session_delete_api` / `test_web_session_cwd` | `extra=payload` |
| 7 | `(client, **payload)`，`task=_SECRET_TASK` | `test_session_archive_api` | `task=_SECRET_TASK, extra=payload` |
| 8 | `(client, **payload)`，`task="建立一个会话"` | `test_purge_stale_tools_api` | `task="建立一个会话", extra=payload` |
| 9 | `(client)`，`task="建立一个会话"` | `test_526_approvals_workflow_api` | `task="建立一个会话"` |
| 10 | `(client)`，`?launch=false` | `test_evidence_api` / `test_goal_api` / `test_retention_api` | `create_session_no_launch` |
| 11 | `(client, app)`，`?launch=false` | `test_task_delivery_api` | 同上（`app` 形参保留，调用点不动） |
| 12 | `(client, tmp_path, name)`，cwd 目录断言 | `test_task_lease_api` | 建目录后 `create_session_no_launch` |
| 13 | `(client, app, *, permission_mode)` + `SESSION_STARTED` 断言 | `test_web_session_permission` | 只收敛建会话，保留事件断言 |
| — | `(client, tmp_path)` + `_settle_progress` | `test_progress_api` | **保留专属**（建后等进度落定） |
| — | `(port, task, cwd)` 异步 SSE 流式 | `test_web_ws_relay` | **保留专属**（不是 TestClient 形态） |

> 提交信息 `2b1c018af` 写的"21 份 `_create_session` 收敛"应为 **19 份收敛 + 2 份
> 专属保留**；两份专属在上表末两行。历史提交信息不改（不 rewrite），在此更正。

## 未纳入的同类块（刻意不动）

- `tests/web/test_run_pause_resume_api.py:825-940`：单个用例内的 inline server 块
  （自建 app + 自带 gate 编排，teardown 仍是旧式立即 cancel）。
- `tests/recovery/_approval_http_ack_kill_child.py:113-126`：子进程脚本里的 inline
  server 块（靠 `asyncio.run` 整进程退出来回收）。

两处都不是票面枚举的 `_start_server` helper，且"不为建 conftest 搬全部测试"是本票
明确的非目标。

**按 helper 名枚举时不在范围内的同类块**（票面口径是"按 helper 名收容"，这些块
不是独立 helper、或 helper 名不在枚举名单里，故本票不动；记下指针供后续清理票）：

- `tests/web/test_session_list_workspace.py:43-57` 的 `_create`：内联
  inline-SSE → `_DATA_PREFIX` 解析，与 `test_progress_api` / `test_artifacts_api`
  同形；名字不叫 `_create_session`，未在枚举名单内。
- `tests/web/test_artifacts_api.py:386-397`、`tests/web/test_progress_api.py:118-121`：
  用例内的 inline `_DATA_PREFIX` 解析（各自仍带 `_DATA_PREFIX = "data:"` 常量）。
- `DATA_PREFIX` 在 4 个测试文件里各自维护一份同值常量（上列三处 + `_harness.py`）；
  统一为共享常量属后续清理票候选（本票已加注释标注同值，不改调用点）。

## SSE 红线

不带 deadline 的 SSE 读辅助器（`httpx2.AsyncClient(timeout=None)` + 裸
`async for response.aiter_lines()`）**没有**进共享层；`_harness.py` 里只有
`session_id_from_sse(resp)`，解析的是**完整响应体** `resp.text`，不读流。
