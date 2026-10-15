# ADR-0055：Docker workspace 的恢复与硬删除串行化（卷缺失即 fail-closed）

- 状态：已接受
- 日期：2026-10-15
- 相关：issue #1132（W-31 follow-up）、ADR-0029（会话硬删除）、
  ADR-0054（私有 Docker named volume 隔离）、ADR-0001（Sandbox 抽象）

## 背景

`WorkspaceRegistry.get()` 在缓存未命中时会重建 Sandbox 并 `ensure_started()`；
Docker 后端的 `containers.run(volumes=...)` 对**不存在的具名卷**的语义是
**创建并挂载一个空卷**（Docker 官方 Volumes 行为，不报错）。

会话硬删除（`SessionService.delete_session` → `cleanup_docker_workspace_resources`
→ `discard_session_artifacts`）会删掉该会话的容器、私有卷与映射。两者并发时：

1. 恢复读到映射、构造好 Sandbox（尚未 `ensure_started`）；
2. 删除在窗口内跑完（容器与卷已删、映射已摘）；
3. 恢复继续 `ensure_started()` ⇒ **重建一个空卷**，"成功"返回一个空 workspace；
4. 原文件不可达，且映射已被摘除 ⇒ 空卷/容器成为**无法回收的孤儿**（再删一次也
   不会碰它们，因为 `cleanup_docker_workspace_resources` 依赖映射定位所有权）。

真 Docker 实测（Docker 29.8.1）：legacy 形状的映射在该交错下恢复**静默成功**，
读原 `marker.txt` 抛 `FileNotFoundError`；post-#1048 形状则被导入完成标记门拦成
`WorkspaceImportIncompleteError`——**但空卷与孤儿容器照样被建出来了**（闸门只是
挡住了"返回"，没挡住"重建"）。

## 决策

1. **per-workspace 进程内串行化锁**。`WorkspaceRegistry._workspace_lock(owner_id)`
   返回 `(registry root, owner session id)` 维度的 `threading.RLock`，由
   **类级 `WeakValueDictionary`** 持有 ⇒ 同一 root 下的多个注册表实例（web app 与
   capability wiring 各建一个）**共用同一把锁**；无持有者时自动回收，不按会话数累积。
   用 `RLock` 而非 `Lock`：`get()` 临界区内可能再进入同一会话的加锁路径。
   - 取锁方：`get()` 的冷恢复临界区、`cleanup_docker_workspace_resources`、
     `discard_session_artifacts`。删除路径的键经 `_owner_lock_key()` 解析到 owner
     （别名与 owner 共用一把）；解析失败退回自身 id，**绝不因此抛错**（删除必须幂等）。
2. **卷缺失即 fail-closed**。`DockerSandbox.workspace_volume_exists()` 检查具名卷；
   `get()` 在**同一临界区内、`ensure_started()` 之前**校验，缺失即抛
   `WorkspaceBindingError`，绝不"重建一个空的"。锁内同时**重读映射**并以它为准：
   等锁期间映射可能已被删除（fail-closed）或被改写（legacy 迁移落盘）。

**两个机制都承重，缺一不可**（消融实测，真 Docker）：

| 变体 | 结果 |
| --- | --- |
| 无锁、无检查（基线） | 空卷重建 + 孤儿（`empty_volume_orphan=true`） |
| **仅** per-method 锁 | **与基线逐项相同**（临界区是方法级的，窗口在方法之间） |
| **仅**卷检查、无锁 | **复现原症状**：检查通过 → 删除删卷 → `ensure_started` 重建空卷、恢复静默成功于空 workspace |
| 锁 + 锁内检查 | 闭合：卷不被重建、恢复 fail-closed、无孤儿 |

⇒ 锁把"检查 → `ensure_started`"变成**不可被打断的临界区**；检查让"卷已没"**失败关闭**
而不是重建空卷。任缺其一，本缺陷都仍然成立。

## 边界与代价

- **进程内**锁。跨进程（两个进程同 root）不在本票范围：本仓 `instance_lock`
  （#150）已规定同一 `workspace_dir` 同时只允许一个进程，CLI 与 Web 并发被拒。
- **不改变** ADR-0029 D3 的删除顺序与幂等语义：锁只在方法级串行，各步语义不变；
  删除仍然可重试（映射保留到 `discard_session_artifacts`）。
- **不引入**新依赖、不改 Sandbox 路径边界（ADR-0001 不变）。
- 锁表弱引用，稳态开销与活跃 workspace 数同阶，不随历史会话数增长。
- 外部（非本进程）删除具名卷后恢复同样 fail-closed——这是**有意**的：卷没了就是
  没了，恢复不该悄悄给一个空的。
