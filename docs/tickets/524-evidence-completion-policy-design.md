# #524 设计定稿：EvidenceCompletionPolicy（完成门证据验证 + 纠偏臂，选项 A 落地）

> 状态：设计定稿（2026-10-05）。依据 = 票面 2026-10-05 用户裁决（选项 A）+ 方案依据块
> （issuecomment-5983721507，Pi / Claude Code / Aider / Codex / TTSR 五源核实）。
> 实现基线：分支 `zcode/T520-imp01-cache-usage`（设计定稿时 tip `d34862ae`，实现提交
> `8720bae8` 落在其后的 `29589c1b` 设计稿提交之上——两轴审查 P4 已核对为唯一漂移）。

## 1. 机制定位（裁决逐条对照）

「事中纠偏」在本设计里的准确含义 = **完成时刻纠偏**（Claude Code Stop hook / Pi
`before_run_end` 同构）：模型给出最终答复、六谓词 quiescence 通过之后，证据策略对
「这次完成主张有没有 durable 证据支撑」做判定；证据不足 ⇒ 拒绝完成并把缺什么证据
作为纠正消息注入，循环继续让模型补证据。不是工具批中途的干预缝（那是 IMP-02/#447
的辖区，本票不碰）。

| 裁决条款 | 落地 |
| --- | --- |
| 可选 domain policy，默认关闭、显式装配开启 | `EvidenceCompletionPolicy` 仅在装配方显式构造并传入时生效；默认装配仍是 `DefaultCompletionPolicy`，行为逐字节不变 |
| 挂现有 CompletionPolicy seam，不进 Core quiescence | 六谓词与 `collect_quiescence_report` 零改动；策略在既有调用点（quiescence 通过后）被调用 |
| "passed" 文本不是完成判据 | 证据判定只读 durable 事实（`tool/result` 事件的成功结果）；`final_text` 只用于声明式主张匹配，其内容永远不构成「已完成」的证明 |
| 复用 stuck 阈值 / 一次 replan / 预算，不造第二台 stuck 机器 | 纠偏循环无新计数器、无新闩、无新暂停路径：边界 = 既有预算闸门（循环顶部照判）+ 既有 stuck 检测器逐轮 `advance`（重复形态达阈值走既有 replan-once / paused 分级） |
| 失败方案允许环境变化后有据重试（非永久禁止） | 拒绝不是黑名单：模型补上证据（新的 durable 事实）即通过；同 run 内纠偏循环与跨 run 重入都开放 |
| 新反馈不得重跑已执行 mutating tool（以 Operation Ledger 判定） | 证据判定**接受会话内已发生的 durable 结果为证据**（含 mutating 工具的成功结果）——纠偏在机制上永不要求重跑副作用；纠正消息显式携带「引用既有结果、勿重复执行」指令。Ledger/事件流是判定的唯一事实源 |
| 不与 W-07/W-08 服务端证据重复 | 本策略的证据 = 运行时 `tool/result` 事实、判定面在完成门；不写 task 交付事件、不设服务端验证端点、不读 W-07 三轴状态 |

## 2. seam 扩展（Core 最小接触面，`agent/completion.py` + `agent/runtime.py`）

1. **`decide` 签名增加可选 keyword**：`events: Sequence[SessionEvent] | None = None`
   （runtime 传 `arms.session.events`）。理由：证据判定必须读 durable 事实，而现有
   签名只有 `(report, final_text, run_id)`——策略拿不到事件流就只能是主观判定（恰是
   票面禁止的）。keyword 带默认值：`DefaultCompletionPolicy` 忽略它；keyword-only
   保证既有按位调用不破。ADR-0047 增补节记录此契约扩展。
2. **`correction_feedback(decision) -> str | None` 非抽象默认方法**：默认返回
   `None`（= 既有 blocked 臂不变）；纠偏型策略覆写它，仅在自己的拒绝决策上返回纠正
   消息文本。runtime 只通过 ABC 调用，不 import 具体策略类（可选能力不与 Core 耦合）。
3. **runtime 完成闸门臂插入点**：policy 拒绝后、**既有 stuck 判定之后**、
   `_terminal_quiescence_blocked` 之前——仅当「无 stuck 信号且
   `policy.correction_feedback(decision) is not None`」走新纠偏臂；其余路径
   （quiescence 拒绝、stuck 命中、默认策略拒绝）逐字不变。ADR-0047 D3 的
   **blocked 臂零写入契约保持不变**：纠偏臂是独立的新臂，不碰 blocked 臂。

纠偏臂动作（复用 `_stuck_replan_arm` 的双事件先例，:2624-2681）：

- 结构化事件 `completion/evidence-blocked`：data = `{policy, reason}`（实现定稿：
  `rule_id` 不单列键——runtime 侧只有稳定 reason 串可传，`evidence_missing:<rule_id>`
  已把它载于 reason；无主张原文、无参数值——ADR-0047 D3 纪律：诊断面不是泄漏通道）；
- 纠正 `USER_MESSAGE`：`{content, injected_by: "completion_evidence_policy"}`——
  非真实用户发言的既有标记纪律自动生效（memory/extractor.py 按非空
  `injected_by` 单点过滤；derive.py 同源判定），消息文本来自 prompt 片段注册表
  新增 fragment `corrective:completion_evidence`（含 rule_id / 证据要求占位）；
- 返回事件按序镜像给流消费者（与 stuck replan 同契约），然后 `continue` 回循环
  顶部——预算准入照常先判，暂停边界上不会多出一次模型调用。

## 3. EvidenceCompletionPolicy（`agent/completion_evidence.py`）

```python
@dataclass(frozen=True)
class EvidenceRule:
    rule_id: str            # 稳定 id（进拒绝理由，可断言）
    claim_pattern: str      # 主张匹配：对 final_text 的正则（声明式，不用 LLM 猜）
    required_tool_name: str # 证据要求：该工具在本会话有 success 的 tool/result
```

- 规则表由装配方显式构造传入；**零规则 = 行为等同 DefaultCompletionPolicy**
  （装配了策略但没有规则 ⇒ 零上下文税、零行为差）。无内置规则——把 coding 规则
  硬塞 Core 正是裁决禁止的形态。
- `decide(report, final_text, run_id, events=None)`：
  1. 非静止报告 → 拒绝（`report.refusal_reason()`，与 Default 同款防御：第三方
     绕过 runtime 直调不得顺带绕过六谓词）；
  2. 对每条规则：`re.search(claim_pattern, final_text)` 命中 且 events 中**不存在**
     该工具的成功执行配对（实现定稿，随两轴审查 P4 措辞修正：`tool/result` 的
     data 只有 `{tool_call_id, content}`，无 tool_name 键——配对以 `tool/call`
     的 `tool_name` + 同 `call_id` 的 `tool/result` content JSON `ok=true` 为准，
     与生产 executor 落盘形状一致）→ 拒绝，`reason = "evidence_missing:<rule_id>"`
     （自有稳定前缀；`POLICY_REJECTED_PREFIX` 仍只是"拒绝但没给理由"的缺省兜底）；
  3. 全部满足 / 无命中 → `accepted=True`。
- 证据 scope = **会话**（与 quiescence 谓词同 scope，ADR-0047 D1 推导一致）；
  新鲜度窗口（如「最近一条真实用户消息之后」）是 V2 精化，登记不做的理由：scope
  先例一致性与「证据 = durable 事实」的诚实口径优先。
- 构造校验响亮失败：非法正则 / 空 `rule_id` / 空 `required_tool_name` →
  `ConfigError`（与 catalog 校验同纪律）。
- `correction_feedback(decision)`：仅对本策略产生的拒绝返回纠正消息（文本含
  rule_id、要求的证据描述、「引用既有 durable 结果、勿重复执行已完成的 mutating
  调用」指令）；accepted 或非本策略决策 → `None`。

## 4. 事件词汇与生成物链

新类型 `COMPLETION_EVIDENCE_BLOCKED = "completion/evidence-blocked"`（语义：完成门
被证据策略拒绝、纠偏消息已注入——此前没有任何事件能表达它，按 #317 T9「新语义落
新结构化事件」的先例走词汇链）：`session/event.py` 常量 + EVENT_TYPES 登记 →
`scripts/gen_event_vocabulary.py` / `gen_event_types.py` 再生成两份生成物 →
web `projection.ts` 显式 no-op 登记（Record 穷尽性，tsc 强制不漏）。

## 5. 装配开关

`build_runtime`（assembly.py）与 `AgentFactory`（factory.py）各增一个透传参数
`completion_policy: CompletionPolicy | None = None`（镜像 `model_call_gate` 的
既有透传先例）；`None` = `AgentRuntime` 内部默认 `DefaultCompletionPolicy`，全链
行为零变化。V1 无 settings/HTTP 配置面（编程装配即「显式装配开启」）；规则表的
配置化序列化登记为后续票。

## 6. ADR 归宿

**ADR-0047 追加 dated 增补节**（不新开 ADR）：记录 decide 签名扩展、
correction_feedback 默认方法、纠偏臂插入点（stuck 判定后 / blocked 臂前）、
有界性论证与 blocked 臂零写入不变。ADR-0047 是完成闸门的机制权威，扩展就近增补
（ADR-0050 D1–D4 增补先例）。

## 7. V1 显式非目标（登记）

1. **Executor 级「同参数 mutating 重跑」硬闸**：需要参数规范化语义（空白/键序/
   幂等重试的误杀面），是独立的判定面设计——另票。本票的防重跑 = 证据判定接受
   durable 结果（机制上无需重跑）+ 纠正消息显式指令；模型仍自主发起的重复调用由
   既有 Permission/Approval 闸门治理（不变量 #11：Runtime 边界不靠 prompt）。
2. 证据新鲜度窗口（见 §3 scope 说明）。
3. 规则表的 settings/HTTP 配置化。
4. 服务端证据轴（W-07/W-08 辖区）。
5. **claim_pattern 回溯灾难（ReDoS）防护**（两轴审查 P3 登记）：pattern 是受信的
   装配期配置，但匹配输入 `final_text` 是模型可控文本，劣质模式（如 `(a+)+$`）
   理论上可挂起事件循环。V1 接受该风险（模式来自编程装配、非用户/模型输入），
   长度上限 / 安全正则引擎 / 匹配超时是后续票的判定面；登记为本策略的已知边界。

## 8. 测试面（红先行）

- **红**：装配 `EvidenceCompletionPolicy`（rule：主张 /全部完成/ 要求 `bash`
  success）+ 模型直接给「全部完成」最终答复（无 bash result）→ 期望
  `completion/evidence-blocked` + 纠正 USER_MESSAGE（injected_by=...）+ 第二次
  模型调用发生；实现前该场景 = run 直接 blocked 结束、零注入（红可复现）。
- **绿**：证据在场 → accepted → `run/completed`；零规则/主张不命中 → 等同
  default；模型补证据（第二轮带 bash result）→ 完成；stuck 信号在场时 stuck 臂
  照旧优先（纠偏臂不越过护栏）；`DefaultCompletionPolicy` 拒绝路径回归钉
  （blocked 臂零写入、零注入，逐字不变）。
- **policy 单测**：规则构造校验（ConfigError 三态）/ 理由稳定串 /
  会话范围证据扫描（success 才算、失败结果不算）/ `correction_feedback`
  accepted 时 None / `decide` 的 events 缺省（None）= 无证据路径（防御诚实：
  拿不到事实就不放行，不猜）。
- **词汇链守卫**：生成物再生成后 diff 一致 + projection no-op 登记（tsc）。
