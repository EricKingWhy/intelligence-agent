# ADR-0038 — 测试隔离：复位 sse_starlette 的进程级关机闩锁 `AppStatus.should_exit`

**Status**: Accepted（2026-09-20）
**Date**: 2026-09-20
**Related**: 原遗留项「Web/SSE 跨用例状态泄漏（精确机制未定位）」；
`docs/troubleshooting/SSE_TROUBLESHOOTING.md` §1（其上一版结论被本 ADR 推翻）；
`docs/review_ledger.tsv` B-21 行；`docs/SDD_TICKET_TRACKER.md` B-22 行（其「117 → 7 由 B7/#275 修好 110 条」
的归因被本 ADR 撤销）；第三方库 `sse_starlette`（`AppStatus`）
**决策授权**: 用户 2026-09-20 批准处理该项遗留（「Web/SSE 跨用例状态泄漏的精确机制还没定位到根因」）。
本 ADR 的决策**只落在测试隔离层**，不改产品代码、契约或对外行为。
**真实证据**: 见下表（对象全在本机，可复算）。

| 运行 | 命令 | 例数 / 失败 | 备注 |
| --- | --- | --- | --- |
| 闩锁未复位 + 探针 | `pytest -q -p wbi_probe3` | 2635 / **131** | 日志里**唯一一次**翻转 |
| 闩锁未复位、无探针 | `pytest -q` | 2635 / **67**、2635 / **68** | 同签名，两次独立复现 |
| **闩锁已复位** + 探针 | `pytest -q -p wbi_probe4` | 2635 / **0** | 翻转计数 **0** |
| **闩锁已复位**、无探针（本批终局树） | `pytest -q` | 2635 / **3** | 3 条全部是 `tests/evaluation/*` 的 `SystemExit(1)`（FS 批量删除守卫），**与本 ADR 无关**；闩锁族为 **0** |
| 必要性/充分性实验 | `.workbuddy/smoke_20260920/probe_latch.py` | — | `VERDICT: NECESSARY_AND_SUFFICIENT` |

---

## Context

### 1. 症状

全量串跑时 `tests/web/*` 大面积红，主签名是
`AssertionError: SSE 流里没有 session_id：[]`，次签名是
`httpx2.RemoteProtocolError: peer closed connection without sending complete message body (incomplete chunked read)`。
**同一批文件单独跑全绿**。读数量级在 0 到 131 之间浮动（本机先后测到 0 / 5 / 67 / 68 / 117 / 131），
所以它长期被记成「跨用例状态泄漏」，并被当作环境噪声放过。

### 2. 机制：一个**进程级、单向、不提供复位路径**的第三方全局量

`sse_starlette.sse.AppStatus.should_exit` 是一个**模块级布尔量**（`sse.py:184`）。库只在两处把它置 `True`：

- `_shutdown_watcher`（`sse.py:134`）——每个事件循环一个任务，每 **0.5 s** 轮询一次
  「uvicorn `Server.should_exit` 是否已置位」；
- `AppStatus.handle_exit`（`sse.py:214`）——`uvicorn Server.handle_exit` 被 sse_starlette 在 import 时打补丁而来。

**库不提供任何复位路径**——它的语义是「这个进程要退出了」，一次性事实。

链条：`EventSourceResponse.__call__` 里的 `_listen_for_exit_signal` 只要看到 `should_exit=True` 就**立刻返回**
⇒ `cancel_on_finish` 取消任务组 ⇒ `_stream_response` 已经发出 `http.response.start`，却在生成器吐出**第一帧之前**
被取消。表现出来就是 **HTTP 200 + 零 `data:` 帧**——调用方拿到的响应「成功但空」，
与「对端提前关闭」是同一根因的两种可见形态。

### 3. 本仓库为什么一定会踩到

本仓库有 **11 个用例**为了让一个**真实 uvicorn 服务**停下来而直接写 `server.should_exit = True`：

`tests/test_sse_disconnect.py`、`tests/web/test_web_cancel.py`、`tests/web/test_web_batch51_spec_contract.py`、
`tests/web/test_sse_keepalive.py`、`tests/web/test_multiturn_queue_http.py`、`tests/web/test_context_usage.py`、
`tests/web/test_web_stream.py`、`tests/web/test_web_send_message_stream.py`、`tests/web/test_web_phase5_approval.py`、
`tests/web/test_web_ws_relay.py`、`tests/mcp_client/test_client_lifecycle.py`。

写 `server.should_exit = True` 本身是**合法**的用法。问题在于它同时是那个 0.5 s 轮询 watcher 的**触发源**：
一旦轮询窗口与这次置位重叠，闩锁被翻成 `True`，**此后本进程内所有用例**都排空收流。
这解释了全部三个此前互相矛盾的观察：

| 观察 | 由本机制解释 |
| --- | --- |
| 只在本文件/本模块单独跑时全绿 | 小集合里没人翻闩锁（实测两文件子集 `RC=0` 且 `should_exit=False`） |
| 全量串跑才现形，且量级浮动 | 翻转是 0.5 s 轮询 vs 毫秒级置位的**竞态**；翻不翻决定了「0」还是「几十上百」 |
| 失败集中且签名一致，看上去像「互相污染」 | 它们不是互相污染，是**共享同一个被翻转的进程级闩锁** |

### 4. 三条被排除的假说（都曾经很像，各留一条受控实验给后人）

| 假说 | 受控实验 | 结果 |
| --- | --- | --- |
| 外部进程持 `.agent/workspace/.instance.lock` ⇒ fail-closed 启动失败 | 用一个只取该锁的进程持有它，再跑 `test_workspace_files_api + test_web_stream + test_web_api` | **RC=0** — `locksub.out`（36 passed / 70.45 s）、`locksub3.out`（70 passed）⇒ **否** |
| 机器 CPU 负载 | 8 路 CPU 占满 + web 子集 | **RC=0** — `load8.out`（36 passed / 88.54 s）⇒ **否** |
| 根级 web 测试之间的顺序污染 | 根级 23 个文件 + 目标文件 | RC=0，但**本轮未留存独立落盘产物**（只有当时的结论）⇒ **按未证实处理**，不计入排除清单 |

> 区分机制靠的不是上表，而是 D4 的**必要性/充分性实验**（同进程内只翻这一个布尔量就能复现 0 帧、复位即可恢复）。
> 上表的用途是排除「看起来很像」的干扰项。另：`InstanceLock` 是 OS 级 advisory lock
> （Windows `msvcrt.locking` / POSIX `flock`），**不是 pid 文件锁**——工作区里残留一个 `.instance.lock`
> 文件**无害**，文件里的 pid 只用于诊断。这条曾把一次调查带偏。

### 5. 为什么必须写下来（否则下次会被"清理"掉）

修复是一行看起来**完全多余**的赋值（`AppStatus.should_exit = False`，指向一个第三方模块的私有全局）。
不知道机制的人**极可能把它当死代码删掉**——删掉之后 SSE 会重新大面积红，量级还是浮动的、
还是"单模块全绿"，于是又会走一遍「跨用例状态泄漏 / 环境噪声」的老路。
一条**看起来像噪声的失败**和一条**真正的噪声**在读数上无法区分，这是本次最贵的教训。

---

## Decision

### D1：测试侧复位这个闩锁；**不改产品代码**

`tests/conftest.py` 增加 autouse 夹具 `_reset_sse_shutdown_latch`，在**每个用例前后各一次**把
`AppStatus.should_exit` 复位为 `False`。

**不修产品代码是决策，不是省事**：真实部署里「服务器正在关机 ⇒ 排空所有 SSE 流」是
`sse_starlette` 的**正确**行为，本项目也**依赖**它（优雅关停）。错的是**测试**让这个
「进程要退了」的一次性事实泄漏给了后续用例。因此在**离真实语义最近的一层**（用例边界）复位，
而不是去改库或包装产品代码。

### D2：夹具在用例**前后各复位一次**

前一次保证用例不继承上一个用例的闩锁，后一次保证本用例的泄漏不传染下去。两次都是幂等的纯赋值。
`import` 失败时静默跳过（离线子集里没有该模块，也就没有闩锁可复位）。

### D3：这层隔离**不承担**「证明竞态窗口不存在」的责任

夹具**收口**的是「一次性闩锁跨用例永久生效」这个**放大机制**，不是那个 0.5 s 竞态窗口本身。
单次「没翻转」的运行**不能**证明窗口不存在。该残余的解除条件见 Consequences。

### D4：可执行的判据（防删）

- 必要性/充分性：`.workbuddy/smoke_20260920/probe_latch.py` —— 同一进程里
  **闩锁 OFF ⇒ 200 + 6 帧**（含 `session_id`）、**闩锁 ON ⇒ 200 + 0 帧**、
  **再复位回 OFF ⇒ 200 + 6 帧**。⇒ `NECESSARY_AND_SUFFICIENT`，并且**复位有效**。
- 全量：闩锁未复位 2635 / 67（另一次 68；带探针 131，日志里唯一一次 `FLIP False->True`
  落在 `tests/test_sse_disconnect.py::test_client_disconnect_during_tool_execution_does_not_cancel`
  的 teardown）；复位后 2635 / **0**，翻转计数 **0**。

---

## Consequences

**正面**

- 「全量串跑 SSE 大面积红」有了**可判定、可复现**的解释，不再是环境噪声。
- 该族失败全部消除（2635 / 0）。
- 复位只碰一个第三方全局，**零行为契约改动**，不触及任何产品代码。

**负面 / 代价（必须承认）**

- 夹具依赖 `sse_starlette` 的**私有**全局量（`AppStatus` 是库内部状态，不是公开 API）。
  上游若改名/移除，夹具会退化成静默 no-op（`ImportError` 分支）——**失败模式是"悄悄不再生效"**，
  所以 ADR-0038 这份记录本身就是防线，且本 ADR 的第 4 节判据要留着随时复算。
- **竞态窗口仍然存在**：复位是"每次用例后擦干净"，不是"让窗口不发生"。
  残余解除条件：连续 3 次全量串跑 + 探针，`FLIP` 计数恒 `0` 且该族失败恒 `0`；
  或人为拉长窗口构造一条**确定性**用例，把它钉成回归。

---

## Non-Goals

- **不改 `sse_starlette`**（第三方库，不打补丁、不做 wrapper）。
- **不改产品代码**：不改 `EventSourceResponse` 的使用方式，不改优雅关停语义（见 D1）。
- **不改那 11 个用例**写 `server.should_exit = True` 的写法——它们是**合法**用法，
  真正的缺陷在"进程级闩锁被测试边界泄漏"，不在它们。
- **不把该族失败当成 `xfail`/`skip` 处理**：那是把红灯改绿灯。
- **不动 `tests/evaluation/*` 的 3 条 `SystemExit(1)`**：那是沙箱批量删除守卫与测试收尾写盘的交互，
  与闩锁无关，另行登记（解除条件：非沙箱环境复跑该文件）。

---

## Alternatives considered

| 方案 | 为什么没选 |
| --- | --- |
| **①（已选）用例边界复位 `AppStatus.should_exit`** | 在离真实语义最近的一层收口；零产品改动；判据可执行（D4）。 |
| **② 改产品代码**：自己包一层 `EventSourceResponse`、或用库的其它入口 | 会把「服务器关机 ⇒ 排空流」这个**正确**行为一起改掉，等于为了测试去动产品契约（违反 Scope Lock / §8）。 |
| **③ 把那 11 个用例改成不设 `server.should_exit`**（换个方式停服务） | 治不了根：闩锁由 0.5 s 轮询置位，触发源可以有**很多**种（任何让真实 uvicorn 进入关机路径的写法都算）。改一处，下一个人再写一处，同样的红会回来。 |
| **④ 把该族失败标 `xfail` / `skip`** | 用关闭规则换绿灯，直接违反项目红线。 |
| **⑤ 什么都不做，当环境噪声** | 事实是它**不是**噪声：它让整批 `tests/web` 失去信号。本次的读数量级（0–131）已经证明"噪声论"会让真实的 SSE 回归被淹没。 |
| **⑥ 在 pytest 启动时一次性复位**（`pytest_configure`） | 不够：闩锁是在**用例执行过程中**被翻的，只在启动复位等于只擦了开工前一次。必须逐用例。 |
| **⑦ 用 `-p no:cacheprovider` / 进程隔离（`--forked`、每文件一个进程）** | 代价大（全量时间显著上升）且治标——它把问题从"同进程泄漏"换成"更多进程"，而根因仍在；且换掉的是**测试拓扑**这条基础设施，不是本项遗留的施工面。 |
