# #350（W-06）方案依据：进度文件重读、外部编辑冲突与 Fork 分家

协议 §1.3 五字段。核实日期：2026-10-06。

## 来源（≥2 独立来源）

1. **Anthropic「Effective harnesses for long-running agents」**
   `https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents`（HTTP 200，2026-10-06 WebFetch 核实）
2. **Pi Compaction Reference**
   `https://pi.dev/docs/latest/compaction`（HTTP 200，2026-10-06 WebFetch 核实）；本地二手核对：`/home/hatch/workspace/dg-ai-notes`（commit `5f4abd34`，Pi 源码精读笔记，TS 章节对上游核实的既有结论见 `docs/agents/reference-sources.md` 版本漂移实测段）。

## 机制摘要

- **Anthropic**：长任务跨窗口接班靠外置交接文件（`claude-progress.txt` + 特性清单 JSON）。每个新会话**开头先读进度文件与 git log** 取得状态（"Start the session by reading the progress notes file and git commit logs"）；特性清单逐项带 `passes` 状态，模型只能改状态字段、"removing or editing tests is unacceptable"——**完成判定来自可核对的文件事实，不是模型自报**（反模式原文："a later agent instance would look around, see that progress had been made, and declare the job done"）。
- **Pi**：compaction 摘要固定含 Goal / Constraints & Preferences / Progress / Key Decisions / **Next Steps**，原始 entry 保留不删（"Omitted raw entries remain stored"）；branch summarization（`/tree` 切换分支）把被离开分支的上下文以 `BranchSummaryEntry` 注入新分支——**父子分支上下文互相隔离**，跨分支只通过显式摘要条目传递。

## 契合点（对 `AGENTS.md` §7 不变量）

- 本仓 progress.md 已是 SessionEvent 的确定性投影（W-05/#349），与 Anthropic 的"完成判定来自文件事实"同构；W-06 增加的是**磁盘重读对账**（digest 复用 `progress_content_digest` 公共契约，禁第二套归一化——W-05 残余⑤的收口点）。
- Pi 的 Next Steps 摘要节 ↔ 本票"压缩后模型可见输入含经核对的原目标/下一步"：压缩（COMPACTION_END bracket 在场）后注入经核对的进度块，机制与 W-29 清单重注入同形（纯事件判据 + ephemeral SystemMessage 块），不新增持久事件、不碰 compactor。
- Pi 父子隔离 ↔ 不变量 #19/#22 与 spec 03 §7：fork child 独立 progress 文件（parent ID/fork seq 来自 child 事件流里的 `session/forked`，derive 已支持），父文件零写入。
- 外部编辑冲突：文件文本绝不升级为授权（不变量 #11：Permission 是 Runtime 边界）——冲突检测只读不采信，真相更新必须经既有事件路径（丢弃 → 服务端投影重写；确认 → 追加 USER_MESSAGE 新用户指令）。

## 判定

- **PORT DESIGN**（Anthropic 进度文件接班 / Pi 摘要-Next Steps 与分支隔离思想，Python 实现）；不复制任何上游代码。
- **REUSE**：`progress_content_digest` / `derive_progress_document` / `write_progress_file`（W-05 既有）；W-07 task 命令 + `_apply_task_and_refresh`（UI 修改先落确认事件再重写文件的既有链路）；`session/fork.py` 合法边界（本票不改 fork 边界语义）。
- **BUILD**（最小）：verify/compare 函数、builder 进度注入块、conflict-aware writer 守卫、resolve 端点——上游无对应物可直接复用（Pi/Anthropic 无"文件 vs 事件流对账"机制可搬）。

## License

仅思想级借鉴（文章 + 文档），无代码复制，无 License 传导义务。
