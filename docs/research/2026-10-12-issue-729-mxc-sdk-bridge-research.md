# #729：MXC 官方 .NET / Node SDK 接入与 PSEC-only 结论

调研日期：2026-10-12。只核对 Microsoft MXC 的公开包、SDK API 与 pinned 源码；未改产品代码或 issue。源码克隆：D:\reference\mxc，commit **c6f301d53a1430c4c921a05c57af838f7392348f**。

## 结论

无需重写隔离引擎：Microsoft 发布的 MXC .NET 与 Node SDK 均可在当前进程内调用打包的 mxc_ffi，公开 API 有 request-aware probe、run 和 streaming spawn。Python 需要一个很薄的进程桥来传递请求、进程流和退出状态；这部分才是本项目的适配代码。

两套公开 V1 API 都未暴露 wire 字段 fallback.allowDaclMutation。但这不阻碍本票严格使用 PSEC：给同一个 ProcessContainer 请求设置非空 processContainer.filesystem.enumeratePaths（工作区目录列表本身也需要此能力）。MXC 原生代码会先要求 BaseContainer/PSEC 可完整服务该请求；不支持时直接报错，不能退到 AppContainer tier；可服务时明确选择 BaseContainer。随后运行仍会执行同一检查，因此探测后的能力变化也不会让这条请求静默降级。该能力要求 Windows BaseContainer 与 PSEC 1.1 fs_enumerate；主机不支持时应失败关闭。

PSEC 只约束 MXC 托管的子进程。Python 进程中的 Sandbox read_text/write_text/copy_in/list_files/delete 等宿主文件 API 仍需独立的句柄锚定安全实现，不能认为调用 SDK 后这些宿主操作自动受 PSEC 保护。

## 已发布包与运行前提

| 包 | 当前稳定版 | 原生资产 / 运行时 | 对 Python 桥的含义 |
|---|---|---|---|
| [Microsoft.Mxc.Sdk](https://www.nuget.org/packages/Microsoft.Mxc.Sdk/1.0.0) | 1.0.0；目标框架 net8.0，NuGet 显示无包依赖 | 包含 win-x64、win-arm64 原生资产；SDK 经进程内 mxc_ffi 调用，不要求启动 wxc-exec executor | 一个小型 .NET 8 helper 可用 Probe + Spawn；framework-dependent 发布要求目标机有对应 .NET runtime，或将 helper self-contained 发布 |
| [@microsoft/mxc-sdk](https://www.npmjs.com/package/%40microsoft/mxc-sdk/v/1.0.0) | 1.0.0；MIT；Node >=24 | 包含平台原生资产；源码 loader 按 bin/<arch>/mxc_ffi.dll 查找 Windows DLL；公开 API 经进程内 mxc_ffi | 一个小型 Node helper 可用 probe + spawn；Node 24 是运行前提，Windows 流式 stdio 还要求 Node 24.21.0+ 或 26.8.0+；包依赖 koffi、semver、node-pty |

.NET 包的 nuspec 把原生文件放入 runtimes/win-x64/native 与 runtimes/win-arm64/native，并声明 net8.0；NuGet 页面当前显示 1.0.0 且无依赖。[NuGet 包说明](https://www.nuget.org/packages/Microsoft.Mxc.Sdk/1.0.0) .NET 8 framework-dependent 与 self-contained 是两种发布方式，前者需要目标机提供 runtime。[Microsoft .NET 部署说明](https://learn.microsoft.com/en-us/dotnet/core/deploying/)

Node 包的官方 registry 当前发布版为 1.0.0，要求 Node 24+；仓库 package manifest 声明其运行依赖。[npm 包页](https://www.npmjs.com/package/%40microsoft/mxc-sdk/v/1.0.0) [MXC Node SDK README](https://github.com/microsoft/mxc/blob/c6f301d53a1430c4c921a05c57af838f7392348f/sdk/node/README.md)

## 请求 PSEC-only

- .NET：Containment.ProcessContainer + ProcessContainer.Filesystem.EnumeratePaths；MxcContainer.Probe(request) 可检查所选 tier，MxcContainer.Run/Spawn 执行同一 ContainerRequest。
- Node：containment: { type: 'processcontainer', config: { filesystem: { enumeratePaths: [...] } } }；probe(request) 返回 tier，run(request) / spawn(request) 执行同一请求。
- enumeratePaths 是真实的目录查询能力：只允许查询/列举，不授予文件内容读取。用工作区根路径满足 list_files 的实际需求，不要加入虚假的无关路径。

PSEC-only 保证来自 MXC 运行时契约，而非只信任前置 probe：can_backend_service_request 会验证 BaseContainer API 与 PSEC enumerate 能力；choose_backend_tier 对非空 enumerate 请求在能力不足时立即返回 EnumeratePathsUnsupported，能力满足时才返回 BaseContainer tier。fallback.allowDaclMutation=false 仅控制 DACL 修改同意，也不是通用“只许 BaseContainer”开关；当前公开 V1 request 类型没有这个字段。MXC 的 schema 文档也把 enumeratePaths 标为仅 BaseContainer/PSEC 1.1 支持，并规定不支持时拒绝而非回退。[MXC schema](https://github.com/microsoft/mxc/blob/c6f301d53a1430c4c921a05c57af838f7392348f/docs/schema.md#L375-L381)

## 最小 Python 桥接边界

Python 不能直接调用这两个托管 SDK 的类型系统。可用已有 asyncio.create_subprocess_exec 启动一个 Node 或 .NET helper，通过 JSON 行协议提交请求；helper 调 SDK 的 probe，核对 tier 为 base-container，再用 spawn 转发 stdin/stdout/stderr、退出码、超时和终止。保留 enumeratePaths 的原始请求字段，使启动时原生能力检查仍然强制 fail-closed。

选择 .NET 还是 Node 取决于产品已经随包提供哪一个 runtime：NuGet SDK 本身依赖更少；Node 包的 Python IPC/流转接自然，但要求 Node 24 与其 npm 依赖。两者都复用同一 MXC native engine，无需本地 Rust/Cargo。当前任务不应复制 MXC sandbox 内核或 Codex 私有 helper。

## 可复核源码（D:\reference\mxc @ c6f301d53a1430c4c921a05c57af838f7392348f）

- .NET 公共 API：sdk/dotnet/Microsoft.Mxc.Sdk/V1/MxcContainer.cs:36,202,262；ProcessContainer 与 enumeratePaths：.../V1/ContainerRequest.cs:109,130,217-221；目标框架/版本：.../Microsoft.Mxc.Sdk.csproj:4,11；NuGet RID 资产：...Package/Microsoft.Mxc.Sdk.nuspec:17,21-27。
- Node 公共 API 和请求类型：sdk/node/src/v1/probe.ts:78-95、sdk/node/src/v1/container.ts:216-244,310-312,428-479、sdk/node/src/v1/types.ts:203-235,566-600；native DLL 路径：sdk/node/src/native-library.ts:44-63；包版本/运行时：sdk/node/package.json:3,27-28,61-67。
- PSEC 选择和 fail-closed：src/mxc-sdk/src/backends/process_container/common/base_container_runner.rs:431-476、src/mxc-sdk/src/backends/process_container/common/fallback_detector.rs:284-325,561-605；wire Fallback 为内部生成类型：sdk/dotnet/Microsoft.Mxc.Sdk/Generated/MxcConfigV1_0_0.g.cs:178-187,590-597。

以上本地行号可用固定 commit 对应的 [MXC GitHub 源码](https://github.com/microsoft/mxc/tree/c6f301d53a1430c4c921a05c57af838f7392348f)复核。
