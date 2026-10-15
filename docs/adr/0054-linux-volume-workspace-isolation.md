# ADR-0054：受影响工作区迁到私有 Docker named volume（Linux 文件系统语义）

- 状态：已接受
- 日期：2026-10-14
- 相关：issue #1048、ADR-0025（宿主侧来源闸）、ADR-0028（host-directory 边界）、ADR-0055（Docker workspace 恢复与硬删除并发）、
  `docs/spec` 冻结规格 `05_SANDBOX_CODING_TOOLS.md` §3-4（**不回写**）

## 背景

NTFS **hard link** 是同一卷上**同一文件对象的多个名称**（同一 MFT 记录，非副本）。
路径授权 / ACL 约束的是"名字"而不是"对象"，因此在宿主目录上用 PSEC/ACL 授权**无法**
在对象身份层面隔离——workspace 内的一个名称可 hard link 到 workspace 外的文件，
容器内改写该名称即写到同一对象，外部 canary 被改。

结论：隔离对象身份**只能靠换文件系统 + 副本式导入/导出**，不能在 Windows 上加 ACL。

## 决策

1. **受影响隔离会话使用每会话私有 Docker named volume** 挂到 Linux `/workspace`。
   容器不挂宿主项目目录、不挂 docker socket、不挂其它可写宿主路径（Docker 官方
   Volumes 语义：named volume 由 daemon 管理、独立于任一宿主路径；只有 bind mount
   才直链宿主目录）。
2. **导入 = 快照（snapshot）**：建会话 / legacy 迁移时把宿主源目录**副本**导入私有
   volume（逐项 tar 复制 ⇒ 容器内对象与宿主源**不同 inode**）。导入完成前会话不可
   运行；导入失败/中断重试不把半份 workspace 当已初始化；已持久化 volume 恢复不重导入、
   不覆盖 agent 后续修改。导入拒绝会逃出源根的符号链接（fail-closed）。
3. **导出 = 显式副本到全新宿主目录**：本地 REST `POST /api/sessions/{id}/workspace/export`
   显式触发，请求带**绝对**宿主目标路径（非绝对即 422）；来源可信闸沿用既有
   host-directory API 口径（`web.projects.require_trusted_origin`，ADR-0025 D1(b)/ADR-0028
   D2，与 `GET /api/host/dirs` 同一份）——注意它只判定**请求来源**（本机/可信
   Origin），**不是路径级授权**；接受任意绝对路径与既有 host-directory API 口径一致
   （协调员裁决 2026-10-14），故**不设** `Settings.workspace_dir` 根限制。目标
   **已存在即拒绝**（不覆盖；并发建同一目标后到者亦 409）；导出树**先全量校验成员**
   再建目标目录（成员不安全即整体拒绝，不留半个/空目录）；拒绝绝对路径 / `..`
   穿越 / 不安全特殊项；符号链接不写穿新目标、硬链接按内容复制（不保留对象身份），
   外部 canary 不受影响。成员不安全 → 422，服务端故障（磁盘/权限）→ 500。
4. **Docker-backed fork**：父 volume 复制到**独立子 volume**，不对 `/workspace` 用
   宿主 `Path`/`shutil.copytree`；子会话沿用 Docker 后端，父子写互不共享。
5. **语义（backends）**：项目指令读取、文件工具路径解析、审批身份计算、web workspace
   文件路由对 Docker workspace 一律走 **Sandbox/POSIX 语义**；宿主源路径**仅**作项目
   身份与导入来源。`local` 子进程后端仍是宿主目录语义。

## 写入语义：docker 就地截断写 vs local 原子替换（协调员裁决 2026-10-14）

AC4 要求**完全位于 workspace 内**的 hard-link pair 在导入后**仍保持可用**。docker
导入按单个 tar 记录 inode，pair 导入后同 inode；但若 `write_text` 用
`put_archive` 直接落目标，会 **unlink 旧 inode**、把 pair 拆成两个独立对象（实测
`a.txt`→新 inode）。因此 `DockerSandbox.write_text` 改为**容器内就地覆盖**：
先落临时文件，再 `cat 临时文件 > 目标`（截断写保留 inode），docker-only。

**代价**：docker 侧**放弃了** `local` 后端那样的 temp + `os.replace` 原子替换。
**裁决（协调员 2026-10-14）**：**接受**，不改回。理由：① AC4「内部 pair 仍保持可用」
是硬要求；② POSIX 就地截断写即标准写语义；③ 本 ADR §5 明定 docker 走 **Sandbox/
POSIX 语义**（而非宿主目录语义），`local` 仍是宿主目录语义故保留原子替换；④ 无既有
测试要求 docker 写入必须原子。docker 与 local 的这一差异是**有意的**、已在此登记。

## legacy（PSEC）迁移与失败语义

Windows PSEC **不再作为新会话可选/可请求后端**（只用于解析既有映射，属 legacy）。
旧 PSEC 映射在**下次恢复**时自动迁移：顺序固定为「探针 → 快照导入私有 volume →
完整导入成功后才**原子**改映射并返回 `DockerSandbox`」。任一步失败即抛错——**不继续
返回 PSEC、不丢弃旧映射、不动宿主文件**（fail-closed，不回落）。

**已知残余风险（`snapshot_import` 闸门取舍）**：导入完成标记的检查只对**快照导入
路径建立**的映射（`mapping.snapshot_import=true`）生效——pre-#1048 的 Docker
映射没有该字段，其 volume 从未经过快照导入，故恢复时**跳过**标记检查、直接放行
（否则这些 legacy volume 会被误判为"半份导入"而 brick）。取舍：没有任何正记录
能区分"legacy volume 正常"与"legacy volume 被清空"，若 legacy volume 实际为空/
内容丢失，恢复后会话会在**空工作区**上静默运行（AC3"不把半份当已初始化"在此角落
降级为"不阻断 legacy 恢复"）。该取舍是解 P2 brick 的必要代价，已如实登记；
`test_pre_snapshot_docker_mapping_recovers_without_marker` 只覆盖"能恢复"，不覆盖
空 volume 判定。

## 关于 Windows NTFS 的如实登记

- **不宣称** PSEC/ACL 能隔离 NTFS hard link——正相反，正是它不能，才有本决策。
- Linux 上用 Linux `os.link` 等价验证对象身份隔离（同为"多名称同一对象"语义）。
- 真实 Windows NTFS + MXC PSEC 的端到端硬链接验收**部分登记未完成**（本环境无
  Windows），不虚构结论。

## 并发与部署边界

`WorkspaceRegistry.create()`、local workspace restore、Docker restore、legacy PSEC migration、registry-owned `DockerSandbox.ensure_started()` 与恢复 launch commit point，共同遵守按规范化 sessions root 和 canonical owner 分区的进程级 owner lock；同一进程的所有 `WorkspaceRegistry` 实例共享这把锁。get/restore 与 migration 在锁内重读并校验映射；launch 在锁内检查删除标记并提交。缓存实例也必须在每次启动前检查该标记，因此已装配但尚未 launch 的 runtime 不能在删除后重建卷。

`SessionService.delete_session()` 持有 `RunManager.session_lock` 直到硬删完成。初始创建在 `session/started` 落盘后立即取得同一把锁，并持有到进度、项目索引、预算等后续工作完成，以及 `launch=True` 提交或 `launch=False` 返回；恢复也在提交 launch 的临界区使用该锁，并在锁内重读持久化事件状态。创建或恢复先提交 launch 时，硬删不会仅因 launch 先发生而拒绝；若删除检查时仍有在途 run，则按现有守卫拒绝。删除先完成时，等待中的创建或恢复发现 session 不存在并失败关闭。`launch=False` 的创建与删除同样串行，删除不能在创建仍写入会话关联或预算时越过它。同 owner 的 alias 恢复也由 owner lock 和删除标记保护。

硬删对每个 owner 映射都在 owner lock 内原子写入 `workspace_delete_in_progress`，然后才删除 Docker 资源；无映射时写入 marker-only 映射。local workspace 与 legacy PSEC 宿主工作区不会在这一步被删除。标记保留到 `discard_session_artifacts()` 删除映射为止；标记存在时，owner、alias、任意 registry 缓存恢复、旧 sandbox 启动与 launch 提交都失败关闭。清理失败时保留标记与映射，以便同一 hard-delete 重试；重试可以继续清理。这覆盖资源清理后的异步 DB / 文件删除窗口，也避免进程重启后重新创建已删除的 volume。

当前支持每个 sessions root 运行一个服务进程。多进程 / 多 worker 共享该 root 不受支持，因为 run ownership 与 active-session 状态同样为进程内状态；跨进程支持前须先实现协调式 session ownership 与 workspace locking。

ADR-0055 补充 Docker named volume 存在性检查与删除锁 owner-key 解析；本 ADR 的删除标记、RunManager session lock 和 launch 提交点仍定义完整删除窗口，ADR-0055 不缩窄这些语义。
## 后果

- 冻结 Engineering Specification **不回写**；本 ADR 与操作说明是唯一新增语义面。
- 复用既有 `DockerSandbox`（named volume + tar `copy_in`）与 host-directory 来源闸，
  不新增 Windows ACL/AppContainer/第二套内核。
