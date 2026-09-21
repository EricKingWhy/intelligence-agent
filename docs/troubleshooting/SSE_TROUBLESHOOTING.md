# SSE 套件级失败排障手册

> 适用范围：本仓库的 FastAPI + Uvicorn + `sse-starlette` 流式测试与本地联调。
> 事件、端点、`seq`、`after_seq`、`stream/truncated` 和 detached-run 契约以
> `docs/BACKEND_CONTRACT_STREAMING_UI.md` 为准。本文件记录排障事实和判别顺序，
> 不定义新的运行时协议。
>
> 最后核验：2026-09-20。不要读取、打印或复制 `.env` 中的任何凭据值。

## 0. 先看现象，不要先猜根因

| 现象 | 第一检查 | 不要直接归因 |
| --- | --- | --- |
| 单独测试通过，套件中首帧没有 `session_id` | **先查 `sse_starlette` 的 `AppStatus.should_exit` 是否已被翻成 `True`**（§1 / ADR-0038）；再看测试顺序、server/lifespan 收尾、残留 task | Docker、JSONL 或当前业务 diff |
| 没有逐字输出、数据集中到达 | `curl -N`、响应头、网关 buffering | 模型没有流式输出 |
| 断连后 run 没有取消 | 当前 detached-run 契约与 `/cancel` 调用 | SSE producer 一定泄漏 |
| `seq` 重复或缺口 | `after_seq`、持久化 events、重连游标 | 只按 `event_id` 猜测 |
| `stream/truncated` | backlog 阈值和 `latest_seq` | 把控制帧当成 durable SessionEvent |
| SSE 500/序列化错误 | 三个入口是否共用 serializer、首个异常栈 | 再造一套响应序列化 |
| E2E 假红 | 5173/8000 上实际运行的 clone 和 server | 先改产品代码 |

## 1. 已确认的套件级失败形态

曾观察到：Web/SSE 大集合或并行运行时，多条用例报首帧没有 `session_id`；相同或相近用例单独运行可以通过。

**根因已定位（2026-09-20；本节上一版结论被推翻）**：`sse_starlette.sse.AppStatus.should_exit` **就是**一个跨实例共享的**进程级**模块全局量（`sse.py:184`，初值 `False`）。库只在两处把它置 `True`——`_shutdown_watcher` 轮询到某个 uvicorn `Server.should_exit` 已置位（`sse.py:134`）、或 `AppStatus.handle_exit` 被调用（`sse.py:214`）——**且不提供任何复位路径**（它的语义是「这个进程要退出了」）。本仓库有 **11 个**用例为了让真实 uvicorn 服务停下而写 `server.should_exit = True`（`tests/test_sse_disconnect.py:133`、`tests/web/test_web_cancel.py:55`、`tests/web/test_web_stream.py:54`、`tests/mcp_client/test_client_lifecycle.py:210` 等）⇒ 一旦那个 0.5 s 轮询窗口与这次置位重叠，闩锁被翻 `True` 并**在进程内永久生效**，此后**每个** `EventSourceResponse` 都在吐出首帧之前被取消 ⇒ **HTTP 200 + 零 `data:` 帧**（即 §0 表里那一行「首帧没有 `session_id`」），或连接被提前关闭（`incomplete chunked read`）。

> ⚠ 本节上一版的结论（「当前版本没有跨实例共享的 `AppStatus.should_exit`，优先怀疑生命周期残留」）**是错的**，已删除：它只静态检查了 `uvicorn.Server` 与 `LifespanOn`，**漏了 `sse_starlette.AppStatus`**。

判据、必要性/充分性实验、三条被排除的假说（外部 `.instance.lock` / CPU 负载 / 根级测试顺序）与全量读数见 `docs/adr/0038-test-isolation-reset-sse-shutdown-latch.md`。测试侧修复 = `tests/conftest.py` 的 autouse 夹具 `_reset_sse_shutdown_latch`（用例前后各复位一次）；**产品代码不动**（真实部署里「服务器关机 ⇒ 排空流」是正确行为）。

下面这些**仍然是事实**，只是不构成本现象的主因：

- 每个 `uvicorn.Server` 有自己的 `should_exit`、`force_exit` 和 `server_state`；
- `uvicorn.lifespan.on.LifespanOn` 另有自己的 `should_exit`、startup/shutdown 状态；
- `AppState` 每个 app 实例独立，`shutdown()` 会关闭 RunManager 和 capability wiring；
- 多个测试文件各自复制 server 启动/关闭 helper。

因此「未完成的生命周期收尾、连接、后台 run 或 task 残留」仍值得查，但**不是**本现象的主因——主因是上面那个进程级闩锁。

## 2. 推荐判别顺序

### 2.1 先确认失败类型

记录：

- server 是否 `started`；
- HTTP status 和 SSE response headers；
- 是否收到 `: ping`；
- 是否收到首个 `data:`；
- 是否中途断开；
- `run/completed` 或 `run/failed` 是否落盘；
- 是否有 `ReadTimeout`、`No response returned`、`CancelledError` 或 pending task。

不要把这些情况统称为“ SSE 失败”。

### 2.2 从单例到最小组合

先跑单文件，再跑顺序组合，最后才扩大范围：

```bash
uv run pytest -q tests/web/test_sse_keepalive.py -vv
uv run pytest -q tests/test_sse_disconnect.py -vv
uv run pytest -q tests/web/test_sse_keepalive.py tests/test_sse_disconnect.py -vv
uv run pytest -q tests/test_sse_disconnect.py tests/web/test_sse_keepalive.py -vv
uv run pytest -q tests/web/test_web_stream.py tests/web/test_web_cancel.py tests/web/test_sse_keepalive.py tests/test_sse_disconnect.py -vv
```

如果只有组合失败，优先建立生命周期污染证据，不要反复重跑完整套件。

### 2.3 逐个 server 记录生命周期状态

对失败前后的 server/app 记录：

- `server.started`、`server.should_exit`、`server.force_exit`；
- `server.server_state.connections` 和 `server.server_state.tasks` 数量；
- `serve_task.done()` / `serve_task.cancelled()`；
- lifespan 的 shutdown/error 状态；
- `app.state.agent._closed`；
- RunManager 是否仍有 active run/task；
- stream/client context 是否已经退出。

不要把这些内部观测变成产品 API；它们只用于测试诊断。

### 2.4 验证 shutdown 顺序

优先验证以下顺序：

```text
退出 response/client stream context
→ 等待测试需要的 run 终态
→ server.should_exit = True
→ 等待 serve_task 自然返回
→ 仅在明确需要时再取消 task
```

历史 helper 常见的危险形态是“设置 `should_exit=True` 后立即 `serve_task.cancel()`”。这可能打断 FastAPI lifespan 的 `AppState.shutdown()`、RunManager 收口、EventSourceResponse generator finally 或 socket 清理。

这条是排查实验和修复方向，不代表任何未经测试的全局结论。

### 2.5 区分测试层

`TestClient` / ASGITransport 适合 JSON、headers 和完整 response 断言，但不等价于真实中途断连。真实流式断连应使用：

```text
uvicorn.Server + asyncio.create_task(server.serve())
+ httpx2.AsyncClient + response.aiter_lines()
```

自定义 `_DisconnectingASGI` 只能证明应用对模拟 `http.disconnect` 的处理，不能替代真实 TCP 测试。

### 2.6 最后再检查 SSE 实现

只有在 server/lifespan 收尾正常后，才检查：

- EventSourceResponse 是否收到 `http.disconnect`；
- generator finally 是否 unsubscribe；
- ping 是否按约定发送；
- `sse-starlette` 版本和 Uvicorn 版本是否匹配；
- 是否发生重复取消或跨 task 的 anyio cancel scope 使用。

## 3. 当前契约中的常见误判

- detached-run 中客户端断连不等于显式取消；取消应走 `/cancel`。
- reasoning 不出现可能是 provider 没有输出 reasoning，不应伪造 reasoning 事件。
- `stream/truncated` 是 transient 控制帧，不是 durable SessionEvent。
- 网关 buffering 会让真实 SSE 看起来“不流式”，但不必然是后端 producer 错误。
- 测试读到目标帧后立即退出时，后台 run 可能仍在运行；进入 shutdown 前要明确是否需要等终态。
- 在单测稳定、专项稳定且没有当前 diff 因果证据前，不得把套件红归因于 Docker、JSONL、#261 或 #262。

## 4. 证据记录模板

```text
Incident YYYY-MM-DD / short-name

环境：
分支/commit：
测试命令：
endpoint：
session/run：
现象：
原始脱敏证据：
单例结果：
组合结果：
server/lifespan/task 状态：
已验证：
未验证：
判定边界：
下一步：
是否需要新 ticket / ADR：
```

命令输出中只允许出现非敏感诊断信息；不要打印 `.env` 值、Authorization、API key、密码或 token。

## 5. 文档边界

- SSE/WS 端点和事件契约：`docs/BACKEND_CONTRACT_STREAMING_UI.md`；
- 架构机制决策：ADR；
- 一次性集成步骤：`docs/INTEGRATION_PROMPT_*.md`；
- ticket、commit、门禁和残余：`docs/SDD_TICKET_TRACKER.md`；
- 运行排障事实：本文件。

不要在本文件复制整段架构规格，也不要把未实测猜测写成根因。
