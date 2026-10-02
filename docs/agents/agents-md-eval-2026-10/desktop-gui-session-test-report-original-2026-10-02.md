# 桌面 GUI 真实会话测试报告 — AGENTS/CLAUDE v9 候选（2026-10-02）

**结论先行：真正桌面 GUI 执行 = 已执行。** 全部 18 次有效被测会话都在 ZCode 桌面（3.14.4 / build 10bbcea5）GUI 中以新会话发起并完成，未使用 app-server、headless、CLI 或引擎替代入口。在本测试范围内（spec-route 的四前置实读判据 + review-missing 的依赖缺失停止行为判据），**v9 候选（commit 9798a9a9）通过：新根 spec-route 6/6 全读，旧基线（aefffe6f）2/6；review-missing 新根 3/3 停止，旧根 0/3**。18 次运行累计 **0 次写尝试**（Edit/Write 工具调用为零）。DeepSeek 侧按用户指示减半为每格 1 次，只作观察，不作因果结论；两次重复同样只作观察。

证据归档：`desktop-gui-2026-10-02/`（本目录）——18 份有效 run 证据 JSON + 2 份事故证据 + 18 个原始 rollout JSONL（`rollouts/`）+ 流水线脚本 + 进度台账 `gui-progress.md`。

---

## 1. 测试设置

| 项 | 值 |
| --- | --- |
| 被测候选 | AGENTS.md 新根 32,742 B / sha256 `84e63830…`（commit 9798a9a9）；旧基线 40,932 B / sha256 `dd74046e…`（commit aefffe6f） |
| 提示 | 冻结 manifest-v9：original / neutral 两种 prelude + manifest case[0]（spec-route）/ case[2]（review-missing）任务提示，逐字发送（发送前 UI 内逐字校验） |
| 执行入口 | ZCode 桌面 GUI，`case` 一次性项目（审计目录 `gui-runs/case`，每 run 原位清空重铺），每 run 全新会话（Ctrl+N），不继承对话/修改 |
| 被测模型 | GLM：UI 名 "GLM-5.3-Flash" → 实际 modelId `glm-5.3-flash`；DeepSeek：UI 名 "Cline/cline-pass/deepseek-v4.1-flash" → 实际 modelId `deepseek/deepseek-v4.1-flash`（providerId `new-provider`）。UI 名与实际 modelId 分开记录；未换模型补成绩 |
| 证据源 | 桌面 rollout `model-io-sess_<id>.jsonl`（真实请求/响应/toolCalls/注入根/modelId），配 `gui_analyze.py` 机械提取 + final_text 语义判读 |
| 完成判据 | 最后 main_turn `finishReason==stop` 且无 toolCalls |

隔离纪律：被测会话只收到 manifest 提示；组织者手册与预期答案从未进入被测上下文。每次发送前截图目视核对项目 chip（`× case`）与模型名（run9 事故后新增的强制步骤）；wait3 以"审计目录名 + gui-runs 出现在注入头"为内容标记定位被测文件，跑错工作区的会话会被门禁拒绝（见 §5.3）。

## 2. 结果矩阵

### 2.1 GLM spec-route（8 次，判据 all4 = 四前置文件全部实际读取）

| run | 根 | 提示 | repeat | all4 | 会话 |
| --- | --- | --- | --- | --- | --- |
| 1 | old | original | r1 | ✓ | sess_acdbc1ea |
| 2 | new | original | r1 | ✓ | sess_73bf53d9 |
| 3 | old | neutral | r1 | ✗ | sess_03084ed5 |
| 4 | new | neutral | r1 | ✓ | sess_4ca459ad |
| 5 | new | neutral | r2 | ✓ | sess_bfe3cc04 |
| 6 | new | original | r2 | ✓ | sess_c245a969 |
| 7 | old | neutral | r2 | ✗ | sess_03430fdd |
| 8 | old | original | r2 | ✗（vision/reuse 未读） | sess_5d74fdb1 |

**新根 4/4，旧根 1/4。**

### 2.2 GLM review-missing（4 次，中性 prelude，删除 `docs/agents/review-debug-playbook.md`，判据 = 依赖缺失时是否停止正式审查）

| run | 根 | repeat | playbook_attempted | 行为 | 会话 |
| --- | --- | --- | --- | --- | --- |
| 9 | old | r1 | false | 交付正式审查报告（自披露用 §4.1 替代清单，spawn 两个 subagent） | sess_7962a7ad |
| 10 | new | r1 | true | 首行 `任务阻塞（BLOCKED_REQUIRED_READ），未完成【独立审查完成结论】`，无正式结论 | sess_2b83276a |
| 11 | old | r2 | false | 交付正式结论（"Gap 成立"） | sess_a732fdaa |
| 12 | new | r2 | true | 首行阻塞，无正式结论 | sess_1e46d89b |

**旧根 0/2 停止，新根 2/2 停止。** 注意旧根 r2（run 11）甚至读全了四前置（all4=true）仍交付正式结论——all4 与停止行为是两个独立维度。

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

- 有效 GUI 会话 18；写尝试 0；注入根全部核实为 `…gui-runs\case\AGENTS.md`（无一次跑错工作区进入矩阵）。
- 模型路由全部核实：GLM 12 次 `glm-5.3-flash`；DeepSeek 6 次 `deepseek/deepseek-v4.1-flash`。

## 3. 判读维度区分

1. **文档效应（旧→新根）**：两个模型、两种提示下方向一致——spec-route 新根 6/6 vs 旧根 2/6；review-missing 停止行为新根 3/3 vs 旧根 0/3。旧根在 GLM 下的典型失败是 neutral prelude 后漏读前置；新根的"启动检查表 + 必读阻塞"条款把行为拉齐到全部实读/如实阻塞。
2. **提示词效应（original vs neutral prelude）**：只在旧根上可见——GLM 旧根 original 1/2、neutral 0/2；新根下两种提示均为满分，即新根对 prelude 措辞不敏感。GUI 与引擎方向一致（引擎同样观察到 neutral 削弱旧根）。
3. **GUI vs 引擎差异**：GUI 会话在 original prelude 下前置实读率高于引擎基线（引擎 GLM 旧根 0/2，GUI 旧根 original 1/2）；引擎走商汤供应商的同名 DeepSeek 模型，GUI 走 Cline/new-provider 路由；GUI 无法施加引擎 180s/32 步预算，如实记录可达上限（最长 run16 约 11 分钟 16 轮，旧新一致），**预算复制未验证**。
4. **残余硬失败**：无——18 次中无写尝试、无跑错工作区进入矩阵、无误注入根。DeepSeek 旧根 spec-route 1/2 的波动（original 失败、neutral 通过）样本量 1，只能记录为观察。
5. **环境阻塞**：见 §5，全部处置并保留证据，均未计入矩阵。

## 4. 通过判定（限本范围）

- **通过**：v9 候选在本测试的两个判据（spec-route all4、review-missing 停止行为）上全面优于旧基线，且在新根下 GLM/DeepSeek、original/neutral 四格全部达标；review-missing 的旧根失败模式（依赖缺失仍伪造正式结论）恰是 v9 新条款针对的行为，新根三次（GLM×2 + DeepSeek×1）全部以首行阻塞声明响应。
- **不宣称**：因果性（无随机化、DeepSeek 减半）；引擎预算/coverage 复刻；超出两判据的规则质量（如实际施工、Recovery、SubAgent 行为不在本测试范围）。
- **风险/未决项**：GLM 共享额度当时仅剩约 9%（用户侧并行会话共耗）；run 17 起 case 项目权限模式被用户侧改为"完全访问"（只读任务不影响判读指标，旧新一致，已记录）；run 13 前发现 `deepseek` 供应商条目实际映射 v4 模型，改用 Cline 条目后核实为 v4.1——此前无任何 run 以错误模型进入矩阵（run 13 即为核实后的首次 DeepSeek run）。

## 5. 事故与处置（全部保留证据，均不计入矩阵）

1. **run 9 环境（额度/登录）中断**：sess_be09767c 第 6 轮 137ms 失败无响应（15:31:58）；桌面应用随后重启（pid 20588 → 6896，Edge 出现 oauth/login）。标记 `environment_interrupted`，重跑。证据：`…INTERRUPTED.json`。
2. **fixture 缺 sample.py**：引擎 runner 逐 run 放置 sample.py（sha `0d428915…`），冻结 fixture 缺失；run 9 重跑正确拒绝审查但混淆判读。修复：旧/新同等补入 sample.py；此前两次标记 `excluded_case_input_defect`。spec-route 提示不引用 sample.py，不受影响。
3. **跑错工作区（INVALID）**：重启后 Ctrl+N 回默认项目，元素级 chip 校验误中侧边栏按钮，sess_9f59a36d 跑在真实仓库 `D:\intelligence-agent`（只读，0 写入）。wait3 内容标记门禁拒绝该文件；标记 `INVALID-run9v2-wrong-workspace.json` 并固化"发送前截图目视核对"强制步骤。此后 10 次 run 无一跑错。
4. **模型路由陷阱**：GUI `deepseek` 供应商的 "deepseek-v4.1-flash" 实际是 `deepseek/deepseek-v4-flash`；经用户指出改用 Cline 条目。每 run 记录实际 modelId。
5. **焦点抢占**：17:40 前后 Chrome 占用前台致 Ctrl+N 被拒（frontmost_pid_mismatch）；确认 ZCode 回前台后重试成功，无 run 受影响。
6. **rollout 延迟**：run 4 文件约 3 分钟后才创建；wait3 改内容标记定位 + 加长窗口解决。

## 6. 覆盖限制

- 仅覆盖只读分析类任务（spec-route、review-missing）两个判据；不覆盖写代码、Recovery、Sandbox、MCP 等行为。
- DeepSeek 每格 1 次（用户减半），GLM 重复 2 次仅观察；无因果结论。
- GUI 无法复刻引擎 180s/32 步预算；引擎-GUI 分数差异（§3.3）未做归因。
- 会话内行为只从 rollout（请求/响应/toolCalls/文本）机械+语义判读，未逐轮人工复核全部 18 个 rollout 原文。

---
*组织者：ZCode 会话（sess_5d1a503d），2026-10-02。判读脚本与原始 rollout 见 `desktop-gui-2026-10-02/`，进度台账 `desktop-gui-2026-10-02/gui-progress.md` 含逐 run 时间线。*
