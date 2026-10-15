# ADR-0055：Docker workspace 恢复与硬删除并发控制（缺卷失败关闭）

- 状态：已接受
- 日期：2026-10-15
- 相关：issue #1132、issue #1159、ADR-0029（会话硬删除）、ADR-0054（私有 Docker named volume 隔离）、ADR-0001（Sandbox 抽象）

## 背景

Docker containers.run(volumes=...) 在目标具名卷不存在时会创建并挂载新空卷，见 [Docker 官方 Volumes 文档](https://docs.docker.com/engine/storage/volumes/)。如果恢复已读到旧映射，硬删除随后移除卷，恢复再执行 ensure_started()，表面上会成功，但 workspace 内容丢失；缓存实例也可能在卷被删后仍被返回。

真 Docker 复现（Docker 29.8.1）观察到 legacy 映射恢复静默返回空 workspace，读取原 marker.txt 得到 FileNotFoundError；导入完成标记只能阻止部分恢复返回，不能阻止空卷与孤儿容器被创建。

## 决策

1. WorkspaceRegistry 使用进程内 per-workspace RLock。锁键为规范化 registry root 与 canonical owner id；类级 WeakValueDictionary 让同一 root 下的多个注册表实例共享锁，且不按历史会话数累积。创建、恢复、资源清理和工件摘除都在对应 owner 锁内运行。
2. get() 在锁内重读 session 与 owner 映射，并以当前映射为准。Docker 冷恢复在 ensure_started() 前检查 named volume；缓存命中也检查同一卷。缺卷时抛 WorkspaceBindingError，绝不把卷重建为空。
3. cleanup_docker_workspace_resources() 与 discard_session_artifacts() 通过 _owner_lock_key() 解析到 canonical owner，使 alias 与 owner 使用同一把锁。映射缺失或损坏时退回 session id，不让锁键解析破坏硬删除幂等性。
4. 完整硬删除窗口由 ADR-0054 的持久 workspace_delete_in_progress 标记与 RunManager.session_lock 共同保护：标记在 Docker 清理前写入，保留至映射移除；创建或恢复的 launch 提交也受 session lock 和 workspace restore guard 保护。这里记录的 registry 锁与卷检查补强底层恢复安全，不替代或缩窄该完整窗口决策。
5. 卷缺失时失败关闭；Docker API 的其他错误照常传播。已有 snapshot_import 完成标记语义不变。

## 证据与消融

原始 #1132 真 Docker 消融隔离了 registry 恢复竞态：

| 变体 | 结果 |
| --- | --- |
| 无锁、无卷检查 | 空卷重建并留下无法回收的容器 / 卷孤儿 |
| 只有分段持锁，没有完整删除标记 | 清理与映射摘除之间仍能复现空卷重建 |
| 只有卷检查，没有检查到启动的共同锁 | 检查通过后仍可被删除插入，随后重建空卷 |
| owner 锁与卷检查组合 | 恢复与资源清理串行，缺卷时失败关闭 |

ADR-0054 进一步覆盖清理后的异步 DB / event / file 删除窗口及 launch 提交；当前实现同时保留这层持久标记和 session lock。

## 边界与代价

- 锁是进程内机制；同一 sessions root 只支持一个服务进程。多进程协调边界见 ADR-0054。
- 外部移除具名卷后，恢复会明确失败并要求修复持久 workspace；系统不会用空卷代替丢失数据。
- 不新增依赖，不改变 Sandbox 路径边界或硬删除的工件所有权规则。
