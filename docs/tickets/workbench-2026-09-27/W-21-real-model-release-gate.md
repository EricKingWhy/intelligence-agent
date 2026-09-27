# W-21 · 两次独立真实模型长任务 + Windows Desktop/TUI 发布 Gate
**目标仓库**：intelligence-agent（发布协调与 docs/live_gate/ 证据）；只测已冻结的 backend/frontend commit，不在本票修产品代码。

**类型/优先级**：P0 Release Gate。**依赖**：W-09–W-20、W-22 客户端离开暂停契约、W-23 创建入口、W-24 证据保留；[#319](https://github.com/EricKingWhy/intelligence-agent/issues/319)、[#337](https://github.com/EricKingWhy/intelligence-agent/issues/337)、[#341](https://github.com/EricKingWhy/intelligence-agent/issues/341)、[#342](https://github.com/EricKingWhy/intelligence-agent/issues/342) 的相关验收状态。**范围**：执行、取证、缺陷回票；不以此票替代任何旧票的未完成工作。

## 执行协议

1. 冻结精确代码树/安装包 SHA-256、模型/provider ID（不写密钥）、W-20 样例版本、Windows VM 环境与生效权限/预算。两次从**独立全新**样例目录与 Session 开始，不共用修好的结果。
2. **Run A**：从安装后的桌面启动真实模型，让它完成定位→修改→测试→真实浏览器页面验证→diff 审阅；期间触发至少一次 compaction 和一次摘要调用失败，在新 context window 重新读取 `progress.md`/原 Event 后继续，检查旧 7 条禁令与未完成项未漂移。
3. **Run B**：从安装后的 TUI 冷启动同一 Python 服务协议，真实模型完成同样完整任务；在数据库提交后、ToolResult 前 kill Host，再从桌面打开，先查询 DB/Ledger、reconcile、人工确认需要项、手动续跑；最终浏览器验证和 diff 审阅仍须完成。桌面/TUI 同时在场和单独退出规则各实测一次。
4. 两次都由 W-20 判定器与人工证据审阅共同判断。记录实际模型请求、Tool calls、真实 API/DB 查询、UI 截图、所有失败/重试、Operation IDs、SessionEvent seq、文件 manifest、最终 diff、测试命令/退出码及用户接受状态；保留原失败日志的安全 ref。

## 通过/失败规则

两次**独立完整通过**才可标此 Gate passed。任一次失败都保留结果与根因票；修复后从新样例/新 Session **重新取得两次完整通过**，不能用失败片段重测折抵。真实模型服务故障、Chrome 缺失、Docker 缺失、UNKNOWN 无法裁决、进度文件损坏均报告 `blocked/fail` 的具体原因，不能记为 pass。不得打印 `.env` 或其他凭证值。若关键工程测试/#319/#337/#341/#342 仍有阻断，本 Gate 不可宣布发布完成。

**验收**：W-20 所有 DB/HTTP/UI/恢复/上下文/diff 断言两次均 pass；Windows 安装→启动→退出→更新→恢复的 W-16 烟测通过；桌面/TUI 同一 Task 状态与 Event seq 一致；审查者可从 evidence refs 重放关键结论。跑当前 V3.1-lite 全量门禁、双轴独立审查、review coverage，并按相应旧票规则处理 #338 已知间歇红。**不做**：只用 fake model 宣称产品通过、隐藏失败重跑、替代 #319 五场景或 #304 Memory Gate。

**成熟参考/复用**：[Anthropic effective harnesses](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)要求跨窗口明确进度与真实端到端验证；[Codex long-horizon](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex/)建议 plan→edit→test/build→observe→repair→status 循环。`PORT DESIGN` 挑战组织，`REUSE` 本仓 V3.1-lite 与 Phase 16 真实 kill/reconcile 设施。
