#729 Codex/MXC PSEC 复用决策与权限结论

> 日期：2026-10-12。依据公开上游源码/文档、此前本机隔离探针与本轮只读权限核查。此记录更新后端选择，不表示 PSEC 已通过 #729 的完整无竞态验收。

## 结论

可以直接复用成熟产品。#729 现改用 Codex/Microsoft MXC 的 BaseContainer/PSEC 约束不受信任子进程；Python 宿主自身执行的 Sandbox 文件 API 不受子进程 PSEC 保护，继续用 HCSSHIM 风格的固定根 handle + `RootDirectory` + `OBJ_DONT_REPARSE`。不再实现 DSH restricted-token + 持久 workspace ACL/Low-label 作为进程边界，也不做 AppContainer/DACL 或裸本机 fallback。

这是两个不同执行面上的最小可行组合：PSEC 限制受控子进程，handle-relative API 限制宿主内建文件操作。PSEC 只适用于它创建的子进程，不能把 Python 宿主里的 `ReadTool`、`WriteTool`、workspace HTTP 路由或项目指令读取误认为已经隔离。

## 复用证据与选择

- **Codex/MXC PSEC（主选，ADAPT）**：本地 Codex 浅克隆固定在 `7f892275e31002f0422477c6219189284560e689`。`codex-rs/mxc-sandbox/README.md` 说明 wrapper 只走 PSEC、通过运行时能力探测、不修改宿主 ACL、PSEC 不可用时拒绝执行。`codex-rs/mxc-sandbox/src/policy.rs:309-310` 明确写明 PSEC 在访问时按 junction 解析后的目标执行限制。Codex Apache-2.0，MXC MIT。当前本机安装版 Codex wrapper 的既有探针已验证 CPython 3.12.6 父/子进程、CNG SHA-256、workspace 写入、根外写拒绝与静态 junction 拒绝；有限压力测试未观察到 canary/DACL 变化，但只测一个方向，7,413 个访问错误未分类，安装二进制与固定源码的精确对应未验证。因此这是首选机制与兼容性证据，不等于完整验收通过。
- **HCSSHIM SafeFile（宿主 API，PORT DESIGN）**：固定 commit `ab8249e8a1110ff5f052d3710fe57f29c45503d` 的 `internal/safefile/safeopen.go` 使用目录 handle、相对打开和 `OBJ_DONT_REPARSE`。复用其安全打开模型，不引入 Go runtime。
- **MXC #462（辅助 OS 证据）**：微软协作者 2026-08-05 关闭 issue，称 junction 已由 OS code hard-block in AppContainer。它说明平台原生 containment 能把 junction 防护放到 OS 层；本机 AppContainer 启动返回 577，原因未知，不能把该候选视为本机可用，也不能据此证明 PSEC 路径。
- **Pi（排除）**：仓库 sandbox 扩展示例只支持 macOS/Linux，Windows 明确报告 unsupported；可复用扩展接线概念，不存在可移植的 Windows OS 隔离实现。
- **DSH Windows ACL（排除为本票进程边界）**：MIT 代码确实提供 restricted token + workspace DACL + Low label，解决 Python/CNG 兼容的证据明确；其方案会持久改写 ACL/Integrity label，并公开记录 hard-link 别名等残余，不能当作严格、无宿主安全描述符副作用的 PSEC 替代品。

## 更正此前 Low-label 权限结论

此前将本机缺少 `SeRelabelPrivilege` 写成 DSH Low label 路线的阻塞条件，表述不正确。Microsoft `SECURITY_INFORMATION` 文档对 `LABEL_SECURITY_INFORMATION` 的设置列出 `WRITE_OWNER`；`SE_SECURITY_NAME` 仅对 `SACL_SECURITY_INFORMATION` 或 `SCOPE_SECURITY_INFORMATION` 有要求。DSH 自身 README 也要求目标目录由调用者拥有且具备 `WRITE_OWNER`，并说明 owner 隐式权限只有 `READ_CONTROL` 与 `WRITE_DAC`。

本轮只读 ACL 核查：`D:\intelligence-agent` 的 owner 是当前用户，但对该路径显示的是 Modify ACE，不是 FullControl/WRITE_OWNER；当前 `issue-729` worktree 的当前用户 ACE 是 FullControl。由此不能再声称缺少 `SeRelabelPrivilege` 本身阻止 DSH；实际可行性取决于目标 workspace 的 `WRITE_OWNER`，且 DSH 的持久 ACL/Low-label 与 hard-link 边界仍使它不适合本票要求。该更正已经写入 GitHub #729。

## 修订后的实现边界

1. `permission_mode` 只表达审批；显式 PSEC backend 负责 OS 文件写边界，`danger-full-access` 不能关闭该边界。
2. 子进程通过窄 Rust/native adapter 调用 MXC PSEC；运行时验证 `CreateProcessSecurityEnvironment` 和请求所需文件策略能力。策略无法完整实施时启动前失败，不回退到 AppContainer/DACL 或 local 裸执行。不依赖用户另外安装 Codex CLI，也不依赖 Codex 未公开的内部 helper 命令。
3. 宿主 Sandbox 文件 API 保留固定 workspace 根身份（卷序列号 + file ID）和 handle-relative 操作；这部分不声称受 PSEC 保护。
4. 验收要覆盖 CPython/subprocess/CNG、根外写拒绝、PSEC 与宿主 API 各自的 reparse 对抗路径、workspace→外部及外部→workspace 两方向竞态、根与中间组件替换、canary 内容/ACL 不变、backend fail-closed、无宿主 DACL/SACL/完整性标签变化。
5. hard-link 是对象别名，不是 reparse point；不在本票 reparse AC 中作无风险承诺，保留 #1048 独立跟踪。

## 票面变更与未完成项

GitHub #729 标题已改为 `[W-31] Windows OS 级沙箱（Codex/MXC PSEC + 安全文件 API）`，正文替换为上述 PSEC + handle-relative 方案及 AC；回读确认 Issue 仍 OPEN、指派 `EricKingWhy`，`enhancement` 与 `in-progress` 标签保留。

仍未完成：本机 PSEC 竞态探针需按新 AC 补齐两个方向并分类错误；AppContainer 577 根因未查明；旧方案的 worktree 改动尚未迁移到新架构；尚未跑新方案测试、review 或门禁。不得把当前探针当作完整通过证据。

## 主要来源

- Codex 固定版本：[MXC wrapper](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/mxc-sandbox/README.md)、[PSEC junction 目标说明](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/mxc-sandbox/src/policy.rs#L309-L310)。
- Microsoft MXC 固定版本：`D:\reference\mxc`，commit `c6f301d53a1430c4c921a05c57af838f7392348f`，PSEC `secenv.rs` 与运行时 support probe。
- Microsoft HCSSHIM：[固定 commit 的 safeopen.go](https://github.com/microsoft/hcsshim/blob/ab8249e8a1110ff5f052d3710fe57f29c45503d/internal/safefile/safeopen.go)。
- Microsoft：[SECURITY_INFORMATION 权限表](https://learn.microsoft.com/en-us/windows/win32/secauthz/security-information)；[SetNamedSecurityInfo](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setnamedsecurityinfoa)。
- Microsoft MXC：[Issue #462](https://github.com/microsoft/mxc/issues/462)。
- Pi 固定 commit `1b34779`；DeepSeek Harness 固定 commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`（MIT）。
