# 0046 · 最后一个任务客户端离开时安全暂停

**Status: accepted（2026-09-27 用户确认）**。#305/ADR-0044 将 `run/paused` 限定为预算、deadline、stuck，并让零订阅者孤儿回收写 `run/failed(reason=orphaned)`；个人长任务工作台需要让用户明确退出 TUI/桌面后停止 token 消耗，同时保留同一 Run 的可恢复事实。决定只为受产品客户端在场协议管理的 Task 增加 `run/paused(reason=client_absent)` 与用户显式返回的 `resume_basis=client_return`：先停止新模型/工具/子任务接纳，已有副作用按 Ledger 结清，未知状态优先 NEED_RECONCILE，continuation 确定性生成且不额外花 token。旧入口的 orphaned 和 #305 三类暂停验收保持原样；规格落点是 Spec 02 §5.2.1、03 §3.4/§5、11 §6.2，实现前置票另立。这样避免把正常的客户端退出写成任务失败，也避免靠进程内挂起或伪装成 deadline 来保存状态。
