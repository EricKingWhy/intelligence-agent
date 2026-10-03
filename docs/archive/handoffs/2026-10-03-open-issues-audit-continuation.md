# 开放 Issue 核查续做手册

日期：2026-10-03（北京时间）。仓库：`D:\intelligence-agent`。GitHub：`EricKingWhy/intelligence-agent`。

## 给接手模型的任务

继续完成用户授权的开放 issue 全量核查与票面优化。用户要求参考建议及成熟产品方案必须有依据；随后因额度不足要求先写本手册、交由其他模型续做。

本轮已读取当时全部 **68 个开放 issue 的正文和评论**，形成 68 项意见，其中 **51 项拟补入 GitHub 正文**，其余 17 项留报告建议。**GitHub 修改次数为 0；不存在已发布的增强结论。** 产品代码、Issue 标题/标签/状态/指派/依赖均未修改。尚未执行缺陷复现、pytest、故障注入或完整实现审查，不能写成“68 个 Bug 已验证”。

当前是交接停止点，不继续发布。接手模型在同一用户任务下继续时，可按原授权做事实纠正和参考补充；改变冻结 AC/架构、拆票、关单、Git 发布另按项目授权，不因本手册自动获准。

## 一、恢复入口与实际基线

新窗口第一项必须完整读取 `docs/SDD_WORKFLOW_PROTOCOL.md`，连续读至 EOF；之后读 Tracker 当前态、相关票及 Git/review ledger。不得先读状态后补协议。这是 AGENTS §16 的强制顺序。确认本任务后再按 §3 实读相关规则，列五项启动检查表。不要把上一模型的 READY 当成自己的阅读证据。

交接时分支 `main`，HEAD：

`94c0d29e0d345291f21143575f12895733c18695`

这是 PR #575 合并树。原工作树 clean；现在新增的仅为审计草稿/脚本/报告/本手册，均未提交。其他 linked worktree 未写入。若 HEAD/工作树已变化，重新核对受影响源码结论；不要强制 reset、切分支或覆盖他人改动。

规格根必须用：

`goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/`

根 `docs/spec/` 不是 Engineering Specification。

上一模型已完整阅读首次五文件 README/00/01/13/14、模块 02–12、CONTEXT、Workbench PRD、PRODUCT、相关 ADR、issue-tracker、reference-sources、review-debug-playbook、完整 SDD。设计机制来自实际来源核实；这些记录供定位，不豁免接手模型自己的必读协议。

## 二、所有成果都在本机

| 文件 | 用途 |
| --- | --- |
| `docs/research/2026-10-03-open-issues-audit.md` | 用户可读报告：68 票意见、重点问题、产品裁决边界、成熟方案对照、32 个来源链接及判定。当前明确“尚未写入 GitHub”。 |
| `.codex-issue-audit/audit-records.json` | 初始 68 张完整正文、评论、标签、更新时间等快照。不要整文件输出：约 385KB 且只有一行。用 JSON 解析按 number 提取。 |
| `.codex-issue-audit/findings.json` | 68 条结构化结论：`[number, status, edit_bool, note, repo_evidence, source_keys]`。51 个 edit_bool=true。 |
| `.codex-issue-audit/sources.json` | 32 个官方/固定版本来源，包含机制、REUSE/ADAPT/PORT DESIGN/DEFER 判定与读取日期。 |
| `.codex-issue-audit/report-content.json` | 报告和票面模板的中文全文。 |
| `.codex-issue-audit/bodies/<number>.md` | 51 张可审阅票面：顶部补充、底部原正文完整保留。尚未发布。 |
| `.codex-issue-audit/body-manifest.json` | 51 张正文路径及新旧 SHA256。 |
| `.codex-issue-audit/render_audit.py` | 已实际运行成功的渲染脚本，生成报告、正文及 manifest，使用原子替换和字节断言。 |
| `.codex-issue-audit/reference-findings.md/.json` | 来源核实子代理的辅助意见，主模型已读取；不是全部采信的最终结论。以 findings/report 为主。 |

所有文件均是本机未提交文件；换 clone/机器时需要带走上述成果，Git 拉取不会自动取得。无需重新抓取/重写已经留存的初始正文。续做时只抓新状态和变更票。

已运行命令：

```powershell
.venv/Scripts/python.exe .codex-issue-audit/render_audit.py
```

成功读数：68 issues、51 body_files、0 verified。生成报告 SHA256：

`72852dc27a726c0b5566e40b91d4c723362ef57273049d765ca90144f81c4e0e`

渲染初次因 `read_text` 换行归一化与原正文 CRLF 不一致触发断言，已改用 `read_bytes().decode("utf-8")`，复跑成功。不要为了比较方便改写原票换行/正文。

## 三、关键结论：避免接手后反向修错

1. **#554 规格误读**：Spec02 §5.1、Spec03 §7、#305 AC-14 明确 fork 建新 SessionBudget、消耗独立，不能修成继承父累计消耗。部署/user 总成本限制是新产品提议。
2. **#527 技术方案错误**：Microsoft 官方 /CREATE 只建目录和零字节文件，不能作为 CoW；hardlink 原地写 child 会改变 parent。block cloning 是否可用须独立验证，不能反向声称 NTFS 永远不支持。
3. **#570 不安全降级**：tiktoken 是直接依赖；chars/4 是英语近似，不是中文/emoji/schema 硬预算安全上界；常量兜底不能伪造低消费。离线缓存需符合库命名和完整性校验。
4. **#552 预算数据**：不得 clamp usage 或把缺失/异常写 0；先验输入后落盘，不能返回 422 前已经消费预算。
5. **#562 AC 冲突**：深度 800/1500 与建议上限 100 不兼容，需要统一项目合同。
6. **#546 证据不足**：ScriptedModel 输出哨兵证明内容可见、权限允许，不证明真实 prompt injection 成功；Skill 本来影响行为，安全边界应是 Runtime 不越权。
7. **#556 无界累积源码可见**，但直接截断最近 N 条会影响已批准 ProtectedFact 保全；裁剪与完整信息保全的兼容方式待裁决。
8. **#559 并发闸门静态疑点**：assembly.py 每次 build_runtime 建 ModelCallGate，与进程级合同不一致；本轮未运行多 Session barrier，不宣称已复现。
9. **#550 shield 不消除调用者取消**；**#549 --init 不保证杀活跃 detached 进程**；**#563 py-spy 不是堆 profiler**。
10. **#520 缓存**：OpenAI 自动缓存；Anthropic 有顶层 cache_control 自动断点。缺逐块标记不是故障充分证据，primary/fallback 必须不同 messages 的 AC 也过强。
11. **#528 defer_loading**：Anthropic 完整定义仍随请求发送，省模型上下文不等于省传输。
12. **#376 已批准 A 挂起观察**：保持 OPEN、不修、不登记已知 flake；#338 同样遵守既有升级裁决。不要照旧正文时间盒自动施工。
13. **#372、#508 已 CLOSED**，父票 #305/#505 状态需按最新评论更新，不把历史前提当现在事实。
14. **#296 R7 已裁决不加独立敏感检测层**；ADR-0049 明确当前 user/project scope；剩余质量门是 #496。不要重开已收口范围。
15. **TS TUI 已被 Workbench PRD 批准复用 Pi 独立包**，不是 Pi Agent Runtime。子代理辅助文档若把 Core 的 TypeScript DEFER 误套到独立 TUI，不能照抄。
16. **MCP elicitation 仍 DEFER**，#521 不自动解除。#547/#357 应共享 Reconcile 后端合同；#549/#358 统一权限默认 owner；#384 判定器须在 #365 最终 Gate 前接入。

本轮未决定新的架构，也未编施工计划。顶部补充明确“事实纠正与候选验收建议”，保留原正文，避免把建议伪装成已批准 AC。

## 四、成熟方案与来源证据

最终报告已有机制、适用边界和判定，直接读取相应 source_keys，不只贴产品名称：

- 长任务：Anthropic long-running harness、OpenAI long-horizon Codex，借鉴进度/清单/真实验证。
- 缓存与工具发现：OpenAI、Anthropic 官方 API 文档；按 Provider 能力 ADAPT，不变更统一 ToolExecutor 权限。
- 持久确认：RabbitMQ confirms、AWS transactional outbox；仅 PORT DESIGN，不引入 broker/AWS 服务。
- 索引：Milvus 主键 upsert 与 provider 状态；计数不等于完整性。
- Windows：Electron 安全、Chrome DevTools MCP、Pi 独立 TUI 包。
- Skill/规则/hook：Codex AGENTS、Claude memory/hooks、Anthropic Skill；before/after 与通知/决策明确分开。

Pi 已读本地 `D:/reference/pi`，commit `1b347794e2a630e4359f2584f4eea388145d0ddf`：

- `packages/tui/package.json:2,15`：@earendil-works/pi-tui 0.99.1、node:test。
- `packages/tui/README.md:743-751`：重绘/同步输出。
- `packages/coding-agent/src/core/tools/edit.ts:23-25`：唯一 oldText 合同。

tiktoken 已读本机安装 0.13.0 的 load.py:35–68，对应官方 tag 已链接。它是安装包来源，不冒称本地浅克隆 commit 证据。其他为官方文档/文章，读取日期 2026-10-03。设计选型实施前仍按 SDD §1.3 核两个独立来源、版本、License；本次没有复制或 port 第三方代码。

## 五、续做步骤：不要重做全量前期阅读/取数

1. 按恢复入口完成协议和本任务前置；读最终报告、findings、sources，确认 68/51 和既有裁决。
2. 用 gh 重列当前所有 OPEN issue，显式 `--limit 1000`，防默认只得 30 条。比初始 snapshot：新增的补查；关闭的跳过；正文/评论变化的重新核对。PR 不混入 issue。
3. 抽查并逐票审阅 51 张草稿，尤其以上关键票。若新版事实推翻结论，只改相关候选补充，不自行变更冻结合同。
4. **仅正文补充**可沿用户“优化增强”授权执行，不发通知、不改标签/状态/标题/依赖、不拆票。若决定扩大已批准范围，先列具体证据/影响并请用户决定。
5. 用落盘脚本顺序发布，subprocess 参数列表、禁止 shell 文本拼接。每票流程如下：
   - `gh issue view <n> --repo EricKingWhy/intelligence-agent --json body,state,title,labels,updatedAt`。
   - state 必须 OPEN；当前 body 必须与初始快照 body 完全一致。若不一致停止该票写入、重新核对，不能覆盖别人新正文。
   - 草稿读字节 decode，校验 SHA256 与 manifest，且原始正文完整作为 suffix。
   - `gh issue edit <n> --repo EricKingWhy/intelligence-agent --body-file <绝对路径>`。
   - 再 view 回读，确认 body==预期、原标题/状态/标签未变；记录前后 hash、时间、结果。原始评论不变。
   - 每票原子更新 `.codex-issue-audit/publish-receipts.json`，结构为数组，每条至少 number/status/after_sha256/reason；成功 status=`verified`，失败/跳过如实登记。render 脚本已经支持此文件。
   - 超时先回读确认是否实际成功，再决定重试，避免重复补充。已有相同 marker 且正文等于预期可登记 verified，不叠加块。
6. 完成后重新运行 render_audit.py，使报告真实展示 verified 数。重列 OPEN issue 确认全量范围；新票/变化票缺口单列，不称“所有已检查”而实际漏掉。
7. 检查 status/diff 与生成文档，报告实际发布数/失败数/剩余裁决。这轮是 issue/文档任务，不拿 pytest 冒充票面验证；真正证据是 GitHub 回读和来源对账。无需因本次审计跑产品全量门禁。
8. 给用户报告链接与严重问题摘要。用户尚未批准代码修复/拆票/关闭/推送，不顺手执行。

目前 **publish 脚本未创建/未运行，publish-receipts.json 不存在**。接手模型需要完成上述真实发布及回读流程，不能把 51 个本地 body 文件当成 GitHub 已更新。

## 六、可直接复制给其他模型的提示词

> 在 D:\intelligence-agent 继续上次“开放 issue 全量核查和优化增强”。先完整读 docs/SDD_WORKFLOW_PROTOCOL.md，再按 AGENTS 恢复顺序核对 Tracker/Git。读本手册和 docs/research/2026-10-03-open-issues-audit.md；初始 68 张已读、51 张正文补充草稿及来源均在 .codex-issue-audit，GitHub 尚未修改。不要重写整个审计，不做产品代码。核对当前开放集合与变化票，审阅草稿，按手册逐票 gh body-file 更新和回读，不覆盖新正文；保留原文与已批准合同，记录真实回执后重生成报告。严守 #554 fork 新预算、#376 挂起观察、#296 R7 和 MCP elicitation DEFER 等裁决。方案变更待用户决定，当前只发布事实核查与参考补充。
