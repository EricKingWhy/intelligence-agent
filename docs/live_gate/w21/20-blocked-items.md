# W-21 阻塞清单（阶段三）

> 诚实原则：以下每一项都**未执行**，未用 fake/mock 冒充。阻塞原因是环境缺失，不是设计缺陷。

## B-1 · Windows VM：安装→启动→退出→更新→恢复（W-16 烟测）

- **阻塞原因**：本机是 Linux 云电脑（Cloud Hypervisor），无 Windows VM、无 Hyper-V/WSL2 之外的 Windows 执行环境；W-16 交付的是 NSIS 安装包（PR #782，`desktop/installer/`），Linux 上无法执行 `.exe`。
- **需要的环境**：Windows 10/11 VM（或物理机）+ 管理员权限 + 出网（模型 API）。
- **谁能做**：王浩宇的 Windows 电脑，或配好 Windows runner 的 CI。
- **前置**：PR #782 的安装包产物（NSIS `.exe`）需先在 Windows 上构建/获取。

## B-2 · Run A：桌面启动真实模型长任务

- **阻塞原因**：依赖 B-1 的桌面客户端；且 #365 要求"从安装后的桌面启动真实模型"，Linux 上无桌面客户端可启动。
- **部分可做说明**：真实模型调用本身在 Linux 可用（CodeBuddy `deepseek-v4.1-flash` 额度已验证可用；本任务中已用它做 W-20 fixture 的 B1/B2/B3 真实修复，见 10-w20-deterministic-evidence.md）。"真实模型修 bug"这件事不阻塞，阻塞的是"在 Windows 桌面客户端里跑完整长任务"。
- **谁能做**：同 B-1。

## B-3 · Run B：TUI 冷启动 + kill/reconcile 真机流程

- **阻塞原因**：依赖 B-1 的 TUI；且 #365 要求"在数据库提交后、ToolResult 前 kill Host，再从桌面打开"，这是 Windows 桌面/TUI 双客户端协同流程。
- **部分可做说明**：kill→重启→查 DB/Ledger→不盲重跑 的**语义**已在 Linux 用 W-20 fixture 手动验证（`FAULT_KILL_AFTER_COMMIT=1`：kill 后审计停留 `processing`、DB 40 条已提交、重启不重写，见 10-w20-deterministic-evidence.md）。这是 harness 层语义演示，**不是** #365 要求的 TUI 真机流程，不可折抵。
- **谁能做**：同 B-1。

## B-4 · 桌面/TUI 同时在场与单独退出规则、同一 Task 状态/Event seq 一致

- **阻塞原因**：同 B-1，无双客户端可实测。
- **谁能做**：同 B-1。

## B-5 · "两次独立完整通过"的 Gate passed 判据

- **阻塞原因**：判据依赖 B-2/B-3 的完整执行。Linux 侧的确定性层（判定器基线 fail + 修复后 pass、浏览器验证、kill 演示）是必要证据，但**不能**宣布 Gate passed。
- **当前状态**：`blocked`（非 `fail`）—— 环境缺失，未执行即不判失败。

## B-6 · 双轴独立审查 subagent

- **阻塞原因**：本 worker 是 depth 2/2 的 subagent，`can_spawn=no`，无法另起独立审查 subagent（强制模板 §5 要求实现者不自审、协调者只监控）。
- **需要的动作**：由 parent（main agent）另行安排独立审查（Claude Code 或另一个 CodeBuddy），审查对象是本 PR 的证据文档 diff。
- **当前状态**：PR 将明确标注 `pending independent review`，不自行 merge。

## 不阻塞的项（已做/在做）

- W-20 判定器确定性跑通（含基线 fail 与真实模型修复后 pass 的完整证据对）—— Linux 可做，已做。
- 真实浏览器页面验证 —— 已做（Playwright 自带 Chromium，真机截图）。
- V3.1-lite 全量门禁 —— Linux 可做，pytest 全量 + 前端门禁后台运行中。
- 证据可重放 —— 本目录文档 + `~/workspace/w21-work/` 下可复跑脚本（`drive.py`、`browser-check.js`）。
