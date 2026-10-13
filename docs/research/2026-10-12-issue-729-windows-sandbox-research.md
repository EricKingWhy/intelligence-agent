# #729 Windows 文件写入沙箱：restricted-token + ACL + 无 reparse 竞态方案

核实日期：2026-10-12
范围：核实 #729 的 Windows 文件写入隔离、DSH/HCSSHIM 成熟实现和本仓安全接入条件。按用户裁决，本文纳入票面/方案修订；产品代码与测试尚未修改。

## 结论摘要

1. DSH 有一套与 #729 路线 B 高度吻合的 Windows ACL 后端：从当前进程 token 创建 `WRITE_RESTRICTED` token，把 workspace/temp capability SID 放进限制 SID 列表，降低到 Low integrity，再把匹配的 ACE 与 Low mandatory label 写到授权目录。子进程通过 `CreateProcessAsUserW` 启动并放入 kill-on-close Job。完整实现可复核于 DSH commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`；仓库与该包均为 MIT。
2. 复用判定：**PORT DESIGN**。复用 DSH 的 token、ACL、删除权限与 fail-closed 设计；不直接依赖 npm 包。上游实现使用 TypeScript/Node、Koffi、DSH 内部 subprocess/Cordis seam，与本项目 Python `Sandbox` Contract 不兼容。Reuse Matrix 的 DSH Sandbox policy 判定同为 `PORT DESIGN`，并要求不引入 DSH TypeScript/Cordis runtime。
3. 路线 B 只证明对文件写入/删除施加 OS ACL 约束。DSH 不隔离读取、网络或进程可见性；硬链接是同一文件对象，外部别名仍可能访问已授权对象。它不能被描述为通用 Windows OS sandbox。
4. 重要范围风险：DSH 将 workspace DACL、拒绝 ACE 与 Low integrity 标签作为持久目录状态；workspace 授权默认不撤销，Low label 会在 DSH 退出后继续存在。其他以同一用户身份运行的 Low-IL 进程也可能在该 workspace 写/删文件。安装/清理、崩溃残留和 workspace 共享语义需要作为明确的生命周期要求。
5. 路径重规范化不是原子 TOCTOU 防护。DSH 的 fs-sandbox 接受检查与文件 syscall 间的残余 race；本票不再把它作为安全边界。采用 HCSSHIM 的相对目录 handle + NtCreateFile + OBJ_DONT_REPARSE，并使用 handle-based SetSecurityInfo。
6. #729 已带 `in-progress` 标签；用户裁决后已修订 Issue 的安全语义、路径边界与验收标准。产品实现尚未开始。

## 方案依据

| 来源 | 核实事实 | 契合点与判定 |
|---|---|---|
| DeepSeek Harness 本地浅克隆 `D:\reference\deepseek-harness`，HEAD `5badb15009ae1756c3afe0ae0cef1faafc290ccc`，remote `deepseek-ai/deepseek-harness`；根 `LICENSE:1-13` 为 MIT，包清单 `packages/sandbox/sandbox-windows-acl/package.json:35` 也标 MIT | 真实 Windows restricted-token + capability SID ACL 实现；来源行号见下表 | 语义与票面路线 B 接近；TypeScript/Cordis/Koffi runtime 不适合作为 Python Core 依赖。选择 `PORT DESIGN`。若拷贝实质代码，保留 DeepSeek 版权与 MIT notice。 |
| Microsoft HCSSHIM，本地浅克隆 `D:\\reference\\hcsshim`，HEAD `ab8249e8a1110ff5f052d3710fe57f29c45503d`，根 `LICENSE:1-13` 为 MIT | `internal/safefile/safeopen.go:61-109`：`OBJECT_ATTRIBUTES.RootDirectory` 锚定现有目录 handle，`OBJ_DONT_REPARSE` 后调用 `NtCreateFile`；拒绝任一中间 reparse point。 `OpenRoot` 仍路径式打开，不能作为安全 bootstrap 证据。复用相对 handle 算法设计，选择 `PORT DESIGN`，不依赖 Go runtime。 |
| Microsoft HCSSHIM，本地浅克隆 `D:\\reference\\hcsshim`，HEAD `ab8249e8a1110ff5f052d3710fe57f29c45503d`，根 `LICENSE:1-13` 为 MIT | `internal/safefile/safeopen.go:61-109`：`OBJECT_ATTRIBUTES.RootDirectory` 锚定现有目录 handle，`OBJ_DONT_REPARSE` 后调用 `NtCreateFile`；拒绝任一中间 reparse point。 `OpenRoot` 仍路径式打开，不能作为安全 bootstrap 证据。复用相对 handle 算法设计，选择 `PORT DESIGN`，不依赖 Go runtime。 |
| Microsoft Learn：`CreateRestrictedToken`、Restricted Tokens、Mandatory Integrity Control、File Access Rights、Security Descriptor Operations | 规定 token 双重 access check、`WRITE_RESTRICTED` 写访问语义、Low integrity 的 no-write-up，以及 `FILE_DELETE_CHILD` 的父目录删除权 | 与 DSH 的 token/ACL 组合机制独立交叉验证；使用 Win32 原语，不引入替代 sandbox runtime。 |
| Chromium 官方 sandbox design 文档（2026-10-12 读取） | 生产浏览器使用 restricted token、Job Object、alternate desktop 与 integrity levels；更强的 process broker/policy/interception 层 | 证明成熟实现会组合多层 OS 控制；Chromium 的额外进程/桌面/IPC 边界远超 #729 文件写权限范围，不搬整套。Low IL 本身不禁止 TCP/IP；本票若只做文件面，不把网络承诺混进验收。 |

## DSH 实现与精确源码位置

以下代码位置均对应 commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`，链接固定到该 commit。

| 机制 | 源码依据 | 核实内容 |
|---|---|---|
| token 基础信息 | [`token.ts:23-41`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/token.ts#L23-L41)，[`token.ts:96-145`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/token.ts#L96-L145) | 从当前进程打开 token、找 logon SID；复制/设置 token default DACL，让受限子进程能创建匿名管道等新对象。 |
| restricted token 与 Low IL | [`token.ts:147-164`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/token.ts#L147-L164)，[`token.ts:181-242`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/token.ts#L181-L242) | `CreateRestrictedToken` flags 为 `DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED`；只读 restricting list 是 logon SID + Everyone，workspace-write 再加 workspace/temp SID。token integrity 设置为 Low。失败抛错，不生成普通权限子进程。 |
| ACL 构造、锁和应用 | [`acl.ts:64-126`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/acl.ts#L64-L126)，[`acl.ts:229-265`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/acl.ts#L229-L265)，[`acl.ts:353-413`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/acl.ts#L353-L413) | 以 `LockFileEx` 对同路径修改串行化；合并现有 DACL，写 capability allow ACE、目录继承的 Everyone `FILE_DELETE_CHILD` deny，以及 Low mandatory label；exact ACE 检查避免重复传播。 |
| 初始化、fail-closed 与副作用归属 | [`index.ts:224-340`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/index.ts#L224-L340)，[`README.md:128-131`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/README.md#L128-L131) | init 期间先 grant 再建 restricted token；失败路径回收 token/临时 grant。workspace ACE/deny/label 是 standing mutation，不随 dispose 撤销；temp grant 可 revoke。目录需要 caller-owned 且授予 `WRITE_OWNER`。 |
| runner 与退出语义 | [`runner.ts:116-220`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/runner.ts#L116-L220)，[`README.md:108-116`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/README.md#L108-L116) | runner 校验 argv/path/SID 配对，初始化 ACL sandbox，启动后镜像子进程 exit code；runner 失败以 127 显式失败。 |
| Windows FFI API | [`ffi.ts:39-111`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/ffi.ts#L39-L111)，[`ffi.ts:235-276`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/ffi.ts#L235-L276) | `advapi32`: token/SID/ACL/SACL、restricted-token、`Set/GetNamedSecurityInfoW`；`kernel32`: file lock、temp/environment/console/file APIs。 |
| 进程 FFI 与 Job | [`win32-process/src/ffi.ts:58-124`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/subprocess/win32-process/src/ffi.ts#L58-L124)，[`README.md:27-32`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/subprocess/win32-process/README.md#L27-L32) | `CreateProcessAsUserW`、pipe/handle APIs、Job APIs；子进程先 suspended、入 kill-on-close Job 后 resume。 |
| 路径边界与 TOCTOU | [`path-boundary.ts:7-40`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/path-boundary.ts#L7-L40)，[`fs-sandbox/index.ts:10-18`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/fs/fs-sandbox/src/index.ts#L10-L18)，[`fs-sandbox/index.ts:111-143`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/fs/fs-sandbox/src/index.ts#L111-L143) | `realpathSync.native` + `relative` 检查 setup paths；fs provider 对每次模型路径操作再 resolve。上游注释承认 check 与 syscall 间仍有 ancestor-symlink race，并将缩窗视为其 trusted-code threat model 可接受；这不能直接等同于 kernel race-free boundary。 |
| 已声明限制 | [`README.md:120-130`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/README.md#L120-L130) | 只限制写/删；读、网络、进程可见性不限制；硬链接别名绕过按路径预期；console isolation 不可用；Low label 和 workspace ACL 持久存在。 |

### Win32 与构建依赖

| 项目 | 已核实依赖 |
|---|---|
| Windows API | `kernel32.dll`：进程/Job/pipe/file/lock/environment；`advapi32.dll`：token、SID、ACL、安全描述符；Node API `uv_get_osfhandle` 用于 CRT fd 到 Win32 HANDLE。上表 FFI 链接给出绑定位置。 |
| 包运行时 | DSH `package.json:1-10,35-45`：Node `^22.19.0 || >=24.0.0`、pnpm 11.7、MIT；`koffi` 3.1.1 动态加载系统 DLL；内部 `dsh-lazy-require`、`dsh-win32-process`；`yaml`；peer 依赖 Cordis/DSH skill/subprocess。此 ACL package 自身是 TS/JS，未见自建 C++ sandbox DLL。 |
| ABI/build probe | [`abi-probe.cpp:1-26`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/verify/abi-probe.cpp#L1-L26)，[`win32-process README:44-50`](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/subprocess/win32-process/README.md#L44-L50) | 验证 Windows 头文件中的 SID/ACL/token/process ABI；说明命令为 `g++ -std=c++20 -municode ...`。这是 ABI 验证工具的编译依赖，不是产品运行时 native extension。token 实现注释记录其在 Windows 11 build 26200 验证；这是已记录验证环境，不据此推断最低支持版本。 |

## Windows 平台语义交叉核对

| 事实 | 一手来源与结论 |
|---|---|
| Restricted SID 的 token access check | [Microsoft `CreateRestrictedToken`](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-createrestrictedtoken) 明确写明：启用 SID 与 restricting SID 各做一次访问检查，两次均通过才授权；`WRITE_RESTRICTED` 让 restricting SID 只参与写访问；`DISABLE_MAX_PRIVILEGE` 保留 `SeChangeNotifyPrivilege`。这与 DSH 采用的两层写允许机制一致。 |
| Low integrity | [Microsoft Mandatory Integrity Control](https://learn.microsoft.com/en-us/windows/win32/secauthz/mandatory-integrity-control) 说明 MIC 在 DACL 前检查；Low token 不能写 Medium 对象，即使 DACL 允许。对象 integrity label 在 SACL 的 mandatory label ACE 中；无标签对象视为 Medium。故每个可写根需要标签，token Lower IL 后普通 workspace 文件不能仅靠用户 DACL 授权写入。 |
| 父目录删除权 | [Microsoft File Access Rights Constants](https://learn.microsoft.com/en-us/windows/win32/fileio/file-access-rights-constants) 定义 `FILE_DELETE_CHILD` 是目录删除该目录内子项（含只读文件）的权限。DSH 对 grant root 的目录拒绝该权限，避免仅限制子文件 DACL 时仍可用父目录权删除。 |
| path API 与 handle API | [Microsoft Security Descriptor Operations](https://learn.microsoft.com/en-us/windows/win32/secauthz/security-descriptor-operations) 说明一般用 `Get/SetSecurityInfo` 操作以 handle 标识的对象，用 `Get/SetNamedSecurityInfo` 操作以 name 标识的对象。DSH ACL merge/apply 为 name-based；如果调用方可并发替换路径组件，需单独证明 target identity 及 reparse handling。`CreateFile(FILE_FLAG_OPEN_REPARSE_POINT)` 只定义最终 symbolic link handle 行为，[官方文档](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilea#symbolic-link-behavior)不能单独证明整条 ancestor 路径无竞态。 |
| Chromium 对照 | [Chromium 官方 Windows sandbox design](https://chromium.googlesource.com/chromium/src/+/main/docs/design/sandbox.md) 使用 restricted token、Job、alternate desktop、integrity level，配合 broker/policy/interception。它也注明 Low IL 可访问 TCP/IP 等共享资源；更强的网络隔离需要额外 AppContainer/capability 决策。它是多层 process sandbox 的成熟设计，不是 #729 文件写 ACL 的直接依赖。 |

以上含 Microsoft 官方 Win32 规范与 Chromium 官方实现文档两类独立一手来源，且 DSH 的实现行已固定到可复现 commit。没有引用博客或二手说明。

## 本仓契合度与复用判定

- 当前代码已定义 Python `Sandbox` Contract 和 workspace path guard（`src/agent_harness/sandbox/base.py:93-100`）；后端选择目前只有 `local` / `docker`（`capabilities.py:20`）。Windows `LocalSubprocessSandbox` 的 `ctypes.WinDLL` / Job Object 代码（`local.py:408-447,463-466`）只负责关闭句柄时 kill process tree，不执行 token/ACL write confinement。
- `pyproject.toml:9-33,38-50` 未声明 `pywin32` 直接依赖；`uv.lock:793,1667,2746-2754` 在 Windows 条件项中出现 pywin32 312（MCP、可选 Docker 的依赖图）。因此不能把当前锁文件中偶然存在的传递包当成 sandbox 稳定 API。标准库 `ctypes` 已在 local backend 有先例；如后续选择 pywin32，应先把版本和 Windows-only dependency 正式纳入直接依赖与包安装契约。
- Phase 3（Docker Sandbox + Coding Tools）已在 `docs/PHASE_STATUS.md:22` 标记完成；当前票是现有 sandbox backend 的 Windows 文件写安全能力扩展。`SPEC_ROOT/05_SANDBOX_CODING_TOOLS.md` 的核心原则是 Sandbox 属于 Runtime 安全边界，Contract 保持薄；全局 Vision 要求安全策略由 Runtime 强制。本票应继续沿现有 Sandbox seam 接入，保持 Docker 行为独立。
- **判定：PORT DESIGN（按 `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md` §3）**。复用 restricted-token + restricting SID 双重校验、Low integrity + workspace allowlist、目录 `FILE_DELETE_CHILD` deny、显式失败、幂等 ACL merge 等设计。不能直接装配 DSH npm 包，也不需要复制 Cordis 或整个 Chromium sandbox。最低新代码取决于现有 Python FFI seam；暂时不指定 `ctypes` 与 pywin32 的最终选择。

## 契合边界、风险与实现前需钉明的事实

1. **持久副作用与清理**：workspace ACL/Low label 会跨 session/process 保留；Low-IL 同用户进程因此可写/删该树。即使程序崩溃，ACL 元数据不会自动回滚。若产品承诺会清除，必须证明拥有完整原 DACL/SACL 快照与恢复路径；单纯关闭 Job/子进程不够。
2. **授权能力和失败边界**：DSH 要求 workspace 目录为 caller-owned、有效授予 `WRITE_OWNER`；不满足时 fail closed。Large workspace 上继承 ACL 会递归作用于 descendants，DSH 将首次 materialization 记为可能耗时数十秒至数分钟，靠 exact-entry 跳过后续重复传播。
3. **Threat model**：`WRITE_RESTRICTED` 只相交写访问；读文件仍按当前用户权限；网络和创建子进程没有被该 token 限制。硬链接将同一对象暴露为 workspace 外路径；DSH 将其列为无法通过路径检查消除的边界。若 #729 的验收把“沙箱”理解为读取/网络/进程隔离，则本来源不足以满足。
4. **路径竞态**：路径检查后再按名称打开/改 ACL 存在 TOCTOU。无竞态方案以固定 workspace directory handle 为根，路径解析在内核单次相对打开中禁止 reparse；所有 I/O 与 ACL 修改使用得到的同一个对象 handle。HCSSHIM 的测试只验证静态链接拒绝，本仓仍须有真实 Windows 并发替换回归。
5. **成熟实现的范围控制**：Chromium 的 broker、Job policy、alternate desktop、AppContainer 属于独立能力；当前票只借鉴 OS token/ACL 机制，保持 Route A Permission 与 Route B OS sandbox 两个责任面独立。不将 Chromium 全套机制扩大为本票要求。
6. **来源许可**：DSH 根 License 允许 MIT 范围内使用、修改和分发，但必须在副本/实质部分保留版权与许可文本。若只移植设计、从头编写 Python API 调用，也应保留来源与差异记录，避免把上游贡献归成自研。

## 参考链接

- [DeepSeek Harness repository](https://github.com/deepseek-ai/deepseek-harness/tree/5badb15009ae1756c3afe0ae0cef1faafc290ccc)
- [DSH LICENSE at pinned commit](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/LICENSE)
- [Microsoft: Restricted Tokens](https://learn.microsoft.com/en-us/windows/win32/secauthz/restricted-tokens)
- [Microsoft: Mandatory Integrity Control](https://learn.microsoft.com/en-us/windows/win32/secauthz/mandatory-integrity-control)
- [Microsoft: File Access Rights Constants](https://learn.microsoft.com/en-us/windows/win32/fileio/file-access-rights-constants)
- [Microsoft: Security Descriptor Operations](https://learn.microsoft.com/en-us/windows/win32/secauthz/security-descriptor-operations)
- [Chromium: Sandbox design](https://chromium.googlesource.com/chromium/src/+/main/docs/design/sandbox.md)


## 2026-10-12 用户裁决后的方案修订：handle-root、无 reparse TOCTOU

用户要求本票的路径边界真正无竞态，拒绝 DSH fs-sandbox 的 resolve/re-normalize 方案。以下结论覆盖本文前文中“DSH 路径重规范化可作为路径防护设计”的任何歧义；产品代码与测试仍未修改。

### 新增成熟实现核实

| 来源 | 核实事实 | 复用判定 |
|---|---|---|
| Microsoft HCSSHIM，本地浅克隆 D:\reference\hcsshim，HEAD ab8249e8a1110ff5f052d3710fe57f29c45503d，remote microsoft/hcsshim，根 LICENSE:1-13 为 MIT | internal/safefile/safeopen.go:61-109：OBJECT_ATTRIBUTES.RootDirectory 锚定现有目录 handle，OBJ_DONT_REPARSE 后调用 ntdll.NtCreateFile；openRelativeInternal 注释明确任何中间路径组件是 reparse point 时失败。 | PORT DESIGN：复用相对 handle + 内核 no-reparse 打开机制，不依赖 Go runtime。OpenRoot 自身仍走绝对路径；不能把它当作安全根初始化证明。 |
| Microsoft Learn OBJECT_ATTRIBUTES | OBJ_DONT_REPARSE 遇到任一 reparse point 会返回 STATUS_REPARSE_POINT_ENCOUNTERED。 | 这是单次内核路径解析的 no-follow 语义，支撑替换重解析点时只可能得到根内对象或失败。 |
| Microsoft Learn Security Descriptor Operations | 一般应对 handle 标识对象使用 GetSecurityInfo / SetSecurityInfo；按 name 的 API 用于 name 标识对象。 | ACL/DACL/SACL 操作以实际打开的 handle 为目标，避免 SetNamedSecurityInfoW 的 path re-resolution。 |

固定源码及许可：
- HCSSHIM commit: ab8249e8a1110ff5f052d3710fe57f29c45503d
- internal/safefile/safeopen.go:61-109，RootDirectory + OBJ_DONT_REPARSE + NtCreateFile
- internal/safefile/safeopen.go:114 起，OpenRelative 公共包装
- LICENSE:1-13，MIT / Microsoft
- GitHub: https://github.com/microsoft/hcsshim/blob/ab8249e8a1110ff5f052d3710fe57f29c45503d/internal/safefile/safeopen.go

### 最小可行方案（进入代码前）

1. 新增 Windows ACL backend，沿用 DSH restricted-token / capability SID / Low IL / FILE_DELETE_CHILD / Job 设计；permission_mode 只管审批，backend 明确选择，不可用时 fail-closed。
2. 初始化阶段从本地卷根开始逐段打开目录 handle，任何路径 reparse point 即拒绝；持有 workspace 根 handle，并把 volume serial + file ID 记入 workspace mapping，恢复时通过新打开的根 handle 比对身份。
3. 在 Windows backend 内建立一个集中式 handle-relative 文件操作层：相对根/父目录 handle 用 NtCreateFile + OBJ_DONT_REPARSE 打开；写临时文件、原子替换、删除、枚举、复制以及递归 ACL 更新都通过句柄完成。拒绝绝对路径、..、ADS、设备命名空间、UNC 和非支持文件系统。无标准库或已装依赖满足该语义；优先 ctypes + Windows 系统 DLL/native API，不新增 runtime 依赖。
4. 将 ReadTool、WriteTool、workspace 文件路由和项目指令读取中“先 resolve 成 Path、后按路径调用文件 API”的入口接到 Sandbox 的 handle-safe 操作；现有 Local/Docker 语义保持不变。任何普通 Path 仅能用于展示/非授权 pathspec 校验，不能作为 ACL 或文件访问授权。
5. real Windows 回归从公开 Sandbox/WorkspaceRegistry API 进入：静态与并发替换末端/中间 junction、symlink、reparse component；读/写/复制/列表/删除/ACL 初始化面对外部 canary 要么在 pinned root 内完成，要么 fail；root path 被替换和恢复时 file identity mismatch 都 fail-closed。另验证子进程 workspace 写入成功、根外写入被 Windows 拒绝、Job kill。
6. 留下明确 threat model：用户已接受 DSH persistent ACL/Low label；无网络/进程/读取隔离保证；管理员/内核/受信任宿主进程主动迁移 root 不在本票防护范围。

### 仍需确认的实现测试 seam

TDD 施工前请用户确认以下公开入口：WorkspaceRegistry.create(..., backend="windows_acl") / 恢复，以及真实 Windows Sandbox 的 read_text、write_text、copy_in、list_files、delete、exec；另通过 ReadTool / WriteTool / workspace-file route / project-instruction loader 验证既有消费者不再绕过 handle-safe API。并发替换测试由 Windows 外部 mutator 进程与 outside canary 驱动。
