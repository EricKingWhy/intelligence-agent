# W-28 · 进度清单 TUI 渲染（Pi 独立 TUI 包复用）
**目标仓库**：intelligence-agent-frontend（TypeScript TUI）。

**类型/优先级**：P2 UI。**依赖**：W-26（服务端契约）、W-17（TypeScript TUI 宿主存在后）。**范围**：W-17 建立的 TUI 包内新增 PlanList 视图。实施前重读 W-17、W-26、PRD §7.5。

## 明确契约

与 W-27 同一渲染契约（四件套），终端表达：N/M 计数行、`in_progress` 项以 `activeForm` + 高亮/前缀显示、完成项划线（终端用删除线 ANSI 或 `✓`+ dim）、折叠以快捷键切换。数据源同为服务端投影，TUI 不私存清单（不变量 #22）。

## 工作指令

1. **REUSE 优先**：[Pi 独立 TUI 包](https://github.com/earendil-works/pi/tree/main/packages/tui)（MIT）——按 #344 复用清单，优先直接依赖，只写 Python API/SessionEvent adapter；Pi Agent Runtime 不接入。
2. 若 Pi 包无现成 list/todo 组件，最小自建一个渲染函数（纯函数：plan state → 终端行），不做交互编辑器。
3. 组件测试用 ink/对应测试工具快照三态清单渲染。

## 验收

- 快照测试覆盖四件套；真实会话 `update_plan` 后 TUI 同步更新（证据截图/录屏）。
- 按 V3.1-lite 记录冻结树与 review。

**不做**：Web/桌面（W-27 已占）、交互编辑、鼠标支持。

**成熟参考/复用**：Pi TUI 包（MIT，#344 复用清单已列）；渲染形态同 zcode/Cline/Claude Code 收敛（调研 P）。
