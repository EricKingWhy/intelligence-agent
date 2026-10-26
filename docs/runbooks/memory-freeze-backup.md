# Memory freeze / plaintext offline backup runbook

本手册对应 #1492 / ticket-010。它保全一个 workspace 的完整 Memory 生命周期，
不把备份接入运行时、检索或 Memory provider，也不执行任何销毁。

## 方案边界与复用依据

实现入口是 [`scripts/memory_freeze_backup.py`](../../scripts/memory_freeze_backup.py)。它是
独立的操作工具，不导入 `agent_harness.memory.v2.assembly`，不会调用 store 初始化或
tombstone purge。`seed` 只用于本地代表性演练；真实生产 workspace 必须由所有者在场操作。

方案判定：

- **REUSE**：使用 Python 标准库 SQLite `Connection.backup()` 对现有数据库做一致 snapshot，
  不复制正在 WAL 写入的 `.db` 主文件作为备份。SQLite 官方说明该 API 的目标副本是启动时
  一致的 snapshot：<https://www.sqlite.org/backup.html>。
- **ADAPT**：复用本仓库已冻结的 V1/V2 表名、V2 job/outbox/tombstone 生命周期和
  `memories/` 下 Markdown sidecar 约定；文件清单保留 `MEMORY.md`、`USER.md`、`.bak.*`、
  `.mem_*.tmp`、lock 与数据库 WAL/journal/shm sidecar。
- **ADAPT**：把 transactional outbox 的“记录与 outbox 同一事务、下游按幂等事实处理”
  约束用于 manifest 对账；AWS 的机制说明见
  <https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html>。
- **BUILD（最小边界工具）**：仓库没有现成的离线冻结/恢复工具，因此新增 standalone
  CLI、manifest 和恢复 rehearsal；不复制第三方源码。使用 Python 标准库 `tarfile` 写出
  明文 tar，不新增 runtime dependency 或密钥流程。归档内容可由能读取目标介质的人直接读取；
  manifest 哈希可发现意外损坏或不匹配，不提供加密、来源认证或防恶意篡改。
- **DEFER**：生产停写由所有者按部署拓扑执行；到期销毁不在本票授权内，保留截止日只触发
  人工复核。

## 必须围住的 writer

在真实 workspace 运行前，所有者要逐项停写/围住并把证据写入 `writer-proof.json`。
本地文件锁只能保护一次文件操作，不能证明跨 clone、跨主机或人工编辑器已经停止，因此
不能作为 `scope` 或 `proof` 的唯一依据。

| writer 分类 | 停写/围住动作 | 停写已生效的证明口径 |
| --- | --- | --- |
| `memory_tool` | 停止提供 Memory tool 写入口的 harness/CLI 服务，并确认没有新请求在飞 | 进程/服务停止记录、最后写事件时间、无新的 outbox/Markdown mtime 变化 |
| `formation` | 停止 formation worker、scheduler 和恢复扫描器；先让正在运行的 job 进入可恢复状态 | worker/scheduler 进程清单、job lease/终态快照、停止后无新 job 写入 |
| `approval` | 停止审批 executor/promote/discard worker，禁止人工审批动作继续提交 | 审批 worker 停止记录、staged/blocked 行计数与最后更新时间 |
| `governance` | 停止治理、迁移、reprojection、维护脚本和定时任务 | 维护任务/cron/Task Scheduler 停止证据，数据库与 Markdown 不再变化 |
| `external_legacy_process` | 枚举所有 clone、主机、服务账号、旧版本进程和定时任务，逐一停止或隔离 | 每个来源的主机/进程/任务标识、停止时间与复核结果；不能只查当前 clone |
| `human_writer` | 关闭 Markdown 编辑器、同步工具和人工操作窗口，通知所有操作者冻结 | 操作者名单、冻结时间、编辑器/同步工具关闭确认与复核后的 mtime 稳定 |

证明文件的最小形状：

```json
{
  "schema_version": 1,
  "freeze_effective": true,
  "checked_at": "<UTC timestamp>",
  "writers": {
    "memory_tool": {
      "category": "memory_tool", "status": "stopped", "method": "endpoint-disable",
      "control_action": "<disable the Memory write endpoint>",
      "scope": {"workspace": "<deployment scope>", "hosts": ["<host>"], "clones": ["<clone>"], "cross_clone_checked": true},
      "proof": {"evidence_type": "service-state", "observed_state": "stopped", "evidence_ref": "<owner evidence reference>"},
      "verification": {
        "host": "<host or control plane>", "active_writers": 0, "active_writer_processes": [],
        "write_activity": "none", "mtime_stable": true, "last_write_at": "<UTC timestamp>",
        "verified_at": "<UTC timestamp>", "evidence_ref": "<owner evidence reference>"
      }
    },
    "formation": {"category": "formation", "status": "stopped", "method": "worker-quiesce", "control_action": "<stop workers>", "scope": {"workspace": "<deployment scope>", "hosts": ["<host>"], "clones": ["<clone>"], "cross_clone_checked": true}, "proof": {"evidence_type": "process-list", "observed_state": "stopped", "evidence_ref": "<owner evidence reference>"}, "verification": {"host": "<host>", "active_writers": 0, "active_writer_processes": [], "write_activity": "none", "mtime_stable": true, "last_write_at": "<UTC timestamp>", "verified_at": "<UTC timestamp>", "evidence_ref": "<owner evidence reference>"}},
    "approval": {"category": "approval", "status": "absent", "method": "endpoint-disable", "control_action": "<disable approval writes>", "scope": {"workspace": "<deployment scope>", "hosts": ["<host>"], "clones": ["<clone>"], "cross_clone_checked": true}, "proof": {"evidence_type": "control-plane", "observed_state": "absent", "evidence_ref": "<owner evidence reference>"}, "verification": {"host": "<host>", "active_writers": 0, "active_writer_processes": [], "write_activity": "none", "mtime_stable": true, "last_write_at": "<UTC timestamp>", "verified_at": "<UTC timestamp>", "evidence_ref": "<owner evidence reference>"}},
    "governance": {"category": "governance", "status": "stopped", "method": "worker-quiesce", "control_action": "<stop maintenance>", "scope": {"workspace": "<deployment scope>", "hosts": ["<host>"], "clones": ["<clone>"], "cross_clone_checked": true}, "proof": {"evidence_type": "scheduler-state", "observed_state": "stopped", "evidence_ref": "<owner evidence reference>"}, "verification": {"host": "<host>", "active_writers": 0, "active_writer_processes": [], "write_activity": "none", "mtime_stable": true, "last_write_at": "<UTC timestamp>", "verified_at": "<UTC timestamp>", "evidence_ref": "<owner evidence reference>"}},
    "external_legacy_process": {"category": "external_legacy_process", "status": "absent", "method": "process-stop", "control_action": "<stop all legacy processes>", "scope": {"workspace": "<all hosts/clones>", "hosts": ["<host>"], "clones": ["<clone>"], "cross_clone_checked": true}, "proof": {"evidence_type": "process-list", "observed_state": "absent", "evidence_ref": "<owner evidence reference>"}, "verification": {"host": "<host>", "active_writers": 0, "active_writer_processes": [], "write_activity": "none", "mtime_stable": true, "last_write_at": "<UTC timestamp>", "verified_at": "<UTC timestamp>", "evidence_ref": "<owner evidence reference>"}},
    "human_writer": {"category": "human_writer", "status": "stopped", "method": "manual-pause", "control_action": "<pause all human editing>", "scope": {"workspace": "<operator scope>", "hosts": ["<host>"], "clones": ["<clone>"], "cross_clone_checked": true}, "proof": {"evidence_type": "manual-confirmation", "observed_state": "stopped", "evidence_ref": "<owner evidence reference>"}, "verification": {"host": "<host>", "active_writers": 0, "active_writer_processes": [], "write_activity": "none", "mtime_stable": true, "last_write_at": "<UTC timestamp>", "verified_at": "<UTC timestamp>", "evidence_ref": "<owner evidence reference>"}}
  }
}
```

工具会 fail-closed：六类必须全部出现，类别必须与键相等，方法必须是受支持的停写动作，
状态只能是 `absent` 或 `stopped`，每项必须有非空 `scope`/`proof`/`control_action`，并且
结构化 verification 必须声明证据主机、`active_writers=0`、`write_activity=none`、最后写入
时间、复核时间和 owner evidence reference。`checked_at`、`last_write_at`、`verified_at` 都必须
带时区且不能是未来时间；`checked_at` 不得早于当前时间 24 小时以上。工具也不会接受
“local file lock only”作为围住证明。时间窗口和零写入声明是结构性门槛，不替代所有者对进程、
任务、mtime、job lease/终态与人工操作证据的审阅。

## 本地代表性 rehearsal

以下命令只在仓库外的临时目录生成代表性 workspace；它不是生产停写证据：

```powershell
$seed = Join-Path $env:TEMP 'memory-freeze-seed-1492'
$archive = Join-Path $env:TEMP 'memory-freeze-1492.iab'
$restore = Join-Path $env:TEMP 'memory-freeze-1492-restore'
.venv\Scripts\python.exe scripts/memory_freeze_backup.py seed --workspace $seed

.venv\Scripts\python.exe scripts/memory_freeze_backup.py backup `
  --workspace $seed `
  --output $archive `
  --writer-proof (Join-Path $seed 'writer-proof.json') `
  --rehearsal-fixture
.venv\Scripts\python.exe scripts/memory_freeze_backup.py restore `
  --archive $archive `
  --restore-root $restore
```

备份以 Python 标准库 tar 流直接生成明文 archive；成功的 backup receipt 必须显示
`encrypted=false`、`archive_format=plain-tar-v1`、`snapshot_method=sqlite-online-backup-api`、
`runtime_loaded=false`、`retrieval_registered=false` 与 `retention_until=2027-10-23`。
本地 seed 只有显式传入 `--rehearsal-fixture` 才允许使用 synthetic fixture proof；生产命令
不得传该 flag，必须使用 owner 生成的 process/service/scheduler/manual evidence。
restore receipt 必须显示 source files、database tables、row/table manifests 和 SQLite
`integrity_check` 全部匹配，并内嵌完整 `source_file_manifest`、各表状态/旧键/lease/provenance
摘要和行哈希。种子 V2 workspace 同时包含带 lease 的 forming job 与带 idempotency key、终态
及 outcome 的 completed job。Manifest 还绑定原 workspace 路径，工具会拒绝把恢复根放回
原 workspace；输出目录必须是 workspace 运行目录之外的全新隔离位置。至少存在 `memory.db` 或
`memory-v2.db` 才允许生成成功备份；两者都缺失会失败，不会把没有 SQLite 历史的 workspace
报为归档成功。恢复也会拒绝没有数据库快照的 manifest。生产验收时，所有者必须将 manifest 的
absent 数据库项与 workspace 预期来源清单逐项核对；若预期存在但缺失，停止验收。

恢复过程会在 restore-root 父目录下创建暂存目录；该父目录必须预先存在且访问受控，Windows
下暂存安全依赖该目录继承的 ACL。只有归档成员、source/db 哈希与 SQLite 完整性检查全部通过后
才发布恢复目录。哈希用于发现意外损坏或不匹配，不认证归档来源，也不防止恶意修改；校验失败时
会清理暂存内容。

## 生产执行（待所有者在场）

当前 issue worktree 未包含生产数据库；其他本地 clone 的 workspace 不自动纳入本票。所有者须
指定每个 deployment workspace，并把 manifest 的 absent 数据库项与预期来源清单逐项核对。
以下生产步骤仍待 owner 在场执行：

1. 所有者按上表完成六类 writer 的停写/围住，审阅证据并生成完整 `writer-proof.json`；
   生产 backup 不得使用 `--rehearsal-fixture`。
2. 选择 workspace 外、访问权限受控且已存在的离线输出目录，并在操作记录中注明获准读取者/
   访问范围；Windows 上核实目录 ACL，若介质不支持 ACL，则记录物理保管方式并确保仅获准者能访问。`.iab` 是明文，包含 Memory 内容，
   任何有权限读取该介质的人都能读取。不得把归档放入 workspace，也不得把它挂载成当前 Memory
   retrieval source。
3. 运行 `backup`，工具会枚举数据库主文件及 WAL/shm/journal、Markdown 主文件与
   bak/tmp/lock，并对每张表记录列、行数、行哈希、状态、旧键、lease、provenance 的
   计数/哈希。冻结期间任何源文件变化都会使命令失败，不会产出可接受的 archive。
4. 在独立主机运行 `restore`；restore-root 父目录必须预先存在且访问受控，并在操作记录中注明
   获准读取者/访问范围。核对 manifest match、每个 DB 的 SQLite integrity check 与表级 hashes；
   不要把恢复出的 snapshot 接入运行时。
5. 将明文 archive 与 receipt 按所有者选定的介质与保留策略登记。保留截止日固定为
   **2027-10-23**；到期只创建人工复核事项，不能自动删除数据库、日志、映射或 backup。
   销毁必须另立票并取得单独裁决。

## 失败处理

- 缺 writer、writer 仍为活动状态、使用本机锁冒充跨主机冻结、workspace 在操作期间变化、
  输出位于 workspace 内或归档成员/哈希/SQLite 校验失败：停止并修正证据/路径后重试。
- 进程被强制终止时不能保证清理；受控输出目录中可能留下隐藏的明文 `.tmp` 归档。确认进程已退出后，
  所有者须检查并按介质保管策略处置；不能仅凭临时文件存在就认定备份成功。
- 不要调用 Memory runtime 初始化来“发现表”，因为初始化路径可能执行 tombstone 清理；
  工具只读现有文件，并用 SQLite Online Backup API 生成 destination snapshot。
- 不要只复制 WAL 活跃数据库的 `.db` 主文件，也不要手工删除任何 sidecar、tombstone、
  outbox、job 或 Markdown 备份文件。
