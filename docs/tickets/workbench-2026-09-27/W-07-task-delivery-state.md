# W-07 · Task、Run、验证、接受四种事实分开
**目标仓库**：intelligence-agent-backend（SessionEvent / REST 投影）。

**类型/优先级**：P0 产品契约。**依赖**：W-02；并发续跑正确性消费 [#342](https://github.com/EricKingWhy/intelligence-agent/issues/342)。**范围**：SessionEvent schema/服务端任务投影与最小 REST 查询/命令；候选 `session/event.py`、`session/service.py`、`web/app.py`。不修改 [#305](https://github.com/EricKingWhy/intelligence-agent/issues/305) 的 `run/completed` / CompletionPolicy。

## 状态契约

Task 身份 = Session ID；Run 是一次执行。同一 Task 可多 Run。产品显示 `执行中/待验证/可交付/已接受`；验收项验证值为 `未开始/进行中/通过/失败/受阻/未完成`；用户接受为 `未接受/已接受/带缺项接受`。三轴事实分别追加和投影。`run/completed` 只能说明 Runtime 收口，不能自动写“通过”或“用户接受”。用户可带原因接受缺证据或失败结果，但相应验证值保持原样。

## 工作指令

1. 为新 Task 保存原始目标、工作目录/读写意图、用户验收项；缺项时 Agent 可提出有可执行判据的验收清单。关键目标模糊/高风险先由用户确认；普通低风险可推进并标记清单未确认。变更 AC 只追加事件，保留旧版来源。
2. 定义合法状态跃迁、版本/seq 并发冲突和 replay/fork 行为。接受/释放操作必须有 `expected_version` 或同等 CAS；重复请求幂等或明确 409，不能双写。
3. API 返回任务状态以及每项验证/接受依据，不返回凭证值。刷新后由 Event 重建同一结果，TUI/Web 不各算一套。

**验收**：模拟 `run/completed` 但无测试、测试失败、证据齐全但未接受、带原因接受失败、Fork 独立接受、两客户端同时接受；逐项断言展示及 durable Event，重启相同。focused event/API tests + 真实现有 run 接入。**不做**：复刻 V3.1-lite Ticket gate、修改 #305 完成语义、UI 组件。

**成熟参考/复用**：[Codex app](https://openai.com/index/introducing-the-codex-app/)将执行与人审阅分成不同步骤；[Anthropic 长任务实验](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)用功能清单判断完成。`PORT DESIGN`；本仓 SessionEvent `REUSE`。
