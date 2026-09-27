# W-06 · 进度文件重读、外部编辑冲突和 Fork 分家
**目标仓库**：intelligence-agent-backend（SessionEvent / Fork / 恢复）。

**类型/优先级**：P0 Recovery。**依赖**：W-05。**范围**：ProgressFile reader/compare、任务启动/恢复/压缩后/交付前注入点、既有 `session/fork.py`，及 API 状态。不得在不合法的父事件位置另造 Fork 语义。

## 工作指令

1. 新任务初始化后、服务重启、压缩之后、交付前，从磁盘**重新读取** `progress.md`；校验 schema、session_id、source_event_seq、生成 hash 与 SessionEvent 事实。仅允许引用可读回的 Artifact；读不到则显示“进度文件不可核对”，不能凭模型记忆宣布完成。
2. 用户从产品 UI 修改目标/约束/验收项时，先追加用户确认事件，再从服务端投影重写文件。检测外部编辑时展示字段级差异和原事件引用；绝不把文件新增的“允许删除数据”等文字自动升级为用户授权。用户选择丢弃手改或将其作为**新用户指令**确认后才更新真相。
3. Fork 仅在既有 `session/fork.py` 的合法稳定边界执行；新 Session 写独立 `agent-progress/<child-id>/progress.md`，带 parent ID/fork seq。父子随后更新互不影响；Fork 失败不能留下误导性半成品 child 文件。

**验收**：改掉进度文件内一项禁令、错 session ID、删文件、旧 source seq、伪造新授权、父子并行更新分别有确定状态；重启和压缩后 Agent 模型可见输入含经核对的原目标/下一步；外部编辑未确认前 Runtime 权限不变。focused session/fork/context tests + 真实磁盘改动演示。**不做**：任意事件位置 Fork、文件变第二套 EventStore。

**成熟参考/复用**：[Anthropic 长任务接班](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)要求新窗口先读进度；[Pi session tree/compaction](https://pi.dev/docs/latest/compaction)可参考父子上下文隔离。`PORT DESIGN` 到现有 Fork/Event，不复制 Pi SessionManager。
