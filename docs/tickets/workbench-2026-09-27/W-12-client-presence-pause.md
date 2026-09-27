# W-12 · 按 Task 的客户端在场、安全暂停与断线宽限
**目标仓库**：intelligence-agent-backend（在场 API / RunManager）+ intelligence-agent-frontend（Web / Desktop / TUI 客户端登记）；先固定服务端 Contract，再接客户端。

**类型/优先级**：P0 并发/副作用。**依赖**：W-11、W-22 已批准的 `client_absent` 事件契约、[#341](https://github.com/EricKingWhy/intelligence-agent/issues/341)、[#342](https://github.com/EricKingWhy/intelligence-agent/issues/342)。**范围**：服务端在场登记与现有 `session/runmanager.py` 的最小编排接缝、Desktop/TUI/Web 连接协议；不重写 #305 的通用 pause 状态机或 #341 的 relay cleanup。

## 状态表

| 场景 | 期望 |
| --- | --- |
| 只开 TUI，明确退出 | 该 TUI 托管的 Task 在安全边界暂停；不再发新 model call。 |
| 桌面+TUI 都托管 Task A，TUI 退出 | A 继续；桌面关窗到托盘也仍算在场。 |
| TUI 查看 B，桌面托管 A，桌面显式退出 | A 暂停；B 不因此暂停。 |
| 本机 Web 短时断线 | 30 秒重连宽限内不发新 model step；重连后不自动恢复已暂停任务。 |
| 最后客户端断线超时 | 安全暂停，保留进度与 Ledger；若操作仍未知，Ledger 记录 `NEED_RECONCILE` 且 Run 投影为 `needs_reconcile`；不写 `run/failed(reason=orphaned)`。 |

## 工作指令

服务端以 `client_id + task_session_id + presence_kind + last_seen` 登记；明确离开与意外丢连接区分。某 Task 的最后客户端离开时阻止该 Task 新模型步骤，等在途 Tool 按 Operation Ledger 可确认地收口；结局未知时 Operation 进入 `NEED_RECONCILE`、Run 投影为 `needs_reconcile`；副作用确认结清后才使用 W-22 的 `run/paused(reason=client_absent)`，不能继续走旧 `orphaned` 失败路径。暂停前更新 W-05 进度；写入失败仍不可盲继续。桌面“退出应用”撤销该桌面的在场登记；只要另一个客户端仍托管同一 Task，该 Task 与共享服务就继续运行。共享 Python 服务只可在没有连接客户端或需服务的 Task 后随最后客户端退出；不得另设可绕过在场检查的强制停止动作。意外断线宽限配置可调，首版固定为 30 秒。

**验收**：真实两个进程+Web 连接组合逐表实测，统计断线宽限期新增 model calls = 0；重启后暂停 Event/Run ID 不伪造失败；Tool 执行中退出的 Ledger 无悬空/盲重放；relay 资源按 #341 修复结果清理。focused RunManager/API tests + kill/reconnect 子进程验证。**不做**：预算、stuck、CAS、relay cleanup 的二次实现。

**成熟参考/复用**：[DeepSeek Desktop 关窗/退出](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)区分隐藏、活动任务检查和退出；其 [`quit-confirmation.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/src/quit-confirmation.ts) MIT 可 `ADAPT` 退出检查 UI，不复用 DSH Host。服务端暂停 `REUSE` 本仓 #305/RunManager。
