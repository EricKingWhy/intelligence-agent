# CLAUDE.md

所有 Coding Agent 共用的项目规则在根 `AGENTS.md`。Claude 开始任务时必须完整读取它，再按其中的任务触发条件读取细则。
必读文件缺失或读取失败时，停止依赖动作，按 AGENTS 文件头的阻塞结论模板报告路径和错误；独立只读分析可继续。

本文件只负责 Claude 入口，不另定义架构、授权、Ticket 完成条件或 Git 流程。
Primary/Reviewer 按任务角色确定，不绑定 Claude 或其他工具。

- 开始任务：AGENTS §2–§3；确认当前用户授权、仓库、分支、工作树。
- Review/Debug：按 AGENTS §4 必须读取 `docs/agents/review-debug-playbook.md` 对应分支。
- 设计/选型：按 AGENTS §6.1 必须读取来源清单与协议“方案依据”。
- 同步/merge/push/PR merge/关单：按 AGENTS §13–§14 必须读取 `docs/agents/git-workflow.md` 对应步骤；授权只以 AGENTS §14.4 为准。
- SDD/跨 context 恢复：按 AGENTS §16 读取当前协议与 Tracker，核对实际 Git 状态。

Skill 使用当前环境实际枚举；没有的命令不伪造。根 AGENTS 和细则随仓库同步，不依赖 Claude 本机专有 skill。
