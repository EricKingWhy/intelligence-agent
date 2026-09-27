# W-10 · 单目录写入租约与持久队列
**目标仓库**：intelligence-agent-backend（WorkspaceRegistry / 持久存储）。

**类型/优先级**：P0 并发/恢复。**依赖**：W-07。**范围**：Session/WorkspaceRegistry、持久 metadata/queue、任务创建/释放 API 与测试；不得变成 #318 SessionBudget 锁，也不实现自动 worktree。

## 精确行为

创建有写入意图 Task 时，按 Windows 规范化绝对目录键尝试排他租约。已有持有者时：第二任务保持持久 FIFO 等待，或由用户选独立目录；不得启动模型写任务。只读 Task 不占租约；读任务想写必须先升级取得租约。持有者 `run/completed`、暂停、断线、待审批、待证据审阅时仍占有；仅“用户接受/归档/显式释放”可释放。释放时原子选出队首；队首 Task 有在场客户端才开始，否则留待用户回来。排队取消可撤销其队列项，不影响当前持有者。

## 工作指令

使用现有 metadata/WorkspaceRegistry 契约和持久存储，不凭前端/进程内布尔值。规范化处理盘符大小写、尾分隔符、符号链接/junction、UNC 与父子目录相交；若不能安全证明两个路径独立，拒绝并要求用户选目录，不能并发写。实例重启扫描租约/队列并与 Session 状态对账；重复释放/两个同时创建采用事务或 CAS 保证唯一写者。

**验收**：同目录两个 Task 并发创建仅一个获租约；不同目录可并发；持有者暂停、`run/completed`、等待审阅不放锁；释放后无客户端不启动、有客户端只启动队首；取消队中项与 crash/restart 后顺序不变；junction/UNC/大小写不能绕过。执行真实 Windows 文件系统与 SQLite 重启测试，断言同目录同时写的计数始终 ≤1。**不做**：自动 worktree、分布式锁、按 Tool 调用的资源冲突替代。

**成熟参考/复用**：[GitHub Copilot app sessions](https://docs.github.com/en/copilot/how-tos/github-copilot-app/agent-sessions)使用任务级独立工作区；本项目首版队列是对隔离成本的推论，`BUILD` 最小租约层并 `REUSE` 现有 Session/WorkspaceRegistry。未来 worktree 阶段再参考 [Git 原生 worktree](https://git-scm.com/docs/git-worktree)。
