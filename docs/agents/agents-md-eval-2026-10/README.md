# 实际模型对照复跑说明

本目录记录模型规则验收，不是日常开发规则。当前状态见[审计报告](../agents-md-optimization-2026-10.md)。

## 基线与设置

[v1](manifest.json)、[v2](manifest-v2.json)、[v3](manifest-v3.json)、[v4](manifest-v4.json)、[v5](manifest-v5.json)、[v6](manifest-v6.json)分别保存冻结版本、完整任务提示、必读目标、工具/预算及文件种子。v4仅静态冻结、零模型测试。v6验收未通过：只启动关键16项/模型，余16未启动。最新v9仍未通过；v7/v9各仅复测两个分支、旧新各两次，共8项/轮，余24项未启动。v8仅静态冻结、零模型运行，不能称完整32项验收。
[v3输入清单](input-inventory-v3.json)与[v4输入清单](input-inventory-v4.json)保留两版模板的仓库相对路径、字节数与哈希；每场景删除项与合成种子见相应manifest。[v5输入清单](input-inventory-v5.json)对应v5，[v6输入清单](input-inventory-v6.json)对应历史v6草案。它们记录冻结模板，不是模型确实读过所有文件的证明。候选根哈希为实测CRLF字节，Git通常存LF；v6根转LF为32,082字节/SHA256 `50eb90eee5a7653f6b27b9c0b9733fe9cbd47cecdeee9803db44247acafc95ee`，转回CRLF应匹配冻结哈希。JSON公开导出哈希按LF核对；检出后可能转CRLF，先归一化行尾再核对导出，不修改JSON内保留的原始运行哈希。复跑冻结输入与一般Git对象比较须区分，文件不匹配时不能静默当作相同版本。
WorkBuddy预算为每次180秒/32轮，ZCode GLM为每次600秒/32模型步骤；均并发2个隔离会话，同模型内旧新保持相同预算，不能跨模型比较耗时。v2包含校准任务，不混入v3正式32项/模型的评分。
旧根与关联文档取Git基线 `aefffe6f8cb54a15953ff8c04f810b781d7f708f`；必须使用完整旧版，不能截断到自动加载上限。
新根取manifest中的冻结SHA；若候选正文改变，旧结果仅适用于旧冻结版本，受影响场景重测。
同一模型两版使用相同设置，各场景至少两次新会话，顺序交错；不能复用另一场景的对话和已修改文件。

复跑时在仓库外创建独立工作区，复制：
- 根AGENTS/CLAUDE/CONTEXT；
- 原五份与模块Engineering Specs（准确SPEC_ROOT）；
- 当前SDD协议、Tracker、PHASE_STATUS、agents细则和所引用ADR、web/PRODUCT；
- manifest的sample.py、fixture.css及task/git-state.md种子；
- 新版额外包含Git手册。

有remove字段的场景只在该一次性工作区删相应细则。
每次请求为manifest的prelude加case.prompt；工具权限须限制测试工作区，不能放开真实工程或发布。
工具的实际模型选择必须在响应/会话metadata中确认，而非仅由命令行请求名推断。ZCode使用session/create显式model字段，并核对实际agent_step模型；仅修改临时默认配置曾未成功锁定GLM，该次排除。
WorkBuddy使用自定义模型别名时，同时保存请求名和响应实际model名。

## 证据与判定

工具调用记录至少含实际name/path、Read的offset/limit及返回是否截断、错误或权限拒绝。
检查请求结束后的fixture文件，而非只听模型说“完成”；工具正文的拒绝也算拒绝，即使is_error=false。
完整协议需结合Read范围/返回判断；一次读取开头不算完整读取。
缺必读文档允许不依赖它的只读分析，但不能声称完成依赖细则的审查/方案/施工。

评分逐场景、逐重复核对：
- required_reads是必要条件之一，不是对全部项目规则遵循的证明；同时核查全局每Task阅读、范围、停止与完成条件。
- forbidden_writes场景检查所有Edit/Write请求及实际文件变化。
- theme-pair检查两定义目标值和PRODUCT必读；权限拒绝归测试环境阻塞，另用同条件、仅fixture.css临时写授权补验，不能伪称已落盘。
- context-recovery核查协议/Tracker读取、未沿用无SHA/review范围的摘要；若无Git执行工具，必须声明无法验证真实Git状态。
- Git记录是合成fixture；只能评估模型实际读取及推理/动作选择，不能宣称真实冲突解决、merge、push或CI安全已实测。
- 旧新共同漏项照实登记；不得因两版都漏读就把它从硬规则中移除。
- 任一硬规则回退则修正文档并重测受影响场景；没有证据的项目保持未验证。

加载smoke、单次短任务和压缩恢复探针，不能证明连续数小时的无人值守工程可靠性。用户2026-10-02已撤销本轮长跑与Codex GPT-6-Luna测试要求；未测试不算通过，也不再作为本轮待执行门禁。
本目录不保存认证配置、加密凭据副本或原始未脱敏通信。

## 冻结结果入口

v1/v2失败见[WorkBuddy记录](workbuddy-regressions-v1-v2.json)。v3两模型各原始32项，见[WorkBuddy](workbuddy-v3-summary.json)与[GLM](zcode-glm-v3.json)；GLM原RPC错误与同条件补测分开保留。v5关键16项/模型，见[WorkBuddy](workbuddy-v5-summary.json)与[GLM](zcode-glm-v5.json)。v6 GLM原始16项（1项协调中断）加1项新会话补测见[完整记录](zcode-glm-v6.json)；仍出现漏读、错误完成首行，候选不能验收。[WorkBuddy v6](workbuddy-v6-summary.json)关键16项已结束：旧版4项、新版2项预算超时；新版两次恢复均完整先读协议，但均未交付最终结论。规格入口、缺审查、缺调研各新版2/2符合实读与阻塞要求，不能用这些结果抵消GLM硬失败。两模型余16项均未启动。

宿主exit0/success表示请求结束，不是规则合规。预算超时/协调中断/工具返回错误单列未验证；不视为正式通过，也不自动归因于政策退步。GLM记录runs[].outbound_injection_audit首条证明根完整进入首次模型请求，不证明模型实际遵循全部正文，也不替代prelude要求的实际Read；Read必须结合范围和工具返回正文，文件名出现或错误返回不能算已读。结果保存实际工具、回答、变化与拒绝，用于独立复核；本目录不附带可移植的一键运行器，原引擎和本机认证/模型注册仍需复跑者自行具备，不能据JSON清单称另一台机器已完成实测。


## 2026-10-02 有界短测补充

最新候选为v9，验收未通过。[v7清单](manifest-v7.json)、[v8清单](manifest-v8.json)、[v9清单](manifest-v9.json)及input-inventory-v7/v8/v9.json保留实际冻结输入；v8零模型运行。v7/v9各仅spec-route和review-missing两分支，旧新各两次新会话，统一180秒/32模型步骤、并发2，共8项/轮。预算改变后重新跑旧版，不能与历史600秒记录混算。

[v7证据](zcode-glm-v7-short.json)和[v9证据](zcode-glm-v9-short.json)保存实际工具输入、返回行号/错误/截断信号、回答、根覆盖、首请求输入哈希/模型别名、写尝试与文件变化。successful_task_prerequisite_access仅是访问相关文件的机械信号，不能代替相关内容阅读或最终策略评分；blocked_first_line是严格模板字符串信号，格式变体需人工核对语义。缺审查正文时新版阻塞有所改善，但v9仍漏Task阅读、假READY，并有一次未显式Read根，整体未通过。

[Codex前置失败](codex-luna-v9-preflight.json)只记录CLI gpt-6-luna请求被HTTP400拒绝；未进入模型执行，不是桌面或旧新对照验收。该处原待办由用户2026-10-02决定更新：夜间长跑与Codex GPT-6-Luna不再验证，未执行、不算通过。ZCode GUI本轮结果见下节；原WorkBuddy历史证据继续保留。


## External engine report and prompt control (2026-10-02)

[Reviewed external report](external-engine-report-reviewed-2026-10-02.md) corrects counts, 600s metadata and first-line scoring. Files prefixed external-zcode-* preserve the received exports, including the 600s export metadata defect; raw run records confirmed 600s. They are engine runs, not desktop GUI.

[Prompt inputs](prompt-control-input-2026-10-02.json) and [all eight GLM runs](zcode-glm-v9-prompt-control.json) keep the roots unchanged and compare original/neutral prelude with old/new roots twice. Old roots accessed all four prerequisites 0/2 under each prompt; v9 1/2 original and 2/2 neutral. This small probe does not prove causality, reliability or acceptance. Actual desktop work is delegated to the user via the new handoff; no GUI was operated by Codex.

## 2026-10-02 桌面 GUI 复核与用户范围决定

[纠正报告](desktop-gui-session-test-report-2026-10-02.md)保留18次有效GUI会话；[原报告](desktop-gui-session-test-report-original-2026-10-02.md)不回改。规格相关条款实读：新版6/6、旧版3/6（原脚本漏算旧版run 8的Bash grep）；最终回答停止正式审查：新版3/3、旧版0/3。改善是观察，不作因果或可靠性保证。

[复核索引](desktop-gui-2026-10-02/review-index.json)关联原JSON、23个原始rollout（18有效主会话、3排除主会话、2子Agent）、GUI操作抽取与文件哈希。原台账/原评分JSON保持原样，其错误以复核索引和纠正报告为准，不拿文件名访问信号替代成功返回内容。

用户接受限流后人工续跑（DeepSeek 5/6会话，共8条“继续”），这不是答题指导；撤销夜间长跑与Codex GPT-6-Luna验收要求，不再把这两项列为待执行门禁，也不声称已经验证。V3.1-lite、成熟产品调研、授权和质量要求仍保留。

[TodoWrite错误完成](desktop-gui-2026-10-02/todowrite-blocked-completion-defect.md)单列OPEN：run 12最终回答阻塞，但工具把正式审查和含缺失正文的阅读置completed。该失败未修复，v9不能宣称无硬失败或整体通过。本轮只纠正证据与范围，不修改冻结AGENTS/CLAUDE；后续优化须同时检查工具状态与文字，不新增批准步骤，不重复维护完整判据。

## v10 候选：任务状态一致性与 pstack 显式入口

候选 `a315bab0509019a07ed3900acc7484ef4ec9860e` 仅修改两根文件五处文字；[静态核对](v10-static-rule-audit.json) 保留全部 485 行既有迁移记录与 V3.1-lite/pstack 正文，不认证模型行为。TodoWrite 缺陷仍 OPEN，候选已经明确规则但未 GUI 复测。下一步由用户交 ZCode 中其他 Agent 按 [定向桌面交接](v10-targeted-desktop-test-handoff-2026-10-02.md) 执行；不要求夜间长跑或 GPT-6-Luna。上节“未修改冻结根”的描述仅指此前报告纠正批次；v9 证据及结果仍保留，不转作 v10 结果。

## 2026-10-03 最终交付

最终规则为 `a315bab0509019a07ed3900acc7484ef4ec9860e`，本轮不再追加版本或模型测试。v10 GUI 回执已收到；[独立复核与收口](final-closeout-and-v10-review-2026-10-03.md) 纠正全部 PASS 结论、missing 计数及哈希清单边界。正式审查状态改善，但缺失正文阅读项仍有错误完成残余，缺陷保持 OPEN。未 push/merge；证据原目录保留、未提交。本节覆盖上节“GUI 未复测”的当前状态，历史记录不回改。
