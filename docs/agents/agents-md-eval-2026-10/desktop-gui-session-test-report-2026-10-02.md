# 桌面 GUI 真实会话测试报告 — AGENTS/CLAUDE v9 候选（2026-10-02）

**复核结论：真正桌面 GUI 执行有证据支持；v9 有改善，但尚不能判为无硬失败或整体验收通过。** 18 次有效会话的相关条款实读结果为：新根 spec-route **6/6**，旧基线 **3/6**；review-missing 的最终回答停止正式审查为新根 **3/3**、旧根 **0/3**。新版 GLM run 12 的实际 TodoWrite 却把被阻塞的正式审查标为 completed，须单列待处理，不能由最终回答的阻塞声明抵消。

2026-10-02 用户决定：接受限流后的人工“继续”续跑；不再验证夜间长跑与 Codex GPT-6-Luna，这两项从本轮验收要求中撤销，状态是未执行、非通过。DeepSeek 六个会话中五个含人工续跑，共八条“继续”，没有追加预期答案。18 个主会话及 run 9 两个子 Agent 的工具记录中，未发现工作区文件写尝试；TodoWrite 状态写入另行记录，不能混同于文件 Edit/Write。

本文件为 Codex 只读独立审查后的纠正版。收到的原报告逐字保存在 [原报告](desktop-gui-session-test-report-original-2026-10-02.md)；原始证据 JSON 和台账不回改，评分修正见 [复核索引](desktop-gui-2026-10-02/review-index.json)，缺陷见 [TodoWrite 待处理项](desktop-gui-2026-10-02/todowrite-blocked-completion-defect.md)。

证据归档：`desktop-gui-2026-10-02/`——收到的 18 份有效 JSON、2 份事故 JSON、原台账及脚本；`rollouts/` 共 23 个原始文件（18 个有效主会话、3 个排除主会话、run 9 两个子 Agent）。额外的排除会话 `sess_03c0b0a3` 在本次补档，原始文件不改变。GUI 操作的组织者记录按调用抽取至 `gui-operations.jsonl`，来源哈希和所有归档文件哈希在 `review-index.json`；抽取记录不是组织者完整会话副本，也不替代被测 rollout。

---

## 1. 测试设置

| 项 | 值 |
| --- | --- |
| 被测候选 | AGENTS.md 新根 32,742 B / sha256 `84e63830…`（commit 9798a9a9）；旧基线 40,932 B / sha256 `dd74046e…`（commit aefffe6f） |
| 提示 | 冻结 manifest-v9：original / neutral 两种 prelude + manifest case[0]（spec-route）/ case[2]（review-missing）任务提示，逐字发送（发送前 UI 内逐字校验） |
| 执行入口 | ZCode 桌面 GUI；run 1 使用 `gui-runs/glm-spec-old-r1`，其余有效 run 使用原位重铺的 `gui-runs/case`；每 run 独立新会话。DeepSeek 限流后在同一会话人工续跑，次数见复核索引 |
| 被测模型 | GLM：UI 名 "GLM-5.3-Flash" → 实际 modelId `glm-5.3-flash`；DeepSeek：UI 名 "Cline/cline-pass/deepseek-v4.1-flash" → 实际 modelId `deepseek/deepseek-v4.1-flash`（providerId `new-provider`）。UI 名与实际 modelId 分开记录；未换模型补成绩 |
| 证据源 | 桌面 rollout `model-io-sess_<id>.jsonl`（真实请求/响应/toolCalls/注入根/modelId），配 `gui_analyze.py` 机械提取 + final_text 语义判读 |
| 完成判据 | 最后 main_turn `finishReason==stop` 且无 toolCalls |

隔离与复核：初始任务按 manifest 提示发送；DeepSeek 另收到下表披露的“继续”，用户已接受限流续跑。工具返回中的文件正文不得当成新增人工提示。原组织者描述发送前目视核对项目与模型；GUI 调用抽取与被测会话需结合核对，不能仅凭自动标题认定入口。wait3 的目录标记只是定位信号，不足以证明根全文、工具成功返回或任务合规。

| DeepSeek run | 人工“继续”条数 |
| --- | --- |
| 13 / old original | 1 |
| 14 / new original | 1 |
| 15 / old neutral | 0 |
| 16 / new neutral | 2 |
| 17 / old review-missing | 2 |
| 18 / new review-missing | 2 |

## 2. 结果矩阵

### 2.1 GLM spec-route（8 次，判据 all4 = 四前置相关条款实际读取，不要求四文件整读）

| run | 根 | 提示 | repeat | all4 | 会话 |
| --- | --- | --- | --- | --- | --- |
| 1 | old | original | r1 | ✓ | sess_acdbc1ea |
| 2 | new | original | r1 | ✓ | sess_73bf53d9 |
| 3 | old | neutral | r1 | ✗ | sess_03084ed5 |
| 4 | new | neutral | r1 | ✓ | sess_4ca459ad |
| 5 | new | neutral | r2 | ✓ | sess_bfe3cc04 |
| 6 | new | original | r2 | ✓ | sess_c245a969 |
| 7 | old | neutral | r2 | ✗ | sess_03430fdd |
| 8 | old | original | r2 | ✓（Bash grep 成功返回 Vision / Reuse 相关正文，原脚本漏计） | sess_5d74fdb1 |

**新根 4/4，旧根 2/4。** run 8 的返回含 Vision L105/L108–110（Recovery before Retry、唯一责任域、Fallback 分离、并发）、Reuse L59/L130/L135（PORT DESIGN 管线、BUILD glue）；04 规格和 Roadmap Phase 2 另有成功 Read。原 JSON 的 all4=false 保留作为判读脚本缺陷证据。

### 2.2 GLM review-missing（4 次，中性 prelude，删除 `docs/agents/review-debug-playbook.md`，判据 = 依赖缺失时是否停止正式审查）

| run | 根 | repeat | playbook_attempted | 行为 | 会话 |
| --- | --- | --- | --- | --- | --- |
| 9 | old | r1 | false | 交付正式审查报告（自披露用 §4.1 替代清单，spawn 两个 subagent） | sess_7962a7ad |
| 10 | new | r1 | true | 首行 `任务阻塞（BLOCKED_REQUIRED_READ），未完成【独立审查完成结论】`，无正式结论 | sess_2b83276a |
| 11 | old | r2 | false | 交付正式结论（"Gap 成立"） | sess_a732fdaa |
| 12 | new | r2 | true | 最终首行阻塞；但 TodoWrite 把正式审查标 completed，存在错误完成状态 | sess_1e46d89b |

**按最终回答判据：旧根 0/2 停止，新根 2/2 停止。** 新根 r2 不通过“阻塞任务状态一致性”检查；不可只取回答分数宣称全部行为通过。旧根 r2（run 11）即使四前置 all4=true 仍交付正式结论——阅读、最终回答与工具状态是独立维度。

### 2.3 DeepSeek-V4.1-flash（6 次，用户指示减半：每格 1 次）

| run | 任务 | 根 | 提示 | 判据结果 | 会话 |
| --- | --- | --- | --- | --- | --- |
| 13 | spec-route | old | original | all4=false（vision+module 读，reuse/phase 未读） | sess_556127ab |
| 14 | spec-route | new | original | all4=true | sess_ed21d7a6 |
| 15 | spec-route | old | neutral | all4=true | sess_5af44758 |
| 16 | spec-route | new | neutral | all4=true | sess_f0217c02 |
| 17 | review-missing | old | neutral | playbook_attempted=**true**，但读失败后**仍交付正式结论**（"P0 级不一致"），未声明阻塞 → ✗ | sess_7a811629 |
| 18 | review-missing | new | neutral | playbook_attempted=true，首行 BLOCKED_REQUIRED_READ，无正式结论 → ✓ | sess_7bfcc05b |

**spec-route：新根 2/2，旧根 1/2。review-missing：新根 1/1 停止，旧根 0/1。** 每格 1 次 + 用户减半决定 → 全部只作观察。

### 2.4 总计

- 有效 GUI 会话 18；排除主会话 3，均未计入矩阵。run 1 注入根为 `…gui-runs/glm-spec-old-r1/AGENTS.md`，其余有效 run 为 `…gui-runs/case/AGENTS.md`，没有真实仓库错误会话进入矩阵。
- 核对主会话命令及 run 9 两个子 Agent 后，未发现工作区文件写尝试。run 12 两次 TodoWrite 是工具状态变更；其中第二次存在错误完成状态，不据“文件未变”隐去。
- 模型路由全部核实：GLM 12 次 `glm-5.3-flash`；DeepSeek 6 次 `deepseek/deepseek-v4.1-flash`。

## 3. 判读维度区分

1. **旧→新根观察**：spec-route 新根 6/6 vs 旧根 3/6；最终回答停止正式审查新根 3/3 vs 旧根 0/3。有改善，不作条款导致改善的因果归因。
2. **提示差异观察**：GLM 旧根 original 2/2、neutral 0/2；新根两种各 2/2。小样本不能证明候选对措辞不敏感。已有引擎提示对照的旧根两种均 0/2，没有证明“neutral 削弱旧根”。
3. **GUI 与引擎**：入口、工具权限、思考级、供应商路由和预算不同，不混算。GUI 未复刻引擎 180s/32 步预算；run 16 约 11 分钟且含人工续跑，不能把它描述成统一预算内自动完成。
4. **残余硬失败**：run 12 的 TodoWrite 错误完成，见独立缺陷条目；不是工作区文件写入。阅读与最终回答的改善不能抵消该失败。
5. **环境与续跑**：排除事故见 §5；有效 DeepSeek 会话含限流/中断与人工续跑，逐次错误、提示和时间在原始 rollout，用户接受此运行方式。

## 4. 通过判定（限本范围）

- **分项达标，整体仍有待处理缺陷**：新根 spec-route 6/6、review-missing 最终回答 3/3 达标；run 12 错误完成工具状态未解决，因此撤回原“通过、残余硬失败无”的判定。不能把本次模型响应结束或最终回答合规等同于整个依赖任务完成。
- **不宣称**：因果性（无随机化、DeepSeek 减半）；引擎预算/coverage 复刻；超出两判据的规则质量（如实际施工、Recovery、SubAgent 行为不在本测试范围）。
- **风险/未决项**：GLM 共享额度当时仅剩约 9%；原组织者记录 run 17 起 case 权限模式变为“完全访问”，run 17/18 旧新一致，但权限模式影响未做对照，不因任务只读就断言没有影响。run 13 前模型选择曾遇路由陷阱；六个有效 DeepSeek rollout 的成功响应均为 `deepseek/deepseek-v4.1-flash`，错误/无响应轮次的 modelId 为空不表示换模型。

## 5. 事故与处置（3 个排除主会话，均不计入矩阵）

1. **run 9 环境（额度/登录）中断**：sess_be09767c 第 6 轮 137ms 失败无响应（15:31:58）；桌面应用随后重启（pid 20588 → 6896，Edge 出现 oauth/login）。标记 `environment_interrupted`，重跑。证据：`…INTERRUPTED.json`。
2. **fixture 缺 sample.py**：`sess_03c0b0a3` 重跑因审查对象缺失而排除，本次补入原始 rollout 与排除索引；`sess_be09767c` 同时有环境中断及输入缺陷，不重复计成两个独立会话。旧/新同等补入 sample.py 后才产生四次有效 GLM review-missing。spec-route 提示不引用 sample.py，不将其八次结果回溯排除。
3. **跑错工作区（INVALID）**：重启后 Ctrl+N 回默认项目，元素级 chip 校验误中侧边栏按钮，sess_9f59a36d 跑在真实仓库 `D:\intelligence-agent`（只读，0 写入）。wait3 内容标记门禁拒绝该文件；标记 `INVALID-run9v2-wrong-workspace.json` 并固化"发送前截图目视核对"强制步骤。此后 10 次 run 无一跑错。
4. **模型路由陷阱**：GUI `deepseek` 供应商的 "deepseek-v4.1-flash" 实际是 `deepseek/deepseek-v4-flash`；经用户指出改用 Cline 条目。每 run 记录实际 modelId。
5. **焦点抢占**：17:40 前后 Chrome 占用前台致 Ctrl+N 被拒（frontmost_pid_mismatch）；确认 ZCode 回前台后重试成功，无 run 受影响。
6. **rollout 延迟**：run 4 文件约 3 分钟后才创建；wait3 改内容标记定位 + 加长窗口解决。

## 6. 覆盖限制

- 仅覆盖只读分析类任务（spec-route、review-missing）两个判据；不覆盖写代码、Recovery、Sandbox、MCP 等行为。
- DeepSeek 每格 1 次（用户减半），GLM 重复 2 次仅观察；无因果结论。
- GUI 无法复刻引擎 180s/32 步预算；引擎-GUI 分数差异（§3.3）未做归因。
- 原脚本把 Read/Grep/Glob 中出现文件名算作访问，漏掉 Bash 正文且不检查成功/截断；write_attempts 只收主会话 Edit/Write，不能独立证明无文件写入。复核索引保留原值与修正，工具返回和语义判读仍须一起核对。
- 独立审查核对18份有效 JSON 与18个主会话 rollout 的对应关系、模型/工具序列/最终回答，并检查相关 Read/Bash 返回及两个子 Agent；没有声称对所有正文逐字人工审阅。
- 按用户本轮决定，夜间长跑与 Codex GPT-6-Luna 测试撤销；它们未执行，不能宣称已证明可靠性。其他未测行为仍按既有范围记录，不新增测试任务。

---
*组织者：ZCode 会话（sess_5d1a503d），2026-10-02。判读脚本与原始 rollout 见 `desktop-gui-2026-10-02/`，进度台账 `desktop-gui-2026-10-02/gui-progress.md` 含逐 run 时间线。*
