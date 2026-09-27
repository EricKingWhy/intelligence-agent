# W-22 · 扩展 client_absent 持久暂停/恢复契约
**目标仓库**：intelligence-agent-backend（事件契约 / RunManager / Resume API）。

**类型/优先级**：P0 Runtime Contract。**依赖**：[#305](https://github.com/EricKingWhy/intelligence-agent/issues/305) 已落地的暂停/恢复基础与 [#342](https://github.com/EricKingWhy/intelligence-agent/issues/342) 并发 resume CAS；是 W-12 的明确前置。**用户批准的规格变更**：Spec 02 §5.2.1、03 §3.4/§5、11 §6.2 与 ADR-0046。只新增产品客户端离开原因，不改 #305 的三类既有暂停验收。

## 精确事件语义

只对**已纳入产品 Task 客户端在场协议**的 Run，最后客户端明确退出或断线宽限到期后阻止新的 Model/Tool/Child 接纳。在途 Tool 按现有 Ledger 收口：存在 UNKNOWN 时进入 NEED_RECONCILE，不能写暗示可安全继续的 paused；全部确定后追加恰好一条 `run/paused(reason=client_absent, trigger_dimension=client_presence, closeout_source=deterministic)`，保留同一 `run_id`、预算版本/消耗、已完成/未完成 continuation。暂停时不能再发一个“总结用”模型请求。客户端重连只展示状态；用户明确选择继续并带 `expected_version` 和 `resume_basis=client_return`、确有 Task 客户端在场且 reconcile 完成后，才追加 `run/resumed`。并发两次续跑至多一次成功。

## 工作指令

先写 Event schema/生成词表/投影测试，随后在现有 Runtime/RunManager 的单一路径加 admission gate 与稳定边界暂停，不另造任务引擎。原有 `budget_exhausted/deadline/stuck` 和旧入口 `run/failed(reason=orphaned)` 均维持；显式 `/cancel` 仍是取消。特别测试“已经 paused 时又收到离开事件”不能双写、“最后客户端退出时 Tool 正在提交”不能盲重试、“服务在暂停事件落盘前 crash”须按既有 interrupted/reconcile 恢复。

**验收**：单独真实子进程与 FakeModel admission 计数：离开后新请求数 0；事件 replay/重启投影一致；同 run_id/consumed 不重置；UNKNOWN 先 NEED_RECONCILE；两个续跑请求一成一 409；旧 Web 无产品 presence 注册时仍触发既有 orphaned 语义。focused session/runmanager/API tests + 一次真实 Tool 退出探针。**不做**：客户端心跳/托盘 UI（W-12/W-15）、#305 预算/stuck 算法或 #341 relay cleanup。

**成熟参考/复用**：[DeepSeek Harness Desktop 关窗/退出/活动任务检查](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)证明产品需区分隐藏与明确退出；具体 `client_absent` Event 是本项目的 `BUILD` 契约，复用现有 `run/paused`、Operation Ledger 和 CAS。可参考其 MIT [`quit-confirmation.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/src/quit-confirmation.ts) 的 UI 设计，但不要把 Node Host 引入 Core。
