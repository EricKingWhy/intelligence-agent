# 集成 AI 执行提示词：W 批 frontier 四票单线顺序施工（W-22 → W-11 → W-07 → W-05）

## 0. 使命与范围
你是本批唯一执行 Agent，顺序完成 EricKingWhy/intelligence-agent 的四张 issue：#366 [W-22]、#355 [W-11]、#351 [W-07]、#349 [W-05]。四票目标仓库均为 intelligence-agent-backend。一次一票：当前票达到「本地门禁全绿 + 分支就绪 + 记账落盘」后才领下一张。不要领本批之外的 W 票（其余 W 票均有 open 阻塞者，见附录 B）。四票彼此无原生依赖，但集成一次一条线，顺序按 §5。

## 1. 开工前必读（先读后动）
- docs/SDD_WORKFLOW_PROTOCOL.md 全文（V3.1-lite）与 docs/SDD_TICKET_TRACKER.md 当前态；
- 根 AGENTS.md（§13 仓库模型、§14 Git 授权与门禁、§16 入口）；
- docs/agents/git-workflow.md、docs/agents/verification.md §1–§2、docs/agents/implementation-discipline.md、docs/agents/issue-tracker.md；
- 父 spec 票 #344 正文（gh issue view 344）、docs/PRD_RECOVERABLE_REVIEWABLE_AGENT_WORKBENCH.md、docs/tickets/workbench-2026-09-27/ 对应票面；
- 每票的模块规格与 ADR（附录 A）、SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md 相关段与票面「成熟参考/复用」块。
上下文被压缩或恢复时，第一动作执行协议自愈条款：重读协议 + Tracker + 核对 Git，不凭记忆继续。

## 2. 每票执行闭环（重复四次）
1. 接票：gh issue view <n> --comments 核对 AC；核对代码、测试、分支与工作树；把目标写成可验证结果并锁范围（协议 §1.1）。认领：gh issue edit <n> --add-assignee @me，并在票上加一条 comment 记录本线 worktree 与分支名（单一 GitHub 身份下 assignee 不区分线，分支名才是线标识）。动标签前先读 docs/agents/triage-labels.md。
2. 方案依据：这四票是契约/功能票，不是缺陷修复，协议 §1.3 的豁免不适用；四票票面均已自带「成熟参考/复用」块（≥2 来源 + 判定），你的义务是按 §1.3 五字段（来源/机制摘要/契合点/判定/License）逐条核实并补全「方案依据」块，缺字段先补调研再进设计；不为走过场补空块。
3. 文件级避让（git-workflow §2.2）：对拟改路径检查 backend 在途线（main worktree 当前分支 fix/i559-i556-i567-i564-runtime-core）与 frontend clone 的未提交/暂存/已提交在途工作；发现同文件在途工作即停止并报告，等协调，不叠写。
4. TDD：先写命中真实症状/契约的红测试，再修绿（Matt tdd skill，按协议 §9 路由读正文执行）。
5. 逐票 focused 验证：PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q -p no:randomly <focused>；.venv/Scripts/ruff.exe check .；触及 web/** 或事件词汇时加前端 ④ tsc -b、⑤ vitest run、⑥ oxlint 与 ⑦ 生成物同步守卫（漂移先跑 scripts/gen_event_types.py / gen_event_vocabulary.py）。
6. 冻结树（§8.1）：记录 sha 与 sha^{tree}；全量只在该冻结树跑一次：./scripts/run_tests_clean.sh（清空 PYTHONPATH）；中间树只跑 focused，不重复全量。
7. 派单前清零（§8.2 第 4 条）：在冻结 sha 上跑裸 python scripts/gate0.py（落盘 docs/gate/<sha>.json）；派单判据 = 除 coverage 外全绿、且 coverage 的 ❌ 集合 ⊆ 本票代码笔；红了先修、重新冻结、重跑。
8. 两轴独立审查（§2 风险分档：四票均命中默认值得独立 review 的类别——凭证/权限/Host 边界、持久化/SessionEvent/Ledger、并发/取消/生命周期、公共 Contract）：派 Standards 轴与 Correctness/Spec 轴，派单写预算（目标 sha、报告上限、墙钟上限、自写变异条数上限、无论跑到哪一步必须给结论行）。只读纪律：变异只在主工作树之外的副本做（PYTHONPATH=<副本>/src；操作副本前先探测行尾；替换命中数断言 ==1）。发现阶段两轴都必须给结论；修后重审每轴 ≤1 轮，该轮若在本轮修复新引入代码面再给 P0/P1 ⇒ 停止修复、如实登记、交用户裁决。
9. 记账（§5/§8.5）：docs/review_ledger.tsv（或 docs/review_ledger.d/）新增行：日期/票号/range base..tip/fixed point/两轴结论/P 计数/证据指针，整行 ≤600 字符；更新 Tracker 的状态、commit、门禁证据与残余；门禁读数只写 docs/gate/<sha>.json 路径、不抄数字；逐条明细进 docs/phase_status/2026-10.md。
10. 本地提交：小步提交，commit message 描述工程事实；在干净检出上跑 .venv/Scripts/python.exe scripts/check_review_coverage.py 要求 exit 0；git diff --check 无输出。到此 = 本票「分支就绪」，按 AGENTS §11 写交付报告，再领下一票。

## 3. 工作区与分支
- backend clone 主工作树被在途线占用，不动它；现有 9 个 linked worktree 属其他线、默认只读，不写入、不复用。
- 开工时向用户报备后新建一个 linked worktree（新写入）：先 git -C D:/intelligence-agent-backend fetch origin --prune 刷新基准，再 git -C D:/intelligence-agent-backend worktree add D:/intelligence-agent-backend-wt-wbatch -b zcode/T366-w22-client-absent origin/main。
- 每票从最新 origin/main 开独立短分支，在同一 worktree 内顺序切换（切换前工作树必须 clean）：zcode/T366-w22-client-absent、zcode/T355-w11-single-service、zcode/T351-w07-delivery-contract、zcode/T349-w05-progress-file。
- 中途不主动把 main 合回自己；验证需要时按「先回后正」（git-workflow §3.1），冲突转 §4 停止报告。

## 4. 授权边界（硬约束）
- 你只做到「本地门禁全绿 + 分支就绪」。push 分支、开 PR、PR merge、关单：每一项都单独向用户申请，不提前执行，不把前一阶段批准扩展到下一种 Git 写操作。
- PR 开出后等服务端 gate0 绿；strict 要求与 origin/main 同步，落后时先回再验证新增 diff。本地绿不代替 CI 绿。
- 关单按 git-workflow §5 与 AGENTS §14.12：本批默认不关单（尚未集成），只准备关单 comment 草稿（分支、commit、集成负责人、证据路径）交用户裁决。
- reset --hard、rebase、push --force、branch -D、worktree 删除默认禁止；确需时列理由单独申请。

## 5. 集成顺序建议（获用户批准后执行，一次一条线）
W-22 → W-11 → W-07 → W-05。理由：W-12 的两个前置（W-11 + W-22）先落地，疏通 W-13/W-15/W-17/W-21 关键路径；W-22 与 W-07 都改事件词汇与生成物（web/src/generated/*、docs/EVENT_VOCABULARY.md、web 投影登记），用不改词汇的 W-11 隔开；第二张词汇票集成时按漂移处置重跑生成器与守卫 ⑦。每次集成前按 git-workflow §2.1 重新取基准并做祖先判断；冲突按 §4 九项报告获批后最小修复；集成后按 §14.9 通知其他线回补。

## 6. 异常出口（一律停下报告，不自行裁决）
- AC 不可实现、新证据推翻票面或需改架构/范围/迁移（协议 §3.1、AGENTS §9.1.1）：停止相关施工，给可复现证据、影响与最小替代方案，请用户裁决；不擅改票面、AC 或制造完成记录。
- 修后重审第二轮在新代码面给 P0/P1（§8.3 第 4 条）：停止修复、登记轮数与残余、交用户裁决。
- 未知红 = 阻断，按 §3.1 报告；既有红 only 按 §8.6 三条件处置（关单 comment 逐条写哪条 AC 不成立 + 证据 + 为何判既有；不许写「全绿」；记一条待用户裁决）。
- 必读文件缺失或读取失败：回复首行明确「任务阻塞（BLOCKED_REQUIRED_READ），未完成【依赖动作】」并列路径/错误，停止依赖动作。

## 7. 交付报告（每票结束与批结束）
按 AGENTS §11 五项：改了什么 / 为什么符合 Spec / 测了什么（读数写路径）/ 还剩什么 / 风险与未决项。协议 §8.8.3 失败回退边界表附表追加本批触发行（未触发则记「未触发」）。

## 附录 A：四票要点与预期触碰面
- #366 W-22（P0 Runtime Contract）：client_absent 持久暂停/显式恢复契约。规格依据 Spec 02 §5.2.1、03 §3.4/§5、11 §6.2 与 ADR-0046（用户已批准定向扩展）。最后客户端离开/宽限到期后阻止新 Model/Tool/Child 接纳；在途 Tool 按 Ledger 收口，UNKNOWN 先 NEED_RECONCILE；恰好一条 run/paused(reason=client_absent, trigger_dimension=client_presence, closeout_source=deterministic)；resume 需 expected_version + resume_basis=client_return + 在场 + reconcile 完成；并发续跑至多一次成功。不改 #305 三类暂停与 orphaned 旧入口；不做心跳/托盘（W-12/W-15）。预期触碰：session 事件词汇/生成物/web 投影、RunManager admission gate、Resume API。复用块：DeepSeek Desktop 关窗/退出检查 PORT DESIGN，REUSE 现有 run/paused、Operation Ledger、CAS。
- #355 W-11（P0 Host 契约）：唯一本机 Python 服务 + Desktop/TUI/Web 附着协议 + 鉴权 + 旧数据原位复用。竞态闭环「发现并认证附着 → 否则竞争启动 → 二次检查」，锁残留先证明失效不强抢；默认 127.0.0.1 受管端口，token/握手只走限权本机通道，不进 URL/进度文件/日志/诊断；迁移前备份可回退，验证 #303 禁删项（SessionEvent/Artifact/workspace/凭证）；验收用真实子进程 + loopback 套接字。预期触碰：instance_lock.py、web/app.py 启动与健康/鉴权、cli.py、keyring 复用。复用块：DeepSeek Desktop 共享 Host 数据与受认证连接 PORT DESIGN，REUSE 本仓 FastAPI/InstanceLock/Store。
- #351 W-07（P0 产品契约）：Task/Run/验证/接受四事实分轴。Task=Session、一 Task 多 Run；执行中/待验证/可交付/已接受与验证值、接受值三轴分别追加和投影；run/completed 不自动写「通过/接受」；接受/释放必须 expected_version CAS，重复请求幂等或 409；API 返回依据、Event 重建一致、不返回凭证。不改 #305 完成语义、不复刻 Ticket gate、不做 UI 组件。预期触碰：session/event.py、session/service.py、web/app.py、事件词汇/生成物。复用块：Codex app 执行与人审阅分步 + Anthropic 长任务功能清单 PORT DESIGN，REUSE 本仓 SessionEvent。
- #349 W-05（P0 产品事实投影）：agent-progress/<session-id>/progress.md 原子生成。同目录临时文件 + flush/fsync + 原子替换，Windows 锁/kill/权限/盘满不留半文件；脱敏在写入前，凭证值/Cookie/.env 值绝不入文，大原文只留 Artifact ref；保留上一版本与 source seq/hash；写失败任务状态可见；文件可进 Git diff 但系统不 git add；两 Session 不互覆。不做 UI 编辑/Fork/自动提交。预期触碰：新增 session 进度文件 writer + SessionEvent 投影接口 + focused Windows 文件系统测试。复用块：Anthropic claude-progress.txt 与 Codex 长任务项目文件 PORT DESIGN，REUSE 本仓事件与 Artifact。

## 附录 B：依赖事实（2026-10-04 核对，原生依赖图与票面声明一致）
四票的 open 阻塞者均为零：#366 仅 #342（closed）；#355 仅 #342（closed）；#351 为 #342/#346（closed）；#349 仅 #346（closed，即 W-02）。四票互无依赖边，均无 assignee。下游解锁面：W-22→{W-12, W-21}；W-11→{W-12, W-14, W-15, W-16, W-20, W-21}；W-07→{W-08, W-10, W-16, W-20, W-23}；W-05→{W-06, W-20}。其余 open W 票（W-06/W-08–W-10/W-12–W-21/W-23/W-24/W-28/W-30）均有 open 阻塞者或票面 open 前置，不在本批范围。

## 附录 C：2026-10-04 参考补强（已核实来源与你的核实义务）
- **W-22（#366）新增已核实来源（2026-10-04 读取）**：[GitHub Codespaces idle timeout](https://docs.github.com/en/codespaces/setting-your-user-preferences/setting-your-timeout-period-for-github-codespaces)——"A codespace will stop running after a period of inactivity"；个人交互/终端活动重置 idle。机制：在场维持运行、缺席即停。契合点：与 client_absent 的在场/缺席判定同形。判定：PORT DESIGN（只借在场语义，不借云端生命周期）；resume 契约的权威仍是已获用户批准的 Spec 02/03/11 变更与 ADR-0046。连同票面既有 DSH 来源 = 两独立来源齐备；接票时把本来源写进方案依据块（链接+读取日期+机制+契合点+判定）。
- **W-11（#355）：截至 2026-10-04 无新增已核实来源**。三个候选（Jupyter Server security 的 loopback+token、Docker Desktop architecture 的单后端附着、Electron `app.requestSingleInstanceLock`）当日均抓取超时/404 未取到正文。你在接票时必须按 §1.3 自行核实其一（优先顺序如前），记录链接+读取日期+机制+契合点+判定；票面既有 DSH 为来源1。若外网不可用：改用本地已安装文档/源码核实并如实说明核实方式；**不得编造读取日期**。
- **W-07（#351）/ W-05（#349）**：票面两来源（Codex app 介绍 + Anthropic 长任务项目文件）已足，无需补强。
- `docs/agents/reference-sources.md` 已新增「Workbench 桌面 / 宿主 / 在场 / 发布（2026-10-04 补强批次）」小节与「待核实候选」清单；接票前重读该节。待核实清单里的条目一律不得当作已核实来源引用。
