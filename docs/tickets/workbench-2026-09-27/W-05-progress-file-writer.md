# W-05 · 项目可见进度文件：来源、脱敏、原子写
**目标仓库**：intelligence-agent-backend（Python SessionStore / 进度文件）。

**类型/优先级**：P0 产品事实投影。**依赖**：W-02。**范围**：新的小型 ProgressFile writer + SessionEvent 投影接口、测试；候选位置 `src/agent_harness/session/`，不把文件写入 Memory V2。

## 文件契约

默认 `<用户选定项目根>/agent-progress/<session-id>/progress.md`，每个 Session 独立。固定标题/字段：schema_version、session_id、parent_session_id/fork_point_seq（可空）、source_event_seq、generated_at、原目标、约束/授权、验收项与状态、已验证里程碑、决策、失败尝试/坑点、未决 Operation、证据 refs、阻塞与下一步。保留精确 ID/金额/用户禁令的原文字面和来源 seq，但**绝不**写凭证值、Cookie、`.env` 值、私密原始 Tool 输出。大原文只留 Artifact ref。文件可进入 Git diff；系统不 `git add`。

## 工作指令

1. 从 W-02 保护事实与既有 SessionEvent 确定性生成内容；首次在目标/约束确认后创建，里程碑/决策/失败结论/压缩前/暂停前/交付前更新。
2. 使用同目录临时文件、flush/fsync（平台可行处）与原子替换；保留可恢复的上一版本及 source seq/hash。Windows 文件锁、进程 kill、权限拒绝、磁盘满不得留下半文件。
3. 脱敏在写入前；检测无法安全呈现的内容时阻止该段而标出缺项。写失败在任务状态可见，不得把旧文件说成最新。

**验收**：正常创建/重复更新幂等，两个 Session 不互覆；写入中 kill 后旧版或新版完整可读；外部只读/锁定目录出现明确失败；文件在 Git diff 中可见但 index 不被自动修改；用假凭证断言正文与日志都无值。focused Windows 文件系统测试与真实目录操作。**不做**：UI 编辑、Fork、自动提交。

**成熟参考/复用**：[Anthropic `claude-progress.txt`](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)与 [Codex 长任务项目文件](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex)提供外置交接思路，`PORT DESIGN`；本仓事件与 Artifact 直接 `REUSE`。

## 方案依据（§6.1/§1.3 五字段，2026-10-04 核实）

- **来源（≥2 独立）**：
  1. Anthropic 工程博客 Effective harnesses for long-running agents（票面自带，2026-10-04 WebFetch HTTP 200）：外置进度日志——初始化会话创建 `claude-progress.txt`（记录 agent 已完成工作的日志文件），每次编码会话结束写入结构化更新（随描述性提交信息留下结构化进展记录），新会话开局读它恢复状态（新会话先读 git 日志与进度文件了解最近工作）；诚实记录：该文未给字段结构、未提原子性/锁/位置设计，可靠性只靠 git 配套。
  2. OpenAI Codex 长时程任务博客（票面自带，2026-10-04 WebFetch HTTP 200）：多 markdown 外置状态文件（prompt/plan/implement/documentation.md）作 durable project memory——documentation.md 是共享记忆与审计日志（作者离开数小时后仍能凭它了解发生了什么），执行中"Update documentation markdown file continuously"；外部化状态（repo/文件/docs/worktrees/outputs）是循环可恢复的前提。
- **机制摘要**：两文同构——把「目标/计划/当前状态/决策/坑点」外置成人类可读文件，让任意新上下文窗口（或离开数小时的人）凭文件+事件历史恢复工作现场；Anthropic 用单一日志文件按会话追加，Codex 用分角色文件持续覆写。两者都由 agent 自由文本维护，无确定性保证。
- **契合点**：progress.md ↔ claude-progress.txt / documentation.md（跨窗口外置交接）；固定字段（原目标/验收项/里程碑/决策/失败/未决 Operation/证据 refs）↔ documentation.md 的状态+决策审计结构；持续更新 ↔ "continuously"；本仓加码——内容**确定性派生自 SessionEvent**（非 agent 自由文本，两文均无此保证），脱敏与原子写是票面新增要求（两文均未覆盖，属我们的工程补强）。
- **判定**：外置进度文件**思路** = PORT DESIGN（只借「外置交接文件 + 状态/决策审计 + 持续更新」三原则；不借 Anthropic 单文件自由追加形态、不借 Codex 多文件分工形态）。实现 = REUSE 本仓 SessionEvent 流与词汇常量（spec 03，投影零新增事件类型）、`derive_protected_facts`（session/derive.py，W-02 约束/授权节）、W-07 `derive_task_state`（session/task.py，验收项与状态节）、Artifact ref 词汇（证据 refs 只留 ref 不复制大原文）、store.py flush+fsync 先例；BUILD 仅限 `session/progress.py`：事件→文档纯投影 + markdown 渲染 + 写前脱敏（凭证值/Cookie/.env 值不入文、不可安全呈现段阻止并标缺项）+ 同目录临时文件/fsync/`os.replace` 原子替换 + 上一版本与 source seq/hash 保留（meta sidecar）。零新依赖（标准库 tempfile/os/json/hashlib）。
- **License**：两来源为公开网页参考（无代码复制）；本票零新依赖。
