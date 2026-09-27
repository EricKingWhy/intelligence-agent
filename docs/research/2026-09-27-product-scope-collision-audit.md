# 产品方向与现有票据碰撞审计（2026-09-27）

> 盘点 GitHub Issue、现有 PRD/规格及代码树；用于 grill、PRD 与拆票去重。产品票建立前的相关 OPEN Issue 快照为 12 张（见表）；#302 已关闭，新出现 #341/#342。文档最初基于 `063487a`，随后核对并同步到 `origin/main` commit `1774f4fb`。该最终基线包含 #342 经 PR #369 合入的并发恢复修复；#341 仍 OPEN。产品票为父 #344 与 24 个子 Issue #345–#368。

## 已确认的用户方向

- 首款产品面向需要完成复杂修复并审阅证据的开发者/小团队；保留通用 Python/Async Core。
- 首个入口是复用现有 Web/TS 界面的桌面薄宿主，同时保留简洁、Pi 式 CLI。
- 用户希望完成判断有测试、真实操作、diff 审阅证据；本机操作采用按范围授权、高风险审批、未知副作用先裁决。
- “首字慢”留到后续性能优化，不进入本轮 PRD/票据。
- 第三轮 Q16–21：首版 Docker 仅作为现有隔离执行环境；不接入其他桌面软件控制。产品任务对应一个 Session（可含多个 Run），Fork 保留来源关系。桌面关闭窗口缩到托盘，明确退出停止服务，重启后待用户决定继续。TUI 采用 Pi 式终端滚动交互。现有 Chrome 登录态优先复用官方 Chrome DevTools MCP。Web API 默认审批与桌面/TUI 一起做安全迁移。

## 当前 OPEN Issue 的归属

| Issue | 已占有的范围 | 对新产品票的边界 |
| --- | --- | --- |
| [#305](https://github.com/EricKingWhy/intelligence-agent/issues/305) | 长任务分层预算、暂停恢复、stuck、可靠 `run/completed`、CLI/Web 投影的父 PRD | 不重写预算、暂停和通用 CompletionPolicy。产品“已验证交付”与 Runtime 终态必须先澄清是否为不同概念。 |
| [#317](https://github.com/EricKingWhy/intelligence-agent/issues/317) | stuck 指纹、一次 replan、证据化恢复 | 不另建第二套卡死检测或“继续”按钮逻辑。 |
| [#318](https://github.com/EricKingWhy/intelligence-agent/issues/318) | 跨 run/fork 的 SessionBudget/委派树持久化 | 不再建另一套预算账本。 |
| [#319](https://github.com/EricKingWhy/intelligence-agent/issues/319) | 五个真实长任务 Live Gate 场景，最终树每项 3/3，CLI/Web 对账 | 新挑战只补摘要失败后重载、跨窗口接班、真实用户操作和产品证据；五场景仍由 #319 验收。 |
| [#320](https://github.com/EricKingWhy/intelligence-agent/issues/320) | 证明迁移安全后移除 `max_steps` alias | 新 CLI 票不得另起兼容性移除。 |
| [#337](https://github.com/EricKingWhy/intelligence-agent/issues/337) | 重启后陈旧审批 fail-closed 结清 | 新恢复票不得再实现该路径；桌面恢复体验只能消费其结果。 |
| [#296](https://github.com/EricKingWhy/intelligence-agent/issues/296)、[#303](https://github.com/EricKingWhy/intelligence-agent/issues/303)、[#304](https://github.com/EricKingWhy/intelligence-agent/issues/304)、[#338](https://github.com/EricKingWhy/intelligence-agent/issues/338) | Memory V2 及其迁移、真实 Gate、间歇红；#302 已关闭 | 产品工作台不能把 Memory V2 重包装成新基础设施票；相关测试状态仍需在集成时对账。#303 明确不得删除 SessionEvent、Artifact、workspace、凭证。 |
| [#341](https://github.com/EricKingWhy/intelligence-agent/issues/341) | WebSocket relay cleanup / detached-run shutdown 加固 | 新客户端生命期票只消费修复结果，不重复实现 relay 清理。 |
| [#342](https://github.com/EricKingWhy/intelligence-agent/issues/342) | 并发 resume CAS SeqConflict / `run/completed` 缺失修复 | 共享服务、并发续跑与产品交付投影应把它列为相关验收前置，不另开同一故障修复票。 |

## 已关闭票与当前规格仍要尊重

- [#133](https://github.com/EricKingWhy/intelligence-agent/issues/133) 已交付 Pi/oh-my-pi 风格多轮 REPL 和 slash 命令；[#137](https://github.com/EricKingWhy/intelligence-agent/issues/137) 已交付模型切换/Fork，Phase 9/14 已有 CLI Renderer、fork/replay。现有[CLI 入口](../../src/agent_harness/cli.py)是 argparse 命令，[REPL](../../demo/live_agent.py)仍在 demo；该 checkout 的 `/compact` 与 `/cancel` 仍提示“尚未实现”。因此未来只可为**实测缺口、产品化入口与跨客户端一致性**开窄票，不可再开一套“做 Pi CLI”。
- [Engineering Spec 11 §3/§6.1](../../goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/11_STREAMING_API_WEB_UI.md)已要求 CLI 第一公民，SessionEvent/服务端投影是 CLI 与 Web 状态的唯一权威。[多轮 PRD §7](../PRD_ENTERPRISE_MULTI_TURN_SESSION.md)已列出 `/new /resume /fork /compact /model /history /cancel /clear /help`。
- [ADR-0044 D6](../adr/0044-long-run-execution-boundaries-budget-pause-resume-stuck-completion.md)固定：通用 Runtime 在 quiescence 后可接受最终模型答复并落 `run/completed`；领域策略可增加证据。若把 Q8 的“测试+实际操作+diff”强制改成**每个通用 run** 的终态前置条件，会实质改变 #305 / 已完成 T8 的契约，不能在桌面 PRD 里暗改。
- [Reuse Matrix 13 §2](../../goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md)已把 Pi TypeScript runtime 标为 DEFER，且禁止整仓 fork/翻译。Pi [官方仓库](https://github.com/earendil-works/pi)采用 MIT 许可，但也[明确没有内建权限系统](https://github.com/earendil-works/pi#permissions--containerization)。可借用兼容的独立 UI 代码并保留许可证与来源；现有 Python Core、ToolExecutor、Ledger 不由 Pi 接管。

## 可新增但尚未拍板的边界

1. **桌面宿主与生命周期**：现有 OPEN Issue 没有覆盖桌面包装/托盘/本机服务管理；需要按已决定的进程所有权、CLI 并发入口、窗口关闭与重启语义落实最小本机资源桥。参考 [DeepSeek Harness Desktop](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)；它复用 Web 应用并让后台任务在窗口隐藏后继续。
2. **产品交付证据**：须先区分 `run/completed` 与“已验证/待审阅/用户接受”的产品语义，再定义可审计事实和投影；不得让 UI 的局部状态成为第二套 Session 真相。
3. **摘要失败后重载丢约束**：已有[挑战提案](2026-09-27-long-task-resilience-challenge.md)记载可复现缺口。原 Phase 5 compaction 覆盖当轮 fallback，但此跨持久化投影问题可成为独立、窄范围 Bug 票，不能重开整个 Context 系统。
4. **补充长任务挑战**：现阶段是研究提案，尚无夹具或 Gate 结果；与 #319 现有五场景互补，不把它冒充已完成验收。

## 待 grill 的决定

2026-09-27 第二轮已决定：

- 用户 Q10=A：`run/completed` 与产品交付状态分离；模型/Runtime 可结束，但成果在证据不足时显示“待验证”，证据齐备才“可交付”，用户接受后“已接受”。产品状态的具体持久化/投影契约与缺项处理尚待 grill，不擅改 #305 的通用 CompletionPolicy。
- 用户 Q11=A：桌面与 CLI 共用一处本机 Python 服务，同一 Session 由它唯一写入；这直接回应[领域词汇表](../../CONTEXT.md)的并发 seq 风险。
- 用户 Q12=B：CLI 要 TypeScript TUI，成熟产品除了 Pi 还要看 Cline、oh-my-pi。现有 Python 命令入口及 REPL 不删除；TS TUI 只能作为 Core 的客户端，不得复制 Pi 的 Runtime/Session/权限链。组件库/代码复用取舍待查官方源与许可证。
- 用户 Q13=A：首版必须使用现有 Chrome 登录态。独立内置浏览器不会自动获得 Chrome cookies；[Codex 官方说明](https://help.openai.com/en/articles/20001277-using-the-built-in-browser-in-the-chatgpt-desktop-app)采用 Chrome 扩展连接既有资料。
- 用户 Q14=A：Windows 为首版交付平台。
- 用户 Q15：首个非浏览器本机应用是 **Docker**。本仓已有 `DockerSandbox` 与恢复测试，它是 Agent 的**执行环境**；用户所说“操作 Docker”还需精确区分查看/管理 Host Docker Engine、项目 Compose 操作、还是点击 Docker Desktop GUI。后续新票不能把已有 Sandbox 重新发明一遍。

2026-09-27 第三轮已决定：

- 用户 Q16=A：Docker 仅作本仓已有的隔离执行环境；Host Docker Engine/Compose 管理与 Docker Desktop GUI 自动化均属后续计算机操作阶段。不得为首版新开 Docker 管理能力票。
- 用户 Q17=A：产品层一个任务对应一个 Session，同一 Session 可跨多个 Run；Fork 是带来源关系的新 Session。交付证据和审阅状态的事实/投影仍需细化。
- 用户 Q18=A：桌面关窗缩到托盘且任务继续；显式退出停止服务；Windows 重启后先恢复可审阅状态，执行续跑等用户决策。现有 RunManager orphan grace 和 shutdown 行为须在实现票中对账。
- 用户 Q19=A：首版 TypeScript TUI 采用 Pi 式终端滚动记录、编辑器、工具卡、会话切换、审批和证据入口；diff 可调用外部查看器或桌面端。Pi 的独立 TUI 组件是 REUSE 候选，须实测 Windows 和审计依赖/许可。
- 用户 Q20：优先直接复用 Google [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) 作为可选 MCP Capability。其[高级用法](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/advanced-usage.md)确认默认启动专用 profile；Chrome 144+ 的 `--autoConnect` 可连接用户已运行的 Chrome 并复用登录状态，但会获得所选 profile 的全部打开窗口访问权。Host 权限与 ToolExecutor 审批范围不能由 MCP 本身代替；不得承诺它原生只访问选中标签页。手动 remote-debugging-port 路径更暴露整个浏览器且 Chrome 要求非默认资料目录，不作为首版默认。
- 用户 Q21=B：旧 Web API 的 `auto_approve=True` 等默认配置须纳入统一安全迁移和兼容性验收；桌面/TUI/Web 以同一权限策略运行，不只修新客户端。

下一轮的未决前沿：产品交付证据缺项与验收裁决、Chrome MCP 可选配置和连接现有 profile 的信任边界、桌面显式退出/重启后的继续动作、TUI 与本机服务冷启动的拥有者、Web 旧默认值的迁移兼容性。

2026-09-27 第四轮已决定：

- 用户 Q22=A：Chrome DevTools MCP 是可选插件；未连接时开发任务仍可运行，需要浏览器的验证显示缺项。
- 用户 Q23=A：现有 Chrome 的 MCP 连接由用户手动启用，明确告知它能看到所选资料的所有窗口；每次工具调用仍由现有 ToolExecutor 权限、审批与审计覆盖，敏感站点及有副作用表单要额外确认。
- 用户 Q24=A：以任务的可审阅验收项、真实验证、对应代码版本及 diff 作交付证据；用户强调 V3.1-lite 已有该流程。核查 `docs/SDD_WORKFLOW_PROTOCOL.md` §2/§3/§8.8 后，现有开发 Ticket 的验证、review ledger、冻结树、Gate-0、真机 Runtime Verification 和 Live Gate 证据**确已落地**；后续产品票须复用其流程与数据结构，只对“任意用户任务的证据关联、跨窗口投影与审阅 UI”等实际缺口做最小增量，不再建第二套门禁或泛化证据系统。
- 用户 Q25=A：缺少证据时保持“待验证”，指出缺项/失败/重试；用户可显式接受未完全验证的结果并留下原因，但系统不可记为验证通过。
- 用户 Q26=A：Windows 重启后先 reconcile；对 UNKNOWN 先查询工具及外部系统真实状态，只有仍无法裁定时才请求用户判断，不盲重跑。
- 用户 Q27=A：TUI 可以独立冷启动唯一的本机 Python 服务；退出 TUI 后任务及服务继续运行；桌面端后开时连接现有服务。
- 用户 Q28=B：同仓并行写任务用独立 Git worktree；要求调研成熟产品并优先复用设计/代码。GitHub Copilot app 官方提供每 Session 独立 workspace/branch/worktree，Codex app 用 worktree 隔离运行、保持 diff 可审阅。注意冻结 [05_SANDBOX_CODING_TOOLS.md §7](../../goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/05_SANDBOX_CODING_TOOLS.md) 明言“V1 不做多 worktree 并行 Coding Team”；必须由用户明确决定把本能力排在后续产品阶段并修订对应规划，或变更 V1 范围，不可直接在实现票内悄悄扩范围。

### Q28 复用与代码现状核查

- 成熟产品：[GitHub Copilot app](https://docs.github.com/en/copilot/how-tos/github-copilot-app/agent-sessions)每 Session 可选独立工作树；[Claude Code worktrees](https://code.claude.com/docs/en/worktrees)提供 worktree 创建、恢复、清理约束；[Codex worktree 模块](https://github.com/openai/codex/tree/main/codex-rs/worktree/src)为 Apache-2.0 Rust，但耦合内部 crate；[Cline git-worktree.ts](https://github.com/cline/cline/blob/main/apps/vscode/src/utils/git-worktree.ts)为 Apache-2.0 TypeScript/simple-git，可借鉴 porcelain 解析与界面流程；[oh-my-pi worktree.ts](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/task/worktree.ts)为 MIT TypeScript，但耦合其 VCS/native/task runtime。首选 `REUSE` [Git 原生命令](https://git-scm.com/docs/git-worktree)（Python Core 无 shell 插值调用），上述客户端实现主要 `PORT DESIGN`；若实质移植必须逐模块许可和依赖审查，不能为复用而拉入第二套 Agent Runtime。
- 仓库当前 `src/agent_harness` 无任务 worktree manager。[SessionService](../../src/agent_harness/session/service.py)把既有绝对 `cwd` 绑定至 SessionEvent；[WorkspaceRegistry](../../src/agent_harness/sandbox/registry.py)记录 Session→root 与恢复映射。现有 [Fork](../../src/agent_harness/session/fork.py)通过 `shutil.copytree` 复制父目录；若父目录为 linked worktree，复制 `.git` 指针会指回旧树，不能直接视为新独立分支。现有 WorkspaceRegistry 清理保护用户目录；新托管 worktree 必须独立记所有权并保留未提交状态/证据再清理。
- 首版 Windows 路径需验证盘符/UNC/大小写/junction、锁文件、Git hooks/filters 的副作用、非 UTF-8 终端、submodule/ignored files。工作树路径应由 Python Core 作为 Session 的权威 cwd 绑定，TS TUI/桌面仅请求和展示。

### 第五轮决策与首版简化提议

- 用户 Q29=A：自动 worktree 属 V1 之后产品阶段，先有范围地修订规格/路线图，保留 V1 历史验收语义。
- 用户 Q30=A：后续自动隔离只默认用于 Git 开发任务；非 Git 与纯问答走普通 Session 目录。
- 用户 Q31=A：后续新 worktree 默认从用户选定 checkout 的 HEAD 创建，记录精确 SHA，允许改选 ref。
- 用户 Q32=A：原目录未提交改动默认不复制，显示遗漏范围，用户可显式选择导入指定改动并记录来源。
- 用户 Q33=A：任务结束仍保留 worktree，用户显式归档时先保存可恢复快照并检查脏文件/分支/证据，再清理。
- 用户随后主动提出首版降阶：同一工作目录最多一个写入任务；第二个任务排队或由用户指定独立工作目录。当前建议接受该方向，以降低 Session cwd、copy-on-fork、恢复映射、Windows 文件锁与 Git 清理的联动风险；Q30–33 作为后续 worktree 阶段的设计输入。**首版降阶尚待用户明确确认**，不能把 Q28=B 静默改写为已决定。首版队列仍须定义独占期、恢复与取消语义。

### 第六轮决策：首版并发与客户端退出

- 用户 Q34 确认首版降阶：同一工作目录最多一个写入任务，后续任务排队或由用户指定独立目录；自动 Git worktree 移至下一阶段，Q29–33 保留为该阶段已选设计。此前 Q28=B 不作为首版验收。
- 用户 Q35=A，并补充：窗口关闭缩托盘仍算桌面客户端在场；TUI 是唯一客户端时，明确退出 TUI 后任务可停，以免无人监督消耗 token；桌面和 TUI 同时存在时，只要任一客户端仍在，任务不可因另一客户端退出而停止。此决定**覆盖**此前 Q27=A 所述“TUI 退出后服务与任务继续”；TUI 独立启动服务的能力仍保留。运行任务在最后客户端退出时具体记为暂停或取消、连接异常是否等同明确退出，待下一轮裁决。当前 [RunManager](../../src/agent_harness/session/runmanager.py) 零订阅者 300 秒后把 run 记 `run/failed(reason=orphaned)`；[AppState.shutdown](../../src/agent_harness/web/app.py)关闭在途任务。这些现状不可直接冒充用户所定客户端生命周期。
- 用户 Q36=A：用户接受决定与验证结论分轴记录，可展示“已接受 · 验证未完成”；缺项和接受原因须持续可见，不能伪称验证通过。

### 第七轮决策：暂停、客户端在场与工作目录占用

- 用户 Q37=A：最后一个客户端明确退出时安全暂停，停止新模型调用，持久化进度；在途工具按 Operation Ledger 收尾或待 reconcile，下一次由用户决定继续。不可把退出映射成 `run/failed(reason=orphaned)` 或粗暴杀进程。
- 用户 Q38=A：意外断线给短暂重连窗口，期间不发起新模型步骤；超时安全暂停，重连后显示实际中断事实，不自动续跑。
- 用户 Q39=A：桌面窗口/托盘、TUI、连接本地服务的 Web 页面都计作客户端；每个连接分别登记，关窗缩托盘仍在场。具体 Session 关联方式待下一轮澄清，以免某个客户端在场时无意让所有任务继续耗 token。
- 用户 Q40=A：一个产品任务在完成、归档或用户明确释放之前持续占有同一工作目录的写入权；排队任务不能趁其暂停/待审批时写入。这里“完成”是 Runtime `run/completed`、交付可审阅还是用户接受，仍须精确定义。
- 用户 Q41=A：用户提供的验收项优先；缺少时 Agent 提议可验证清单并展示。普通低风险任务可推进，关键目标模糊或高风险操作需用户确认；验收项变更留记录。该产品态须复用 V3.1-lite 已有 Ticket 验收/门禁概念，不另建等价工程流程。

### 第八轮决策：同目录交付边界与队列

- 用户 Q42=A：`run/completed` 不释放工作目录；验收证据未齐、diff 待审阅时仍由原任务占有。直到用户接受、归档或明确释放目录，排队任务才可写。
- 用户 Q43=A：客户端在场按产品任务/Session 计算；TUI 看任务乙不会维持任务甲的模型执行。桌面托盘继续维持桌面端明确托管的任务。
- 用户 Q44=A：同目录等待队列及次序需持久化；前一任务释放后，下一任务有在场客户端才自动启动，否则停在待用户回来状态。此规则是结合 [DeepSeek Harness Session 排队](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/session.md)与 [Codex 重连时待审查排队提交](https://learn.chatgpt.com/docs/changelog)的本项目推导，不应误称成熟产品已有完全相同的跨任务目录队列。

### 第九轮决策：渐进式压缩与任务交接

- 用户 Q45=A：目标、用户约束/授权、验收项、精确标识、决策、未完成项、工具副作用及证据出处不能仅托付给 LLM 摘要；以带原事件引用的结构化 Session 事实保全，摘要负责叙述和选取。不可变成长时 Memory V2 的平行事实源。
- 用户 Q46=A：旧 Tool Result 只有在完整内容已保存、引用可回读时才能从 Runtime Context 投影中确定性裁剪；Tool call/result 配对、必要结论与错误信息须保留。沿用现有 ArtifactStore；参考 Pi/oh-my-pi/DeepSeek 的裁剪设计，不能直接移植其可变 session store。
- 用户 Q47=A 并细化：摘要失败后做有界重试；再次失败须明确显示失败原因。结合本仓硬护栏，不能提交空/不合格摘要，也不能无限循环重试或越窗继续。重试次数、阈值以下的继续策略和硬护栏暂停的 UI 文案待票面明确。
- 用户 Q48=A：Artifact 保存失败时不伪造引用；暂保原始输出/尝试安全裁剪，触到硬护栏仍放不下即暂停并告知存储/输入问题。
- 用户 Q49=A：任务进度、下一步、阻塞、验证事实由 SessionEvent/确定性投影追溯；**另有可读进度文件作为跨窗口保险**。文件应包含原始目标、约束、任务状态、坑点、关键证据结论和未完成项，以防数小时运行后指令漂移。文件位置、更新点、版本/冲突校验和重启时注入规则待下一轮决定。

### 第十轮决策：进度文件机制

- 用户 Q50 在 A/B 均可中明确偏好 **B：默认放项目根目录可见**。具体是根目录单文件还是根目录下任务专属文件、Git 跟踪方式仍待定；不能按先前推荐 A 默认藏入应用数据目录。
- 用户 Q51=A：目标/约束确认时初始化，在可验证里程碑、关键决策、失败结论、压缩前、暂停前、交付前更新；原子写入、带版本。
- 用户 Q52=A：SessionEvent 用户指令和确认决定优先；进度文件携带来源 seq/版本，外部修改造成的差异显式呈现，不能静默改写约束；用户经产品界面修改须追加新事实后再生成文件。
- 用户 Q53=A：新任务初始化后、重启恢复、压缩之后、交付前，Agent 必须读取并核对进度文件与 Session 事实；读取失败需可见，不许凭记忆宣布完成。
- 用户 Q54=A：摘要有界重试后仍失败，显示原因、不提交压缩、在安全窗口内保留原投影继续；触及硬护栏仍无法安全缩小时暂停。

### 第十一轮决策：进度文件项目化

- 用户 Q55=A：项目根目录 `agent-progress/<session-id>/progress.md` 每 Session 一份；任务名与路径在界面可见。避免后续任务覆盖前一任务进度。
- 用户 Q56=B：进度文件默认视为项目文件，应能进入 Git diff 和后续提交。**此处尚未授权自动 `git add`、自动 commit 或 push**；Git 索引行为、文件敏感信息和忽略规则待进一步明确。现有 Spec 05 的 Git Tool 默认只读不得因“可提交文件”被悄悄扩成自动提交。
- 用户 Q57=A：Fork 从明确的父会话事件位置生成子任务独立进度文件，记录来源；后续父子各自更新，不共享一个可变文件。

### 第十二轮决策：首版形态与验证

- 用户 Q58=A：进度文件出现在项目 diff 与审阅界面，但系统不自动 `git add`；是否纳入提交由用户明确选择。Q56 的“可进入 Git”指它作为项目文件可被跟踪，不代表自动加入索引。
- 用户 Q59=A：Windows 首版采用 Electron 薄宿主，复用现有 React/Web UI，并连接唯一的本机 Python 服务；Python Core 仍拥有 Session、权限、ToolExecutor 与恢复语义。
- 用户 Q60=A：首版差异化聚焦跨窗口目标/进度/证据/副作用 reconcile；桌面控件广泛自动化与插件数量不是首版竞争指标。
- 用户 Q61 修正产品对象：首版主要供用户**个人使用**；小团队多人协作明确留待后续，首版 PRD 与新票不得提前加入共享任务、多账户或实时协作。Git/diff/证据可用于人工交接，但不构成多人服务端需求。
- 用户 Q62=A 且强调：验收以**真实模型驱动的完整运行、真实 UI 操作与真实恢复链**为主；固定样例/故障注入只负责可重复地触发故障和核对结果，不得以纯模拟通过替代真实模型 Gate。费用、运行次数及失败证据门槛待后续票面明确。

### 第十三轮决策：Windows 交付与本机边界

- 用户 Q63=A：首版交付可安装的 Windows 应用，安装包内含 Python 服务和 Web 静态资源；安装后无需用户预装 Python/Node 或从源码运行。开发期仍可源码运行；安装、启动、卸载须验证。
- 用户 Q64=A：Windows 重启后由用户手动打开应用；打开后先恢复任务视图并 reconcile，续跑仍需用户选择。首版不设置登录自动启动。
- 用户 Q65=A：桌面共享服务默认仅本机 loopback 且连接鉴权；既有远程 Web 若保留，作为独立部署入口处理，不让个人桌面服务默认开放局域网。
- 用户 Q66=A：复杂长任务挑战至少两次**独立真实模型完整运行**，覆盖中断恢复、上下文压缩/跨窗口接班、真实 UI 验证与 diff 审阅；固定 fixture 和故障注入仅用于稳定复现，不能代替实跑。不得覆盖 #319 独有的五场景门禁。
- 用户 Q67=A：桌面、TUI 与 Web 复用现有模型配置/凭证入口及服务端状态；未配置时引导配置，不在进度文件或前端普通存储中复制密钥。首版不创建第二套账号系统。

### 第十四轮决策：交付物、旧数据、锁与发布 Gate

- 用户 Q68=A：Windows 安装包同时提供 TUI 命令及运行所需依赖；即使桌面窗口/进程未打开，TUI 也可冷启动或连接唯一的本机 Python 服务。须在未预装 Node/Python 的 Windows 环境验证。
- 用户 Q69=A：安装桌面版后原位复用已有 Web 会话、模型配置和证据；需要数据迁移时先备份，迁移可重试/回退，保持唯一 Session 真相并对账 `InstanceLock`。
- 用户 Q70=A：创建带写入意图的任务时就排他占有工作目录；同目录第二写入任务排队或由用户指定独立目录。只读/纯问答任务不占写锁；原任务的锁直到用户接受、归档或明确释放才结束。
- 用户 Q71=A：手动安装更新前先列出受影响任务并安全暂停；在途 Tool 按 Ledger 收尾或待 reconcile。暂停失败中止安装；新版打开后先 reconcile，由用户决定续跑。
- 用户 Q72=A：长任务真实模型挑战若任一次失败，修复后须重新取得至少两次独立的完整通过；保留失败证据，片段重测不计入完整通过次数。

### 第十五轮决策：首版安全默认值与拆票

- 用户 Q73=A：桌面/TUI/Web 共用统一权限默认值：在已选工作目录内普通读取与编辑可继续；破坏性命令、越界写入、敏感站点和有副作用 MCP 调用逐次通过 ToolExecutor 审批。旧 Web 的默认 `auto_approve=True` 需安全迁移，不可通过前端提示代替 Runtime 拦截。
- 用户 Q74=A：Windows 未装 Docker 仍可在**显式选择**现有本机 Sandbox 后完成普通任务；界面标明隔离差异，Docker 任务缺依赖即显示缺项，禁止静默从 Docker 降级到本机。
- 用户 Q75=A：首个个人使用的 Windows x64 安装包允许未签名，但须有构建来源、SHA-256 和干净 Windows 安装/启动/卸载实测；面向公众的签名发布是后续阶段。
- 用户 Q76=A：任务证据、日志和 artifacts 首版不按固定天数自动删除；展示占用空间，用户显式清理/归档前列出将失去的引用并按可恢复规则处理。
- 用户 Q77=A：一张产品父 Issue + 按可独立验证结果拆子 Issue/Ticket；每张票自包含现状、源码边界、依赖、成熟产品/代码参考、Reuse Matrix 类别、异常路径、AC、真实验证及不做范围。既有 #305/#319 等作依赖和去重边界。

用户已明确授权结束拷打、生成详细 PRD、tickets 与 GitHub Issues。后续遇到普通实现细节由票面作合理确定；新证据若触及冻结架构/旧票实质变更，则按 AGENTS.md §9.1.1 单独处理。

生成票面后发现冻结 Spec 03 §3.4/§5 将 `run/paused` 限于 budget/deadline/stuck，旧孤儿回收为 `run/failed(reason=orphaned)`，与 Q37 冲突。用户随后**单独批准**：仅为个人工作台后续阶段增加 `client_absent`/`client_return`，定向修订 Spec 02/03/11（ADR-0046），W-22 为 W-12 前置，#305 原验收与旧入口语义不改。另补 W-23 创建任务 UI、W-24 证据保留清理，覆盖原 PRD 承诺而不扩大产品方向。

## 第三轮前的事实核查（只读）

- **Docker 的两种不同角色**：现有 [DockerSandbox](../../src/agent_harness/sandbox/docker.py)负责 Agent 在自身受管容器中执行 Coding Tools，[#3](https://github.com/EricKingWhy/intelligence-agent/issues/3)、[#24](https://github.com/EricKingWhy/intelligence-agent/issues/24)、[#129](https://github.com/EricKingWhy/intelligence-agent/issues/129) 已交付与恢复验证；[Web AppState](../../src/agent_harness/web/app.py)当前却固定使用 `backend="local"`。仓库没有让 Agent 列举/管理用户 Docker Engine 容器、镜像或 Compose 项目的专用 Tool，也没有 Docker Desktop GUI 自动化。Docker 官方提供 [Engine API 与 Python SDK](https://docs.docker.com/reference/api/engine/)和[Compose CLI](https://docs.docker.com/reference/cli/docker/compose/)；选哪种操作面之前不能立管理票。
- **Docker 权限**：用户已选按范围授权，但当前 [Web 会话请求](../../src/agent_harness/web/app.py)缺省 `permission_mode="workspace-write"` 与 `auto_approve=True`。Docker 官方[提醒 Docker daemon 凭证可赋予对宿主的高权限](https://docs.docker.com/engine/security/protect-access/)；用户 Docker 资源管理不得通过把 socket 或广泛 Docker CLI 权限交给未受控沙箱来绕过 ToolExecutor。桌面/TUI 的新默认是否连带改变旧 Web 默认是待决迁移问题。
- **本机服务生命期**：现有 [Web lifespan](../../src/agent_harness/web/app.py)有同一数据根 `InstanceLock`；[AppState.shutdown](../../src/agent_harness/web/app.py)会关闭在途 detached runs。共享服务已选定，但窗口关闭、显式退出、Windows 注销/重启与更新时的服务归属仍需定义。
- **TS TUI 复用候选**：[Pi 独立 TUI 包](https://github.com/earendil-works/pi/tree/main/packages/tui) MIT，提供 Editor/Markdown/SelectList/ScrollView 等组件，可作为薄客户端 `REUSE` 候选；它有 [Windows 原生辅助模块](https://github.com/earendil-works/pi/blob/main/packages/tui/src/native-platform.ts)，打包须实测。[oh-my-pi](https://github.com/can1357/oh-my-pi) 的 TUI 包依赖其 Agent/Core/AI/catalog 等内部包，整体复用会带来第二套 Runtime，优先只借鉴交互或隔离复制兼容组件。[Cline CLI](https://github.com/cline/cline/blob/main/apps/cli/README.md)用 OpenTUI/React 做全屏 TUI，另提供 one-shot、NDJSON 和后台模式；适合参考多入口与工具/审批展示，直接复制组件需连同依赖和 Apache-2.0 声明评估。三者的 Agent Loop、SessionStore 与默认审批策略均不接管本项目 Core。
