# ADR-0040 — 领域服务的构造契约：显式 collaborators + 传输侧单点适配（#248）

**Status**: Accepted（2026-09-21）
**Date**: 2026-09-21
**Related**: GitHub #248（冻结决策 2026-09-18 grilling，2026-09-18 起 blocked by #243 第一批）；
`docs/research/2026-09-18-full-codebase-architecture-quality-audit.md` §5.2 / §11.3；
`docs/tickets/architecture-audit-remediation-2026-09-18.md` T12；
`docs/SDD_TICKET_TRACKER.md` B-26 段（操作性事实）
**决策授权**: #248 的 Scope 原文——「先盘点真实 consumer 和最小所需字段。只有出现至少两个实现/测试替身
并能形成真实 seam 时，才由 domain 定义窄 Protocol 或显式 collaborators；**不要**仅把 AppState 换成
同样宽的 Protocol。」
**⚠ 边界（未获用户批准，如实登记）**: 两处接口级耦合按 AGENTS.md §9.1.1 登记为待裁决项，本批次
**未**擅自改写票面、也未擅自搬模块或改构造契约——

1. AC2 的「新 interface 不引用 `web`」只满足到**容器类型 + 15 个 collaborator** 的粒度：唯一残余
   `RunManager`（+ `ManagedRun` / `Subscriber`）的模块家仍在 `web/`（§4 R1）；
2. 本票**新增**了一条通向组合层 `assembly` 的类型级引用 `stores: RecoveryStores`（§4 R2）。
**真实证据**: §5 列了可复算的读数、红证产物与门禁数字（树 = 本文档所在提交的父提交 + 本提交）。

---

## 1. Context

改造前 `SessionService` / `ProjectService` 的构造契约是**传输层容器**：

```python
class SessionService:
    """会话领域服务：统一 CLI / Web 的 session 生命周期操作。

    包装 AppState（store / run_manager / approval_queues / capability 装配），……
    """
    def __init__(self, state: AppState) -> None:
        self._state = state
```

`self._state.<field>` 出现 **117** 处；`ProjectService` 同款。父提交侧读数：

```bash
git show 77b80eb:src/agent_harness/session/service.py | grep -c "self\._state\."   # 117
git show 77b80eb:src/agent_harness/session/projects.py  | grep -c "self\._state\."   # 3（同款写法，不同数量）
```

两个后果：

1. **类型级依赖反转**：`agent_harness.session.*`（领域）在注解里命名 `agent_harness.web.app.AppState`
   （传输）。`#248` 的 Important correction 已经澄清：该 import 在 `if TYPE_CHECKING:` 内，
   **不是** runtime cycle——本票是 P2 的静态耦合与形状问题，不夸大。
2. **service-locator 形状**：任何 `AppState` 成员都自动对领域层可见，"这个领域服务到底需要什么"
   没有可读答案；新增一个容器成员不需要任何人做决定。

### 1.1 消费者与实现盘点（本树实测，决定"要不要建 Protocol"）

| 类型 | 真实 consumer（`src/`） | 实现个数 |
| --- | --- | --- |
| `SessionService` | `web/app.py`（HTTP 路由）、`web/websocket.py`（3 处）、`web/lineage.py`（fork）、`web/workspace_files.py`（`has_session` 闸） | 1 |
| `ProjectService` | `web/projects.py` | 1 |
| CLI | **不消费**——`grep -n "SessionService\|ProjectService" src/agent_harness/cli.py` 无输出；旧 docstring 写的"统一 CLI / Web"是不实描述，本次一并对齐为"Web 传输层" | — |

每个 collaborator 的真实实现**各一个**（`JsonlSessionStore` / `RunManager` / `MessageQueueManager` /
`Sqlite*Store` / …）。测试里**没有第二个实现**：替身是 `MagicMock()`、duck-typed 局部对象，或对真实
实现的**故障注入子类**（`tests/session/store_fixtures.py::RejectingStore` / `FailingFromStore`、
`tests/observability/test_tool_tracing.py::_FailingTerminalLedger` 等）——它们都复用真实契约、
只把某一步变坏，**不构成另一套可替换实现**。按冻结决策的口径这不构成真实 seam。

⇒ 没有真实 seam ⇒ 不建 Protocol，改用显式 collaborators。

---

## 2. Decision

**D1 — 领域服务的构造契约 = 它自己拥有的显式 collaborators。** 关键字唯一（16 个同形而不同实体的
参数靠位置传必然错位），不命名任何传输容器类型。清单与用途见 §3（AC1）。

**D2 — 适配只发生在传输侧组合根。** `web/app.py::session_service(state)` /
`project_service(state)`；调用方只写 `session_service(app.state.agent)` /
`session_service(state)`。做成**模块级函数**而不是 `AppState` 方法：调用点手上只有 `state`
（真容器，或测试里的 duck-typed 假 state）；方法形态要求假对象自己实现 `session_service()`，
等于把组合根重新塞回容器类型；函数形态只按属性取所需成员，缺谁就是 `AttributeError`，不静默。

**D3 — 不建窄 Protocol。** 依据 §1.1：单实现 + 无可替换替身。冻结决策明文禁止"把 `AppState`
换成同样宽的伪 Protocol"，同样禁止为单一实现制造浅 seam。

**D4 — 每次调用现取属性（不缓存）。** 与旧 `SessionService(state)` 的行为逐字一致：构造后替换
`state.run_manager` 等打桩仍然生效。代价是每次调用新建一个轻对象（16 次属性读 + 赋值），
与旧实现的差别仅此。

**D5 — 领域层在代码与注解里不出现 `AppState` 标识符。** 守卫见 §5（AST 判据，docstring/注释不算）。

---

## 3. AC1 —— 字段清单与使用方法（本树实测）

计数命令：`grep -c "self\._<字段>\b" src/agent_harness/session/service.py`（含构造赋值 1 处；
有 property 的字段再多 1 处）。旧容器侧对应关系：13 个 `AppState` 属性 + 3 个访问器
（`stores` property、`ensure_stores()` / `get_wiring()` 方法）。

| # | collaborator（类型） | 旧 AppState 成员 | 用途（实测调用点） | 引用数 |
| --- | --- | --- | --- | --- |
| 1 | `store: JsonlSessionStore` | `store` 属性 | 会话日志唯一读端：`list_session_ids` / `read_session_summary` / `read_events` | 28 |
| 2 | `run_manager: RunManager` | `run_manager` 属性 | detached run 的**启动/在途判定/取消**：`launch` / `get_active` / `is_busy` / `cancel` | 13 |
| 3 | `settings: Settings` | `settings` 属性 | 只读 `approval_timeout_seconds`（审批 fail-closed 超时） | 13 |
| 4 | `workspace_registry: WorkspaceRegistry` | `workspace_registry` 属性 | `discard_session_artifacts`（删会话时清理 sandbox 产物） | 11 |
| 5 | `session_meta_store: SqliteSessionMetaStore` | `session_meta_store` 属性 | `upsert`/`get`/`list_all`/`set_archived`/`cleanup`/`clear_delegation_parent`（归档位、provenance、fork 子关系） | 10 |
| 6 | `message_queues: MessageQueueManager` | `message_queues` 属性 | `enqueue`/`register_steer`/`take_queue_item`/`take_steer`/`restore`/`cancel`/`cleanup`（#234 接力投递） | 10 |
| 7 | `approval_queues: dict[str, PendingApprovalQueue]` | `approval_queues` 属性 | 会话级待审批队列：`get` / `pop`（`/approve` 对接） | 8 |
| 8 | `workspaces_root: Path` | `workspaces_root` 属性 | `resolve()`——sandbox 根目录 | 6 |
| 9 | `workspace_index: WorkspaceIndex \| None` | `workspace_index` 属性 | `create`/`attach_session`/`detach_session`（项目账本；`None` = 未启用） | 8 |
| 10 | `operation_ledger: SqliteOperationLedger` | `operation_ledger` 属性 | `delete_for_session`（删会话时摘账本） | 4 |
| 11 | `transport_ledger: SqliteTransportLedger` | `transport_ledger` 属性 | `delete_for_session` | 2 |
| 12 | `checkpoint_store: SqliteCheckpointStore` | `checkpoint_store` 属性 | `delete_for_session` | 2 |
| 13 | `harness_db: Path` | `harness_db` 属性 | 建"恢复/扫描"用的数据库句柄（`database_path=` 两处） | 3 |
| 14 | `stores: RecoveryStores` | `stores` property | 传给恢复接线（`stores=` 两处） | 3 |
| 15 | `ensure_stores: Callable[[], Awaitable[None]]` | `ensure_stores()` 方法 | 8 个入口的惰性初始化（兼容不走 lifespan 的测试路径） | 9 |
| 16 | `get_wiring: Callable[[], Awaitable[tuple[CapabilityRegistry, CapabilityWiring]]]` | `get_wiring()` 方法 | 审批回调与模型变更需要真实装配集 | 3 |

`ProjectService`（`session/projects.py`，计数命令同形）：`store`=2、`workspace_index`=2、
`ensure_stores`=2——`store.read_started_header` 读会话头部、索引做项目 CRUD、`ensure_stores()`
保证索引就绪。

**"比 AppState 明显更窄"的判据（可复核）**：`AppState` 公开成员 21 个
（16 个实例属性 + 5 个公开方法/属性）。AST 统计口径：`AppState.__init__`（`web/app.py:343-410`）
里 `self.X =` 共 **22** 项（16 公开 + 6 私有）——全文件是 27 项，别按全文件数；`class AppState`
的 7 个方法里 2 个私有。本层用到的正是上表 16 个，**每一个都有调用点**；
余下 5 个（`provider_store` / `context_snapshots` / `sessions_root` / `shutdown` / `wiring`）
是传输层自己的事，领域层**永不触碰**，也不再能"顺手拿到"。

---

## 4. 残余与已知边界（如实登记）

**R1 — `RunManager`（+ `ManagedRun` / `Subscriber`）的家仍在 `web/runmanager.py`。**
这是 AC2 唯一未闭合处。事实澄清（避免把"位置"误读成"依赖"）：

- 该模块**不 import 任何 web 依赖**：模块级只 import `agent_harness.agent`（`AgentEvent` /
  `AgentRuntime`）、`agent_harness.memory.types`、`agent_harness.session`（事件常量 + `Session`）。
- 本层只用到它的 4 个方法（`launch` / `get_active` / `is_busy` / `cancel`，见 §3 第 2 行）。
- 引用发生在 `if TYPE_CHECKING:` 内，运行时零成本。

**待用户裁决**：(a) 接受为残余登记 + 另开 ticket 把模块搬到 `session/`（纯移位，运行时零影响，
但跨 ~13 个测试文件的 patch 路径），或 (b) 现在就搬。在拿到裁决前不自行搬迁（AGENTS.md §8 Scope
Lock：不顺手重构）。

**R2 — `assembly.RecoveryStores`（本票**新增**的接口级类型引用）与 `assembly.build_runtime`（既有）。**
两件事要分开说：

- `build_runtime`：`session/service.py` 在改造前就在**运行时** import 它（`git show
  77b80eb:src/agent_harness/session/service.py | grep -n "from agent_harness.assembly import"`
  → `38:from agent_harness.assembly import build_runtime`）。`assembly` 是 web / CLI **共用**的运行时
  装配层（其 docstring 明写"web 与 CLI 是它的两个 adapter"），不属"传输层反控领域"。
- `RecoveryStores`：**本票新引入**——改造前该符号在 `service.py` 里根本不出现
  （`git show 77b80eb:src/agent_harness/session/service.py | grep -n "RecoveryStores"` → 无输出），
  现在它是构造参数 `stores: RecoveryStores` 的类型（`TYPE_CHECKING` 内）。也就是说，领域层的
  构造契约**多了一条通向组合层（`assembly`）的类型级引用**——不是 `web`，AC2 的字面不受影响，
  但按 §9.1.1 的口径这是一处**本票引入**的接口耦合，必须留痕而不是记成"本来就有"。

**待用户裁决（与 R1 同一批）**：接受这条引用（`RecoveryStores` 只是三个 store 的只读束，
且 R4 已记"合并参数"被否的取舍），或改为由领域层自己从三个 ledger 组束、把该类型从构造契约里
去掉（少 1 个参数，代价是领域自建上层 bundle）。

**R3 — 两条守卫各自的作用域要说清**（窄验证第二轮实测后订正——此前把两条的作用域写混了）：

- `test_importing_the_domain_does_not_load_the_web_app`（子进程）：只看**真正被加载**的模块，
  即模块级 web import；函数体内的惰性 import 不执行、因此不在其内（今天 `web/websocket.py`
  就是从 `web.app` 惰性 import 组合根的，方向相反、不受影响）。
- `test_types_only_reference_to_web_is_the_documented_residual`（AST）：扫**全部 import 语句**，
  含函数体内的惰性 import（实测：在 `service.py` 函数体里插 `from agent_harness.web.app import …`
  也会红）。相对导入按被扫文件的包解成绝对模块名（`from .. import web` 会被判出，见 §5 红证 13）。
  它**不覆盖**的是**动态导入**（`__import__("agent_harness.web.app")`、
  `importlib.import_module(...)`，表达式根本不是 `Import` 节点）与 **star import 的"内容"**
  （`from .. import *` 这条**语句**会被扫到，但星号展开出哪些名字无法静态解析；今天
  `src/agent_harness/__init__.py` 只有一条 docstring、没有任何 re-export，故无实际暴露面）。
  这两类属**声明范围外**，不视为缺口（审查第三轮提出前两类漏判，本批已收全；相对导入同一轮收全）。

另外实测：今天任何模块级 `agent_harness.web.*` 运行时 import 都会**立刻成环**
（`web/__init__.py` eager import `app`，`app` 又 import `session.projects` → `session.service`）
——该性质因此是结构性约束（§5 红证 4）。窄验证第二轮另实测：两条**漏判**写法
（`from agent_harness import web`、多别名 `import` 里 web 排第二）在修复前虽能骗过 AST 守卫，
却当场撞上这个循环（`ImportError: cannot import name 'validate_session_id' from partially
initialized module …`）⇒ 它们在今天**不可利用**；但守卫已按 findings 收全（§5 红证 9/10）。

**R4 — 未采纳的收窄方案（逐条留痕）**：

| 方案 | 为什么没做 |
| --- | --- |
| 窄 Protocol（domain 定义 4–5 个方法的 run host 协议） | 单实现 + 无可替换替身（§1.1）；冻结决策明文禁止为单一实现造浅 seam |
| 合并 `stores` 与三个 ledger 参数（它们是同一批对象的两种视图） | 只少 1 个参数（15 vs 16），却让领域自建上层 `assembly` bundle；收益与噪声不成比例 |
| 把 `RunManager` 搬出 `web/`（R1 的 (b)） | 纯移位、运行时零影响，但属跨模块重构，需用户裁决；见 R1 |

---

## 5. 验证与证据

**门禁（本文档所在批次，`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest`）**：

- `tests/session`：改造前 **366**（`77b80eb`）→ 新增证据文件后 **379**（`ed1c5fa`）→
  审查补强两条用例后 **382**（`61aaa20` 及其后的测试侧提交，本轮 tip 实测 52.20 s，独立审查者
  在导出树上逐棵复算 366/379/380/382）。每条数字都能对着它的树复算，别把某一轮的数字安到另一棵树上
  （上一轮 review 就是这么踩的）。
- `tests/web` + `tests/workspace` + 5 个顶层 web 文件（`test_web_api` / `test_web_lineage` /
  `test_web_phase5` / `test_sse_disconnect` / `test_measure_sse_streaming`）**493 passed**
  （220.33 s；独立审查者同命令复算 188.26 s）——**命令即上面那串**：只写"495"而不给文件清单
  是不可复算的（上一轮 review 的读数 451 / 493 / 502 三种口径都能自圆其说，所以这里把口径写死）。
- 全量：**跑过全量的是 `978e960` 与 `ed1c5fa` 两棵树**（数字记在 tracker B-26 / 月档，本文档不复制
  以免两处漂移）；其后的提交只动测试与文档（`61aaa20` / `d96e148` / 本轮 tip 均为测试或 docs-only），
  终点树的全量读数在同一批次的登记/集成条目里给。**别写"本文档所在提交"**——上一轮 review
  就是照这句话去复算、发现该提交上并没有全量读数（finding N4）。
- `ruff check` 改动文件：All checks passed；`git diff --check` 无输出。

**调用点计数（AC5 用；`grep -c` 会把 `def session_service(` 与注释里的引用一并计入，所以按调用点口径数）**：

```bash
grep -rn "session_service(\|project_service(" src/agent_harness/web/*.py \
  | grep -vE ":\s*#|def (session|project)_service" | wc -l    # 27 = 26 + 1
```

分文件：`app.py` 21、`websocket.py` 3、`lineage.py` 1、`workspace_files.py` 1（`session_service`），
再加 `projects.py` 的 1 处 `project_service`。

**字段集合的机械保真（改造前后逐字相同）**：

在**父提交**侧（`77b80eb`；本票已把 `self._state` 全数改掉，所以在 `978e960` 之后跑是 0）：

```bash
git show 77b80eb:src/agent_harness/session/service.py | grep -o "self\._state\.[a-zA-Z_]*" | sort -u | wc -l   # 16
git show 77b80eb:src/agent_harness/session/projects.py  | grep -o "self\._state\.[a-zA-Z_]*" | sort -u | wc -l  # 3
```

16 / 3 与 §3 表格逐字一致（没有丢成员、没有凭空造成员）。容器整体是否被外传，另看
`grep -n "self\._state" … | grep -v "self\._state\."`：`77b80eb` 的 `service.py` 命中 **2** 处——
代码只有 `self._state = state`（第 281 行），另一处是 docstring 散文（第 1745 行，讲候选方案的
文字，不是调用）。即没有第二条第代码路径把容器递出去。

**红证（逐条变异真实源文件 → 跑真实守卫 → 观察预期失败 → 还原并比对 `git hash-object`）**：
脚本 `.workbuddy/red_248_guards.py` —— 在**仓库工作树内**、仅被 `.gitignore` 忽略（不提交、不入
tracked 树）。脚本自己断言锚点唯一、还原后哈希一致，任何一条没变红就 `sys.exit`。下表即其
完整变异清单（脚本是动过真实文件的，所以这里逐条留痕；跑法见脚本头部注释）：

| # | 变异 | 观察到的失败 |
| --- | --- | --- |
| 1 | 在 `web/workspace_files.py` 加 `__RED_PROBE = SessionService(**{})` | `Extra items in the left set: 'agent_harness/web/workspace_files.py'`（构造点守卫） |
| 2 | 把 `session/service.py` 的 `has_session` 注解改成 `state: AppState` | `领域层又命名了传输容器：['agent_harness\\session\\service.py:486', '…:486']`（同一行报两次 = `ast.arg` 的注解节点与注解里的 `ast.Name` 各命中一次；**行号随文档串增删滑动，按用例名定位**） |
| 3 | 把组合根的 `approval_queues=state.approval_queues` 改成 `approval_queues={}` | `AssertionError: approval_queues 没有被搬进服务`（字段搬家可证伪） |
| 4 | 模块级加 `from agent_harness.web.runmanager import …` 到 `session/service.py` | 循环 `ImportError`（exit=4）——即该性质是结构性约束；同一探测片段换成导入 `web.app` 时输出 `LOADED: ['agent_harness.web', 'agent_harness.web.app', …]`、exit=1（探测器对照，证明"绿"不是片段失效） |
| 5 | 在 `web/workspace_files.py` 加**属性形式**构造 `__RED_PROBE = _svc.SessionService(**{})` | `Extra items in the left set: 'agent_harness/web/workspace_files.py'`。补强前该形式会漏（独立审查者用同一判据复现：只认裸名字时集合为空），因此这条同时是 finding 的修复证据 |
| 6 | 在 `session/service.py` 的 `TYPE_CHECKING` 块内加第二条 web 引用 `from agent_harness.web.app import AppState as …` | `Extra items in the left set: 'from agent_harness.web.app import AppState as _RedProbeAppState'`（`test_types_only_reference_to_web_is_the_documented_residual` 的集合相等断言；**该用例内的行号随文档串增删滑动，一律按用例名定位**）。补强前只断言"残余存在"，这条会静默通过（审查者 findings） |
| 7 | 同上位置改加**plain import** 等价写法 `import agent_harness.web.app`（旧判据只收 `ImportFrom`，这条曾整条绕过） | `Extra items in the left set: 'import agent_harness.web.app'`（同一条集合相等断言）。补强前该形式既不进 `runtime_web_imports` 也不进 `ImportFrom` 记录，守卫仍是绿的（窄验证 findings） |
| 8 | 把 `is_web_module` 退回子串实现 `"agent_harness.web" in module` | `test_web_module_match_is_not_a_substring_test` 红（`assert not True`）——证明该精度用例不是空转（窄验证 findings） |
| 9 | `imported_modules` 的 `ImportFrom` 分支只回 `[base]`（即漏 `from agent_harness import web`） | `test_import_statement_forms_are_all_considered` 红：`AssertionError: 'from agent_harness import web' 判成 False，应为 True` |
| 10 | `imported_modules` 的 `Import` 分支只取 `names[0]`（漏 `import x, agent_harness.web.app`） | 同上用例红：`AssertionError: 'import agent_harness.websearch, agent_harness.web.app' 判成 False，应为 True` |
| 11 | `is_type_checking_test` 换回子串判据 `"TYPE_CHECKING" in ast.unparse(test)` | `test_type_checking_test_must_be_positive` 红：`assert not True where True = is_type_checking_test(<ast.UnaryOp …>)`（即 `if not TYPE_CHECKING:` 被误当豁免块） |
| 12 | 把登记表 `EXPECTED_TYPE_ONLY_WEB_IMPORTS` 清空 | 同一条残余用例的键集合断言红：`Extra items in the right set: 'agent_harness/session/service.py' / 'agent_harness/session/projects.py'`（否则守卫会静默空转成空循环） |
| 13 | 相对导入忽略 `level`（`prefix` 不按被扫文件的包回退） | `test_import_statement_forms_are_all_considered` 红：`from .. import web` 判成 False |
| 14 | `is_type_checking_test` 的属性分支放宽成只看 `attr`（不看 `value`） | `test_type_checking_test_must_be_positive` 红：`settings.TYPE_CHECKING` 被判成豁免块 |

十四条变异全部按预期变红，且还原后 `git hash-object` 与变异前一致（脚本内断言）。第 5–14 条是
审查 findings 的修复证据：独立审查（第 5/6 条）、窄验证第一轮（第 7/8 条）、窄验证第二轮
（第 9–12 条）、窄验证第三轮（第 13/14 条）。
**方法学留痕（两条）**：① 第 11 条探针第一版只改 `Name` 分支、漏了 `Attribute` 分支与末尾
`return False`，于是 `not TYPE_CHECKING`（`ast.UnaryOp`）照样走到 `return False`、守卫**没红**——
脚本当场 `sys.exit("红证失败")` 把这次"探针不判别"暴露出来，改成整段替换后才成立；② 本轮改动
`is_type_checking_test` 之后，第 11 条探针的锚点**当场失效**（锚点唯一性断言报"出现 0 次"）——
即"改完实现要同步改探针"这件事由脚本机械保证，不靠人记得。**探针本身必须先被证明能判别。**

---

## 6. AC 矩阵（逐条，含未闭合项）

| AC | 结论 | 证据 |
| --- | --- | --- |
| AC1 文档列出每个 AppState 字段与使用方法 | **满足** | §3 表格 16 行（每行含用途与实测引用数）+ 紧随其后的 `ProjectService` 3 项（散文，非表格行）；测试 `test_session_service_takes_exactly_the_documented_collaborators` / `test_project_service_takes_exactly_the_documented_collaborators` 把两份清单钉在代码上，漂移即红 |
| AC2 新 interface 不引用 `web`，且比 AppState 明显更窄 | **部分满足** | 容器类型与 15 个 collaborator 已无 web 引用（§2 D1/D5 + 守卫）；"明显更窄"的判据见 §3 末段（16 个参数全被使用、容器 21 个公开成员中 5 个永不触碰）。**未闭合**：残余 R1（`RunManager` 的模块家仍在 `web/`），待用户裁决 |
| AC3 transport composition root 负责适配 | **满足** | §2 D2；守卫 `test_services_are_constructed_only_in_the_composition_root`（`src` 全树唯一构造点 = `web/app.py`）；红证 1 |
| AC4 构造测试不再依赖魔法属性，运行行为不变 | **满足** | `tests/session/test_service_collaborators.py`：普通 duck-typed 对象即可构造、16 个字段逐个 identity 可证、缺字段 `AttributeError`、调用时现取属性；`tests/session/conftest.py::make_session_service` 提供无容器构造夹具；全量门禁不变（tracker B-26） |
| AC5 删除新 seam 会重新造成跨层类型耦合，而不是只少一个 wrapper | **满足** | 删掉组合根后，**26 个 `session_service(...)` 调用点 + 1 个 `project_service(...)` 调用点**（`app.py` 21 / `websocket.py` 3 / `lineage.py` 1 / `workspace_files.py` 1；命令见 §5）**各自**要命名 16 个 collaborator——那正是把适配复制 27 遍；构造点守卫与导入边界守卫会同时变红（红证 1/4） |
