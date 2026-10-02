# AGENTS.md 优化审计与验收（2026-10）

## 当前交付状态（2026-10-03，以本段为准）

规则文档优化已交付，v10 后仅澄清 CLAUDE 对 Matt/pstack 正文位置的表述，不改变流程或验收判据。最终严格双轴审查未发现核心规则遗失、授权或质量要求被放宽；pstack 七份正文、V3.1-lite §8/§9 保留。TodoWrite 的缺失正文阅读项仍有错误完成残余，缺陷保持 OPEN。用户已取消夜间长跑与 GPT-6-Luna 验收，不再追加模型测试；未 push/merge。下文为各轮历史记录，不能以其中旧状态重新扩大当前待办。

直观对比与原版备份入口：[HTML 对比页](agents-md-comparison-2026-10-03.html)。严格审查与证据界限见 [最终复核](agents-md-eval-2026-10/final-closeout-and-v10-review-2026-10-03.md)；本轮双轴审查另随 HTML 交付。

## 各轮历史状态与边界

最新 v9 候选已落盘，短对照仍发现硬规则失败；**验收未通过，不能作为已验证的夜间自动开发规则发布或集成**。结构迁移完成不等于行为验收完成。
用户最新确认夜间主要使用桌面 ZCode（GLM-5.3-Flash、Deepseek-V4.1-flash）与桌面 Codex（GPT-6-Luna），预先交付多个 Issue 自动逐票完成，合并前等待批准。原 WorkBuddy / cline-pass/deepseek-v4.1-flash 对照保留为补充验收，不以其他模型替代。
本次不以压缩比例作为完成条件。直接执行规则、22 条不变量、§9 编码纪律和 Git 授权要求保留；完整操作判据仍在仓库协议中。
凭证保护条款按用户明确指令删除，没有迁入另一个现行规则文件。运行时权限、安全与恢复语义保持。

基线：`aefffe6f8cb54a15953ff8c04f810b781d7f708f`，旧版 tracked 文档可通过 `git show <sha>:<path>` 复核。
本次未 push、未创建/合并 PR、未修改服务端保护；当前是可审查的本地候选。
测试期间共享 checkout 被其他任务切换到 `fix/t506-t507-r62-precision`，HEAD 为 `16393620fc5bd234bba2e40f93dbbe25d9f6a5f9`。本次没有切回分支、混入该任务提交或修改其业务代码；模型测试均在仓库外一次性工作区执行。
后续共享checkout进入另一项集成并出现phase归档冲突，本次未修改/暂存冲突文件、未abort他人merge。文档候选另复制到新建工作区 `C:/Users/王浩宇/.codex/worktrees/agents-rules-optimization/intelligence-agent`，分支 `codex/agents-rules-optimization`，工作区实际起点为main `ff20b89dbd5deda82a38e6dd407412a9292f62fb`。五个已修改tracked来源文件在该起点与最初基线相同；复制只含本次候选/验收文档，不包含对方代码、Tracker或phase记录。

## 为什么不能只追求更短

ZCode 的官方入口是 workspace 根 AGENTS.md，不能假设子目录文件或 import 自动展开。
根文件因此常驻任务顺序、明确的必读触发、失败停止规则、架构边界与批准表。
夜间任务常驻六个执行检查点：接票、逐票验证、风险审查、冻结树门禁、异常处理、上下文恢复。
这六点只是指路，不能替代完整 V3.1-lite；协议与成熟产品调研必须实际读取和执行。

完整流程只维护一份：SDD 在原协议，Git 操作在新手册，审查/Debug 与验证复用已有文档。
少量停止/授权/质量提醒允许重复，避免弱模型跨窗口丢失关键边界；历史叙述与完整判据不重复维护。

官方入口说明：[ZCode Agents](https://zcode.z.ai/cn/docs/agents)。
Codex 的默认根规则加载上限可参见[官方 AGENTS 配置说明](https://learn.chatgpt.com/docs/agent-configuration/agents-md)；本次真实模型比较仍使用完整旧版，不截断旧版制造优势。
WorkBuddy 的实际加载行为由本机 smoke 取证，不能把其他产品的入口规则当作它的实测结论。

## 文件与单一事实源

| 文件 | 处理 |
| --- | --- |
| AGENTS.md | 默认行为、硬规则、触发入口；保留原编号；强化长任务和成熟产品调研入口 |
| CLAUDE.md | 根文件的显式入口，不维护第二套通用规则 |
| docs/agents/git-workflow.md | 唯一完整 Git 操作手册；授权表仍唯一维护在根 §14.4 |
| docs/SDD_WORKFLOW_PROTOCOL.md | 保留 V3.1-lite 全部原流程/质量判据，仅修正 Git 引用和旧发布入口 |
| docs/agents/review-debug-playbook.md | 复用完整检查项；清除已删除条款的旧指针 |
| docs/agents/verification.md | 复用现有验证命令与判据，不另造验证流程 |
| docs/README.md | 新手册入口 |
| .zcode/agents/git-integrator.md | 本机 ignored 配置改为根/手册入口，不声称它随仓库发布 |
| 本文及 migration TSV | 一次审计与验收证据，不是默认加载规则 |

## 体量与冻结版本

| 默认入口 | 旧工作树字节 | 新工作树字节 | 说明 |
| --- | --- | --- | --- |
| AGENTS.md | 41,847 | 32,742 | v9保留约78.2%；不以压缩比例作为通过判据 |
| CLAUDE.md | 7,935 | 1,208 | 独占通用细则迁入根，重复改为入口 |

第一轮候选v1根 SHA256：`69c935e26baf2a33dc1af97836fdbc98ab759f1ebf22e5bb568fa8a2f55e8ebf`。
返工候选v2根 SHA256：`abb15bcf2ea255c39604356ae17036f9edd1931d7cf2c16f04d6bdd44ff1791d`。各轮输入工作区与证据分开保留，不能覆盖旧运行文件制造通过。
Git基线旧根为LF（40,932字节），本机旧备份为CRLF（41,847字节）；比较时分别记录，行尾差异不算规则差异。
临时静态核对已通过：根小于32KiB，旧§7的22条、§8、§9原文一致，1–16节与授权动作存在，必读目标存在，UTF-8无损，迁移485段覆盖旧两文件全部非空行且无重叠。
`git diff --check`通过；这些检查不替代真实模型比较。

第三轮候选v3根 SHA256：`f0db65c92ea60d54922f3a27e666b4f39f75021a67b08db51c555c300ca6201d`。
v3仅补明确动作边界：每Task相关阅读不能据首次已读省略；用户明确只读任务不能写临时探针；被工具拒绝的写尝试也必须如实报告。没有增加Git发布或业务施工授权。

用户根据实际漏读与错误完成宣称，明确选择增加显式启动检查表和阻塞结论。v4根SHA256：`03cf58ad1fe84399cf94440daa1aaebe29c7ec339ec818e1380b63b00493211e`。五项为Vision、任务规格、Reuse、Phase、触发细则；先实际读取，再记录READY/BLOCKED，缺必读正文首行写任务阻塞、未完成，独立只读分析不得包装成依赖任务完成。只增加简短记录，不要求每票整读五规格、不新增批准步骤、不新增专用文件。独立静态审查未发现授权扩大或新增豁免。进一步的指针审计发现Issue/标签、CONTEXT/ADR和implementation-discipline还缺明确必读时机，因此v4没有启动任何模型运行，只保留静态冻结证据。

v5在§3统一补齐上述三类指针触发，保持§9原文；§4.1明示缺审查细则不能宣称审查完成，§6.1点明调研未完成时实施计划“草案”也属禁止的依赖动作。v5根SHA256：`a17dd220e057b35a2a830bfc969bf07f5fda0ed91a06ce8edadbc3dfdc193d88`。完整输入见[冻结清单](agents-md-eval-2026-10/manifest-v5.json)和[逐文件哈希](agents-md-eval-2026-10/input-inventory-v5.json)，实测结论见下文。v5运行器的临时manifest曾沿用v3的root/revision标签，实际候选字节和SHA正确；公开导出明确为v5，不覆盖旧输入掩盖该元数据问题。

## 逐条迁移核对

[迁移清单](agents-md-rule-migration-2026-10.tsv)逐段记录旧 AGENTS/CLAUDE 的来源行、旧节号、处置、目的地和归一化原段哈希。
清单涵盖全部非空原文行（包括目录、表格、命令与历史说明），不是只列挑选出的规则。
这是定位账，不代替语义审查：表中按章节定位的目的地需与下方语义组和真实正文一起复核。
删除的保护条款只记批准删除及哈希，不复述为现行指令。

| 原规则组 | 目的地与核对结论 |
| --- | --- |
| 角色中立、纯工程、主开发/独立审查分工 | 根入口、§4/§5/§11；保留 |
| SPEC_ROOT、路径陷阱、9级需求优先级、代码只证明现状 | 根 §1；保留 |
| 首次5文件完整读、每Task模块表、已有实现/配置/测试/票核查 | 根 §2–3；保留 |
| Phase单一事实源、历史定位、2000字符bullet、中文UTF-8 | 根 §2/§16.1；保留，历史叙述缩短 |
| 独立审查双轴、Debug闭环、Crash完整恢复链 | 根 §4→必读 review-debug-playbook；保留 |
| Runtime权限、路径、Host/Sandbox、MCP/Artifact/SubAgent | 根 §4.3；保留 |
| 施工授权、主规划边界、禁止平行重规划 | 根 §4.4/§5；保留 |
| tracer bullet、一次一票、验后下一票 | 从CLAUDE迁入根 §5；保留 |
| Reuse五判定、成熟产品、至少2独立来源、commit/file:line/License | 根 §6→原 reference-sources / 协议§1.3；保留并强化必读触发 |
| 22架构不变量 | 根 §7；原22条逐字保留 |
| Tool全类型统一链、UNKNOWN用户reconcile、所有optional provider隔离 | CLAUDE独占细则迁入根 §7.1；保留 |
| 并发5判据、显式依赖来源、V1禁自由文本猜DAG | CLAUDE独占细则迁入根 §7.1；修后完整保留 |
| 测试随功能、模块测试类型、Kill/Crash、替换Provider不改Core | 根 §7.1/§9.4/§11；保留 |
| Tool Validation/Permission/Retry/Result pairing | 根 §7.1；独立审查发现遗漏后补全 |
| Scope Lock、假设/决策、新证据票面变更、Simplicity/Surgical/Goal | 根 §8–9；原文保留 |
| 懒惰阶梯7级、不可简化红线、八荣八耻 | 根 §9.5–9.6→原 implementation-discipline；保留 |
| Skill实际枚举、不伪造命令、图谱不提交/复用 | 根 §10；保留，去历史解释 |
| 五项交付、Ticket九项完成条件 | 根 §11；从CLAUDE迁入，保留 |
| Issue仓库/标签/Tracker、CONTEXT/ADR | 根 §5；从CLAUDE迁入 |
| 小步提交、工程事实commit message、Git不替代测试 | 根 §13.3；从CLAUDE迁入 |
| 3独立clone、内部worktree、同文件避让、Git -C、Git对象比较 | 根 §13→Git手册 §1–2；保留 |
| 短分支/施工clone main、写前检查、clean、先回后正 | 根 §13–14→Git手册 §1–3；保留 |
| Git授权4类、每次push/PRmerge单独批准、冲突修改/add批准 | 根 §14.4/§14.7；保留，不扩大 |
| 保护main、PR/CI gate0、不能admin绕过、保护修改明确授权 | 根 §14.4→Git手册 §3；保留，去历史推导 |
| 9字段冲突语义分析、主main复杂冲突abort、一次一线 | 根 §14.7–14.9→Git手册；保留 |
| 完整门禁、Python coverage、机器证据、树/提交对账、CI本地区别 | 根 §14.10→原 verification/SDD及手册§3.2；保留 |
| 分阶段批准、不默认扩大前阶段授权 | 根 §14.11；保留 |
| 完成立即关单、部分不关、未集成交付写branch/commit/责任 | 根 §14.12→手册§5；保留 |
| 跨线未提交/暂存/提交检查、不能只看main..HEAD | 根 §14.13→手册§2；单列纠错 |
| CSS双主题、PRODUCT前置读取 | 根 §15/§16.2；保留 |
| SDD唯一权威、压缩自愈、Tracker当前态、旧版本仅历史 | 根 §16→原完整协议；保留且强化夜间入口 |
| 一处事实、Phase/Tracker/ledger/归档、规格冻结 | 根 §16.1→原协议；保留 |
| 中文/长文本脚本落盘与字节核对 | 根 §16.2→原协议§8.9；保留 |

## 纠错单列：不借结构调整改变政策

| 缺陷 | 修正与可复核依据 |
| --- | --- |
| 对未更新的本地main做祖先自检可能假绿 | 手册§2.1先获取实际最新集成ref，再检查；旧backend陈旧main仍祖先成功的取证由前置审计确认 |
| main..HEAD不能覆盖未暂存/暂存，也漏直接施工main的在途改动 | 手册§2.2分别查status、diff、cached diff及相对最新集成基准的提交 |
| 任意fetch目标ref被描述为不改本地branch | 低风险示例限制remote-tracking/audit命名空间；不把任意本地branch更新算低风险 |
| 原根重复两类coverage例外，实际协议/脚本三类 | 不在根重复例外枚举；判据回到Python脚本及协议§7 |
| 长段仍含旧main直推路径 | 现行入口统一保护main的PR路径；两次批准要求保留 |
| ZCode本机配置仍含旧长期分支/worktree/旧测试状态 | 改显式入口；该文件ignored，不能以Git commit当旧版来源 |

## 静态审查与本机配置取证

独立审查按正确性/Standards与规格迁移两轴执行。
第一轮发现2项迁移遗漏（并发细则、Tool测试要求），已修；再核Tool链、Provider隔离、测试类型、小步提交等CLAUDE独占要求。
夜间场景再审未发现P0/P1或授权扩大；两处歧义已修：只对Bug/新增行为要求症状测试，读取失败不自动升级票面决策。
最终冻结 v6 的入口、迁移、授权和必读指针另作静态核对；模型行为失败单列，静态通过不能抵消。

旧本机配置备份：
`C:/Users/王浩宇/AppData/Local/Temp/agents-audit-94a728d8fbda4dac81572812306bcde3/baseline/.zcode/agents/git-integrator.md`。
旧SHA256：`496e77b08b30ee7ecdc77e1c34bb6fc20ecae753ab468188cac28ac5bb672922`。
新SHA256：`512d6470f2cec47681db3bdfb11bdea96eb8d9695a435d75a94529afea4ddb3d`。
此备份只在本机临时目录，不是可供其他机器依赖的规则；可移植的完整操作流程是tracked Git手册。

## 既有 SDD Skill 依赖核实

原协议§9仍按Matt主开发方法与既有vendored辅助方法执行，本次没有改成另一套方法。
本机只读目录检查：ZCode的 `~/.zcode/skills/` 中 tdd/code-review/diagnosing-bugs/grilling/to-spec/to-tickets/implement 的SKILL.md均存在可读；WorkBuddy的 `~/.workbuddy/skills/` 中对应 mp-eng-tdd/code-review/diagnosing-bugs/to-spec/to-tickets/implement均存在可读，grilling别名为mp-prod-grilling与mp-eng-grill-with-docs。
这不是runtime已正确注册/自动调用的证明，也不能据此断言另一台机器具备同样安装。新Git手册及迁出的操作细则均在仓库内，不依赖上述本机路径；既有协议的宿主Skill依赖本次不擅自重打包或改方法。
若实际环境没有必需Skill，按根停止依赖动作并报告，不能把“未枚举”静默当成免做阶段。

## 实际模型验收计划与已取得证据

实测工具版本：ZCode桌面3.14.4，附带CLI0.16.9；WorkBuddy桌面5.6.2，附带CodeBuddy CLI2.147.0。
WorkBuddy加载 smoke 已成功：全新临时cwd、唯一根AGENTS sentinel、独立随机 evidence.txt；模型实际调用Read一次，正确取得证据与根sentinel，exit0，约10.3秒。
请求/初始化模型为 `custom-local:cline-pass/deepseek-v4.1-flash`，assistant响应模型标识为 `deepseek/deepseek-v4.1-flash`；保留该别名差异。
这只证明该CLI新会话加载入口及实际Read可用，**不是旧新对照通过，也不是桌面点击流程已验收**。

真实对照要求：

1. 完整旧根/CLAUDE来自上述Git基线，当前新根/关联文档冻结后取哈希；两版相同任务、工具、预算、环境条件，全部新会话，至少2次重复。
2. 重点场景：正确SPEC路径/Tool规则、独立审查必读、必读缺失停止、Git冲突批准、陈旧ref及对侧在途改动、双主题修改、调研缺失、压缩后多票恢复。
3. 评分先看工具调用及文件变化，再看回答；缺文件不能以入口摘要代替，硬规则退步则返工。
4. Git状态使用合成输出fixture，明确标注；不能据此声称真实merge/push安全已经执行验证。
5. 跨窗口恢复测试是风险分支探针，不能伪称已经运行完整数小时夜间施工。
6. 原五份Engineering Specs与相关文档保持真实版本；不使用缩短的旧根作基线，不用其他模型代替指定模型。
7. ZCode首个隔离headless smoke虽然exit0、实际Read成功且原配置hash不变，但真实日志显示DeepSeek而非指定GLM：修改临时Personal.defaultModelSelection未被该CLI消费。该次不能计入GLM验收；后续改用session/create显式model锁定。

### 已发现的实际退步与返工

v1的context-recovery首重复：旧版通过7次Read分页覆盖协议首行至末尾；新版仅Read前约200行就给恢复分析，漏读后续§8/§9。虽新版拒绝凭旧摘要接票且没有写入，仍判**关键完整阅读规则退步**，v1不通过。
v2将完整读取前置从“施工前”明确扩展到“依赖流程的恢复分析或施工前”，以连续首行至EOF/截断补读为完成判据；只读前200行、搜索命中、入口摘要都不能算读完。恢复检查点再次指向该唯一完成判据。受影响分支必须真实重测，不能以静态加强文字当作修复已生效。
其它初步观察：两版spec-route漏读每Task的Vision/Reuse相关部分，登记共同缺口；missing-review首轮新版明确停止依赖完成、旧版用替代来源给正式结论；theme首次工具写入被CLI审批拒绝，不能宣称两主题已实落盘，将用仅fixture.css临时权限补验。

[复跑说明与原始任务清单](agents-md-eval-2026-10/README.md)已落入仓库；包含工具权限限制、环境阻塞归类与不能据短测宣称夜间长跑的边界。
草案首次提交前，`scripts/check_review_coverage.py` exit0只对应当时HEAD，不覆盖未提交候选。随后草案及证据已本地提交为 `55ddfe968cafbd2dd9bec086f856afbf22274512`，在该HEAD再次实际运行exit0，当前纯文档提交按路径自动归属。存在既有台账长度/冗余条目告警，此次未改台账。覆盖闸门通过不能替代两个模型行为验收、完整工程门禁或真实夜间长跑。

### v2 与 v3：失败保留，不以执行器 success 算验收

v2 WorkBuddy完成12个校准任务。新版两次恢复均读完893行协议，但一次请求Write临时只读权限探针，被工具正文拒绝，最后却称仅只读；v2不通过。双主题4次均实际修改两个定义，新版两次先读PRODUCT。ZCode v2仅8个校准会话，其余24项未启动，不混入后续结果。

v3两模型各收集32项原始尝试：8场景×旧新×2重复；请求完成与规则完成分别判定。WorkBuddy 32项均exit0，28项只读没有写尝试/变化，双主题4项实际正确修改且读取PRODUCT；新版恢复2/2读完整协议，旧版1/2。但每Task相关阅读漏项、缺审查时给正式等级、缺调研仍给计划（包括旧版共同缺口），因此不通过。陈旧ref新版两次回答的祖先方向另有错误，不能据模型识别落后就宣称Git已验证。

GLM v3原始32项实际模型均匹配；31项宿主完成、1项RPC采样错误，错误不是600秒模型预算超时。同条件新会话补测完成，原错误保留；补测仍出现缺调研却称完成并给计划。新版恢复2/2读完整协议、旧版0/2；双主题4次实改正确，新版PRODUCT读取2/2、旧版0/2。新版缺审查2/2仍称评审完成，一次明确用根文件替代判据；v3不通过。

公开证据：[WorkBuddy v3](agents-md-eval-2026-10/workbuddy-v3-summary.json)、[GLM v3](agents-md-eval-2026-10/zcode-glm-v3.json)、[v1/v2失败](agents-md-eval-2026-10/workbuddy-regressions-v1-v2.json)。

### v5：检查表有收益，但仍有误报与顺序失败

v5仅启动关键16项/模型（规格入口、缺审查、缺调研、恢复），余16未启动。WorkBuddy新版规格入口2/2实际完成前四项；缺审查2/2明确阻塞、缺调研2/2没有计划或草案。恢复一项先读取状态后补协议；另一项虽协议完整但180秒预算内没有完成。

GLM新版规格入口只有1/2完成前四项；一项仅根和模块就作结论。缺审查、缺调研虽能阻塞，却有“文件存在/未用到”当READY的假核对。新版恢复2/2完整读协议、旧版0/2，但一项先读Git状态再补协议。两模型均没有写尝试/文件变化，不能据此抹去阅读与完成宣称失败。

公开证据：[WorkBuddy v5](agents-md-eval-2026-10/workbuddy-v5-summary.json)、[GLM v5](agents-md-eval-2026-10/zcode-glm-v5.json)。

### v6：落实用户决定，仍未通过

用户另明确两项阅读边界：CONTEXT在方案、实施或正式审查依赖领域知识时必读，纯事实提取可只读相关ADR；纯恢复先完整协议与状态/review核对，确认具体Task后才执行五项检查。v6把两个入口写到文件头，并禁止具体Task凭存在性填READY、凭未读取填N/A。未增加批准步骤，未减少V3.1-lite、成熟产品调研或逐票验证。

v6根SHA256：`235e3da800335c2266964eb6db3f14ce3dae2fbf30893a4471c6f1cb752deeb4`，32,623字节；两默认入口合计33,799字节，保留原49,782字节约67.9%。这里候选根为实测CRLF字节；Git入库LF为32,082字节，SHA256 `50eb90eee5a7653f6b27b9c0b9733fe9cbd47cecdeee9803db44247acafc95ee`。行尾转换不算新候选或规则变化，复跑仍须按冻结输入逐文件核对。完整输入见[v6清单](agents-md-eval-2026-10/manifest-v6.json)与[逐文件哈希](agents-md-eval-2026-10/input-inventory-v6.json)。

GLM关键16项原始请求中15项宿主完成、1项受到协调执行器中断，不能当模型失败或通过；同预算的新会话补测保留为第17项。17次真实首请求均取证到完整根及§16末尾，实际模型glm-5.3-flash，无600秒超时、无写尝试或文件变化。完整输入不等于正确执行：

- 新版规格入口仅1/2完成Task前四项。另一项只读取根和模块，漏Vision/Reuse/Phase及检查表，却称核对完成。
- 新版缺审查补测首行“审查完成”，后文才说阻塞、未完成；另一项首行称所需文件已读完，再报告缺失。后文BLOCKED不能抵消前面的错误完成宣称。缺审查new2及new1补测均未实际Read根正文（覆盖0），即便首请求完整自动注入已证明，仍不满足本次prelude的显式Read要求。
- 新版缺调研2/2真实完成前四项、遇缺文件阻塞，没有计划或草案；旧版两次仍给计划。
- 新版恢复2/2先完整读协议正文再读取状态正文，旧版0/2。第二次没有读取合成git-state，检查无.git及缺review ledger后保守阻塞；不能宣称Git/覆盖已验证。协议前有Glob，须与“状态先读”的失败分别记录。

[GLM v6全部工具与回答证据](agents-md-eval-2026-10/zcode-glm-v6.json)保留原中断与补测。其余16项未启动。已出现指定模型硬失败，因此暂停扩大本轮，避免用更多易通过场景稀释失败。

WorkBuddy关键16项全部结束：每版8项，旧版4项exit0/4项180秒超时，新版6项exit0/2项超时；全16项根实际完整读取，无写尝试或文件变化。新版规格入口、缺审查、缺调研各2/2实际完成相关读取和诚实检查表；缺文件首行阻塞，未用替代判据给正式结论或实施计划。新版恢复2/2协议完整且先于Tracker，但2/2预算内未交付最终报告，只算预算未验证，不能记通过或政策回退。一次Tracker整读超过256KB返回错误，不能据文件名出现算已读；另一次再次分页读取协议，模型可见截断原因尚未确证，不归因于候选文字。旧版同样有超时，不能把预算差异当作已证明的负担改善。余16项明确未启动。

[WorkBuddy v6全部工具与回答证据](agents-md-eval-2026-10/workbuddy-v6-summary.json)：313,623字节，SHA256 `90c2da2f7ab61946c62ba2fc8f6d404b2309eb8c15ad9dbb2999ca48f001f7d6`。未观察到该模型新版硬回退，不等于通过整体或数小时验收。最终独立静态两轴核对未发现P0/P1或授权扩大，确认正常Task前四项、§7/§9/Git授权保留，纯恢复边界为用户明确决策；模型可靠性与恢复完成仍未通过。

ZCode实际入口加载取证见[请求探针](agents-md-eval-2026-10/zcode-root-injection-probes.json)。直接探针仅记录布尔/哈希，未修改请求或导出认证；v5/v6首请求完整输入已实际核实。v3没有同样逐请求探针，不能追溯宣称每次均已取证。两工具均使用桌面附带引擎，未操作桌面UI；Git仍为合成记录，恢复仍为分支探针，**没有完成连续数小时真实夜间施工验收**。

### 当前交付与下一轮讨论边界

当前可审查结果是仓库内草案、485段迁移账和失败证据；不是已验收优化版。最终静态检查与git diff --check通过：旧§7的22条、§8、§9原文一致，编号/授权动作/夜间入口保留，必读目标存在，迁移全非空行覆盖且无重叠，UTF-8字节核对通过。这些结果只证明文档结构与迁移，不证明模型执行可靠。最新v6在隔离codex分支，共享D工作区仍有本次较旧草案与其他任务改动；没有用v6覆盖该工作区，没有解决他人冲突。

继续仅增加强制措辞的效果尚无证据支持。可以讨论两条方向：继续小范围修改文本并重测；或先调研ZCode/WorkBuddy是否支持仓库内、基于实际读取证据的准入检查。这只是待用户决定的范围选择，尚未授权实现运行护栏，也没有形成设计/选型实施计划。后者须先按既有要求核实成熟来源、工具能力及误阻塞边界，不能承诺两个桌面工具已可强制执行。


### v7–v9 有界收尾（2026-10-02）：候选保留，验收未通过

本轮按用户授权仅做短分支复测与文档收尾，没有实现或启用 Hooks，没有扩大到数小时夜间跑。共享桌面 ZCode 正在执行另一项实际任务，且输入框有未发送内容；仅做窗口只读观察，未点击、输入、切换项目或新建验收会话。所有模型动作均发生在仓库外一次性 fixture，不覆盖共享 D 工作区。

v7将原阻塞完整模板逐字前移至根文件头，原位置改为指针；正常 Task 入口列明四项实际阅读与五项检查表，审查入口明确成功/缺失分支。CLAUDE 同步指向文件头。v8进一步明确正常 Task 包含只读事实提取，只做静态冻结，没有启动模型。独立语义审查发现旧 CLAUDE:42 的“不得另建第二套 Engineering Specification”迁移后仅约束非主开发，属 P2 / partial；v9在§5恢复“所有 Agent”约束，并删除§7.1历史迁入括注。独立窄复验确认该缺项闭合，未增加授权、批准次数或质量豁免。

最新根工作树 CRLF：32,742字节、542行，SHA256 `84e63830ab20bf5c2b9446d96241ffcfbb73d4f88cda6f3bd22774598fb0c773`；CLAUDE 1,208字节。默认两入口合计33,950字节，约为旧49,782字节的68.2%，没有压到43%。根距32KiB仅26字节，后续增补仍须核对完整加载，不能默认有空间。Git入库LF根为32,200字节，SHA256 `92859eaaff24c8e1d3336f8acc1c3850a4e9467e8aa1ca451b9b31f2932581ca`；LF/CRLF差异不算规则变更。版本输入见[完整冻结v7](agents-md-eval-2026-10/manifest-v7.json)、[冻结v8](agents-md-eval-2026-10/manifest-v8.json)、[冻结v9](agents-md-eval-2026-10/manifest-v9.json)与各自逐文件哈希。

每轮 GLM 短对照均为两个场景 × 旧新 × 两次新会话 = 8项，预算统一180秒/32模型步骤，并发2。两轮各8项均由宿主结束，无超时；实际请求别名均为glm-5.3-flash，没有换用其他模型。模型读取与策略完成分别判定：

| 轮次/分支 | 旧版实际行为 | 新版实际行为 | 判定 |
| --- | --- | --- | --- |
| v7 规格提取 | 前四项实读0/2 | 前四项实读0/2，仍以只读为由省略 | 共同缺陷未修复 |
| v7 审查正文缺失 | 2/2首行称评审完成，使用替代判据 | 2/2首行明确任务阻塞且不称正式审查完成；一次Reuse检查表误指reference-sources | 阻塞结论改善，整场不等于通过 |
| v9 规格提取 | 前四项实读0/2 | 1/2访问四项并列检查表；另一次以根摘要充Vision、Reuse/Phase填不适用，仍宣称完成 | 存在假READY及漏读，未通过 |
| v9 审查正文缺失 | 2/2仍称评审完成 | 2/2报告阻塞、未给正式完成；一项未实际Read根，违反相同prelude，一项首行措辞/格式偏离指定模板 | 语义阻塞有改善，根实读出现退步 |

v9新版review-missing-new-1虽首请求完整自动注入已取证，却没有显式Read根（覆盖0/542），旧版两项均实际完整Read；不能用自动注入抵消本次显式Read要求。v9另一审查会话采用带head_limit的Grep，文件访问成功不能自动证明所有相关原则已读完。两轮16项均无Edit/Write/Bash尝试、无业务文件变化，原宿主配置哈希未变。这些只读证据不能抵消阅读、顺序或完成宣称失败。[v7实际工具与回答](agents-md-eval-2026-10/zcode-glm-v7-short.json)、[v9实际工具与回答](agents-md-eval-2026-10/zcode-glm-v9-short.json)保存全部八次尝试，不以执行器success评分，不删除旧失败。每轮其余24项未启动，不能声称全场景通过。

Codex CLI 0.145.0 的 GPT-6-Luna 只尝试一次前置烟测：使用gpt-6-luna、只读sandbox、ephemeral及完整根加载预算，服务端返回HTTP400：`The 'gpt-6-luna' model is not supported when using Codex with a ChatGPT account.`，exit1，未开始实际模型工具执行；没有改用其他模型。这是CLI入口限制，不证明用户桌面GPT-6-Luna不可用，也不是旧新对照。[原始错误与入口记录](agents-md-eval-2026-10/codex-luna-v9-preflight.json)保留失败。

本轮实际启动依据记录在本文，不新增检查表文件：

| 前置项 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision | 00_PROJECT_VISION §2.5/§3 | READY |
| 当前任务规格 | 用户确认的规则治理范围、本文基线与迁移账；不推进产品模块实现 | READY |
| Reuse | 13_OPEN_SOURCE_REUSE_MATRIX §1；复用现有SDD/审查/验证文档 | READY |
| Phase | 14_IMPLEMENTATION_ROADMAP 执行原则；本任务为规则治理、不推进产品Phase | READY |
| 触发细则 | 完整SDD协议1–893行、Independent Review正文、writing-for-agents/code-review方法；读取截断部分已补读 | READY |

485段机械覆盖、归一哈希、关联目标和独立语义核对已完成；上述partial已单列修正。22条不变量、§8、§9原文、V3.1-lite完整阅读、成熟产品调研、逐票验证及Git授权均保留。静态与diff检查只证明这一候选可供审查，不能证明弱模型可靠执行。最终候选仍在隔离分支，未push/PR/merge，不覆盖其他任务。

剩余验收：桌面ZCode的两种指定模型实际旧新对照、桌面Codex GPT-6-Luna旧新对照，以及多Issue连续运行/压缩恢复/逐票证据/发布待批准场景。原WorkBuddy补充验收及历史未通过项仍保留。当前结论是“文档与短测收尾完成，优化行为验收未通过”；不得把v9交给夜间任务当作已验证版本。进一步强制机制仍需另行明确范围，本轮未实施。


### 外部引擎复测复核与提示歧义探针（2026-10-02）

用户提供其他Agent报告后，核对三个JSON和600秒原始记录。复核版见[外部报告](agents-md-eval-2026-10/external-engine-report-reviewed-2026-10-02.md)，外部原始公开导出见[GLM](agents-md-eval-2026-10/external-zcode-glm-v9-desktop.json)、[DeepSeek180秒](agents-md-eval-2026-10/external-zcode-deepseek-v9-desktop.json)、[DeepSeek600秒](agents-md-eval-2026-10/external-zcode-deepseek-600-v9-desktop.json)。虽文件名含desktop，全部仍是app-server随附引擎；桌面GUI未完成。共20次、15次宿主完成/5次超时，不是原报告汇总的24次。600秒导出run_settings误标180，原始四项均budget_seconds=600；保留错误导出并单列纠正，不覆盖旧尝试。

外部结果支持规格阅读改善（GLM新版1/2 vs旧0/2，DeepSeek新版2/2 vs旧0/2）及缺审查时停止正式结论的改善；不支持“两个预算全部通过”、全部首行合规或“慢不是候选文字造成”的推断。GLM一次以“审查完成核查”开头，DeepSeek一次先输出标题再阻塞；后文阻塞不能自动抵消首行要求。未测场景是覆盖缺口，不是已观察到的模型硬失败。

用户同意修正报告并调查提示歧义，明确真正桌面会话交给用户安排其他Agent。本轮未改变候选AGENTS/CLAUDE，仅新增报告、提示输入与证据，未操作ZCode GUI。GLM spec-route实验保持同一旧/v9冻结根、模型、180秒/32步骤/并发2，采用2提示×2根×2次新会话=8次；原提示保留“入门五份规格检查已在前置阶段完成”，中性提示只取消该断言，未增加Vision/Reuse/Phase预期答案。

| 提示条件 | 旧根相关前四项访问 | v9相关前四项访问 |
| --- | --- | --- |
| 原提示，同轮重新采集 | 0/2 | 1/2 |
| 中性提示 | 0/2 | 2/2 |

实际返回章节与新版检查表另核对：中性new-1全文Vision/模块、相关Reuse检索与Phase正文读取，中性new-2相关Vision/Reuse/Phase检索及模块全文；均未用N/A代替前四项。原提示new-2仍以只读/前置检查为由漏三项并称完成。8次均宿主完成、无超时，无写尝试/业务文件变化，根显式Read完整，配置哈希未变，实际请求glm-5.3-flash。[全部8次实际工具与回答](agents-md-eval-2026-10/zcode-glm-v9-prompt-control.json)及[冻结实验提示](agents-md-eval-2026-10/prompt-control-input-2026-10-02.json)保留全部尝试。

这只是2次重复的线索，不能证明稳定修复、因果或整体规则通过；不把提示收益算成AGENTS进一步优化收益。DeepSeek中性提示未重跑、review-missing未纳入此提示实验、其余分支及夜间长跑未执行。文案与根字节未改，不新建v10规则。

真正桌面任务已写成[给用户安排其他Agent的交接](../archive/handoffs/2026-10-02-zcode-desktop-session-test-task.md)：先实际GUI跑GLM8次提示×根对照，有硬失败先报告；短分支通过后按交接补缺审查和DeepSeek。禁止用CLI/app-server替代GUI，未取得证据写未验证。候选仍未通过整体验收，未push/merge。
