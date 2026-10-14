# #729：成熟产品复用与 Windows 竞态边界

调研日期：2026-10-12。只读检查本机三个上游克隆；没有改产品代码或 Issue。来源版本：Codex openai/codex @ 7f892275e31002f0422477c6219189284560e689；Pi earendil-works/pi @ 1b347794e2a630e4359f2584f4eea388145d0ddf；DeepSeek Harness @ 5badb15009ae1756c3afe0ae0cef1faafc290ccc。三个上游工作树均 clean；#729 worktree 原有 dirty 状态未触碰。

## 结论

**直接复用 MXC 做子进程隔离；复用/移植 Codex 的句柄相对 no-reparse 原语做宿主文件 API。** 三个产品没有一个能整包放进当前 Python 项目并同时覆盖这两个运行边界。Pi 不提供内建 OS 权限边界；DeepSeek Harness 的 Windows ACL 后端明确是 partial，不能满足严格 workspace-only。

### Codex：最匹配的两块成熟实现

- 子进程边界：Codex 当前 Windows MXC 入口调用 BaseContainerRunner，要求 PSEC 可用；启动前探测失败就拒绝。其策略把 read/write/deny 路径交给 MXC，并关闭 DACL fallback。代码是 Rust，并依赖 MXC 的 Rust crate / Windows SDK bindings，耦合 Codex 自己的 PermissionProfile、路径模型和网络代理。它能作 MXC 接入的参考；Python 不能直接 import 这段 Rust。见 [native.rs:28-42](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/mxc-sandbox/src/native.rs#L28-L42)、[policy.rs:260-311](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/mxc-sandbox/src/policy.rs#L260-L311)、[lib.rs:89-100](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/mxc-sandbox/src/lib.rs#L89-L100)。Codex 注释还说明 MXC/PSEC 在访问时检查 junction 解析后的目标；这是实现说明，不应代替本项目的并发探针。[policy.rs:309-311](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/mxc-sandbox/src/policy.rs#L309-L311)。Microsoft MXC 上游的 wxc-exec.exe 可接受稳定 JSON schema，官方 README 把它列为 SDK 无法嵌入时的执行入口：[MXC README:71-78](https://github.com/microsoft/mxc/blob/c6f301d53a1430c4c921a05c57af838f7392348f/README.md#L71-L78)。
- 宿主文件操作边界：Codex 的 no-reparse helper 以 NtCreateFile + OBJ_DONT_REPARSE 打开路径，并可用 RootDirectory 句柄相对创建；guard 文件阻止已固定目录被原地转成 reparse point。原子写入通过固定父目录句柄创建临时文件，再用 handle-relative rename 替换目标。这个 helper 只接受绝对本地盘路径，拒绝 UNC 与 ADS；移植前需核对 #729 是否承诺网络路径。[no_reparse_dir.rs:73-90](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/windows-sandbox-rs/src/no_reparse_dir.rs#L73-L90)、[open_directory_no_reparse:112-137](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/windows-sandbox-rs/src/no_reparse_dir.rs#L112-L137)、[guard/open_no_reparse:198-279](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/windows-sandbox-rs/src/no_reparse_dir.rs#L198-L279)、[tests:179-193](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/windows-sandbox-rs/src/no_reparse_dir_tests.rs#L179-L193)。原子写入实现见 [file_write.rs:54-145](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/windows-sandbox-rs/src/file_write.rs#L54-L145)。Microsoft 文档说明 NtCreateFile 可通过 OBJECT_ATTRIBUTES.RootDirectory 做相对句柄路径解析：[InitializeObjectAttributes](https://learn.microsoft.com/en-us/windows/win32/api/ntdef/nf-ntdef-initializeobjectattributes)。
- 竞态证据边界：Codex helper 明确要求调用方安装 guard 后重新核验路径身份，并在特权操作期间保留目录句柄；这说明仅 canonicalize/check-then-open 不够。现有测试覆盖静态的最终 junction 与祖先 junction，[no_reparse_dir_tests.rs:131-160](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/windows-sandbox-rs/src/no_reparse_dir_tests.rs#L131-L160)；没有证明并发替换压力测试覆盖。故可复用的是 native handle 机制和调用约束，不能把上游静态测试说成完整 race-free 验收。
- 分层限制：MXC 约束它启动的子进程；本项目 Python 进程自己调用的 read/write/copy/list/delete 不会因此自动受限制。这一点也与已有桥接调研一致：[MXC SDK bridge research](2026-10-12-issue-729-mxc-sdk-bridge-research.md)。

### Pi：借鉴隔离部署方式，不借它的文件边界实现

Pi README 明说没有内建 filesystem/process/network/credential 权限系统，默认继承启动用户权限；官方做法是把整个 Pi 放进 Docker/OpenShell，或把特定工具路由到 Gondolin micro-VM。[README:40-48](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/README.md#L40-L48) Pi 的 read/write 默认直接调用 Node fs；write tool 只解析相对 cwd 的路径、mkdir、writeFile，不做 workspace containment。[write.ts:27-89](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/coding-agent/src/core/tools/write.ts#L27-L89)、[path-utils.ts:44-50](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/coding-agent/src/core/tools/path-utils.ts#L44-L50)。可借鉴的是整进程容器化或显式隔离执行后端的产品边界；不能把 Pi tool path helpers 当安全沙箱。Pi 是 TypeScript/Node，MIT；官方容器方案依赖 Docker/OpenShell/Gondolin，不是 Windows 原生 reparse helper：[containerization.md:1-28](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/coding-agent/docs/containerization.md#L1-L28)。

### DeepSeek Harness：可借鉴 fail-closed seam，ACL 方案不满足本票

DSH 把 SandboxProvider 做成跨平台 subprocess seam，没可用 runner 就 fail closed；Windows runner 是 restricted token + workspace/temp capability SID ACL + Low integrity label。[sandbox docs:5-30](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/subsystems/sandbox.md#L5-L30)、[runner.ts:1-43](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/src/runner.ts#L1-L43)。该实现使用 TypeScript/Node、koffi 3.1.1 和共享 Win32 process package：[package.json:35-45](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/package.json#L35-L45)。

DSH 官方 README 将 Windows 保证标为 partial，列出 hard-link 文件对象别名与 AppContainer ACL 目录兼容边界；工作区 ACE、deny 与 Low label 是 standing，退出不撤销。[README:10-36](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/README.md#L10-L36)、[README:120-130](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/sandbox/sandbox-windows-acl/README.md#L120-L130)。它可复用 provider seam、失效关闭、reported enforcement 等设计；不应作为严格 workspace-only / 无竞态方案直接照搬。检查其 sandbox-windows-acl 与 sandbox-local 源码时未发现类似 Codex 的 OBJ_DONT_REPARSE / handle-relative 文件访问入口，因此它没有给本票补上宿主路径竞态保护。

## 最小复用判定

| 边界 | 复用判定 | 理由 |
| --- | --- | --- |
| 托管 shell / Python 子进程 | REUSE：Microsoft MXC 官方 wxc-exec.exe + 稳定 JSON schema；Codex 的 PSEC-only、fail-closed 接入方式作参照 | 正式 OS 进程边界；避免自己造 token/ACL sandbox。wxc-exec 属 MXC MIT 包；Codex 源码 Apache-2.0。 |
| Harness 自己的 workspace 文件 API | ADAPT：移植 Codex 的 no-reparse、handle-relative helper 模式 | 必须用同一 root identity 锚定目录句柄、每级拒绝 reparse、在句柄上执行文件操作；仍须补并发 junction/symlink 替换测试。Python 直接重写 Win32 FFI 等于新写安全关键实现；若可以接受一个极小 Rust helper，直接移植更贴近上游。 |
| Pi | PORT DESIGN：仅借整进程容器或 backend seam 的部署概念 | 无内建 OS 边界，文件工具是本机 Node fs。 |
| DSH Windows ACL | DEFER：不选作严格方案 | 上游自己报告 partial；有 hard-link 越界，且 ACL / Low 标签具有持续副作用。 |

许可：Codex 仓库为 Apache-2.0；MXC、Pi、DSH 为 MIT。若移植代码，保留对应版权和许可文本，并只带入必要文件/依赖。当前 Python 项目无法直接调用 Rust/TypeScript 包的内部类型；优先调 MXC 官方可执行文件，避免复制其引擎或照搬产品耦合层。

本记录是针对上游能力与复用面的设计证据，不是并发竞态验证报告；并发替换仍须在 #729 已批准的真实 Windows seam 上验证。
