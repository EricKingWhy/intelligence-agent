# W-17 · 基于 Pi 独立 TUI 包的 TS 终端客户端
**目标仓库**：intelligence-agent-frontend（新建 TypeScript tui/ 包）。

**类型/优先级**：P0 CLI 产品化。**依赖**：W-11、W-07、W-12、W-14。**范围**：新 `tui/` TypeScript 客户端与安装包命令；复用已有 Python CLI/REPL 合同，不另起 Runtime 或 SessionStore。

## 首版最小操作面

终端滚动对话、输入编辑器、流式模型字/工具卡、Task/Session 选择、新建/继续/Fork、批准/拒绝、预算与暂停原因、`/compact` 与 `/cancel` 实际接服务端、进度文件/证据/Artifact 入口、外部 diff 查看入口。短任务一条命令能进入；长任务可看到原目标、下一步、失败/待裁决事实。兼容 Windows PowerShell/Terminal 的 UTF-8 中文、宽字符、终端 resize、Ctrl+C/退出。

## 工作指令

直接依赖 [Pi 独立 TUI 包](https://github.com/earendil-works/pi/tree/main/packages/tui)（当前候选 `@earendil-works/pi-tui`，MIT），先验证其 Windows 安装、键盘输入/宽字符/剪贴板，再写**薄** REST/SSE/WS→Pi 组件 adapter。所有 Task/审批/模型/权限/预算真相向 W-11 查询；TUI 不持有第二份会话 JSONL，也不连接 Pi Agent Runtime。`oh-my-pi` 和 Cline 供编辑器/命令/工具卡交互参考，只有能隔离依赖且许可合规的组件才可实质复制。桌面与 TUI 同时在线和 TUI 单独退出按 W-12；Ctrl+C 不直接杀 Python Core 的在途 Tool。

**验收**：Windows 安装包环境无 Node/Python 预装时终端命令可用；真实模型短问答和有工具的长任务各一次；TUI 独开退出导致安全暂停，桌面仍在则不暂停；断网/服务重启后 seq 回放无重复卡，审批拒绝不可绕过；运行中 resize、中文/emoji 不破屏。TypeScript typecheck、组件测试、真实 Windows PTY 交互记录。**不做**：复制已闭合 [#133](https://github.com/EricKingWhy/intelligence-agent/issues/133) 的 Python REPL、Pi/OMP/Cline Agent Loop。

**成熟参考/复用**：Pi TUI MIT `REUSE`；[oh-my-pi TUI](https://github.com/can1357/oh-my-pi/tree/main/packages/tui) MIT 与 [Cline CLI](https://github.com/cline/cline/blob/main/apps/cli/README.md) Apache-2.0 为 `PORT DESIGN`，其整体包依赖各自 Agent/Core。实质复制文件须记录 commit、保留许可证/NOTICE。
