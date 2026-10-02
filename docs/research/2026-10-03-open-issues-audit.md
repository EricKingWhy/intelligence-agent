# 开放 Issue 全量核查与成熟方案依据

2026-10-03 | https://github.com/EricKingWhy/intelligence-agent | baseline `94c0d29e0d345291f21143575f12895733c18695`

本轮完整读取 68 个开放 issue 的正文与评论，核对当前规格、相关 ADR、关键源码、既有用户裁决与官方参考。覆盖所有票面，代码只按疑点定位读取；不是全部实现的独立代码审查，也未执行缺陷复现、故障注入或全量测试。旧票中的报告编号和测试日志保留为作者证据，本轮未重新证明其运行结果。

票面中存在真实风险，但不能把每个静态疑点直接叫作已复现缺陷。主要问题是：将正确规格误判成漏洞；参考方案能力被夸大；验收条件与方案自相矛盾；一票混合多个架构主题；与已批准工作或裁决重复。建议优先做事实纠正，再按既有风险与依赖排序施工。

本次仅在 51 张票正文顶部补充核查意见、候选验收建议及来源。原正文逐字保留在后面，标题、标签、状态、指派、依赖关系与已批准规格均不改。涉及预算、权限、指令信任、压缩信息保全或新协议的合同调整仍须用户明确裁决，本补充不构成新的施工授权。

## 实际 GitHub 操作与验证

GitHub body updates: **51/51** verified; 0 skipped/failed.

| Issue | Result | Readback SHA256 |
| --- | --- | --- |
| 305 | verified | 79e611ce7d5d5c86d0b5a60b603a67dc06982195f055a30f8dc986dcf36d16bf |
| 296 | verified | 757bab020e8f1f412ef1ffe3a469a683fb76211ed1e296f210e681d2913a107f |
| 570 | verified | a8c01eef5e3148d314bec304bd978fc90fca16ab8ea9d12350a8f44c7bff7050 |
| 569 | verified | 984c9fdd94c78a8601d46571f0ff13401821e1d89fade21dce48299d6bfbc926 |
| 568 | verified | d0151b2887d2153a1a3a0785f2b2bbf32c0c3ef59b476579edd3da871930cc50 |
| 567 | verified | e08f06f01a91001f7efb6d54600143b4d64a7f3a541415685d520cf212e95fa9 |
| 566 | verified | 715ad41089eee4db2b726c296f06f589130447acb5a4e236b362cf27748f3ac9 |
| 565 | verified | 07726998aa54754858aa452a3779e9786e842b6ed36b7e322b14744980f2f7a6 |
| 564 | verified | 4f1def0a1d734f759791815093ef451c22545d3d7659e8b79685d97cdd8aae71 |
| 563 | verified | f2178cc14416fc9fc0b5f9a7d1d3a2b010bffdf6a0832f2c862ff2c69ed69f2b |
| 562 | verified | c837db37ad096578db2cd398984dc46bad1e67347c505d3850097ad66ea7fd6c |
| 561 | verified | 52a3bc2e00c9061afd2a3273bf9583f298055c2732d698d8cbe5364aae5d3116 |
| 560 | verified | 780372bda51a7643552ea8c4494cf495458abfbdd8d664724d34dc2a97e1e31e |
| 559 | verified | 5ff4226fa777f351716e1260e72d345e7d6e973abf73447bd1cfa63323d8946b |
| 558 | verified | bf7b7ecb18a3d35be2f3685c8d3a0fe6c7aa626c30e9c6f95c90b6f1b321160c |
| 557 | verified | a31898b4e1e51fb138ab5922ca38f3a61cf19b9952f195abd96989888d973a7f |
| 555 | verified | 944ef4c61e37c8d5d9cf7bc338104618f410c66a16706c71ed081104ffb31032 |
| 554 | verified | 5e99eea36cbb23cdd7c0422ee0ee8f085f597c4296fc969256b509b513684cd9 |
| 553 | verified | c6199c0ce252674cd8db1ae04ae1c2c74ea583e7c809b2d35968eda1b6c1106f |
| 552 | verified | 5dc2b69e9de25612ad336f532e457bc7fcf927da8f11734b7d2833a4bc73b46f |
| 551 | verified | 59dec25548738e6b987da9bb9ceee4d82d7d577ab0316d7db89c4aa06e62dec8 |
| 556 | verified | f14e228848af3a409217825522c72c63a1a296ec7dc4cbb75dd58ac456574073 |
| 550 | verified | c1c1a4ef8777bd48d1e987fafa098b84bbf0e5a4e92a418921f2e67991aa3702 |
| 549 | verified | 2a7966158838e2f022a0aaeea5a5975e222cc9e7fb06731fdae584a59f600bfa |
| 548 | verified | 384551fbc211d3927eb8342a63a6078fa31f9e8ed74023b0f5a851604f37550f |
| 547 | verified | 2b2efc88a9b5edebe49dbc2f9e6cf5e6818ce801d43f243e60c2f593bcb591b8 |
| 546 | verified | a30f2e45865c0d5a50d8aaf2dcff76a49e6a0c78f9a755398913d048752daa70 |
| 545 | verified | 54bf7d68ac1975944d210276a15bb8f0eebd3ac28e1e9486b0acc07e0f8dc6fb |
| 530 | verified | c5b8c69d5027ad9874a49e096adc151adeb607c81992372c685ba67ff2346f28 |
| 529 | verified | 6b627a1dc1bf96a3b28f7ed6442cc2565d92f8c0ac2afc5d37da4373f7f2201d |
| 528 | verified | a13a76058120d71728285199a84b0df04d22e26e38a1460925dcb9c5b5551564 |
| 527 | verified | f04594ab891fc56f6127ab56934c9a2b92196844bfef8e1ec31f46b41adc2f63 |
| 526 | verified | fe99add509c1113ebbf968e2d9c0b8f3b3c860fe73bd3ce67b7eae16e45e1db2 |
| 525 | verified | b2f4a005f6c5df5940d9761ca157c66c930435bae1d524121775a90503fc0e6f |
| 524 | verified | 618c17db41d0dc7e825da1089d8852b469fb350637f1fa39ebaa08813e42024e |
| 523 | verified | ff3af543c989836f7898132131567985fb3434ae26ec92dd318bfd1d4a68fafc |
| 522 | verified | b5ace4908bd14537ac1f6a9d3b81ca13e6053da7a24c9736b4a6aab749b91384 |
| 521 | verified | 1c1ac4dc54e317dc89bdf4bcf1ecf168814b5ef821d3d25c1dfb4615a4ea6d36 |
| 520 | verified | 62a03b91e36d05da82831163d81bbe7401dbf041fdb470655a1f46b4a7e30093 |
| 505 | verified | 12796ab2d220d24ee426201406098fd4eac3121e07348e4e6fb56bc0744abb87 |
| 496 | verified | 5acc7335567b29ed48b225fbe648ef294bdcab7377d6d07aec8d4499aea68b41 |
| 447 | verified | ab844acd6f92a374b79222acba8ae04fb6bd46b6229960ec1ae34a5a59f6023b |
| 384 | verified | 4aca605c6edd73dae702ab180df675d5302201a223ad7595c2e21a2823abd9bd |
| 382 | verified | ba9bc105cd2316ca21ada9ffee27bfbdc1d5da667b152f96e34c334b323b1504 |
| 376 | verified | 50bac9ee22818529dda5f5b8296f01db64e831be6ceff65d661ed7411d004e5c |
| 370 | verified | 06b7a8f8c9cecbf3b8c79f3804bea1f9401571ac8195a0189a1e93d1244b3a29 |
| 358 | verified | 94422b5fddb9eb61e79af12d4e969ad9571d2846a8312eb647b1358bb8675f53 |
| 357 | verified | dd9e7c4db22fd9df03ec5eed5a7d46b69b6c775024cc11c4755db8f8a7f43c83 |
| 356 | verified | b6e31498e7725aa945be91682fc57465b34d52c9d5efb536e2232e093e1e0a7c |
| 354 | verified | a5684fe9f9cba8291243e5c6b85c9167391f0d2a4edfc1d429169ef8361316c5 |
| 352 | verified | 57d386e30e574112889851ce6ab6dea2f58b5f42da3bab7c19f649c6785141ec |

68 issues / 纠偏 12 / 增强 35 / 待裁决 13 / 状态纠偏 5 / 保留 3

## 启动检查表

| 前置项 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision 相关原则 | SPEC_ROOT/00_PROJECT_VISION.md：轻量 Core、Runtime 权限与可恢复可观测边界 | READY |
| 当前任务规格 | SPEC_ROOT/02–12 模块相关合同；Workbench PRD；web/PRODUCT.md | READY |
| Reuse 相关判定 | SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md；docs/agents/reference-sources.md | READY |
| Phase 依据 | SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md；docs/PHASE_STATUS.md 当前态 | READY |
| 触发细则 | SDD_WORKFLOW_PROTOCOL.md 全文；issue-tracker.md；review-debug-playbook.md；CONTEXT.md；相关 ADR；research 与 prove-it-works 技能 | READY |

`SPEC_ROOT` = `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/`.

## 优先纠正的票面

- [#554](https://github.com/EricKingWhy/intelligence-agent/issues/554): 先纠正规格判断：fork 是新预算，不是共享父累计消耗。
- [#527](https://github.com/EricKingWhy/intelligence-agent/issues/527): 立即纠正技术依据：/CREATE 为零字节文件；hardlink 不提供写隔离。
- [#570](https://github.com/EricKingWhy/intelligence-agent/issues/570): 离线词表问题保留；chars/4 或常量不能作为硬预算安全兜底。
- [#552](https://github.com/EricKingWhy/intelligence-agent/issues/552): 不要 clamp/伪造 usage；先验输入再持久化，预算不可证明时明确 unavailable。
- [#562](https://github.com/EricKingWhy/intelligence-agent/issues/562): 深度 800/1500 与提议上限 100 互相矛盾，先统一验收。
- [#546](https://github.com/EricKingWhy/intelligence-agent/issues/546): 脚本模型回显哨兵不是现实 prompt injection 成功证明；Skill 生效不等于越权。
- [#559](https://github.com/EricKingWhy/intelligence-agent/issues/559): 进程级并发闸门合同与每次 build_runtime 新实例存在静态疑点，应以多 Session barrier 验证。
- [#376](https://github.com/EricKingWhy/intelligence-agent/issues/376): 最新用户裁决覆盖旧下一步计划；保持挂起观察。

## 成熟产品机制对照

| 领域 | 产品 | 机制 | 本仓判定 | 参考 |
| --- | --- | --- | --- | --- |
| 长任务执行与验收 | Anthropic / Codex | 进度文件、可核对清单、逐项推进、真实入口验证 | PORT DESIGN；复用已有 CompletionPolicy/Workbench，不另造 Agent Loop | [long_anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), [long_codex](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex) |
| 工具发现与缓存 | Anthropic / OpenAI | 延迟上下文暴露、Provider 能力与 cached usage 可观察 | ADAPT；先量 schema 成本；不扩大权限、不假定各 Provider 相同 | [tool_anthropic](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool), [tool_openai](https://developers.openai.com/api/docs/guides/tools-tool-search), [cache_anthropic](https://platform.claude.com/docs/en/build-with-claude/prompt-caching), [cache_openai](https://developers.openai.com/api/docs/guides/prompt-caching) |
| 持久确认与索引修复 | RabbitMQ / AWS / Milvus | 确认边界分层、事务 outbox、幂等消费与主键更新 | PORT DESIGN + REUSE；不因此增加 broker 或第二套 SessionStore | [rabbit](https://www.rabbitmq.com/docs/confirms), [outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html), [milvus](https://milvus.io/docs/upsert-entities.md) |
| Windows 宿主与浏览器 | Electron / Chrome DevTools MCP / Pi TUI | renderer 隔离、官方浏览器连接、独立终端组件 | ADAPT + REUSE；Python Core 与统一 ToolExecutor 保持唯一 | [electron](https://www.electronjs.org/docs/latest/tutorial/security), [chrome](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/advanced-usage.md), [pi_tui](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/tui/README.md#L743-L751) |
| 规则、Skill 与 Hook | Codex / Claude Code | 目录规则发现、可信来源、before/after 不同决策边界 | PORT DESIGN；MCP elicitation 仍 DEFER，运行时权限不由规则文本代替 | [agents_openai](https://learn.chatgpt.com/docs/agent-configuration/agents-md), [claude_memory](https://code.claude.com/docs/en/memory), [skills](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview), [hooks](https://code.claude.com/docs/en/hooks), [mcp_elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation) |

## 仍需产品决策的边界

- 预算：#554 与冻结 fork 新 SessionBudget 合同冲突，不能作为继承消耗的修复施工；部署/user 总成本限制若需要，应独立决定。#567 回收 closeout 预留也属于准入语义变更。
- 信息保全：#556 无界投影与 ProtectedFact 保全两目标如何兼得需裁决，不能只截断最近 N 条；#548 孤立 Unicode 代理的拒绝与替换也需统一产品语义。
- 权限与信任：#526 自动授权缓存、#546 Skill 注入角色、#523 仓库规则优先级、#358 Local containment/default 迁移需明确合同。不要依赖 prompt、相似文本或字符串命令过滤做 Runtime 边界。
- 拆票与依赖：#549 七类问题与 #530 两个主题不宜一次原子验收；#547 和 #357 统一 Reconcile 后端合同；#549 与 #358 统一权限 owner；#384 W-30 判定器应先接入 #365 最终 Gate。
- 新功能：#521 MCP elicitation 仍 DEFER；#525 hashline 不覆盖既有 exact edit 合同；#529 自动生成/激活 Skill 超出手动加载 V1。只列候选，不替用户批准。
- 已裁决事项：#376 保持 OPEN 挂起观察、不修复、不登记已知 flake；#338 按再现留签名停线规则。#296 的 R7 不新增独立敏感检测层；Memory 当前 user/project scope 按 ADR-0049，不重开已收口的范围争议。

## 68 张票逐项核查

“保留”表示票面方向与已读依据兼容，不表示其实现或历史测试已通过。“增强”包含候选验收，不能作为新的冻结规格。

| Issue / title | 判定 | GitHub 补充 | 意见与候选验收 | 仓库依据 | 参考 |
| --- | --- | --- | --- | --- | --- |
| [570](https://github.com/EricKingWhy/intelligence-agent/issues/570) [R4-低] tiktoken 首次使用需公网下载：离线环境 estimate_tokens 抛未分类异常杀死 run（UI-02） | 纠偏 | 是 | tiktoken 是 pyproject.toml:22 的直接依赖，不是 optional；tokens.py:9 调 get_encoding 的缺缓存下载风险成立。删去“chars/4→常量”作为硬护栏安全兜底的推定。建议优先预装校验过的离线缓存；空/损坏缓存且断网时明确不可用，不能虚构低 token 数继续发送。验收含中文、emoji、代码、工具 schema 和首启断网；若新增估算模式须单独证明保守边界。 | ADR-0007 子决策1；Spec06 Context 硬护栏；pyproject.toml:22；context/tokens.py:9 | [tokens](https://help.openai.com/en/articles/4936856-understanding-and-counting-tokens), [tiktoken](https://github.com/openai/tiktoken/blob/0.13.0/tiktoken/load.py) |
| [569](https://github.com/EricKingWhy/intelligence-agent/issues/569) [R4-低] 磁盘满时 POST /messages 裸 500 纯文本（CHAOS-02） | 纠偏 | 是 | prlimit --fsize 模拟文件大小限制，不等价磁盘满；需分别测 EFBIG、ENOSPC、SQLITE_FULL、SQLITE_IOERR。#515 已交付后应在当前树重跑精确逃逸路径，先确定是否还漏转译。入口 free-space 预检有竞态，不能替代每次写入异常处置；不得统一重试所有 OperationalError，尤其 commit 后回读失败可能重复副作用。验收 JSON 错误契约、未启动模型及账本/Event 保留完整。 | Spec07 持久化/恢复；#515 交付记录；web/error_contract.py | [sqlite](https://www.sqlite.org/rescode.html), [python_sqlite](https://docs.python.org/3/library/sqlite3.html#sqlite3.Error) |
| [568](https://github.com/EricKingWhy/intelligence-agent/issues/568) [R4-低] 知识库 ingest 幂等跳过不校验向量库实际 chunk（KB-01） | 增强 | 是 | 同 hash 不等于索引完整的问题值得保留；chunk_count 相等也不足以证明 chunk ID/hash/embedding 版本完整。建议明确 vector provider 可观测的 source+generation/IDs 校验，或 force-rebuild 管理入口；无原文可回读不能承诺自动重建。测缺一个 chunk、同数错内容、异步可见性延迟、部分重建失败及正确幂等跳过。无需新增 Kafka/DLQ/独立队列。 | knowledge/service.py:84；Spec09 Knowledge 生命周期 | [outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html), [milvus](https://milvus.io/docs/upsert-entities.md) |
| [567](https://github.com/EricKingWhy/intelligence-agent/issues/567) [R4-低] 可数预算 N ⇒ 最多 N-1 轮真实工作；N=1 零产出且 closeout 空烧额度（OBS-R4-06） | 待裁决 | 是 | Closeout 在预算内预留是已批准行为，N-1 工作容量本身不应当作计数 Bug。零真实工作时跳过模型 closeout 是可讨论优化，但“把额度还给工作”会改变准入语义，不能混成文档修复。先区分各维度、scope、既有 continuation；测试 N=1/2、有/无工作、模型输出是否被正确使用、确定性 closeout 不增真实请求。保留预留红线，改行为前确认票面。 | Spec02 §5.1/§5.2；#305 EB-3、预算合同；agent/run_budget.py | 以仓库合同/裁决为准 |
| [566](https://github.com/EricKingWhy/intelligence-agent/issues/566) [R4-低] 崩溃恢复给审批门前「从未执行」的工具合成「结果未知」（F3） | 增强 | 是 | “待审批肯定没执行”必须由 durable Ledger PENDING 与审批→执行顺序证明，不能仅凭缺 tool/result 推定。复用现有 stale approval 与 recovery classification；查明当前 requested/resolved/call/Ledger 顺序，再测审批中 kill、批准落盘后 kill、执行后丢结果三窗口。UNKNOWN 保持 NEED_RECONCILE；Structured Outputs/promptfoo 不能证明副作用未发生。 | Spec07 §6/§9；ADR-0047 残余5；#337 已交付 | [rabbit](https://www.rabbitmq.com/docs/confirms) |
| [565](https://github.com/EricKingWhy/intelligence-agent/issues/565) [R4-低] events.jsonl 中间行损坏静默跳过：seq 留洞且无任何信号（E8S-02） | 增强 | 是 | 完整带换行的坏 JSON 行与未写完整的末尾片段不同：不能把所有“最后一行坏了”都静默丢弃。跳过中间坏行会造成 seq gap 或丢 permission/tool 事实，恢复必须拒绝或显式受阻。保留原字节和位置/hash，脱敏 diagnostic 记录，不新增“诊断 SessionEvent”混淆 Event≠Log。验收 partial tail、完整坏尾行、中间损坏、seq 重复/缺号。 | CONTEXT SessionStore/Diagnostic Log；Spec03 append-only；Spec07 恢复 | [json](https://www.rfc-editor.org/rfc/rfc8259) |
| [564](https://github.com/EricKingWhy/intelligence-agent/issues/564) [R4-中] session 作用域 tool_call_limits 不校验工具名是否注册（BUG-R4-04） | 增强 | 是 | 漏校验注册工具名在 session_limits_from_request 当前源码可见；复用 run 校验是合适方向。补 create/resume/idle messages、两 scope、大小写拼写错误、已禁用/收窄工具、MCP 名和 bool/string 值；拒绝前后事件与账本无新预算消费。合法 delegation-only 工具应按预算 scope 定义处理，不能简单以根 profile 过滤掉整个子树合法配额。Pydantic strict 属输入类型校验，K8s unknown fields 不能单独证明未知工具名合同。 | agent/run_budget.py:1457/2690；Spec02/11 配额合同 | 以仓库合同/裁决为准 |
| [563](https://github.com/EricKingWhy/intelligence-agent/issues/563) [R4-中] uvicorn RSS 持续线性上涨：泄漏嫌疑 vs 在途堆积，待堆分析定罪（S4） | 纠偏 | 是 | “188MB→568MB / 120min”与“+1.09MB/min”口径不自洽，先核 samples 时间单位、起止与拟合窗口。RSS 不回落不能定罪泄漏；py-spy 是调用栈/CPU 采样工具，不是堆分配 profiler。#516 后同树固定数据/到达率/在途量重测，暖机后 tracemalloc 快照差分，清理后任务/对象数量；原生内存需另外归因。新增 /metrics 不是本缺陷的必要修复，宜独立选型。 | Spec12 Optional Observability；#516 已交付；原票 S4 数据 | [tracemalloc](https://docs.python.org/3/library/tracemalloc.html), [pyspy](https://github.com/benfred/py-spy) |
| [562](https://github.com/EricKingWhy/intelligence-agent/issues/562) [R4-中] wire 层输入加固：422 序列化 500（surrogate/inf）+ 深嵌套 RecursionError 500 + reason 无长度上限（FUZZ-01/RL-04/F6） | 纠偏 | 是 | AC“深度1500拒绝、800不变”与方案“上限100”矛盾，不能据两者同时验收。选一个明确项目上限并测 limit-1/limit/limit+1，字符数与 UTF-8 字节数分别写清。错误响应不得回显原始秘密或递归结构；保护入站 body 与响应序列化，拒绝而非悄悄截断。第三方深度100不等于本仓已批准100。 | Spec11 REST validation；原票深度/长度 AC | [json](https://www.rfc-editor.org/rfc/rfc8259) |
| [561](https://github.com/EricKingWhy/intelligence-agent/issues/561) [R4-中] Inspector 不接管外部启动的 run：实时流/审批弹窗/错误态全不出现（UI-01） | 增强 | 是 | 外部启动 Run 的订阅可达性问题保留；优先复用已有事件/stream cursor 和 attachLiveStream，不为此引入新状态库/长轮询 API。加选中后 API launch、快速完成、切换 Session、重复 run/started、重连 gap、审批/失败三种路径，验证订阅数≤1、清理旧监听、不会错接别的 Session。此修复不依赖 #526 审批缓存新设计。 | web/src/hooks/useSession.ts；web/PRODUCT.md Event 投影；Spec11 | [react](https://react.dev/learn/you-might-not-need-an-effect) |
| [560](https://github.com/EricKingWhy/intelligence-agent/issues/560) [R4-中] 突发并发下 mode=queue 的消息被 409 丢弃而非入队（RL-02） | 增强 | 是 | 本仓 SessionStore 是 JSONL，不能照搬“SQLite 内事务生成 seq”当现成修复。复用会话写入串行 owner 与既有 seq 冲突规则；不能盲 retry append 造成重复事件/执行。8 并发 AC 用 ScriptedModel barrier 保持首 run active，才能稳定断言1 launched+7 queued；否则快模型完成后多次 launched 未必错误。验证8输入各一次、FIFO、唯一 seq、重启恢复与取消。 | CONTEXT SessionStore；ADR-0030 D4/D5/D7；Spec03 | [rabbit](https://www.rabbitmq.com/docs/confirms) |
| [559](https://github.com/EricKingWhy/intelligence-agent/issues/559) [R4-中] 模型并发闸名义「进程级全局」，实际按会话隔离（M10-6） | 增强 | 是 | assembly.py:286 每次 build_runtime 新建 ModelCallGate，支持原票“进程级合同 vs per-runtime 实现”疑点。把共享闸门归属装配生命周期，测试跨两个 Session、child、fallback、摘要、取消失败后 permit 归还；用 barrier 和真实峰值计数≤N，避免只测 sleep 总耗时。不要夹带 circuit breaker 或跨进程调度器。 | assembly.py:286；PHASE_STATUS Phase12 进程级闸门；Spec02 Model Provider | [asyncio](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation) |
| [558](https://github.com/EricKingWhy/intelligence-agent/issues/558) [R4-中] 中文「密码/密钥是…」绕过记忆密钥守卫（写入与回灌双路径）（MEM-01） | 增强 | 是 | 保持用户 R7 裁决：不新增独立9类敏感检测层；本票只硬化已批准 secret 边界。NFKC 会改变长度，归一化串索引不能直接切原文；需可靠映射或匹配后整体拒绝。覆盖全角/组合字符/ligature、正文/typed payload/evidence/版本/outbox/recall；扫描输出不泄密。不得宣称通用正则能保证所有敏感事实检测。 | #298 R7 用户裁决；#296 secret 边界；Spec06 Memory | [skills](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview) |
| [557](https://github.com/EricKingWhy/intelligence-agent/issues/557) [R4-中] MCP 韧性：server 崩溃后重连永不触发 + 确定性协议违规烧 90s+ 超时（MCP-F4/MCP-F2） | 纠偏 | 是 | 未知 ID 先区分 JSON-RPC request id、SSE event cursor、session generation、取消/超时迟到响应及恢复重投，不能单靠日志定远端协议违规。采用官方 SDK transport，不另写 read loop。重连只恢复连接，不重放远程 mutation。以握手/连接创建计数和无重复执行证明重连，而非“耗时>100ms”；验证 timeout/cancel/remote hang/真实错误各臂。 | ADR-0012 D2/D6/D7；mcp/client.py | [mcp_transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports), [asyncio](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation) |
| [556](https://github.com/EricKingWhy/intelligence-agent/issues/556) [R4-中] 压缩摘要「原始目标与用户约束」节无界逐字累积（F-COMP-1） | 待裁决 | 是 | compactor.py:454 extend、:470 原文 JSON 无界累积的源码事实成立；但“最近N条/截断原约束”可能违背已批准原目标与 ProtectedFact 保全。建议只裁剪可替代的冗余投影，原文留 Event/Artifact 可回读，保护事实逐项保持或明确因预算不足暂停。3轮短文本收敛不足，补大首消息、多轮distinct目标、旧禁令/精确ID读回和重启；信息保全与上限如何兼得需明确决定。 | context/compactor.py:393-481；Workbench PRD §4.3；Spec06 | [anthropic_context](https://platform.claude.com/docs/en/build-with-claude/context-editing), [long_anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) |
| [555](https://github.com/EricKingWhy/intelligence-agent/issues/555) [R4-中] kill -9 中断 fork workspace 拷贝 → 僵尸 child session：无标记、无清理（CHAOS-01） | 增强 | 是 | fork 先 Session.start 后 workspace copy/adopt/meta 写入，异常留下部分 child 的风险源码成立。atomic rename 仅保护一次文件替换，不能把 JSONL+目录+SQLite 谱系变成一个事务。需定义各阶段失败/取消/kill后的可见性与幂等补偿，清理仅 Harness 新建且拥有的临时目录，父文件逐字不变。manual fork 独立于 parent run，不能照搬 child TaskGroup 取消绑定。 | session/fork.py:175-346；ADR-0017 D1/D3/D5/D7 | [asyncio](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation), [sqlite](https://www.sqlite.org/rescode.html) |
| [554](https://github.com/EricKingWhy/intelligence-agent/issues/554) [R4-中] fork 是 session 预算的未设防逃逸口（含已暂停/砖化父会话）（OBS-R4-05） | 纠偏 | 是 | 原票核心前提有误：Spec02 §5.1、Spec03 §7、#305 EB-11/AC-14 明确 fork 创建新的 SessionBudget，记录父 snapshot/lineage，消耗独立；fork.py:183也明确该合同。不得修成继承累计消耗/共享父账本。若担忧用户反复fork规避部署总成本，应作为独立 deployment/user级预算产品提议待批准；父限额是否继承须与既有层级区分。现有新账本语义不能直接定性“规格漏洞”。 | Spec02 §5.1；Spec03 §7；#305 AC-14；session/fork.py:183 | 以仓库合同/裁决为准 |
| [553](https://github.com/EricKingWhy/intelligence-agent/issues/553) [R4-中][未复现] /approve 返回 200 时决策尚未落盘——崩溃窗口内批准被翻转为拒绝（F2） | 增强 | 是 | 审批 resolve 唤醒内存 waiter 与 durable permission/resolved 之间的窗口需 barrier+kill 实证，不能仅凭静态分析宣称已复现。HTTP成功必须对应可恢复决策；重复提交first-writer-wins，stale/conflict409，不执行两次。批准已持久但工具结果未知仍走Ledger reconcile；durable批准不等于可盲重跑。RabbitMQ只借确认语义，不引入队列服务。 | Spec04 Approval；Spec07 Ledger；session/approval.py | [rabbit](https://www.rabbitmq.com/docs/confirms), [asyncio](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation) |
| [552](https://github.com/EricKingWhy/intelligence-agent/issues/552) [R4-中] 超大 usage/ceiling 数值未钳制：run 炸裂两本账分裂 + 消息因 500 丢失（BUG-R4-02/03） | 纠偏 | 是 | 不能 clamp usage 或把异常/缺失记0；规格要求 unavailable 不伪造。输入上限先校验再持久化，/messages拒绝不能先写budget再返回422。测 int64 ceiling边界和两个合法usage相加溢出、负数/bool/非整数、Run与Session计数一致、错误后继续的预算安全。异常 provider usage 应显式 unavailable/停止无法证明的显式预算，不当免费请求。 | #305 EB-6/预算投影；Spec02/11；storage/delegation_tree.py | [sqlite](https://www.sqlite.org/rescode.html) |
| [551](https://github.com/EricKingWhy/intelligence-agent/issues/551) [R4-高] 模型流终结语义缺口：提前 EOF 无诚实标记 + fallback 重答拼接 + 双挂错误归因掩盖（M10-2/M10-3/M10-8） | 纠偏 | 是 | SSE未完成帧与已完成帧中的模型部分答案不同；不得引用 WHATWG 来删除所有部分内容或按字符串去重。合法重复文本必须保留。明确 attempt边界、interrupted保留内容、fallback新块及最终消息配对；缺[DONE]但有合法finish_reason与无finish_reason中断分开测试。Model fallback仍在Provider责任域，不能改写历史事实。 | runtime.py:723 interrupted顺序；Spec02 流式块；Spec03 原始历史 | [sse](https://html.spec.whatwg.org/multipage/server-sent-events.html), [mcp_transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) |
| [550](https://github.com/EricKingWhy/intelligence-agent/issues/550) [R4-高][未复现] 连续两次 /cancel → run 永久丢失终态事件（RL-01） | 增强 | 是 | asyncio.shield 不消除 caller 的 CancelledError；独立清理 task 要有强引用、受控等待与重复cancel幂等，不能“一包shield就保证收尾”。用 barrier 在 append/refund/checkpoint/工具收尾分别二次取消，再核唯一 run终态、账目、pair与UNKNOWN。真实OS kill不执行finally，必须另测启动reconcile。 | Spec02 §17；Spec07 Recovery；agent/runtime.py:1999 | [asyncio](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation) |
| [549](https://github.com/EricKingWhy/intelligence-agent/issues/549) [R4-高] local sandbox 隔离语义家族：进程树不回收/setsid 逃逸/无路径围栏/无配额/WriteTool 异常映射（SB-01/02/03/04/05/07·MCP-F5） | 待裁决 | 是 | 七类平台/进程/权限问题不能作为一张原子 tracer ticket验收。建议将可复现进程树泄漏、资源限制、错误映射与权限默认值分开；权限默认/W-14由#358统一，ADR-0027已明确Local不是路径硬围墙。Docker --init负责reap僵尸，不保证杀掉仍活跃detached进程；RLIMIT是POSIX进程限制非Windows全树隔离。只用有界子进程探针，不在真实Host跑fork bomb。重分票/改变安全合同须用户决定。 | Spec05；ADR-0027 Security；#358 W-14 | [docker_init](https://docs.docker.com/engine/containers/multi-service_container/) |
| [548](https://github.com/EricKingWhy/intelligence-agent/issues/548) [R4-高] 被接受的输入含 lone surrogate 稳定 500——第二条独立 500 路径（FUZZ-F1） | 待裁决 | 是 | “校验通过”不等于 Unicode 标量文本合法。入口拒绝lone surrogate与U+FFFD替换是不同产品语义，替换会丢用户数据；建议明确422拒绝及无副作用，再加异常响应安全编码。ensure_ascii=False对合法中文/emoji本身正确，不能以全仓消灭该表达式作为AC。测high/low单代理、合法代理对、中文/emoji、两输入入口与错误出口；不要借此全仓重构safe_dumps。 | Spec11 validation；Spec03 持久原文；原票接受路径 | [json](https://www.rfc-editor.org/rfc/rfc8259) |
| [547](https://github.com/EricKingWhy/intelligence-agent/issues/547) [R4-高] kill -9 落在工具执行中 → 会话经 HTTP 永久变砖，无人工裁决入口（RL-03） | 待裁决 | 是 | UNKNOWN fail-closed正确，缺用户可达裁决是产品Gap；与#357 W-13恢复页统一一个服务端Reconcile合同。不要把“只能fork丢历史”当可恢复性完成条件。复用RecoveryCoordinator/ReconcileCallback，用户确认已成功/已失败/放弃/显式重试与真实Ledger状态分别定义；身份、operation/version绑定、防重复提交、先查外部事实及无自动重跑必须验收。Airflow mark成功和MCP elicitation三态不是副作用证据；elicitation仍DEFER，无需引入MCP才能做本机UI。 | Spec07 §9；CONTEXT ReconcileCallback；ADR-0012 D1；#357 | [rabbit](https://www.rabbitmq.com/docs/confirms), [mcp_elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation) |
| [546](https://github.com/EricKingWhy/intelligence-agent/issues/546) [R4-高] skill 正文注入直达模型上下文并被转成工具执行（SK-01） | 纠偏 | 是 | scripted模型把哨兵转tool_call只证明内容可见且权限允许，不能证明真实模型已被注入成功。Skill本来用于指导任务，不能要求全文零行为影响；安全AC应是恶意Skill不能绕过Runtime授权/身份/路径边界，同时可信Skill正常有效。当前ADR-0011把catalog放SystemMessage但声明数据，若要改可信来源/注入角色需显式设计裁决，靠标签或哨兵扫描不构成安全保证。 | Spec09 Skills；ADR-0011 Q3/Q4；AGENTS §4.3/§7 | [skills](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview), [long_anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) |
| [545](https://github.com/EricKingWhy/intelligence-agent/issues/545) [R4-高] /messages 续跑在交互式会话上必崩——审批回调从未绑定 Session（F1-approvals) | 增强 | 是 | 根因应钉到default permission_mode缺省+auto_approve=false的恢复分支，保持#423首次创建交互语义和显式档位优先级。添加first launch→idle messages→resume→permission changed→pending审批禁止改档→cancel的入口回归，真实callback决策与权限事件配对。只修漏接线，不统一改全套审批默认值（默认迁移归#358）。 | session/service.py:1438-1506；ADR-0041 D3-D6；session/approval.py:220 | 以仓库合同/裁决为准 |
| [530](https://github.com/EricKingWhy/intelligence-agent/issues/530) [P2][后端] delegate 不能指定输出 schema + 事件 payload 无 schema（IMP-17/19） | 待裁决 | 是 | 事件信封typed与payload typed不同，event.py:274 data=dict确有可讨论缺口；但生产只warning后写无效关键事件会破坏恢复。新关键写入应校验fail-closed，存量读取按schema version显式兼容。MCP任意JSONSchema复用现有jsonschema adapter，不把全部schema硬转Pydantic。child结构化输出修复只允许有界model修复并计预算，不能重跑delegate mutation；两主题建议分票。 | session/event.py:274；Spec03 typed事实；Spec10 SubAgentResult；ADR-0012 实现注记 | [json](https://www.rfc-editor.org/rfc/rfc8259), [tool_anthropic](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool) |
| [529](https://github.com/EricKingWhy/intelligence-agent/issues/529) [P2][后端] Skill 无「成功 run → 验证 → Skill」沉淀闭环（IMP-12） | 待裁决 | 是 | 自动生成/激活Skill超出Spec09 V1手动发现加载边界，先定义candidate→review→user activation及来源/版本/回滚。一次成功不等于可复用流程；与#296 procedural memory区分生命周期、避免重复抽取系统。静态lint不替代真实rehearsal；未经核验的“/run-skill-generator官方命令”不能作为依赖。本轮仅建议人工候选，不授权自动执行/发布新Skill。 | Spec09 §2 V1；#296 procedural合同；ADR-0011 | [skills](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview), [long_anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) |
| [528](https://github.com/EricKingWhy/intelligence-agent/issues/528) [P2][后端] 工具定义全量注入，缺延迟加载/曝光级别（IMP-11） | 增强 | 是 | 先量化当前tool schema token成本再决定ROI。Anthropic defer_loading只省context暴露，完整定义仍在每次request；不能宣称省掉传输。本仓runtime一次bind_tools，按需发现须定义动态rebinding/context/cache兼容及权限收窄；tool search结果不能授予执行权。#557连接故障独立修复，不能作为此票顺手施工。新code mode仍须每次过统一Executor，无隐藏调用路径。 | runtime.py:1031-1038；Spec04/09；ADR-0012 | [tool_anthropic](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool), [tool_openai](https://developers.openai.com/api/docs/guides/tools-tool-search) |
| [527](https://github.com/EricKingWhy/intelligence-agent/issues/527) [P1][后端] Fork O(n) 拷贝与命名混乱 + resume 无工作区粒度（IMP-05/18） | 纠偏 | 是 | robocopy /CREATE仅建立目录与零字节文件，不能作CoW；NTFS hardlink共享同一文件，直接写child会改parent，不满足copy-on-fork。只有经验证的block cloning/独立副本可选，失败fallback正常copy；测child原地写不改parent、删除parent后child独立、跨卷/断电/空间满。workspace-only restore涉及新快照/破坏性覆盖，另行规格决定。两个fork point字段可能不同语义，勿盲重命名持久合同。 | ADR-0017 D3/D5；Spec03 §7/§10；session/fork.py | [robocopy](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/robocopy) |
| [526](https://github.com/EricKingWhy/intelligence-agent/issues/526) [P1][后端] 审批无决策缓存 + 缺 Plan 模式（IMP-09/16） | 待裁决 | 是 | “类似工具自动批准”属于权限合同扩大，不是UX修复；默认逐调用审批不能由缓存绕过。若批准会话授权，限定command/path/server/resource/version、TTL/revoke、跨Session隔离和二次校验；不能用tool名/相似文本为批准键。Plan只读执行与平台Sandbox应独立界定，Windows不能照搬Linux隔离。外部run可达性#561不依赖此新功能。 | Spec04 §8；ADR-0041；#358 W-14 | [hooks](https://code.claude.com/docs/en/hooks), [electron](https://www.electronjs.org/docs/latest/tutorial/security) |
| [525](https://github.com/EricKingWhy/intelligence-agent/issues/525) [P1][后端] edit 精确匹配失败率高 + 同文件编辑批间无互斥（IMP-08/14） | 待裁决 | 是 | Spec05冻结exact edit的0/1/multiple命中合同，hashline是新工具设计而非修exact匹配Bug。空白归一化可能抹去Python缩进语义；锚点必须能检测版本过期/碰撞/范围歧义，拒绝旧锚点而非编辑错块。先复用当前edit和resource lock，再决定新增API；测同文件跨runtime竞态和合法不同文件并行。Pi当前仍保留唯一精确片段，不支撑直接废弃旧合同。 | Spec05 §5；Pi edit.ts:23-25 @1b347794；现有Tool resource_keys | [pi_edit](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/coding-agent/src/core/tools/edit.ts#L23-L25) |
| [524](https://github.com/EricKingWhy/intelligence-agent/issues/524) [P1][后端] 缺 TTSR 式事中纠偏 + 完成门缺证据验证（IMP-07/13） | 待裁决 | 是 | 已有CompletionPolicy唯一seam，编码测试/diff证据应为可选domain policy，不把“passed”文本或coding规则硬塞Core quiescence。不重复W-07/W-08服务端证据。TTSR不能另造第二个stuck机器，复用阈值/一次replan/预算；失败计划不是永久禁止再试（环境变化可合理重试）。新反馈不能重跑已执行mutating tool。 | Spec02 §5.4/§9；ADR-0047 D2/D5；ADR-0048；#351/#352 | [hooks](https://code.claude.com/docs/en/hooks), [long_anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) |
| [523](https://github.com/EricKingWhy/intelligence-agent/issues/523) [P1][后端] 不读目标仓库 AGENTS.md / CLAUDE.md 项目指令（IMP-06） | 待裁决 | 是 | SystemMessage内加小标题不使仓库指令自动成为低优先级；需定义部署策略/用户指令/仓库内容的实际角色与可信来源。根止点、子目录继承/覆盖、symlink、冲突、长度和修改后reload都应明确；不能默默截断必读规则仍宣称已读。Codex AGENTS和Claude CLAUDE发现策略不同，仅借鉴机制，不能替本产品默认选择。 | Spec09 Skills/Context；ADR-0023；AGENTS项目入口纪律 | [agents_openai](https://learn.chatgpt.com/docs/agent-configuration/agents-md), [claude_memory](https://code.claude.com/docs/en/memory) |
| [522](https://github.com/EricKingWhy/intelligence-agent/issues/522) [P1][后端] 压缩默认烧旗舰模型摘要 + bracket 缺摘要模型/耗时（IMP-03/15） | 增强 | 是 | Compactor.__init__:155-162已提供summary_model seam，ADR-0007修订注明装配后续票。本票只补装配/观测，不再造摘要Provider；便宜程度须依据实际配置/usage，勿假设所有primary都是旗舰或自动换model。 opt-in设置、未配保持既有链、失败fallback不丢保护事实；记录实际model ID、monotonic耗时及请求预算（不写凭据）。 | context/compactor.py:155-162；ADR-0007 #348修订；Spec06/12 | [anthropic_context](https://platform.claude.com/docs/en/build-with-claude/context-editing), [long_anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) |
| [521](https://github.com/EricKingWhy/intelligence-agent/issues/521) [P0][后端] 事件无干预语义（只读总线）+ MCP 无 elicitation（IMP-02/10） | 待裁决 | 是 | readonly观测总线不等于缺少mandatory拦截能力；notification #447与decision hook分开。MCP elicitation在ADR-0012 D1明确DEFER，启用需先改已批准决策。若以后批准，REUSE+ADAPT官方SDK，不BUILD协议。before可拦截、after已执行仅反馈；改写参数后重新Validation/Permission/Scheduler，不让allow hook提升权限。超时/异常fail策略与非零性能预算需明确。 | Spec09 V1；ADR-0012 D1；Spec04统一路径；#447 | [hooks](https://code.claude.com/docs/en/hooks), [mcp_elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation) |
| [520](https://github.com/EricKingWhy/intelligence-agent/issues/520) [P0][后端] Prompt caching 无主动标记 + fallback 换模型不换 prompt（IMP-01/04） | 纠偏 | 是 | 缓存与模型提示适配是两个独立目标。OpenAI缓存自动路由；Anthropic支持顶层cache_control自动断点，不能以未逐块标记定Bug。prompt_cache_key不是通用启用开关，cached_tokens>0不保证每次必有。fallback复用同语义messages常正确，AC要求primary/fallback messages必不同可能破坏历史/权限。先按provider能力配置与usage可观察，再以弱模型实测证据决定格式适配；P0需真实阻断证据。 | Spec02 Provider/fallback；ADR-0023 稳定prefix；原票AC | [cache_openai](https://developers.openai.com/api/docs/guides/prompt-caching), [cache_anthropic](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) |
| [505](https://github.com/EricKingWhy/intelligence-agent/issues/505) [Tracking] 测试稳定性：间歇红与写锁超时（#376 #338） | 状态纠偏 | 是 | 最新依赖需对账：#508已CLOSED（2026-10-01），#376仍OPEN但用户已批准A挂起观察，#338仍OPEN且有用户升级规则。父票正文与后续评论不能混成“所有红已修”；按已批准closure AC逐项写完成/观察/残余，不能把挂起票当ready自动修，也不能据本轮静态审计关单。 | #376 comment-5939543009；gh实读#508 CLOSED；#338票面 | 以仓库合同/裁决为准 |
| [496](https://github.com/EricKingWhy/intelligence-agent/issues/496) Memory V2 质量层判据：write_precision / write_target_coverage / kind_accuracy 达标（#304 分层裁决③） | 增强 | 是 | 保留用户已批准质量层分票和冻结阈值，不能换模型/改gold/调阈值来凑过。展示各项分子分母、逐例miss和多次完整结果，区分训练调prompt与独立验证集；同一小gold反复调优存在过拟合风险，扩集应另行批准。Memory安全零脏写与coverage/kind质量不同，不用一项替另一项。 | #296最新评论；Tracker MemoryV2 #496；gold v1.9.0 | 以仓库合同/裁决为准 |
| [447](https://github.com/EricKingWhy/intelligence-agent/issues/447) [P2][设计] Capability 运行期事件缝评估（08 spec PORT DESIGN 候选，先限通知型订阅） | 增强 | 是 | 保留notification only，不纳入#521决策hook/权限改写。fire-and-forget仍需强引用/有界队列/卸载清理/异常隔离；notification失败不拖垮Core，但不能无界积压。fake adapter测一次通知+断网/异常/卸载，无真实外部账号时报告blocked而非发送成功。 | Spec08降级；Spec12 Observability；原票notification范围 | [asyncio](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation), [hooks](https://code.claude.com/docs/en/hooks) |
| [384](https://github.com/EricKingWhy/intelligence-agent/issues/384) [W-30] W-21 真实模型 Gate 增补判据（压缩接班 · 清单 · 失败方案） | 增强 | 是 | W-30若依赖W-21已完成，会形成“先发版再补发版判据”的顺序问题；应在最终发布Gate执行前合入判定器，消费W-21 harness而非要求先passed。依赖#365边含义需区分。失败路径禁止重走应限定相同条件/无新证据，合理修正输入后的重试不是违规；清单逐字一致限定压缩本身无plan更新，实时合法更新不能判失败。 | #365 W-21 Gate；Workbench PRD §6；#364夹具 | [long_anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), [long_codex](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex) |
| [382](https://github.com/EricKingWhy/intelligence-agent/issues/382) [W-28] 进度清单 TUI 渲染（Pi 独立 TUI 包复用） | 纠偏 | 是 | 已批准TS TUI首选Pi独立包，当前package为@earendil-works/pi-tui 0.99.1，node:test测试形态；不能无依据加Ink/另一个renderer。复用实际组件/测试工具，纯函数清单投影+真实Windows终端验证Unicode/窄宽/滚动与键盘。50条软上限不得静默丢item。无需引入Pi Agent Runtime。 | Workbench PRD §8明确TS复用；D:/reference/pi/packages/tui/package.json:2/15 @1b347794 | [pi_tui](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/tui/README.md#L743-L751) |
| [376](https://github.com/EricKingWhy/intelligence-agent/issues/376) [Testing] 全量 pytest 下 memory-v2 写锁超时红：test_v2_bulk_confirmation_settings_and_session_recall_redaction（database is locked @ BEGIN IMMEDIATE） | 状态纠偏 | 是 | 2026-10-02用户已批准A挂起观察：保持OPEN、不修复、不登记已知flake；票面“下一步时间盒”不能覆盖最新裁决。busy_timeout=10s和SQLITE_BUSY不独自证明同进程某连接持锁>10s（锁可来自别进程、调度/特殊busy路径）；下次先记录error code、DB路径、实际等待、活连接/进程与服务态，探针当前树适配。仓库外D:/t376_logs证据不可跨机复跑，须有安全可共享来源。 | #376 comment-5939543009/5939260852；SDD §8.6 | [sqlite](https://www.sqlite.org/rescode.html), [python_sqlite](https://docs.python.org/3/library/sqlite3.html#sqlite3.Error) |
| [370](https://github.com/EricKingWhy/intelligence-agent/issues/370) [T9 残余 15] 委派子 agent 的生效策略面未定义 ⇒ 子 run 的 stuck 暂停不支持 policy_change | 状态纠偏 | 是 | #372已于2026-09-30 CLOSED，入口alias不可恢复是历史前提，应先确认当前代码；本票剩余是child自身effective policy定义/取证，不能照抄parent未生效策略。字段省略不是policy change，scope只可收窄，重启与pause/resume摘要同源；用#372交付入口验证本票AC。 | gh实读#372 CLOSED；ADR-0048 D6/D8/残余15；Spec10 | 以仓库合同/裁决为准 |
| [368](https://github.com/EricKingWhy/intelligence-agent/issues/368) [W-24] 证据保留 空间显示与显式清理预览 | 增强 | 报告内建议 | 清理预览/CAS与引用保护方向符合PRD。补索引一致性、跨Task共享ref与误删失败；snapshot原件也不能被同清理删。归档与删除语义分开，远程对象按store能力验证；不以Pi Session删除证明已有Artifact引用回收器。 | Workbench PRD §4.3/§5；Spec06 Artifact | 以仓库合同/裁决为准 |
| [367](https://github.com/EricKingWhy/intelligence-agent/issues/367) [W-23] Task 创建 验收项与队列操作入口 | 增强 | 报告内建议 | 创建入口字段充分。执行前明确服务端Contract与UI交付拆分；进度文件失败需补租约/队列/Session partial创建收口，防永占目录。草稿保留不在客户端存秘密；junction最终目录按服务端规范化。 | Workbench PRD §4.1；#354/#358/#363 | 以仓库合同/裁决为准 |
| [366](https://github.com/EricKingWhy/intelligence-agent/issues/366) [W-22] client_absent 持久暂停与显式恢复契约 | 保留 | 报告内建议 | client_absent有用户批准ADR/规格依据，不应再次判“新增未批准pause”。注意UNKNOWN优先、无closeout模型请求、旧入口orphaned不变，继续按票面真实退出与CAS验证。 | ADR-0046-client-presence-safe-pause；Spec02 §5.2.1 | 以仓库合同/裁决为准 |
| [365](https://github.com/EricKingWhy/intelligence-agent/issues/365) [W-21] 两次独立真实模型 Windows Desktop TUI 发布 Gate | 增强 | 报告内建议 | 两次独立完整真实Gate是用户冻结合同，不能改为mock或片段补测。明确Chrome缺席使必需UI挑战blocked；可选Docker缺席不能自动推导全部产品发布blocked，按其独立lane报告。W-30判定器须执行前接入。 | Workbench PRD §6；#384/#364 | [long_anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), [long_codex](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex) |
| [364](https://github.com/EricKingWhy/intelligence-agent/issues/364) [W-20] 跨窗口导入故障挑战夹具与判定器 | 增强 | 报告内建议 | 挑战fixture与真实Agent Gate分离合理。kill必须实际落在外部DB commit后、ToolResult前，使用可观察barrier而非sleep；坏修复负控含幂等、旧数据与UI假成功，根因不能泄露给模型。 | Workbench PRD §6；Spec07 Ledger | [long_anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), [long_codex](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex) |
| [363](https://github.com/EricKingWhy/intelligence-agent/issues/363) [W-19] Windows Sandbox 显式选择与缺依赖提示 | 增强 | 报告内建议 | Local/Docker显式选择与不静默降级正确；ADR-0041明确cwd的结构不可变，在途/历史Session切换Sandbox需解释路径/artifact语义，不能只改UI值。Docker恢复缺席报告独立blocked/skip。 | ADR-0041 §1.1；Workbench PRD §4.1；Spec05 | 以仓库合同/裁决为准 |
| [362](https://github.com/EricKingWhy/intelligence-agent/issues/362) [W-18] 可选 Chrome DevTools MCP 与登录态审批 | 增强 | 报告内建议 | 采用官方Chrome MCP正确；readOnlyHint不是站点授权，需审阅导航/脚本工具可能的副作用和整个profile访问范围。Cookie不落证据；断开token生命周期依实际SDK/连接模式核实，不承诺不存在的会话token。 | ADR-0012 D5；Workbench PRD §4.5；Spec09 | [chrome](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/advanced-usage.md) |
| [361](https://github.com/EricKingWhy/intelligence-agent/issues/361) [W-16] Windows 安装 更新 卸载与旧数据回退 | 增强 | 报告内建议 | 安装与数据回退票覆盖充分。补冻结词表缓存（#570）、原生依赖/Windows凭证冷启动；不能用装有开发依赖的主机代替干净VM。旧schema备份与旧程序可回读一起验收。 | Workbench PRD §4.5；#570 | [electron](https://www.electronjs.org/docs/latest/tutorial/security) |
| [360](https://github.com/EricKingWhy/intelligence-agent/issues/360) [W-17] 基于 Pi 独立组件的 TypeScript TUI | 增强 | 报告内建议 | TS TUI复用Pi独立包已由产品PRD批准，与Core Python不矛盾。补真实Windows PTY/终端兼容、审批与reconcile可达、附着服务而非第二InstanceLock；不得引入Pi loop或整包Runtime。 | Workbench PRD §4.5/§8；Spec11 CLI | [pi_tui](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/tui/README.md#L743-L751), [electron](https://www.electronjs.org/docs/latest/tutorial/security) |
| [359](https://github.com/EricKingWhy/intelligence-agent/issues/359) [W-15] Electron 薄宿主 托盘与安全桥 | 增强 | 报告内建议 | Electron薄宿主边界合理；把nodeIntegration=false/contextIsolation/sandbox、细粒度IPC、sender验证、CSP/navigation/外链列可执行负控；Python进程与token主进程所有权核实。 | Workbench PRD §4.5；Spec11；#355 | [electron](https://www.electronjs.org/docs/latest/tutorial/security) |
| [358](https://github.com/EricKingWhy/intelligence-agent/issues/358) [W-14] 桌面 TUI Web 统一安全默认权限 | 待裁决 | 是 | 产品已批准更严格权限方向，但当前ADR-0027仍明确Local不是目录硬围墙；实现前写清supersession与旧Session迁移/default矩阵。对任意Bash无法靠字符串过滤保证目录隔离，审批也不等于OS containment。#549相同默认与边界问题由本票统一owner；安全目标必须与Windows可实现机制一致，架构改变先决策。 | Workbench PRD §4.1；ADR-0027 D2/Security；Spec05 | [docker_init](https://docs.docker.com/engine/containers/multi-service_container/), [electron](https://www.electronjs.org/docs/latest/tutorial/security) |
| [357](https://github.com/EricKingWhy/intelligence-agent/issues/357) [W-13] 重启后的 Ledger reconcile 与手动续跑体验 | 增强 | 是 | W-13恢复界面依赖用户可达Reconcile决策API，#547正是同一缺口；先统一backend合同再做客户端，不另做UI-only状态。用户判已成功必须留下用户裁决和来源，不伪造自动验证；UNKNOWN先查实际系统，重复提交/CAS/重启幂等测试。 | Workbench PRD §4.4；Spec07 §9；#547 | [rabbit](https://www.rabbitmq.com/docs/confirms) |
| [356](https://github.com/EricKingWhy/intelligence-agent/issues/356) [W-12] 按 Task 的客户端在场与安全暂停 | 增强 | 是 | W-12依赖W-22正确；还消费W-05的暂停前进度写入与W-10队首在场条件，需显式接口依赖，避免presence↔lease互相调用循环。定义“无需服务Task”是否含durably paused；退出后等待Ledger收口而非强杀。30秒grace不发新模型，重连不自动续跑。 | Workbench PRD §3/§4.4；#349/#354/#366 | [asyncio](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation) |
| [355](https://github.com/EricKingWhy/intelligence-agent/issues/355) [W-11] 唯一本机 Python 服务、鉴权与旧数据复用 | 增强 | 报告内建议 | 单Python服务/loopback鉴权合同充分。验证token不进URL/renderer日志、PID重用不能误kill、两冷启动争锁只有一owner；旧CLI改attach不触发另一次startup orphan scan。 | Workbench PRD §4.5；CONTEXT StartupInterruptionScan | [electron](https://www.electronjs.org/docs/latest/tutorial/security) |
| [354](https://github.com/EricKingWhy/intelligence-agent/issues/354) [W-10] 同目录写入租约及持久排队 | 增强 | 是 | 持久目录租约需要W-12在场信息，W-12又消费租约状态：拆为只读seam，先定义服务端presence合同避免依赖环。暂停/await review不释放是用户决定；显式release后原Task再次写须重新取得租约。Windows路径交集含parent/child、junction、UNC和无法解析fail-closed；不能只比较原字符串。 | Workbench PRD §3/§4.1；#356；Spec05路径边界 | 以仓库合同/裁决为准 |
| [353](https://github.com/EricKingWhy/intelligence-agent/issues/353) [W-09] 任务 diff 与证据审阅界面 | 增强 | 报告内建议 | 六状态审阅页方向符合PRD；依赖#352真实证据与#354租约动作，缺backend DTO时不能自造local truth。端到端至少真实FastAPI→React，fake fixture仅渲染回归。 | Workbench PRD §4.2；web/PRODUCT.md | 以仓库合同/裁决为准 |
| [352](https://github.com/EricKingWhy/intelligence-agent/issues/352) [W-08] 验收项与真实证据的服务端投影 | 增强 | 是 | 全workspace hash包含progress.md时，更新进度会让刚记录证据自我过期。按PRD标明证据覆盖的精确文件manifest、独立元数据hash并明确过期原因，不能悄悄声称同一树。artifact ref必须可回读+权限检查；manifest不是新workspace恢复系统，勿引#527快照基础设施。 | Workbench PRD §4.2末段；Spec06 Artifact；#349 | [long_anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), [long_codex](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex) |
| [351](https://github.com/EricKingWhy/intelligence-agent/issues/351) [W-07] Task 交付状态与验证/接受分轴契约 | 保留 | 报告内建议 | Run结束、验证、用户接受分轴是正确合同；用户带缺项接受不能转绿色pass。已有#305 CompletionPolicy不重造，服务端CAS+Event投影作为唯一真相。 | Workbench PRD §3；Spec02 §5.4 | 以仓库合同/裁决为准 |
| [350](https://github.com/EricKingWhy/intelligence-agent/issues/350) [W-06] 进度文件重读、冲突核对与 Fork 分家 | 增强 | 报告内建议 | 进度文件核对/恢复符合目标；外部文件修改只是差异不是授权。hash/seq/source回读与缺文件显式受阻都要验证，fork文件与父独立。 | Workbench PRD §4.3；Spec03 History/Context | [long_anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), [long_codex](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex) |
| [349](https://github.com/EricKingWhy/intelligence-agent/issues/349) [W-05] 项目可见进度文件的原子生成 | 增强 | 报告内建议 | 原子进度文件与不自动Git add正确。写入失败不得截断旧文件；只放脱敏结论和可回读ref，不能把不可用ref包装成证据；控制更新触发和manifest过期联动。 | Workbench PRD §4.3；#352 | [long_anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), [long_codex](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex) |
| [344](https://github.com/EricKingWhy/intelligence-agent/issues/344) [Spec][Product] 个人可恢复、可审阅的长任务工作台（Windows Desktop + TS TUI） | 增强 | 报告内建议 | 产品父票总体完整且已有用户决策，不另造Roadmap。子票状态/依赖与跨端拆分需保持一个owner；产品TS TUI允许复用独立包，与ReuseMatrix拒Pi Core Runtime并不冲突。发布前解决#547↔W-13、#549↔W-14、#556↔保护事实与W-30顺序。 | Workbench PRD全篇；Spec01 Core边界；#357/#358/#384 | 以仓库合同/裁决为准 |
| [338](https://github.com/EricKingWhy/intelligence-agent/issues/338) [Testing] 具名间歇红（无签名）：test_v2_list_filters_detail_versions_edit_stale_version_and_identity（T7/T8 合计 2/5 遍全量出现） | 保留 | 报告内建议 | 未知签名间歇红保持用户“暂不修、不阻断；再现且留签名停线”的裁决。不能改记已知flake或凭未再现关单。2/5是历史样本不构成当前失败概率；日志需完整保存并绑定树/环境/顺序。 | #338升级规则；SDD §8.6 | 以仓库合同/裁决为准 |
| [305](https://github.com/EricKingWhy/intelligence-agent/issues/305) [Spec][Agent Runtime] 长任务分层预算、暂停恢复与可靠完成判定 | 状态纠偏 | 是 | 父票保留OPEN遵守最新裁决；#372已CLOSED，只剩#370相关收口需核验，不能继续写两票均在途。父body旧max_steps alias与paused三类是历史PRD；当前Spec/#320已迁移、client_absent是定向批准扩展，按版本引用，不从旧body推翻当前合同。Fork新Budget继续按AC-14，不能被#554反向改写。 | #305最新评论；gh#372 CLOSED；ADR-0046客户端presence；Spec02/03 | 以仓库合同/裁决为准 |
| [296](https://github.com/EricKingWhy/intelligence-agent/issues/296) [Spec][Phase 6] Production Long-Term Memory V2 | 状态纠偏 | 是 | 实施子票已关闭、#304阶段性证据收口、质量层#496仍OPEN是当前状态；09-30评论R7待独立敏感层/401等不再是当前阻塞。保留原文历史并指向用户R7裁决、ADR-0049 user/project范围与#496验收；不能宣称所有blocking质量阈值已通过，也不因缺task/agent scope重新开Gap。 | #296 comment-5931235894；ADR-0049；Tracker MemoryV2 | 以仓库合同/裁决为准 |

## 参考来源与能力边界

成熟产品参考的作用是验证机制与能力边界，不以产品名、第三方转述或官方文档存在作为方案已可用的证明。代码参考只有 Pi 使用已读本地 checkout 的 file:line + commit；tiktoken 使用已安装包版本及对应官方 tag；其他来源为官方文档/文章（读取日期 2026-10-03）。未核实的原票外部引用未在补充中背书。Pi 引用不支持把 Pi Agent Runtime 导入 Core。没有新增依赖，没有复制第三方源码；采用代码前仍须核 License 并保留来源。

### tokens

[OpenAI：Token 计数说明](https://help.openai.com/en/articles/4936856-understanding-and-counting-tokens) | 2026-10-03

chars/4 是英语近似，不是各语言硬上界；用于纠正估算安全性推定。

ADAPT（计量原则）

### tiktoken

[tiktoken 0.13.0：缓存加载源码](https://github.com/openai/tiktoken/blob/0.13.0/tiktoken/load.py) | 2026-10-03

缓存路径、命名与内容校验需符合库加载协议；缺缓存仍可能下载，目录变量不等于离线开关。

REUSE（已装依赖；本轮无代码复制）

### tracemalloc

[Python：tracemalloc](https://docs.python.org/3/library/tracemalloc.html) | 2026-10-03

用分配快照差分定位 Python 内存增长；不能覆盖所有原生内存或把 RSS 增长直接判为泄漏。

REUSE（标准库）

### pyspy

[py-spy 官方仓库](https://github.com/benfred/py-spy) | 2026-10-03

调用栈与 CPU 采样工具；不把它写成堆分配分析器。

DEFER（非本票堆证据）

### json

[RFC 8259：JSON](https://www.rfc-editor.org/rfc/rfc8259) | 2026-10-03

交换文本使用 UTF-8；文法可能容纳孤立代理，但互操作结果不可预测。项目深度上限、拒绝或替换须另定合同。

ADAPT（协议依据）

### sqlite

[SQLite：Result Codes](https://www.sqlite.org/rescode.html) | 2026-10-03

区分 BUSY、FULL、IOERR 等错误；错误名本身不能证明某进程持锁时长。

REUSE（既有存储错误码）

### python_sqlite

[Python sqlite3：异常属性](https://docs.python.org/3/library/sqlite3.html#sqlite3.Error) | 2026-10-03

sqlite_errorcode/sqlite_errorname 可辅助精确错误归因；不能凭所有 OperationalError 一律重试。

REUSE（标准库）

### asyncio

[Python asyncio：取消与 shield](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation) | 2026-10-03

shield 保护内部任务，但调用者仍会收到取消；需保留任务强引用并定义收尾等待。

REUSE（标准库；不替代 kill/reconcile）

### rabbit

[RabbitMQ：确认机制](https://www.rabbitmq.com/docs/confirms) | 2026-10-03

发布确认与消费确认是不同边界；借鉴接收、持久化、执行各自确认语义，不视确认作副作用完成证明。

PORT DESIGN（不引入 RabbitMQ）

### outbox

[AWS：Transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html) | 2026-10-03

将数据库变更与 outbox 同事务提交，再异步投递；消费仍须幂等。这不使 JSONL、文件和远端索引天然同事务。

PORT DESIGN（复用仓库既有边界；不新增 AWS 服务）

### milvus

[Milvus：Upsert Entities](https://milvus.io/docs/upsert-entities.md) | 2026-10-03

按主键插入/更新实体；集合计数不是内容完整证明，具体一致性和 upsert 行为需按部署版本验证。

REUSE + ADAPT（既有 vector provider）

### mcp_transport

[MCP 2025-06-18：Transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) | 2026-10-03

JSON-RPC 请求关联与 SSE 恢复 cursor 分属不同层；重连不授予远端 mutation 重试权。

REUSE + ADAPT（官方 SDK）

### mcp_elicitation

[MCP 2025-06-18：Elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation) | 2026-10-03

能力协商和 accept/decline/cancel 交互可借鉴；三态不能证明外部副作用，ADR-0012 当前仍 DEFER。

DEFER（启用前须决策）

### sse

[WHATWG HTML：Server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html) | 2026-10-03

末尾未完成事件不派发；不意味着已完成帧承载的模型部分内容应删除，也不支持按文本去重。

ADAPT（帧与答案层分开）

### react

[React：External store subscriptions](https://react.dev/learn/you-might-not-need-an-effect) | 2026-10-03

外部状态订阅需按生命周期管理；本仓优先修已有 stream 订阅，不能据此另造 Session 真相。

ADAPT（既有前端依赖）

### electron

[Electron：Security](https://www.electronjs.org/docs/latest/tutorial/security) | 2026-10-03

隔离 renderer、限制 Node、验证 IPC sender、CSP 与导航；进程隔离不自动构成任意命令的目录围墙。

ADAPT（薄宿主安全合同）

### chrome

[Chrome DevTools MCP：Advanced usage](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/advanced-usage.md) | 2026-10-03

优先官方浏览器连接能力；具体 profile、启动方式、凭证生命周期需按采用版本和实际连接模式核实。

REUSE + ADAPT（官方实现；尚未批准特定版本）

### pi_tui

[Pi TUI：固定 commit 文档](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/tui/README.md#L743-L751) | 2026-10-03

差分重绘及同步输出可复用；本地 package 名 @earendil-works/pi-tui，版本 0.99.1，测试用 node:test。产品批准独立 TUI 包不等于批准 Pi Runtime。

REUSE + ADAPT（独立包；采用前核许可证）

### pi_edit

[Pi：edit 工具合同](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/coding-agent/src/core/tools/edit.ts#L23-L25) | 2026-10-03

oldText 合同要求唯一匹配；这一证据不足以支持废弃本仓 exact edit 或保证新的 hashline 安全。

PORT DESIGN（合同对照；不复制代码）

### robocopy

[Microsoft：Robocopy](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/robocopy) | 2026-10-03

/CREATE 只建立目录树和零字节文件，不能用来复制内容或作为 CoW。block cloning 是否可用须独立验证。

纠正方案；DEFER 未验证 CoW

### docker_init

[Docker：Multiple processes](https://docs.docker.com/engine/containers/multi-service_container/) | 2026-10-03

--init 辅助回收僵尸进程；不能由此承诺所有存活 detached 进程已终止。

ADAPT（限定能力）

### skills

[Anthropic：Agent Skills overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview) | 2026-10-03

Skill 用于影响任务执行，官方要求谨慎信任来源；安全验收应守 Runtime 权限边界，而非要求 Skill 不影响行为。

PORT DESIGN（可信来源与渐进加载；不改 Core）

### hooks

[Claude Code：Hooks](https://code.claude.com/docs/en/hooks) | 2026-10-03

PreToolUse 可拦截，PostToolUse 时工具已执行；观测通知和决策 hook 应分开，参数变化后重新验证权限。

PORT DESIGN（事件边界；不复制宿主实现）

### tool_anthropic

[Anthropic：Tool search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool) | 2026-10-03

defer_loading 减少模型上下文暴露，但完整工具定义仍随请求提供；发现工具不等于获准调用。

ADAPT 候选（需测 schema 成本与 Provider 能力）

### tool_openai

[OpenAI：Tool search](https://developers.openai.com/api/docs/guides/tools-tool-search) | 2026-10-03

官方支持延迟工具发现；具体模型/API 能力与本仓动态绑定兼容须实测，不能默认所有 Provider 相同。

ADAPT 候选（不新增隐藏执行通路）

### cache_openai

[OpenAI：Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) | 2026-10-03

缓存自动路由，稳定 prefix 有利复用；prompt_cache_key 不是跨 Provider 的启用开关，也不保证每次命中。

ADAPT（Provider 层）

### cache_anthropic

[Anthropic：Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) | 2026-10-03

支持顶层 cache_control 自动断点及显式控制；不能仅以缺逐块标记断言缓存失效。

ADAPT（Provider 层）

### anthropic_context

[Anthropic：Context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing) | 2026-10-03

工具内容清理是上下文管理机制；不能据此假设裁剪用户保护事实无损，原文回读仍由本仓 Event/Artifact 保证。

PORT DESIGN（不改变保护事实合同）

### long_anthropic

[Anthropic：Long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | 2026-10-03

进度文件、特征清单、逐项工作和真实端到端验证可借鉴；这些机制不保证所有约束自动保全。

PORT DESIGN（Workbench 已批准目标）

### long_codex

[OpenAI：Long-horizon Codex tasks](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex) | 2026-10-03

长任务的规划与持续验证经验作对照；本仓预算、恢复和发布门禁仍按冻结规格验收。

PORT DESIGN（参考方法，不移植 Runtime）

### agents_openai

[OpenAI：AGENTS.md discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md) | 2026-10-03

从项目根到 cwd 收集目录指导，存在覆盖次序与大小上限；这是 Codex 的发现策略，不是本产品已批准默认值。

PORT DESIGN（待定义本仓合同）

### claude_memory

[Claude Code：Project memory](https://code.claude.com/docs/en/memory) | 2026-10-03

项目规则与记忆有自己的加载约定；只借鉴明确来源与发现范围，不混成通用指令优先级保证。

PORT DESIGN（待定义本仓合同）

Pi source checkout: `D:/reference/pi` @ `1b347794e2a630e4359f2584f4eea388145d0ddf`.
Read anchors: `packages/tui/package.json:2,15`; `packages/tui/README.md:743-751`; `packages/coding-agent/src/core/tools/edit.ts:23-25`.
tiktoken installed package: `.venv/Lib/site-packages/tiktoken/load.py:35-68` (0.13.0); official tag linked above.

## 操作证据与重读入口

- 本地原始快照：`.codex-issue-audit/audit-records.json`（68 张正文/评论）。
- 逐票意见与来源：`.codex-issue-audit/findings.json`、`sources.json`。
- 发布回执：`.codex-issue-audit/publish-receipts.json`（每次写入后回读原文完整性/标题/状态/标签）。
- 本报告只交付 issue 核查。没有产品代码修改、push、merge、关单或执行外部通知。
