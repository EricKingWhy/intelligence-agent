# Memory V2 clean-slate cutover 操作手册

本操作会不可逆删除 Memory V1/V2 记录与指定 Milvus Memory collection。它不保留旧记忆内容备份。代码回滚不会恢复已删数据。

**一次性执行记录（2026-09-27）**：integration clean-slate 已按批准计划执行并完成，AC8 真实 V2 formation / Milvus retrieval smoke 也已完成；证据见 [`memory-v2-clean-slate-cutover-report-2026-09-27.json`](../evidence/memory-v2-clean-slate-cutover-report-2026-09-27.json)、[`memory-v2-clean-slate-post-reset-plan-2026-09-27.json`](../evidence/memory-v2-clean-slate-post-reset-plan-2026-09-27.json) 与 [`memory-v2-cutover-ac8-live-smoke-2026-09-27.json`](../evidence/memory-v2-cutover-ac8-live-smoke-2026-09-27.json)。不要重放本次 `--apply` 或旧 fence 的 `--resume`；本手册的步骤只适用于经批准的新环境/新目标。

## 范围

仅允许清理：

- `<workspace_dir>/memory.db` 中的 `memory_records`、`memory_outbox`；
- `<workspace_dir>/memory-v2.db` 中的 `memory_v2_records`、`memory_v2_outbox`、`memory_v2_tombstones`、`memory_v2_settings`、`memory_v2_jobs`；
- 精确配置的 Milvus Memory collection。

SessionEvent、`harness.db` 中的 session metadata/checkpoint、workspace 文件、本地 artifacts、Knowledge、evaluation 数据、MinIO/S3 对象和 Langfuse evidence 不属于删除目标。

## 执行

在已集成 #303 代码的仓库根目录运行。执行前先重启/停止所有旧版本应用进程，确保没有仍在运行但不登记 shared-root lease 的实例。未开启逃生门的普通启动若取得主锁后仍发现活动 shared-root lease，会拒绝启动；cutover 会取得 workspace `InstanceLock`、原子发布 startup fence，并再次检查活动 lease，发现活动 writer 会在数据变更前拒绝执行。异常退出留下的 stale lease 会由后续启动或 cutover 清理。`ALLOW_SHARED_ROOT` escape hatch 必须在 cutover 进程中关闭。

先做只读计划：

```powershell
uv run --locked python -m agent_harness.memory.v2.cutover --dry-run
```

执行前人工确认计划中的数据库/table allowlist、行数、Memory collection 身份指纹/Schema/行数、Knowledge count/schema 和 preservation 指纹均符合预期，且 `backup_created` 为 `false`。Milvus endpoint 与认证凭证共同绑定到目标 hash，输出不包含其原值；请在本机私下核对 workspace 与 collection 配置确实指向批准的目标，不要把凭证或环境文件内容贴入日志/聊天。新 cutover 若目标 collection 不存在会拒绝执行；仅带有效 fence 的中断恢复可接受 collection 暂缺。

如果执行因活动 shared-root writer lease 被拒，先正常停止该应用实例，再重新运行 dry-run 并用新 plan hash 执行；不要手动删 lease 或 fence。旧版本进程必须先退出，因为它们不会登记 lease。

Lease 注册与 stale lease 扫描由短时 registry lock 串行化；若报告 registry 忙或不可用，应先确认并停止正在注册的 writer，再重试，不要手动删 registry lock 文件。

用**同一份 dry-run** 输出中的 `plan_sha256` 执行，报告路径必须在 workspace root 外且不存在：

```powershell
uv run --locked python -m agent_harness.memory.v2.cutover `
  --apply `
  --confirm-plan-sha256 <本次-dry-run-plan_sha256> `
  --report "$env:TEMP\memory-v2-clean-slate-report.json"
```

报告只包含身份/schema 哈希、计数、时间与结果状态。完成后核实状态为 `completed`、V1/V2 表计数为零、Milvus Memory 重建后为零、Knowledge 与本地 preservation 指纹未变，并且 workspace 中 `.memory-cutover-in-progress` 已移除。将经内容/凭证扫描确认的报告归档到 `docs/evidence/`。

## 中断恢复

如果进程中断，workspace fence 会阻止 web/CLI 正常启动。不要手动删除 fence，也不要启动其他 Memory writer。先检查原目标与保留域仍符合本机预期，然后使用**新的、尚不存在且位于 workspace root 外**的报告路径：

```powershell
uv run --locked python -m agent_harness.memory.v2.cutover `
  --resume `
  --report "$env:TEMP\memory-v2-clean-slate-resume-report.json"
```

恢复前会比较原始 dry-run baseline、SQLite 目标和 Knowledge/本地保留指纹；取得 workspace 主锁后还会重读完整 fence。任何变化或 fence 已被另一恢复进程移除都会拒绝恢复；若已有成功报告，应先核实结果，不要重放旧的 `--resume`。成功时报告写入后才移除 fence。

## 回滚边界

切换前可以停止操作；切换成功后没有应用级 rollback。重新部署旧代码不会恢复已删除的 V1/V2 内容。V2 可从空库正常运行，新的合规交互可以创建新记忆。MinIO/S3 与 Langfuse 没有被此操作访问或变更；SQLite 临时重建使用的工作空间不是可恢复备份。

执行使用 SQLite 官方 `secure_delete`、`VACUUM` 与 WAL checkpoint，以及 Milvus 指定 collection 删除/计数接口；细节见 [ADR-0046](../adr/0046-memory-v2-exclusive-runtime-and-clean-slate-cutover.md)。
