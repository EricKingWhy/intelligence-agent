# #729 严格写隔离与 Python/CNG 兼容性主源调研

> 日期：2026-10-12。范围为主源调研及当前 Windows 候选隔离探针；不改 #729 票面/AC 或产品代码，探针结果同步记入 Tracker。本报告针对四项同时要满足的约束：宿主侧仅 workspace 可写；正常 CPython 与其子进程可执行；CNG 可用；并且并发替换 reparse point 时没有越界写竞态。

## 项目启动依据

| 前置项 | 实读依据 | 状态 |
|---|---|---|
| Vision 相关原则 | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/00_PROJECT_VISION.md` §§2.1、2.5、3（小而清晰、Reuse First、Python Core） | READY |
| 当前任务规格 | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/05_SANDBOX_CODING_TOOLS.md` §§1、4、9（Sandbox 是 Runtime 边界；薄 Contract；Host 隔离验收） | READY |
| Reuse 判定 | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md` §§3、5–7（DSH Sandbox = PORT DESIGN；Build 门槛、License 与禁止整仓移植） | READY |
| Phase 依据 | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/14_IMPLEMENTATION_ROADMAP.md` Phase 3；`docs/PHASE_STATUS.md` 显示 Phase 3 已完成 | READY |
| 触发细则 | `docs/agents/reference-sources.md` Windows 条目；`docs/SDD_WORKFLOW_PROTOCOL.md` §§1.3、8.9；Research skill。SDD §8.9 的 JSON carrier、UTF-8、fsync/atomic replace 与字节核对已用于本文写入 | READY |

## 结论

目前找到的成熟做法中，没有候选能仅凭公开契约和现有证据证明完整满足「workspace-only 写入 + 普通 CPython/subprocess + CNG + reparse 并发替换无竞态」。最接近进程级目标的是 MXC BaseContainer/PSEC。2026-10-12 本机经安装版 Codex MXC wrapper 的 PSEC 探针验证了 Python 父/子进程、CNG 和静态 junction 拒绝，并在有限压力测试中未观察到越界写；但测试只覆盖一个重解析方向，错误计数未分类，且安装二进制与固定源码版本的精确对应未核实，不能证明真实无竞态。AppContainer 本机进程创建返回 577，应用层行为未测。

AppContainer 有 Microsoft 官方 CPython/subprocess 示例，但它是 MSIX/Win32 App Isolation 路径，进程为 Low IL；Microsoft 文档明确指出：当资源为 Medium IL 或更低且 DACL 同时允许 user/group SID 与 AppContainer Package/Capability SID 时，AppContainer 可以按授权写入；因此缺少 `SeRelabelPrivilege` 本身并不排除给 workspace 设置 AppContainer ACE 的 DACL 路线。Windows Sandbox 给出更强的独立 VM 边界，但需要在 guest 中安装/提供 Python，宿主目录通过映射共享，官方资料没有承诺映射目录对 reparse 竞争无竞态。

## 方案逐项核实

### 1. Restricted token + workspace ACL（现有路线）

DeepSeek Harness 的 Windows 实现用 `WRITE_RESTRICTED` 做第二次写访问检查，并把 workspace/temp 能力 SID 放入 restricting SID 列表。它保留 Logon SID 与 Everyone；上游注释记录，移除它们会导致早期 DLL 初始化返回 `0xC0000142`，CNG 会使 PowerShell 以 `0xE0434352` 崩溃。DeepSeek Harness 用 Low Integrity label 阻止这些 ambient SID 在获准目录外写入，并明确把 Low 标签作为安全边界的一部分。证据：本地浅克隆 `D:\reference\deepseek-harness`，commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`，`packages/sandbox/sandbox-windows-acl/src/token.ts:182-206`、`tests/runner.spec.ts:599-615`、`README.md:98-129`；上游采用 MIT License。

这正好解释当前的 CPython 启动失败症状，但也表明不能只复制 restricting-SID 列表、再省掉 Low 标签：保留 Everyone 且没有 Low 标签会重新允许任何 Everyone 可写的宿主对象；移除 Everyone/Logon SID 又会重现 DLL/CNG 兼容问题。结合本票已测的 `SeRelabelPrivilege` 缺失，这个 DSH 方案不能在当前 DACL-only 前提下满足 workspace-only。该结论针对 DSH 这一套实现与本机权限，不代表所有 Windows restricted token 都必然失败。

### 2. Win32 AppContainer / Win32 App Isolation

Microsoft 的 Win32 文档规定 AppContainer 的有效 DACL 授权需要普通 user/group SID 与 Package/Capability SID 两边都允许，最终取交集；AppContainer 运行于 Low IL，但 Microsoft 文档明确说明：若资源的 Mandatory Label 为 Medium IL 或更低，且 DACL 经 Package/Capability SID 与 user/group SID 双重允许，AppContainer 可按 DACL 权限读写执行；所以普通 Medium workspace 不需要仅为此被重标成 Low IL。文档没有证明 #729 目标机已按所需 ACE 配置，也没有覆盖 reparse race。创建 AppContainer profile 还会提供可写的私有 profile，`LOCALAPPDATA`、`TEMP`、`TMP` 会重定向到该目录。因此「workspace-only」若按宿主所有字节严格解释，还必须处理这块私有存储；否则应明确定义为“仅一个宿主共享目录可写”。依据 Microsoft Learn「Launch an AppContainer」的 Creating the Profile 段落，尤其是文档行 139–146。来源：[Microsoft Launch an AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)、[Microsoft Mandatory Integrity Control](https://learn.microsoft.com/en-us/windows/win32/secauthz/mandatory-integrity-control)。

CPython 兼容方面，Microsoft Developer Blog 用 CPython 3.12.2 打包成 MSIX 后展示了 Python 解释器、文件 API、`os.system`、`subprocess`/PowerShell 示例；文章给出的条件是 Windows Insider build >25357 和 MSIX Packaging Tool，且文件访问依赖包内容、profile、显式权限或 ACL。它证明了“打包版 CPython 可在 Win32 App Isolation 里执行 Python 子进程”的可行性，不证明任意宿主 `python.exe`、任意 venv 或 CNG provider 在本票机器上可直接运行。[Microsoft Python/App Isolation 示例](https://blogs.windows.com/windowsdeveloper/2024/03/06/sandboxing-python-with-win32-app-isolation/)。

Microsoft `BCryptOpenAlgorithmProvider` API 文档把 UWP apps 列为受支持应用类型，因此 CNG API 本身有 AppContainer/UWP 支持面；这不是 CPython 调用 CNG 的端到端测试，也不证明受限 token 缺少必要访问权限时仍可用。[BCryptOpenAlgorithmProvider](https://learn.microsoft.com/en-us/windows/win32/api/bcrypt/nf-bcrypt-bcryptopenalgorithmprovider)。

复用判断：Win32 AppContainer 是成熟、文档化的原生机制；显式 workspace DACL ACE 可作为无需修改 Low 标签的候选；本机缺少 `SeRelabelPrivilege` 不排除此方案。Python 证据要求 MSIX 路径和授权边界；本票若不采用该运行模型，就不能把文章当作兼容性保证。本机按文档形状尝试创建 AppContainer 进程，但 `CreateProcessW` 对 cmd.exe 和 Python 均返回 577，进程未启动；这只说明当前主机上的本次调用未成功，不证明 AppContainer 普遍不支持。reparse-point race 仍无官方保证或上游对抗测试。

### 3. MXC ProcessContainer：BaseContainer/PSEC 与 AppContainer fallback

MXC schema 把 `readwritePaths`、`readonlyPaths`、`deniedPaths` 作为宿主文件访问策略。Tier 1 BaseContainer 在 OS 声明 `PSE_SUPPORT_FS_DENY` 时原生实施 deny path，不修改宿主文件 ACL；Tier 2/3 AppContainer 为实现相应路径政策会在宿主对象临时增加 DACL ACE，并在退出时清理。Tier 3 需要 `WRITE_DAC`；文档还提醒并发运行复用同一个 `containerId` 会互相撤销 ACE。BaseContainer 路径授权以目录为单位向下生效，不应把宽泛用户目录作为补丁。证据：`D:\reference\mxc`，commit `c6f301d53a1430c4c921a05c57af838f7392348f`，`docs/schema.md:373-410,451`、`src/mxc-sdk/src/backends/process_container/common/fallback_detector.rs:70-76,252-275,323-404`。

MXC 的 `secenv.rs:10-29` 记录其 PSEC 实现：用 `CreateProcessSecurityEnvironment` 创建 `PSEC` FlatBuffer 环境句柄，再把句柄通过 `PROC_THREAD_ATTRIBUTE_SECURITY_ENVIRONMENT` 接到普通 `CreateProcessW`；实现会运行时解析 DLL 导出并检查版本/能力。Codex 的 MXC wrapper 选择运行时能力 probe，而不从 OS build number 推断可用；请求 deny path 但宿主不提供 `PSE_SUPPORT_FS_DENY` 时，启动前失败，并且命令失败不会切换到 AppContainer fallback。证据：`D:\reference\codex`，commit `7f892275e31002f0422477c6219189284560e689`，`codex-rs/mxc-sandbox/README.md:1-17,32-34`；Codex wrapper 为 Apache-2.0。MXC 为 Microsoft MIT License。

必须区分：Microsoft Learn 另有 `Experimental_CreateProcessInSandbox` / `Experimental_CreateProcessAsUserInSandbox` API；官方文档明确标为 experimental、Windows 11 experimental minimum、`processmodel.dll`、无公开头文件。[Create Process in Sandbox APIs](https://learn.microsoft.com/en-us/windows/win32/secauthz/createprocessinsandbox)。MXC 代码中的 PSEC（`PSEC` payload + `CreateProcessSecurityEnvironment`）是不同的接口形状；不能把前者当成后者稳定性证明。MXC 当前以运行时 API-set 导出、PSEC schema version 与能力探测为准：`base_container_runner.rs:308-345` 检查 API set，并通过 `QueryProcessSecurityEnvironmentSupport` 检查 deny-path capability；`docs/schema.md:472-478` 将 Windows processcontainer 描述为按 host capability 选择 BaseContainer/AppContainer，并受 `--experimental` 开关影响。本次核查的 Microsoft 官方页面与该固定版本 MXC 源码未给出 BaseContainer/PSEC 的稳定 Windows 最低 build/support matrix。因此 API 符号出现或一次 PSEC create 成功都不能代替对 `PSE_SUPPORT_FS_DENY` 的运行时能力检查。

Python 证据的边界：MXC `tests/scripts/T3-Workloads.ps1:672-752,809-825` 定义了 Tier 3 AppContainer+DACL 的 Python 读取、解释器运算、workspace 写读往返及 `os.chdir` 用例，并在 Python 安装目录需要时加只读 grant。源码脚本证明这些是真实测试入口，不证明本文环境跑过或这些用例曾全绿；它们不是 Tier 1 PSEC 测试，也没有 `ctypes` CNG 探针。`run_processcontainer_filesystem_matrix_test.ps1` 检查了 read/write/denied/control 矩阵及临时 ACL 清理，但没有 CPython/CNG 与 reparse 并发替换覆盖。MXC 主源中未发现 process-container 的 symlink/junction/reparse/TOCTOU 对抗用例。

复用判断：MXC 是最值得验证的进程级候选，其 BaseContainer policy/API 设计可 `ADAPT`；但其执行核心为 Rust，包含 PSEC FlatBuffer、Windows 启动属性、runtime probing 与生命周期管理；Python 项目没有可直接调用的 Python SDK。直接拿 Rust SDK 会新增 native helper / 进程协议和发布面，直接翻译则不再是少代码。先复用其 fail-closed capability-selection 设计，不应把 Tier 3 DACL fallback 当成 Tier 1 的等价替代。本机安装版 Codex wrapper 的 PSEC 探针支持 CPython 3.12.6 父/子进程、CNG SHA-256 和静态 junction 拒绝；有限竞争探针仅是一个方向的观测，既未证明 race-free，也未覆盖上游 MXC 源码构建。

### 4. Windows Sandbox VM

Microsoft Windows Sandbox 使用 Hyper-V 的独立 Windows guest kernel，可一次性运行进程；宿主程序不会自动出现在 guest，需要在 guest 中安装 Python。`.wsb` 可只映射 workspace、关闭网络和 clipboard；默认 network/clipboard 是开启的。可写 mapped-folder 的改动在 VM 关闭后仍会落到宿主，并且官方提醒 sandbox 应用可以影响映射文件。其限制包括 Windows Pro/Enterprise/Education（不支持 Home）、虚拟化和资源前提、可选功能启用可能要重启、同一 logon session 不支持多个实例。[Windows Sandbox overview](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/)、[配置文件与 mapped folders](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file)、[安装前提](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-install)。

Microsoft MXC 对该路线已有成熟实现：`docs/backends/windows-sandbox/windows-sandbox.md:7-21,57-68,91-92,145-200`。但它依赖 guest exe、host daemon、bootstrap、nonce-authenticated IPC、VM ownership/teardown；host Python 不会自动映射。单次调用每次都启动完整 VM；生命周期模式则保留 daemon/guest。该复用工程远大于直接进程 sandbox。Windows Sandbox 正常 guest 的普通 Windows CNG 可用性是基于 Windows guest 的推断；所读资料没有 CPython/CNG 真实运行结果。 mapped-folder 的 symlink/junction 替换也未见官方 race-free 声明或 MXC 对抗用例。

复用判断：作为隔离架构可 `PORT DESIGN`，且宿主最小暴露面可仅为 workspace share；若目标是本地快速 Coding Tool，它的 VM/daemon/guest 成本和运行前提很高。它是安全边界较强的候选，不是已证明满足 #729 的候选。

## Race-free 条件的证据边界

已审查的 Microsoft AppContainer、Win32 App Isolation、Windows Sandbox 文档，以及固定版本 MXC/DSH/Codex 源码，没有对“攻击进程在每次文件操作之间并发替换 junction/symlink/reparse point 时，workspace 外文件和 ACL 始终不变”作出保证。MXC 的 `base_container_helpers.rs:42-75` 将策略序列化为 `fs_read_write`/`fs_read_only`/`fs_deny` 路径字段；所审查的调用侧没有逐次文件操作使用 handle-relative open 或 `OBJ_DONT_REPARSE` 的实现/测试。PSEC 由 OS 内部实施，所以调用侧源码缺少这类打开逻辑不证明内核策略存在竞态；但官方/上游契约和测试也未证明它对并发 reparse 替换无竞态。因此不能根据 PSEC 符号或路径 grant 通过 #729 的 handle-relative TOCTOU 验收。DSH 源码的限制中明确承认 hard link alias 是残余边界。宿主 ACL 临时加 ACE、或路径预检后再按字符串访问，不能仅凭文档推定无竞态。因而这里的结论是“尚无证据证明满足”，不是证明 OS 原语必然存在竞态。

若要把任一后端认定为候选通过，至少需要在真实 Windows 上对其实际启动的 Python 及子进程同时验证：workspace 内读写成功；未授权宿主文件读写失败；CNG open/hash/close 成功；两个重解析点替换方向（workspace 内指向 workspace 外、外部重定向进 workspace）高频交错时外部 sentinel 内容与 ACL/DACL byte snapshot 不变；进程结束后宿主 ACL 与 SACL 不变；同时明确是否允许 AppContainer 私有 profile/temp 写入。本轮 PSEC 只验证了 workspace 内→外方向的一次静态拒绝及有限竞争压力；外→内方向未测，7,413 个并发访问错误未分类。AppContainer 进程未启动，因此其工作负载轴均未测。票面要求的无竞态门槛仍未满足。

## Reuse 判定摘要

| 候选 | 兼容证据 | #729 契合度 | Reuse |
|---|---|---|---|
| Restricted token + DSH workspace ACL | 上游记录移除 Logon/Everyone 会触发 `0xC0000142`/CNG 崩溃；通过 Low label 关 ambient 写口 | 当前缺 `SeRelabelPrivilege` 时无法照搬其完整边界；不满足 strict workspace-only 的证据 | 仅 `PORT DESIGN`；当前配置 `DEFER` |
| AppContainer / Win32 App Isolation | 官方 CPython 3.12.2、`os.system`、`subprocess` 示例；CNG API 有 UWP 支持类型 | 本机 `CreateProcessW` 对 cmd.exe/Python 均返回 577，应用层轴未测；不能推断普遍不支持；profile 与 race 仍需验收 | 可复用 OS 机制；当前仍 `DEFER` |
| MXC BaseContainer/PSEC | 本机安装版 Codex wrapper 验证 Python 父/子进程、CNG 和静态 junction 拒绝 | 有限竞争探针未观察越界写，但仅一个方向/有限次数、错误未分类，且源码对应未核实；不能证明无竞态 | `ADAPT` capability 与 fail-closed 设计；产品选择仍 `DEFER` |
| Windows Sandbox VM | Windows guest；普通 Python/CNG 兼容性合理但本次无实测证据 | 内核隔离强；workspace 通过写映射共享，race 未证明；依赖 guest bootstrap/VM | `PORT DESIGN`；若没有更轻候选通过则重新裁决范围 |

**推荐的研究结论**：不要把 DSH restricted-token + workspace ACL 在缺少 Low label 时宣称已达到 workspace-only；AppContainer 可经显式双 SID DACL 授权写 Medium workspace 的文档机制仍成立，但本机创建进程失败，原因尚未确认，应用行为及 profile/temp、race 边界未验证。MXC/PSEC 在本机显示较好的 Python/CNG/静态路径兼容性，有限竞争测试也没有观察到越界写；由于只覆盖一个方向、存在未分类错误且无正式 race-free 保证，不能说它满足 #729 的无竞态 AC。不要把 AppContainer DACL、PSEC 与 Windows Sandbox 的路径授权互换视作等价。本轮结果不改票面；下一步后端选择或 AC 修订属于架构/验收边界裁决。

## 核查过的主源

- Microsoft：[Launch an AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)（Creating the Profile 段落，尤其行 139–146）；[Mandatory Integrity Control](https://learn.microsoft.com/en-us/windows/win32/secauthz/mandatory-integrity-control)；[Sandboxing Python with Win32 App Isolation](https://blogs.windows.com/windowsdeveloper/2024/03/06/sandboxing-python-with-win32-app-isolation/)；[BCryptOpenAlgorithmProvider](https://learn.microsoft.com/en-us/windows/win32/api/bcrypt/nf-bcrypt-bcryptopenalgorithmprovider)；[Create Process in Sandbox APIs](https://learn.microsoft.com/en-us/windows/win32/secauthz/createprocessinsandbox)。
- Microsoft：[Windows Sandbox](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/)；[WSB configuration and mapped folders](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file)；[WSB installation requirements](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-install)。
- Microsoft MXC MIT，浅克隆 `D:\reference\mxc`，commit `c6f301d53a1430c4c921a05c57af838f7392348f`：`docs/schema.md:373-410,451`；`src/mxc-sdk/src/backends/process_container/common/secenv.rs:10-29`；`src/mxc-sdk/src/backends/process_container/common/fallback_detector.rs:70-76,252-275,323-404`；`tests/scripts/T3-Workloads.ps1:672-752,809-825`；`docs/backends/windows-sandbox/windows-sandbox.md:7-21,57-68,91-92,145-200`。
- DeepSeek Harness MIT，浅克隆 `D:\reference\deepseek-harness`，commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`：`packages/sandbox/sandbox-windows-acl/src/token.ts:182-206`；`packages/sandbox/sandbox-windows-acl/tests/runner.spec.ts:599-615`；`packages/sandbox/sandbox-windows-acl/README.md:98-129`。
- Codex Apache-2.0，浅克隆 `D:\reference\codex`，commit `7f892275e31002f0422477c6219189284560e689`：`codex-rs/mxc-sandbox/README.md:1-17,32-34`。

本文包含主源证据调研及限定范围的本机探针，不是完整通过结论。未执行 MXC 上游 Windows 测试套件或 Windows Sandbox；PSEC 是通过安装版 Codex wrapper 实测，AppContainer 进程没有成功启动。竞争测试只覆盖单一 reparse 方向和有限调度，不能证明无竞态。


## 2026-10-12 目标机隔离探针

### 环境与 PSEC 入口

主机为 Windows 11 Home，build 10.0.26200；Python 3.12.6；安装的 Codex runner 版本为 `codex-cli 0.162.0-alpha.17.2`。通过已安装的 `codex.exe --__codex-windows-mxc` 入口，使用 workspace 可写、CPython 安装目录只读、网络受限的策略运行探针。结果只证明此安装 runner 在此主机上的实测行为；未核实安装二进制与本报告引用的固定 Codex 源码 commit 完全对应，也未运行 MXC 上游测试套件。

功能探针返回：

```json
{"child_exit": 0, "child_outside_write": "outside=denied", "child_workspace_write": "CHILD_OK", "cng_sha256": "2d279b4841e63faac7681b7ab9d82e9a4aa6faa024dae9a2540f13fa0cc6a0e6", "parent_outside_write": "denied", "parent_workspace_write": "ok", "python": "3.12.6"}
```

即父进程及 Python 子进程均可写 workspace，根外 canary 写入被拒，子进程退出码为 0，CNG BCrypt SHA-256 成功。功能探针外部 canary 的前后 SHA-256 相同：`FDA9C6930CEC81CB414C5C84AD624759A63ECC8382AAD885B62A4ECFFAF36137`；DACL 读数前后相同。

### 静态 junction 与有界并发探针

静态探针将 workspace 内 `pivot` junction 指向兄弟目录 `outside`。沙箱内 Python 对 `workspace\\pivot\\probe.bin` 追加写入得到 `PermissionError: [Errno 13] Permission denied`，runner 返回 1（预期拒绝）。外部 canary 前后 SHA-256 均为 `7C67BF630E8A0875B3FCB20275F2216D6B76DC53B66AFDC7EC547DB6AA06859F`；DACL 未变，文件仍为 23 字节。

有界压力探针中，宿主切换 workspace 的 `pivot` junction 2,000 次，在 workspace 内部目标和兄弟目录外部目标之间切换；沙箱 Python 共尝试 12,000 次写入，记录 `allowed=1617`、`denied=2970`、`other_errors=7413`。运行记录的外部 canary 前后 SHA-256 均为 `2C6E4A54AAD8EF305B80C2B49EA8993BDBB07F08208EC0DDBA3684751237F4E5`，DACL/Sddl 未变；workspace 内探针文件长 1,623 字节。7,413 个 `other_errors` 没有分类。该压力测试只覆盖 workspace junction 在 workspace 内/外目标间切换的一个方向、有限次调度；未覆盖外部重定向进 workspace，也不能证明所有并发时序下无竞态。

### AppContainer 启动探针

按 Microsoft 文档形状创建临时 AppContainer profile，并以 `STARTUPINFOEX` 和 `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` 调用进程创建。profile 与线程属性初始化/更新成功；对 `cmd.exe` 和 Python 的 `CreateProcessW` 均失败，Win32 error 577（`ERROR_INVALID_IMAGE_HASH`）。相关 Code Integrity event ID 3002 记录了 `SecureRequired=0x12`、`RequestedSigningLevel=4` 和无 per-page image hashes。该日志与镜像完整性校验有关，但本次证据不能确认失败的精确原因。profile 已删除，AppContainer 进程没有启动，因此 Python/subprocess、CNG、文件系统授权、junction/reparse 与竞争语义全部未测；此结果不表示 AppContainer 在一般情况下不受支持。错误码参考：[Microsoft System Error Codes 500–999](https://learn.microsoft.com/en-us/windows/win32/debug/system-error-codes--500-999-#error_invalid_image_hash)。

### 本轮能得出的结论

PSEC 当前是兼容性证据更强的候选：workspace 父/子进程写入、根外拒写、CNG 和静态 junction 拒绝均有本机运行证据；有界并发探针未观察到 canary/DACL 变化。但这不能回答用户要求的“真正无竞态”：压力测试方向与调度有限、错误未分类、安装 runner 的确切源码对应未知，而且没有上游或官方 race-free 契约。AppContainer 则因本次进程创建失败而没有可比较的应用层数据。现有证据不足以把任何候选标为通过，也不足以据 AppContainer 的本机 577 直接排除该机制。
