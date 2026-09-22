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
**⚠ 边界（首版 2026-09-21 如实登记，2026-09-22 用户裁决后已闭合）**: 首版按 AGENTS.md §9.1.1 把两处
接口级耦合登记为**待裁决项**，本批次当时**未**擅自改写票面、也未擅自搬模块或改构造契约——

1. AC2 的「新 interface 不引用 `web`」当时只满足到**容器类型 + 15 个 collaborator** 的粒度：唯一残余
   `RunManager`（+ `ManagedRun` / `Subscriber`）的模块家仍在 `web/`（§4 R1）；
2. 本票**新增**了一条通向组合层 `assembly` 的类型级引用 `stores: RecoveryStores`（§4 R2）。

**裁决（2026-09-22，用户逐项选定）**：R1 = **「搬到 `session/`」**（对齐 Temporal 把 RunManager 放在
`temporal/` SDK 内、Jupyter Server 把 KernelManager 放在 server 包内的做法）；R2 = **「领域自建端口」**
（对齐 langgraph / langmem 由库自身声明运行所需最小接口、由调用方实现的做法）。两处残余**已闭合**，
闭合后 AC2 由"部分满足"变为"满足"（§6）。裁决依据、落地范围与证据见 §4 R1/R2 与 §5 红证 15–18。

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
| 14 | `stores: RecoveryStoreBundle`（领域端口，见 §4 R2） | `stores` property | 传给恢复接线（`stores=` 两处） | 3 |
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

**R1 — `RunManager`（+ `ManagedRun` / `Subscriber`）的家曾在 `web/runmanager.py`（AC2 唯一未闭合处）。**
首版登记的澄清（避免把"位置"误读成"依赖"）：

- 该模块**不 import 任何 web 依赖**：模块级只 import `agent_harness.agent`（`AgentEvent` /
  `AgentRuntime`）、`agent_harness.memory.types`、`agent_harness.session`（事件常量 + `Session`）。
- 本层只用到它的 4 个方法（`launch` / `get_active` / `is_busy` / `cancel`，见 §3 第 2 行）。
- 引用发生在 `if TYPE_CHECKING:` 内，运行时零成本。

**裁决与闭合（2026-09-22）**：用户选定 **(b) 现在就搬**——`git mv` 到
`agent_harness/session/runmanager.py`（纯移位，运行时零影响；logger 名同步改为
`agent_harness.session.runmanager`，模块内与 `agent/runtime.py` 的两处指路注释一并订正）。
影响面**实测** 13 行 / 10 个文件（`git grep -n "agent_harness.web.runmanager" 55f2deb -- src tests`）；
其中真正的 import 语句是 **11 条 / 8 个文件**（剔除模块自己的 logger 行 1 条与守卫表里的字面量
1 条）——本批最初写的是"7 个文件 / 10 处"（那是改写脚本自己的命中数，口径更窄），按上面这条
可复算命令订正。守卫随之从"残余不得扩大"改成"**残余已归零**"：`EXPECTED_TYPE_ONLY_WEB_IMPORTS`
的三份域文件全为空集（`service.py` / `projects.py` / `runmanager.py`），另加
`test_run_manager_home_is_the_session_package` 钉住新家（旧路径复活即红）。闭合后领域层
（代码 + 注解）对 `web` 的引用数为 **0**。

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

**裁决与闭合（2026-09-22）**：用户选定「**领域自建端口**」——`session/service.py` 自己声明
`RecoveryStoreBundle`（`@runtime_checkable` Protocol），构造参数注解改为 `stores: RecoveryStoreBundle`；
组合根仍造真实束（`assembly.RecoveryStores`），领域层不再 import 组合层**类型**。取舍与依据：

- 端口只声明**形状**、不复制具体类型：束的成员增删与领域层的 import 解耦，改组束不再牵动 `session/`；
- 端口成员清单**由读端决定**：`build_runtime(stores=…)` / `initialize_stores(stores=…)` 实测只读
  `operation_ledger` / `checkpoint_store` / `session_meta_store` / `workspace_index` 四项
  （`assembly.py` 的调用点），端口就这四项、不多不少。**口径写清楚**：这四项是
  「`build_runtime` ∪ `initialize_stores` 的读点」，不是"领域自己会读到的"——`build_runtime`
  只读 3 项，`workspace_index` 仅由 `initialize_stores` 读，而领域层调的是**注入的**
  `ensure_stores`（真实实现 `web/app.py` 的闭包用组合根自己的束，与 `self._stores` 无关），
  领域层自己从不解引用 `self._stores` 的任何成员、只把它转交给 `build_runtime`；
- **别把这条读成"更窄"**：真实束 `RecoveryStores` 今天也恰是这 4 个成员（`assembly.py:83`
  的 dataclass 实测），所以端口与它**等宽**。收益不在成员数，而在两件事——形状由领域自己声明、
  组合层类型不再进领域 import；把"等宽"写成"收窄"会让下一个审查者按错的理由通过它；
- 端口的保证是**弱**的，写清楚免得被高估：仓库门禁只有 `ruff` + `pytest`（`pyproject.toml` 无 mypy
  / pyright 段），所以"注解写的是什么类型"没有静态检查器兜底；`isinstance` 对
  `@runtime_checkable` + 数据成员的 Protocol **只查属性存在性**（成员值改成 `None` 或换个类型
  仍然绿）。真正的机械保证只有两条用例——正向
  `isinstance(recovery_stores(tmp), RecoveryStoreBundle)`（成员改名/删除即红，见 §5.1 红证 16）
  与负向 import 边界；"端口**多写**一个成员"没有任何用例能发现，靠 review；领域层将来真去读
  第 5 个成员时，也没有机制强制先补端口。这是选"自建端口"相对"领域自建 bundle"的已知代价：
  换来的是不复制组合层类型 + 不动装配层。
- `build_runtime` 保持原样：它是本票改造**之前**就有的运行时耦合（上一段），且
  `tests/web/test_web_phase5_permission.py` 用 `monkeypatch.setattr(service_module, "build_runtime", …)`
  把它钉在模块级名字上，动它属 Scope 外；守卫只拦**组合层类型**再进 import，白名单恰为
  `build_runtime` 一项（`test_domain_files_import_no_composition_types`；该用例 2026-09-22
  随扫描扩到三份域文件而更名，旧名 `test_service_imports_no_composition_types`）。

**R3 — 两条守卫各自的作用域要说清**（窄验证第二轮实测后订正——此前把两条的作用域写混了）：

- `test_importing_the_domain_does_not_load_the_web_app`（子进程）：只看**真正被加载**的模块，
  即模块级 web import；函数体内的惰性 import 不执行、因此不在其内（今天 `web/websocket.py`
  就是从 `web.app` 惰性 import 组合根的，方向相反、不受影响）。
- `test_types_only_reference_to_web_is_the_documented_residual`（AST）：扫**全部 import 语句**，
  含函数体内的惰性 import（实测：在 `service.py` 函数体里插 `from agent_harness.web.app import …`
  也会红）。相对导入按被扫文件的包解成绝对模块名（`from .. import web` 会被判出，见 §5 红证 13）。
  它**不覆盖**的是**动态导入**（`__import__("agent_harness.web.app")`、
  `importlib.import_module(...)`，表达式根本不是 `Import` 节点）与 **star import 的"内容"**
  （星号展开出哪些名字无法静态解析。实测 `from .. import *` 在 `package="agent_harness.session"`
  下解成 `["agent_harness", "agent_harness.*"]`，`is_web_module` 全 False ⇒ 这条语句会被**遍历**
  但**不会**被判出；会判出的是 import 路径里写明 `web` 的形式：`from ..web import *` /
  `from agent_harness.web import *` / `from ..web.app import *`。今天
  `src/agent_harness/__init__.py` 只有一条 docstring、没有任何 re-export，故无实际暴露面）。
  这两类属**声明范围外**，不视为缺口（审查第三轮提出前两类漏判，本批已收全；相对导入同一轮收全）。
- **两条 import 边界守卫都不覆盖"属性链"形态**（复验 findings 的 N2，2026-09-22 登记）：
  `import agent_harness` 单独一条不算违规，但导入机制会把子模块挂成包属性 ⇒ 此后
  `agent_harness.assembly.RecoveryStores`（对称地 `agent_harness.web.app.X`）可以**不经
  import 语句**引用到组合层 / 传输层的名字。实测：`assembly_type_imports` 对
  `import agent_harness` 返回 `[]`，而 `hasattr(agent_harness, "assembly")` 为真（`service.py`
  自己 import 了该子模块）。今天没有任何这样的引用（三份域文件 web 引用为 0、组合层引用只有
  白名单 `build_runtime`），且这是 **import 判据的共同边界**，故登记为声明范围外；要收口得另开
  一条"注解与表达式里的限定名"判据（与 `test_domain_never_names_the_transport_container` 同形，
  那条能抓住字符串注解里的 `AppState`），属新票范围。

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
| 把 `RunManager` 搬出 `web/`（R1 的 (b)） | 首版列为"需用户裁决"；**2026-09-22 用户裁决采纳**，本批已执行（见 R1） |
| 领域层从三个 ledger 自建束、把 `stores` 参数整个去掉（R2 的另一条路） | 少 1 个参数，但把"恢复子系统要哪些 store"的知识从装配层搬进领域层；用户选的是**自建端口**（保留参数、只换类型），不是自建 bundle |
| 端口成员照真实束的类型写（`SqliteOperationLedger` / `SqliteCheckpointStore` / `SqliteSessionMetaStore`） | 端口要声明的是**领域的形状**，写具体实现类等于把组合层的具体类型换个名字抄进领域——那正是本票要消除的形状。改用 storage 层的 ABC（`OperationLedger` / `CheckpointStore` / `SessionMetaStore`）。**如实登记一处不一致**：同一构造器里另有 **4** 个参数本来就用 `Sqlite*` 具体类型（`session_meta_store` / `operation_ledger` / `transport_ledger` / `checkpoint_store`；`inspect.signature` 实测，本票改造**之前**就如此，属 Scope 外），所以今天 `session/service.py` 里两种风格并存 |

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
- 全量：**跑过全量的是三棵**——`978e960` / `ed1c5fa`（各 2654）与终点树 `af6f369`（2657；本文档只给
  passed 概数，完整读数——含 skipped / deselected 与墙钟——见 tracker B-26 / 月档）；其余提交
  （`61aaa20` / `d96e148` / `8fcf483` / `c203098`）均只动测试或 docs-only，故未单独跑全量。
  **别写"本文档所在提交"**——上一轮 review 就是照这句话去复算、发现该提交上并没有全量读数
  （finding N4）；**也别把树的枚举写死**——终点树读数落盘后「两棵树」即不成立（finding N5）。
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

### 5.1 R1/R2 闭合批次（2026-09-22）的证据

**门禁（本批工作树 = 提交的代码面）**：全量 `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q
-p no:randomly` = **3090 passed / 2 skipped / 42 deselected / 1 failed / 13 warnings in 617.23 s**。
唯一失败 `tests/sandbox/test_docker_sandbox.py::test_timeout_stops_late_workspace_mutation`
是登记在案的**既有 flake**；**如实说明取证缺口**：那份日志只留了末 20 行，**失败签名已丢**
（登记项的判据要求"`:93` 断言 + stdout 空"逐条对上，本次无法核对）。故不按"签名命中"走 §8.6 的
免复跑通道，改用三条可复核的独立证据判**非本批引入**：① 该用例与它被测的
`src/agent_harness/sandbox/docker.py` 都不在本批 `git status --short` 内（逐字节未动）；
② 同树单跑 **3/3 passed**（call 4.59 / 4.63 / 4.61 s，墙钟各 5.07–5.08 s；形态落在登记项记的
"正常"区间，超预算形态未复现）；③ 本批三个位点（`session/service.py` 端口、`session/runmanager.py`
移位、4 个 web 测试文件的 import 路径）与 docker 沙箱无共享代码路径。
`ruff check` 改动文件 All checks passed（含本批修掉的 4 处 I001：搬迁后 import 需重排）；
`git diff --check` 无 whitespace / conflict-marker 输出。

**红证（同一方法学；脚本 `.workbuddy/red_248_r1r2.py`，日志 `.workbuddy/red_248_r1r2.log`）**：
五条全部按预期变红、还原后哈希一致。

| # | 变异 | 观察到的失败 |
| --- | --- | --- |
| 15 | 造出旧路径 `src/agent_harness/web/runmanager.py`（桩文件；原本不存在） | `AssertionError: 旧路径 web/runmanager.py 不得复活——RunManager 是领域模块` |
| 16 | 从 `assembly.RecoveryStores` 删掉 `session_meta_store` 字段 + 工厂里对应的 kwarg（同一语义的一对锚点） | `AssertionError: assembly 造的 RecoveryStores 不再满足领域端口 RecoveryStoreBundle` |
| 17 | 在 `service.py` 的 `TYPE_CHECKING` 块里加 `from agent_harness.assembly import RecoveryStores` | `AssertionError: 领域文件又 import 了组合层类型：['agent_harness/session/service.py:142: from agent_harness.assembly import RecoveryStores']` |
| 18 | 同位置改加 plain import `import agent_harness.assembly` | 同一条断言：`[…: import agent_harness.assembly]` |
| 19 | 同位置加 `from agent_harness.web.app import AppState as _RedProbeAppState` | `AssertionError: agent_harness/session/service.py 的 TYPE_CHECKING web 引用集合变了：[…]（登记值 []）`——即"残余归零"判据对**任何**新 web 引用敏感 |
| 20 | 同位置加 `from agent_harness import assembly`（等价写法第三种） | 同第 17/18 条断言：`[…: from agent_harness import assembly]` |
| 21 | 同位置加相对导入 `from ..assembly import RecoveryStores`（等价写法第四种） | 同第 17/18 条断言：`[…: from ..assembly import RecoveryStores]` |

**第 20/21 条是审查 findings 的修复证据（P2）**：本批最初那版守卫只看 `node.module` 与
`alias.name`，于是 `from agent_harness import assembly`、`from ..assembly import …`、
`from .. import assembly` 三种等价写法**整类绕过**——判据强度会取决于写法。独立审查当场用合成
源码证明三条都漏（并指出同文件 88-107 行的 `imported_modules` 早就为 web 守卫处理过同一类漏判），
修法是**复用那个 helper**（`assembly_type_imports`），并补一条 `test_composition_import_forms_are_all_considered`
把四种写法（外加白名单 `build_runtime` 与一个无关模块的负例）钉住。判据同时从"只扫
`service.py`"扩到**三份域文件**（审查 findings 的 P3：只守一处等于把另两处的缺口留给下一个人）。

**取不到红证的一条，如实登记**：`test_run_manager_home_is_the_session_package` 的第一条断言
（新家必须存在）**无法以"守卫变红"的形式取证**——把该模块搬走会让这份测试模块在**收集期**
`ImportError`（它模块级 import `RunManager`），红的是收集而不是那条断言。该断言的定位因此是
**可读判据**（"家在哪"写进测试），不是静默风险防线：模块真不在了，11 条 import（8 个文件）
会当场全断。
第 15 条只证了它的第二条断言。

**旧脚本的探针已随本批失效（不修改历史读数）**：`.workbuddy/red_248_guards.py` 的探针 3/4/6/7
锚在"`service.py` 里存在 `from agent_harness.web.runmanager import …`"这段文本上，搬迁后该文本
不复存在 ⇒ 再跑那个脚本会在锚点唯一性断言处 `sys.exit`（脚本自检生效，不是它失效）。表 1–14
记录的是**搬迁前**那棵树上的实测，原样保留。

---

## 6. AC 矩阵（逐条，含未闭合项）

| AC | 结论 | 证据 |
| --- | --- | --- |
| AC1 文档列出每个 AppState 字段与使用方法 | **满足** | §3 表格 16 行（每行含用途与实测引用数）+ 紧随其后的 `ProjectService` 3 项（散文，非表格行）；测试 `test_session_service_takes_exactly_the_documented_collaborators` / `test_project_service_takes_exactly_the_documented_collaborators` 把两份清单钉在代码上，漂移即红 |
| AC2 新 interface 不引用 `web`，且比 AppState 明显更窄 | **满足**（2026-09-22 闭合） | 容器类型与 16 个 collaborator 均已无 web 引用：`RunManager` 的家搬到 `agent_harness/session/`（§4 R1），`stores` 改注解为领域自建端口 `RecoveryStoreBundle`（§4 R2）；三份域文件的 `EXPECTED_TYPE_ONLY_WEB_IMPORTS` 全为空集 ⇒ 领域层（代码 + 注解）对 `web` 的引用数为 **0**。"明显更窄"的判据见 §3 末段（16 个参数全被使用、容器 21 个公开成员中 5 个永不触碰）。原"部分满足"的唯一未闭合项（R1）已由用户裁决并落地 |
| AC3 transport composition root 负责适配 | **满足** | §2 D2；守卫 `test_services_are_constructed_only_in_the_composition_root`（`src` 全树唯一构造点 = `web/app.py`）；红证 1 |
| AC4 构造测试不再依赖魔法属性，运行行为不变 | **满足** | `tests/session/test_service_collaborators.py`：普通 duck-typed 对象即可构造、16 个字段逐个 identity 可证、缺字段 `AttributeError`、调用时现取属性；`tests/session/conftest.py::make_session_service` 提供无容器构造夹具；全量门禁不变（tracker B-26） |
| AC5 删除新 seam 会重新造成跨层类型耦合，而不是只少一个 wrapper | **满足** | 删掉组合根后，**26 个 `session_service(...)` 调用点 + 1 个 `project_service(...)` 调用点**（`app.py` 21 / `websocket.py` 3 / `lineage.py` 1 / `workspace_files.py` 1；命令见 §5）**各自**要命名 16 个 collaborator——那正是把适配复制 27 遍；构造点守卫与导入边界守卫会同时变红（红证 1/4） |
