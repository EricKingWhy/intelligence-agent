# Memory freeze / encrypted offline backup runbook

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
  CLI、manifest 和恢复 rehearsal；不复制第三方源码。AES-GCM 使用已声明的
  `cryptography` 依赖；其官方文档要求密钥保密且同一密钥不得复用 nonce：
  <https://cryptography.io/en/latest/hazmat/primitives/aead/#aes-gcm>。
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
    "memory_tool": {"status": "stopped", "scope": "<deployment scope>", "proof": "<evidence>"},
    "formation": {"status": "stopped", "scope": "<deployment scope>", "proof": "<evidence>"},
    "approval": {"status": "absent", "scope": "<deployment scope>", "proof": "<evidence>"},
    "governance": {"status": "stopped", "scope": "<deployment scope>", "proof": "<evidence>"},
    "external_legacy_process": {"status": "absent", "scope": "<all hosts/clones>", "proof": "<evidence>"},
    "human_writer": {"status": "stopped", "scope": "<operator scope>", "proof": "<evidence>"}
  }
}
```

工具会 fail-closed：六类必须全部出现，状态只能是 `absent` 或 `stopped`，每项必须有
非空 `scope`/`proof`，`checked_at` 必须是带时区且不早于 24 小时的时间戳，且不会接受
“local file lock only”作为围住证明。时间窗口是结构性新鲜度检查，不替代所有者对证据内容的
人工审阅。

## 本地代表性 rehearsal

以下命令只在仓库外的临时目录生成代表性 workspace；它不是生产停写证据：

```powershell
$seed = Join-Path $env:TEMP 'memory-freeze-seed-1492'
$archive = Join-Path $env:TEMP 'memory-freeze-1492.iab'
$restore = Join-Path $env:TEMP 'memory-freeze-1492-restore'
.venv\Scripts\python.exe scripts/memory_freeze_backup.py seed --workspace $seed

# 由所有者从安全输入/密钥管理器设置；不要把密钥写入仓库、日志、报告或命令历史。
$env:MEMORY_BACKUP_KEY = '<base64 encoding of an owner-managed 32-byte key>'
.venv\Scripts\python.exe scripts/memory_freeze_backup.py backup `
  --workspace $seed `
  --output $archive `
  --writer-proof (Join-Path $seed 'writer-proof.json') `
  --key-env MEMORY_BACKUP_KEY
.venv\Scripts\python.exe scripts/memory_freeze_backup.py restore `
  --archive $archive `
  --restore-root $restore `
  --key-env MEMORY_BACKUP_KEY
```

备份在 AES-GCM 加密 tar 流中直接生成，不落未加密的 tar/staging 副本；成功的 backup
receipt 必须显示 `encrypted=true`、`snapshot_method=sqlite-online-backup-api`、
`runtime_loaded=false`、`retrieval_registered=false` 与 `retention_until=2027-10-23`。
restore receipt 必须显示 source files、database tables、row/table manifests 和 SQLite
`integrity_check` 全部匹配。加密 manifest 还绑定原 workspace 路径，工具会拒绝把恢复根放回
原 workspace；输出目录必须是 workspace 运行目录之外的全新隔离位置。

## 生产执行（待所有者在场）

本仓库没有生产数据库，以下步骤在本票中没有被假装执行：

1. 所有者按上表完成六类 writer 的停写/围住，审阅证据并生成完整 `writer-proof.json`。
2. 选择 workspace 外、访问权限受控的离线输出目录；不得把 `.iab` 放入 workspace，也不得
   把它挂载成当前 Memory retrieval source。
3. 从所有者管理的安全输入设置 `MEMORY_BACKUP_KEY`。只传环境变量名给命令；不要把 key
   本体写入文件、日志、报告、issue 或聊天。恢复时由同一密钥管理流程重新注入该环境变量，
   再运行 `restore` 到另一个隔离目录；密钥丢失时联系密钥所有者按其备份/轮换流程恢复，
   本仓库不保存、导出或重置密钥。
4. 运行 `backup`，工具会枚举数据库主文件及 WAL/shm/journal、Markdown 主文件与
   bak/tmp/lock，并对每张表记录列、行数、行哈希、状态、旧键、lease、provenance 的
   计数/哈希。冻结期间任何源文件变化都会使命令失败，不会产出可接受的 archive。
5. 在独立主机/目录运行 `restore`，核对 manifest match、每个 DB 的 SQLite integrity check
   与表级 hashes；不要把恢复出的 snapshot 接入运行时。
6. 将加密产物与不含密钥的 receipt 按组织保管策略登记。保留截止日固定为 **2027-10-23**；
   到期只创建人工复核事项，不能自动删除数据库、日志、映射或 backup。销毁必须另立票并
   取得单独裁决。

## 失败处理

- 缺 writer、writer 仍为活动状态、使用本机锁冒充跨主机冻结、workspace 在操作期间变化、
  输出位于 workspace 内、缺少密钥或密文认证失败：停止并修正证据/路径后重试。
- 不要调用 Memory runtime 初始化来“发现表”，因为初始化路径可能执行 tombstone 清理；
  工具只读现有文件，并用 SQLite Online Backup API 生成 destination snapshot。
- 不要只复制 WAL 活跃数据库的 `.db` 主文件，也不要手工删除任何 sidecar、tombstone、
  outbox、job 或 Markdown 备份文件。
