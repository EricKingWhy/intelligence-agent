# #376 memory-v2 写锁超时：取证战役与机械判定（2026-10-10）

- **执行器**：CodeBuddy / deepseek-v4.1-flash（Agent 在 Muse 系统服务器；测试在云端沙箱 `shell/ia-test`，4 vCPU）
- **日期**：2026-10-10
- **树**：`a932927d315414d104659085ff72efe51168dcf0`（tree `df506e6d49e32e3d48b7d14bcfef6c1924a78d56` = 当日 `origin/main`）
- **Issue**：#376「[Testing] 全量 pytest 下 memory-v2 写锁超时红」
- **结论**：**未复现**（memory-v2）；机械判定 = 环境/墙钟项；按 AC 二选一登记已知 flake（签名，**不生效**），不硬改代码。

---

## 0. 结论摘要

1. **未复现 memory-v2 写锁超时**。一次满载串行全量（7262 passed / 0 failed）中，memory-v2 的写事务**最大连续持锁 105.3 ms**（p50 3.0 ms / p90 5.7 ms / p99 13.3 ms；3057 次 `BEGIN IMMEDIATE`），距 `BUSY_TIMEOUT_MS = 10_000` **约 95×**；全仓所有 SQLite 库合计最大 233.5 ms。⇒ 票面签名（`BEGIN IMMEDIATE` 等满 busy_timeout 后报 `database is locked`）要求**≥10 s 的连续持锁**，本引擎正常执行中不成立。
2. 战役途中唯一一次 `database is locked` 落在**另一个站点**（`harness.db` 的 `SqliteOperationLedger.initialize()` 之 `PRAGMA journal_mode=WAL`），且经 A/B 判定为**取证探针自身造成的伪影**：首版探针的 witness 线程每 50 ms 对每个"有活连接"的库另开连接并发 `BEGIN IMMEDIATE`；实验（§4）机械证明"另有连接持 `BEGIN IMMEDIATE` 时 `PRAGMA journal_mode=WAL` 必报 `database is locked`"。改为**非侵入式 witness**（仅在被跟踪写事务在途时才开连接）后，同命令同负载复跑 **0 failed**。
3. 按 AC 二选一：**未复现 ⇒ 登记已知 flake**（§8.6 第 3 条表，签名 + 不生效状态），**不硬改代码、不伪造修复**。
4. 附带两项 scope 外发现（§6）：memory-v2 写路径**未挂**仓内既有 `retry_on_busy`/`StorageBusyError`（#515 模式）；`-n 4` 全量收集因 `test_ws_auth.py` 的时间戳参数化而**非确定**。二者均**未擅改**，报请用户裁决。
5. 门禁：本仓 Gate-0 **6/6 PASS**（40.1s；收据 `docs/gate/341414a41b74e4f09c2009093ac94fa02700002a.json`）；覆盖闸门 `scripts/check_review_coverage.py` **exit 0**（docs-only 自动归属）。

---

## 1. 票面与验收条件（逐字）

> 现象：全量 pytest 下，memory-v2 的写锁超时红：
> `sqlite3.OperationalError: database is locked`
> `src/agent_harness/memory/v2/store.py:632: in update_settings` → `await connection.execute("BEGIN IMMEDIATE")`
> 调用链：`web/memory.py:544 patch_memory_settings` → `capability.py:247 update_settings` → `store.py:632 update_settings` → 500。

**当前树锚点核对**（树 `a932927d`）：
- `src/agent_harness/memory/v2/store.py:632` = `await connection.execute("BEGIN IMMEDIATE")`（`update_settings`，**未变**）；
- `src/agent_harness/memory/v2/capability.py:247` = `return await self._store.update_settings(`（**未变**）；
- `src/agent_harness/web/memory.py` 的 `patch_memory_settings` 从历史 `:544` 移到 **`:463`**，其 `service.update_settings(...)` 在 **`:469`**（无 `try/except`，`OperationalError` 直接冒泡 ⇒ HTTP 500）。

**AC**：
- [ ] 指名持锁者（连接/用例/事务跨越多长），**或**给出"环境项"的机械判定依据（能指到并发/时钟/工作树的具体量）。
- [ ] 依结论二选一：修掉（含回归钉 + 红证），或按协议 §8.6 登记已知 flake（带签名）。
- [ ] 定位过程可复跑：命令 + 树 + 每轮完整日志落盘。

---

## 2. 取证装置（env-gated pytest 插件 `t376_probe`）

无侵入方式无法既"命名持锁者"又不改产品源码，故用 **env 门控的 pytest 插件**（`T376_PROBE=1` 时装载，否则零行为）。装置见本目录 `t376_probe.py.txt`，设计要点：

- aiosqlite 的全部排队调用都汇聚到 `aiosqlite.Connection._execute(fn, *args)`（`aiosqlite/core.py:150`；`execute`/`commit`/`rollback`/`close` 全走它）。**包裹 `_execute`** 即可把每次 `BEGIN IMMEDIATE` 与其后的 `COMMIT`/`ROLLBACK`/`close` 配对，测出**写事务连续打开的墙钟时长**，并在 `BEGIN` 处抓 `traceback.format_stack()`（= 持锁者的调用点）。
  - 记录：`write_hold`（每次完成、≥`T376_PROBE_FLOOR_MS`）、`long_write_hold`（≥`T376_PROBE_HOLD_MS`，含栈）。
  - **前任探针的 bug**：它把内部签名函数赋给了 `aiosqlite.Connection.execute`（公有名）而非 `_execute`，导致 `memory_v2` 初始化 `init_failed`（`sqlite3` 报 `'str' object is not callable` 类）。本役已修正为 patch `Connection._execute`。
- **witness 线程**（本役两版）：
  - **v1（侵入式）**：每 `T376_PROBE_POLL_MS` 对**每个有活连接**的库另开 `sqlite3.connect` + `busy_timeout=1` + `BEGIN IMMEDIATE`，`SQLITE_BUSY` ⇒ 落 `write_lock_busy_detected` 并快照活 holder。**问题**：它在被测进程里额外开连接、并发持写锁 ⇒ 会扰动系统（见 §4）。
  - **v2（非侵入式，本役主用）**：**仅当 `_holders` 非空**（即有被跟踪的 `BEGIN IMMEDIATE` 在途）时才开 witness 连接。`PRAGMA journal_mode=WAL` 发生在连接初始化、无 `BEGIN IMMEDIATE` 在途 ⇒ witness 此时**不会**开连接 ⇒ 不再可能干扰它。

---

## 3. 战役（全部在云端沙箱 `shell/ia-test`，固定树 `a932927d`）

每轮命令与原始日志见本目录 `commands.txt` / `s1-serial.txt` / `s3-serial.txt` / `s4b-main.txt` 等。代码经 `git bundle create t376.bundle HEAD` + `sbx --cloud cp` 送入沙箱，`uv sync --locked --all-extras` 建 venv（Python 3.13.12），`PYTHONPATH=<repo>/src[:探针目录]`、`no_proxy`/`NO_PROXY` 大小写均清。

### S1 — 串行全量 + 探针 v1（侵入式 witness）+ 2 燃烧器
- 命令：`python -m pytest tests/ -q -p no:cacheprovider -p no:randomly -p t376_probe`（`T376_PROBE_HOLD_MS=100`）。
- 读数（逐字）：**`1 failed, 7261 passed, 28 skipped, 51 deselected in 513.85s (0:08:33)`**。
- 唯一红：`tests/recovery/test_constraint_input_crash.py::test_malformed_constraint_tool_result_fails_budget_recovery_closed[result_data4]`，失败点 = `src/agent_harness/storage/sqlite.py:176` `await connection.execute("PRAGMA journal_mode=WAL")`（`SqliteOperationLedger.initialize()`）报 `sqlite3.OperationalError: database is locked`（库 = 该用例 `tmp_path/harness.db`，**非** memory-v2）。
- **memory-v2 无任何 `long_write_hold`**；全轮最大 `long_write_hold` = 105.7 ms（`recovery.db`，delegation_tree 并发预留用例）。
- 判定：该红**不可归因**（拟态见 §4）——仅此一次，且站点与票面不同。

### S2 — 隔离 A/B（失败用例文件，**无探针**，2 燃烧器）
- 命令：`python -m pytest tests/recovery/test_constraint_input_crash.py -q ...` ×3。
- 读数：**`10 passed` / `10 passed` / `10 passed`（3.86 / 2.79 / 2.88 s）**。
- ⇒ 该文件在隔离下稳定绿。

### S3 — 串行全量 + 探针 v2（**非侵入式** witness）+ 2 燃烧器（主读数）
- 命令：同 S1（探针升级为 v2）。
- 读数（逐字）：**`7262 passed, 28 skipped, 51 deselected in 733.82s (0:12:13)`**，**0 failed**。
- 持锁分布（`BEGIN IMMEDIATE`，3057 次 ≥2 ms；见 `probe-s3.jsonl`）：
  - **p50 = 3.0 ms，p90 = 5.7 ms，p99 = 13.3 ms，max = 233.5 ms**（全体库）。
  - **memory-v2.db：504 次持锁，max = 105.3 ms**（`memory/v2/jobs.py:285` 的 `claim()` CAS，经 `runner.py:542 _pump`，加载下排队）。
  - 全部 ≥100 ms 者 5 条：`105.3 memory-v2.db`、`105.7 / 131.8 / 187.2 / 233.5 recovery.db`（后者为 `delegation_tree` 的**刻意并发**预留用例）。
- witness 触发 `write_lock_busy_detected` 139 次，均在**被跟踪写事务在途**时，无 empty-holder 长窗口。
- ⇒ 满载全量下 memory-v2 写事务最大持锁 **105.3 ms**，**距 10 s busy_timeout 约 95×**。

### S4 / S4b — `-n 4`（派工默认）全量门禁读数
- **S4**（`-n 4`，无探针）：**收集期即失败**——xdist 报 `Different tests were collected between gw1 and gw2`，逐字差异 = `tests/web/test_ws_auth.py::test_refuses_bad_bearer_when_secret_configured[Bearer <JWT>]` 等参数 id 中的 JWT 载荷 `exp` 在 `...985` / `...986`（**秒级时间戳**）间漂移 ⇒ 各 worker 收集到的参数 id 不同。`2 errors in 18.33s`（**与 #376 无关的既有测试基建非确定性**，见 §6）。
- **S4b**（`-n 4 --ignore=tests/web/test_ws_auth.py`，随后该文件串行）：
  - S4b main：**`7227 passed, 28 skipped in 196.82s (0:03:16)`，0 failed**（`s4b-main.txt`）。
  - S4b ws_auth（串行）：**`35 passed in 9.31s`**（`s4b-wsauth.txt`）。二者合计 **7262 passed**，与 S3 的通过数一致、**0 failed**。

---

## 4. S1 命中 = 仪器伪影（机械实验）

S1 的 `PRAGMA journal_mode=WAL` 报 `database is locked`，必要条件是"另有连接在该库上持有写锁"。S1 的 witness v1（侵入式）**正是**在无写事务时也持续开连接并发 `BEGIN IMMEDIATE`。为判定因果，做最小机械实验（本机 `/tmp`，纯 `sqlite3`）：

| 实验 | 场景 | 结果 |
|---|---|---|
| A | 另一连接**仅打开**（无事务）时，新连接 `PRAGMA journal_mode=WAL` | **成功**（返回 `wal`） |
| B | 另一连接持 **`BEGIN IMMEDIATE`** 时，新连接 `PRAGMA journal_mode=WAL`（`timeout=1.0`） | **`sqlite3.OperationalError: database is locked`** |

⇒ 只要 witness 恰在该用例 `PRAGMA journal_mode=WAL` 的瞬间持有写锁，即**必然**复现完全相同的失败。S1 的 witness v1 满足该前提（它在 `connect()` 刚登记路径后即可能开连接并发 `BEGIN IMMEDIATE`）；S3 的 witness v2 不满足（`initialize()` 无 `BEGIN IMMEDIATE` 在途 ⇒ 不开连接）⇒ S3 全绿。**结论：S1 的唯一红是探针伪影，不是产品缺陷，不计为复现。**

（诚实边界：本判定基于"v1 满足前提 + 实验机械复现 + v2 同条件全绿"三者的合取。未逐次回放 S1 的纳秒级时序，故表述为"由探针自身造成"，而非"每一纳秒都已证死"。）

---

## 5. 机械判定（环境项）

票面机制成立的前提是"另一同进程连接真实持写锁 >10 s"（`busy_timeout=10_000` 睡满后 `sqlite3_step` 返回 `SQLITE_BUSY`；见 §7 来源①逐字）。本役可指认的具体量：

| 量 | 值 | 来源 |
|---|---|---|
| `BUSY_TIMEOUT_MS` | `10_000` ms | `src/agent_harness/memory/v2/_sqlite.py:33,41` |
| memory-v2 写事务最大连续持锁（满载串行全量） | **105.3 ms** | S3 `probe-s3.jsonl` |
| memory-v2 持锁 p50 / p90 / p99 | 3.0 / 5.7 / 13.3 ms | S3 |
| 全仓所有 SQLite 库最大持锁 | 233.5 ms | S3 |
| 样本量 | 3057 次 `BEGIN IMMEDIATE`（其中 memory-v2 504 次） | S3 |
| 加载条件 | 沙箱 4 vCPU + `scripts/_t508_burn_cpu.py 2`（2 核死循环） | S3 |

⇒ **要触发票面签名需一次 ≥10 s 的连续持锁，实测上界比它小约 95×（memory-v2）/ 43×（全体）**。在本引擎的正常执行路径（每次写 = 新连接 + `PRAGMA busy_timeout` + `BEGIN IMMEDIATE` + 立即 commit/close；写事务内**不含**任何网络/嵌入调用）中未观察到任何接近该阈值的持锁。历史唯一一次（2026-09-27，Windows，`c998b222`）与"外部 docker 服务由缺席转在场"这一**环境变化**同时发生（Issue comment-1），且该次全量与其后一遍的**测试集不同**（12 条依赖外部服务的用例由 skip 转真跑）——即两次读数不可比。⇒ 判**环境/墙钟项**。

---

## 6. 附带发现（scope 外，**未擅改**，报请裁决）

### 6.1 memory-v2 写路径未挂仓内既有 `retry_on_busy` / `StorageBusyError`（#515 模式）
- 仓内 `src/agent_harness/storage/sqlite.py:77` 定义 `retry_on_busy`，其 `StorageBusyError` docstring 明写：*"调用方（web 层）要按类型把它翻成结构化 503（暂时性故障、可稍后重试），**500 会把它伪装成服务端 bug**"*（来源 #515）。该装饰器已挂到 `storage/sqlite.py` / `storage/delegation_tree.py` / `transport/contract.py` / `workspace/lease_store.py`（`grep -rn retry_on_busy src/` 实测）。
- `src/agent_harness/memory/v2/*.py` **零命中**：memory-v2 写路径既无 `retry_on_busy`，也无 `OperationalError`→503 的映射 ⇒ 锁超时直接 500，与票面症状同形。
- **为何不擅改**：AC 的"修掉"以复现为前提；本役未复现 ⇒ 不硬改（§9.1.1）。且这是**行为变更**（重试策略 / 503 映射），需用户裁决。**建议**：按 #515 既有模式把 `retry_on_busy`（或等价的锁超时→503 映射）扩到 memory-v2 写路径。

### 6.2 `-n 4` 全量收集非确定（与 #376 无关）
- `tests/web/test_ws_auth.py` 的参数化把**秒级 `exp`** 编进 JWT 串（param id 含完整 token）⇒ 各 xdist worker 在不同秒收集 ⇒ `Different tests were collected between gw1 and gw2`，`-n 4` 全量**在收集期即失败**。修法（另票）：参数化用 `indirect`/稳定 id，或在收集期冻结时钟。

---

## 7. 成熟产品一手来源（机制与契合点）

读取日期 2026-10-10；均为官方文档/源码，逐字引用。

1. **SQLite 官方 · `sqlite3_busy_timeout`**（`https://www.sqlite.org/c3ref/busy_timeout.html`）
   > "This routine sets a busy handler that sleeps for a specified amount of time when a table is locked. The handler will sleep multiple times until at least "ms" milliseconds of sleeping have accumulated. After at least "ms" milliseconds of sleeping, the handler returns 0 which causes sqlite3_step() to return SQLITE_BUSY."
   - **机制**：`busy_timeout` 睡满后仍拿不到锁 ⇒ `sqlite3_step` 返回 `SQLITE_BUSY`（Python 层翻成 `OperationalError: database is locked`）。⇒ 报错当刻**锁被连续持有 ≥ 整个超时**。**契合点**：这正是把"报 10 s 超时"机械等价于"有人持锁 ≥10 s"的依据，也定义了本役要找的量（连续持锁时长）。

2. **SQLite 官方 · Write-Ahead Logging §2.2 / §9**（`https://www.sqlite.org/wal.html`）
   > §2.2 "However, since there is only one WAL file, there can only be one writer at a time."
   > §9 "When the last connection to a particular database is closing, that connection will acquire an exclusive lock for a short time while it cleans up the WAL and shared-memory files. … An exclusive lock is held during recovery."
   - **机制**：WAL 下"单写者"，且换 `journal_mode`/收尾/崩溃恢复会取**排他锁** ⇒ 存在若干"短暂 BUSY"路径。**契合点**：解释了 §4 实验 B（`PRAGMA journal_mode=WAL` 撞并发写锁即 BUSY），也界定了"环境项"的候选机理（收尾/恢复锁），但它们**短**、不足以撑满 10 s。

3. **CPython 官方 · `sqlite3`**（`https://docs.python.org/3/library/sqlite3.html`）
   > `timeout` (*float*) – "How many seconds the connection should wait before raising an OperationalError when a table is locked. If another connection opens a transaction to modify a table, that table will be locked until the transaction is committed. Default five seconds."
   > `sqlite3_errorcode` – "The numeric error code from the SQLite API"；`sqlite3_errorname` – "The symbolic name of the numeric error code from the SQLite API"（Added in version 3.11）。
   - **机制**：`timeout` 即 `busy_timeout` 的 DBAPI 面；且异常带 `sqlite_errorcode`/`sqlite_errorname` 可精确归因。**契合点**：`aiosqlite.connect(path)` 走 DBAPI 默认 `timeout=5.0`，随后 `_sqlite.connect` 用 `PRAGMA busy_timeout=10000` 覆盖为 10 s。另可支撑"未来若加 503/重试，按 errorcode 精确判定"（Issue-审计 comment 亦点名此）。

4. **SQLAlchemy 官方 · SQLite dialect**（`https://docs.sqlalchemy.org/en/20/dialects/sqlite.html`，正文标注 truncated）
   > "Because SQLite relies upon whole-file locks, it is easy to get "database is locked" errors …"
   > "A ROLLBACK issued by one session … will also roll back uncommitted work from any other session, and concurrent COMMIT / ROLLBACK calls can interfere with each other unpredictably."（`StaticPool` 共享单连接告警）
   - **机制**：成熟 ORM 把"database is locked"归因于整文件锁语义，并警示**共享单连接**下并发事务互相干扰。**契合点**：与本仓"每操作新连接"的取舍对照——本仓刻意每操作新连接（非共享），避免该干扰，代价是并发写竞争靠 `busy_timeout` 排队。

> 判定：本票属**纯缺陷调查**（`AGENTS.md` §4.2 debug 闭环），非设计/选型 ⇒ 按协议 §1.3 豁免"方案依据"块。以上来源只用于**核实票面机制的 framing**（busy_timeout 的语义 + WAL 单写者语义），不引入任何新依赖或移植代码。

---

## 8. 复跑命令

见本目录 `commands.txt`（含 bundle 制作、`sbx --cloud cp`、`uv sync`、S1–S4b 的完整命令行与环境变量）。要点：

```sh
# 本机（Muse）：固定树 → bundle
git -C <worktree> bundle create t376.bundle HEAD        # HEAD = a932927d…
# 云端沙箱
sbx --cloud cp t376.bundle shell/ia-test:/home/agent/376/t376.bundle
sbx --cloud exec shell/ia-test -- sh -c 'cd /home/agent/376 && rm -rf repo && git clone -q t376.bundle repo && cd repo && uv sync --locked --all-extras'
# S3（主读数）：串行全量 + 非侵入探针 + 2 燃烧器
cd repo && export PATH="$PWD/.venv/bin:$PATH" PYTHONPATH="$PWD/src:/home/agent/376"
export no_proxy=localhost,127.0.0.1 NO_PROXY=localhost,127.0.0.1
export T376_PROBE=1 T376_PROBE_OUT=/home/agent/376/probe-s3.jsonl T376_PROBE_HOLD_MS=100
python scripts/_t508_burn_cpu.py 2 3600 &
python -m pytest tests/ -q --no-header -p no:cacheprovider -p no:randomly -p t376_probe
```

---

## 9. 落盘证据清单（本目录）

- `probe-s3.jsonl` — S3 的完整事件流（`probe_armed` / `write_hold` ×3057 / `long_write_hold` ×5 / `write_lock_busy_detected` ×139 / `probe_summary`）；为控制体积，逐记录 `stack` 字段已裁去（**长持锁的调用点已在 §3/§4 正文逐字给出**）。
- `s1-serial.txt` / `s3-serial.txt` — S1 / S3 串行全量的 pytest 原始输出（含唯一红的完整 traceback）。
- `s4-xdist-collection-error.txt`（收集期 xdist 报错原文）/ `s4b-main.txt` / `s4b-wsauth.txt` — `-n 4` 相关读数。
- `commands.txt` — 送码、环境、各轮命令、§4 最小实验的可复跑命令。
- `docs/gate/341414a41b74e4f09c2009093ac94fa02700002a.json`（仓内）— 本地 Gate-0 **6/6 PASS** 的机器落盘读数（`sha=341414a4…` / `tree=f18fa079…`）。
- `t376_probe.py.txt` — 探针源码（后缀改为 `.txt` 以避免触发 Gate-0 的"未跟踪 `.py` 车道输入"判据）。
