# ADR-0046 — Memory V2 独占运行时与 clean-slate cutover

- **Status**: Accepted（#303 代码已集成；integration clean-slate cutover 与 AC8 真实链路验证已完成）
- **Date**: 2026-09-26
- **Deciders**: 用户（批准 Memory V2 独占运行时与限定范围的 clean-slate reset）+ 本 Agent（机制设计）
- **Related**: Issue #303 / MEM-V2-7；ADR-0024、ADR-0031、ADR-0042；`docs/tickets/mem-v2-7-clean-slate-cutover.md`
- **Supersedes**: ADR-0042 D10 的 V1/V2 共存状态、D11 中“不 cutover / 不启用 V2 formation”的非目标，以及 D12 中“cutover 归 #303”的待办状态；ADR-0031 #300 addendum 中“V1 store/fallback 尚未退役”的状态；ADR-0024 对生产应用仍装配 V1 `MemoryComponents` 的假设。V1 provider 类型与独立库 API 保留为未接线的兼容代码。

## Context

MEM-V2-1 到 MEM-V2-6 已交付 V2 生命周期、持久化、形成、召回与治理能力。并行期曾让 V1 与 V2 共存，以便逐票建设；但 V1 的 heuristic/final-answer 写回、`SESSION` 长期记忆和读取 fallback 与 PRD 中 V2 唯一权威及 `SESSION` 不属于长期记忆 scope 的规则冲突。Issue #303 授权在依赖集成后清除旧数据并收口生产接线。

清除操作不可逆，因此执行必须证明目标精确、写入者已停、保留数据未被本操作改变，并且过程可在中断后安全恢复。记录中不输出环境变量值、记忆正文或旧内容备份。

## Decisions

### D1 — 生产应用只装配 Memory V2

- Runtime assembly 不再注入 V1 `memory_writer`；生产 capability wiring 不装配 V1 `MemoryComponents`、V1 recall context、V1 memory tools 或 V1 API fallback。
- 自动形成、recall/context、显式工具与治理 API 使用同一个 V2 service；SQLite 是唯一记录权威，Milvus 是 V2 派生索引。
- V1 存储、provider factory 和类型仍可被独立兼容代码导入，但应用 assembly 不再能到达它们。V2 故障按 optional capability 的既有降级语义处理，不回退到 V1。

### D2 — 删除 allowlist 固定为 Memory 专属对象

唯一允许删除的本地对象：

| 目标 | 删除对象 |
| --- | --- |
| `<workspace_dir>/memory.db` | `memory_records`、`memory_outbox` |
| `<workspace_dir>/memory-v2.db` | `memory_v2_records`（包括 Profile tier）、`memory_v2_outbox`、`memory_v2_tombstones`、`memory_v2_settings`、`memory_v2_jobs` |
| 已配置的 Milvus Memory collection | 仅该精确 collection：drop 后按 V2 schema 重建 |

只允许预期数据库文件、allowlist 表和 Memory collection schema。Milvus schema 必须精确包含 V2 字段及类型、VARCHAR 长度、`id` 主键、`tenant_id` 分区键、关闭的动态字段和正向 vector dimension。未知 SQLite object、符号链接/硬链接、宽泛/未解析或不存在的 collection 名、与 Knowledge 同名的 collection、非 Memory schema、待提交 SQLite WAL/journal 或活动 workspace writer 均导致拒绝。Milvus URI 与认证凭证共同参与目标 SHA-256 指纹；输出只记录指纹，不回显 `.env` 值。仅已有有效 fence 的中断恢复允许 collection 暂时不存在。

### D3 — 先只读计划，再用计划哈希确认执行

`--dry-run` 只读取 SQLite、Milvus、Knowledge 与保留域的计数/结构指纹，不调用写接口。`--apply` 要求调用者提交本次 dry-run 的 `plan_sha256`；取得 workspace lock 后先发布 startup fence，再异步重算计划，任何目标、计数或保留域变化都会拒绝执行。计划把 Milvus URI 与认证凭证绑定到仅输出 SHA-256 的目标指纹，防止相同 collection 名在另一服务/账号下被误清理。

计划、fence 和报告只记录文件/collection 指纹、schema 指纹、计数、时间与结果状态，不含 memory 内容或凭证值；不创建内容备份。

### D4 — workspace lock 与持久 fence 阻止新写入

执行前必须取得和 web/CLI 一致的 `InstanceLock`。`ALLOW_SHARED_ROOT` escape hatch 开启时拒绝执行。操作先将 `.memory-cutover-in-progress` 写入同目录临时文件并 fsync，再原子替换为 fence；正常 web/CLI 启动在 fence 存在时失败关闭。逃生门启动会持有每进程 OS 锁定的 writer lease，并在注册后复查 fence；未开启逃生门的普通启动在取得主锁后也扫描 lease，有活动 writer 时拒绝启动；cutover 在首次异步盘点前再次扫描 lease，有活动 lease 就在任何数据变更前拒绝。异常退出由 OS 释放 lease，后续启动或 cutover 可清理 stale lease。切换前必须先重启旧版本应用进程，使其具备 lease 注册行为。只有 `--resume` 可在目标及 preservation baseline 仍匹配时继续；`--apply` 遇到已有 fence 会拒绝。完成报告落盘后才删除 fence。

Writer lease 从创建到取得 OS 锁期间，与扫描、清理 stale lease 共用短时 registry lock；registry 忙或不可用时扫描失败关闭。`--resume` 取得主锁后还会重读完整 fence，若它在等待主锁期间已改变或被移除，就在任何数据变更前拒绝恢复。

### D5 — SQLite 旧页与派生索引同时清除

对 allowlist 表以 `BEGIN IMMEDIATE` 清空并提交，启用 `secure_delete`，随后执行 `VACUUM`；若使用 WAL，则要求 truncate checkpoint 完成。最终检查所有 allowlist 行数为零且 SQLite freelist 为零。Milvus 只 drop 已验证的 Memory collection，重建后查询零行；V2 首次形成成功后仍由 SQLite outbox 收敛派生索引。

这采用 SQLite 官方支持的 secure-delete、VACUUM 与 WAL checkpoint 机制，并调用 Milvus 官方 drop-collection / count-query API；不复制或引入新的数据库框架。

### D6 — 保留证明覆盖本地边界与非目标服务

- `sessions/`、`workspaces/`、本地 artifact 根目录及 evaluation 树以文件数、字节数和内容哈希作前后比对。
- `harness.db` 及 WAL/SHM/journal sidecar 以字节哈希作前后比对；该 DB 保存 Session metadata 与 checkpoint。
- Knowledge collection 前后核对 hashed identity、schema hash 与强一致 count；操作不对它发出 drop/delete/upsert。
- MinIO/S3 object store 与 Langfuse 不连接、不读取、不修改；报告明确标注外部存储未访问。Milvus 鉴权用于读取和修改批准的 Memory collection；embedding key 只做配置完整性检查，凭证源不被写入或打印。

外部 object store 的对象内容哈希不在本次证明范围内；证明来自执行路径没有调用对应 Provider/API。此操作不能证明其他并发主体没有修改外部对象。

### D7 — 删除后不可由代码回滚恢复

成功后 V1/V2 记忆正文、Profile、tombstone 与 replay/outbox 状态均不存在，且不保留旧内容备份。回滚代码只能恢复程序版本，不能恢复记忆内容。只有经独立授权的数据恢复副本才可能恢复已删数据；本 ADR 和 #303 明确不创建该副本。

## Consequences

- 正面：生产只有 V2 一条形成/召回/治理路径；旧 V1 工作不会从 SQLite outbox 或 Milvus 索引复活。
- 代价：首次启动从空 V2 store 开始；旧记忆不能由代码回滚找回。真实执行之前必须核验 dry-run 指纹、计数和 preservation 清单。
- 复用来源： [SQLite PRAGMA secure_delete](https://sqlite.org/pragma.html#pragma_secure_delete)、[SQLite VACUUM](https://www.sqlite.org/lang_vacuum.html)、[SQLite WAL](https://www.sqlite.org/wal.html)、[Milvus drop_collection](https://milvus.io/api-reference/pymilvus/v2.6.x/ORM/utility/drop_collection.md)、[Milvus query](https://milvus.io/api-reference/pymilvus/v2.6.x/MilvusClient/Vector/query.md)。

## Implementation evidence

`#303` 代码通过 PR #374 合入，merge `9c7181ce82d33a52f517be6b273101711652f981`；分支 Gate-0 与 PR 必需的服务端 `gate0` 均通过。integration clean-slate 使用用户确认的 plan hash `137aeaa1059c4933a73f4ab7738941593a3c8639ad8112d80b2ffe6a30ca0810` 完成，原始报告见 [`memory-v2-clean-slate-cutover-report-2026-09-27.json`](../evidence/memory-v2-clean-slate-cutover-report-2026-09-27.json)，SHA-256 `6f79b0c16dde7c85387914aca461d18ae664d7fa73c8970b28ab1682dc03f6bf`。完成后及后续只读复核均确认 V1/V2 SQLite allowlist、Milvus Memory 与 Knowledge 计数为零；保留域指纹与报告一致；startup fence 已移除；没有创建旧记忆内容备份。复核 plan hash 为 `dc0883a1281c2cb55bd3ff166e959f1b039fd2719b140a4f70b5b4253205f221`。

AC8 使用真实 memory formation 模型、embedding 与 integration Milvus，在隔离 SQLite/JSONL 和随机身份下完成：job `completed/committed`、形成 1 条带事件来源的 V2 记录、生产 service 与原始 Milvus 检索均命中；经 service 删除后无 active 记录且 Milvus 检索无命中。完整 pytest 为 4435 passed / 14 skipped / 51 deselected；合并后 cutover/runner 定向回归 60 passed。脱敏 smoke 结果、最终 dry-run 计划、覆盖闸门与 Gate-0 证据索引见 `docs/phase_status/2026-09.md`。
