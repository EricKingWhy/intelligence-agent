# #729 MXC PSEC 现场策略修订

> 日期：2026-10-13。范围：复核 MXC 1.0.0 在当前 Windows 主机上的真实请求选择；不改 issue 票面。

## 结论

早期桥接研究建议用非空 processContainer.filesystem.enumeratePaths 强制 MXC 选择 BaseContainer/PSEC。实际安装的 MXC 1.0.0 报告 baseContainerSupportsEnumeratePaths=false；携带该字段的 probe 返回 Windows 不支持该能力。因此本实现没有使用 enumeratePaths。

改用 MXC 1.0 schema 已支持的 filesystem.deniedPaths，拒绝 %SYSTEMROOT%\Temp。该目录原本在只读 SystemRoot 授权范围内；子进程 TEMP/TMP 已指向 workspace/.tmp，故拒绝读取主机 Windows 临时目录符合最小权限边界。请求同时设置 fallback.allowDaclMutation=false。MXC 在 BaseContainer 不支持 native deny 时会考虑 AppContainer 路径；该路径要实施 deniedPaths 需要 DACL 修改，而 fallback 被禁用后会失败关闭，不能静默采用 AppContainer/DACL。成功运行的实际请求也携带同一 deny policy。

## 本机实测

安装二进制：desktop/node_modules/@microsoft/mxc-sdk/bin/x64/wxc-exec.exe，包版本 1.0.0。对生产构造的完整请求执行 --probe，读数：

- returncode=0
- tier=base-container
- needsDaclAugmentation=false
- baseContainerApiPresent=true
- baseContainerSupportsDenyPaths=true
- baseContainerSupportsEnumeratePaths=false
- error=null

使用 exact packaged MXC binary 运行的 tests/sandbox/test_windows_psec_sandbox.py：24 passed、1 skipped。跳过项为本机无法创建文件 symlink；目录 junction 的双向并发替换测试通过。受影响的 Python 回归组：180 passed、2 skipped（另一个 skip 是 Windows 上的 POSIX shell 专用用例）。Desktop npm test、check、build、Windows installer compile-only 与 Ruff check/format 均退出 0。后端全量、Gate-0 和双轴 review 尚未完成。

## 依据与复用

- MXC 固定源码：D:\reference\mxc，commit c6f301d53a1430c4c921a05c57af838f7392348f。schema 将 deniedPaths 定义为文件访问策略，并说明 BaseContainer 使用 PSEC fs_deny；AppContainer 的 deniedPaths 需要宿主 DENY ACE。fallback_detector.rs 检查 native deny 不可用时的 AppContainer 路径，并在 DACL fallback 禁止时拒绝。
- MXC schema: https://github.com/microsoft/mxc/blob/c6f301d53a1430c4c921a05c57af838f7392348f/docs/schema.md#L373-L390
- MXC tier selection: https://github.com/microsoft/mxc/blob/c6f301d53a1430c4c921a05c57af838f7392348f/src/mxc-sdk/src/backends/process_container/common/fallback_detector.rs#L330-L405
- Codex handle-relative no-reparse reference: docs/research/2026-10-12-issue-729-mature-sandbox-reuse.md

## 状态

产品实现提交 0efb0c87。仍待后端全量、Gate-0、双轴审查；未 push/PR/merge/关闭 #729。
