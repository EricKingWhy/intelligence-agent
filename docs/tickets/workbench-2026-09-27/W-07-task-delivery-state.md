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

## 方案依据（§6.1/§1.3 五字段，2026-10-04 核实）

- **来源（≥2 独立）**：
  1. OpenAI Codex app 介绍页（票面自带，2026-10-04 WebFetch HTTP 200）：执行与人审阅分离——"you can review the agent's changes in the thread, comment on the diff, and even open it in your editor"；Automations "the results land in a review queue so you can jump back in and continue working"；"Each agent works on an isolated copy of your code"——先执行、人审阅、再采纳。
  2. Anthropic 工程博客 Effective harnesses for long-running agents（票面自带，2026-10-04 WebFetch HTTP 200）：功能清单判完成——"we prompted the initializer agent to write a comprehensive file of feature requirements"、"These features were all initially marked as 'failing'"、"Only mark features as 'passing' after careful testing"，且禁删改测试。诚实记录：该页只有通过/失败二态、未提人审分离；三态交付语义与四态产品显示来自票面本位。
- **机制摘要**：Codex = 执行（隔离副本）→ 产物进 review queue → 人审 diff → 采纳，完成不由执行者自宣；Anthropic = 预写可执行验收清单作客观完成判据，初始全「未通过」，测试通过才标「通过」，清单不可被执行方改写。
- **契合点**：run/completed 只说明 Runtime 收口 ↔ 执行者不自宣完成；验收项验证值六态 ↔ feature list 逐项状态；用户接受（可带原因接受缺项）↔ review queue 人审采纳；三轴事实分轴追加 ↔ 执行/验证/接受事实互不覆盖、各自留痕。
- **判定**：交付状态机**语义** = PORT DESIGN（只借「执行者不自宣完成 + 客观验收清单判据 + 人审采纳」三原则；不借 Codex review queue 产品形态、不借 Anthropic JSON 文件清单载体）。实现 = REUSE 本仓 SessionEvent append-only 契约（spec 03，事件常量唯一事实源 + 词汇表/前端类型生成物再生成 + 守卫测试）、spec 11 §6.1 CAS/422/409 状态码口径、#305 §6 run 状态集合与完成闸门零写入契约（runtime.py:1850-1854）、#342 并发锁与 domain_errors 409 单一映射惯例、fork 既有谱系机制；BUILD 仅限三轴事实的事件类型与校验（session/task.py）、task 状态纯投影、最小 REST（GET task / POST verification / POST acceptance(+release)）与创建路径 task/defined 接入。
- **License**：两来源为公开网页参考（无代码复制）；本票零新依赖。
