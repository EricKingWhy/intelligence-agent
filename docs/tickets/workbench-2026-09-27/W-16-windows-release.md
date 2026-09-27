# W-16 · Windows x64 安装、手动更新、卸载与旧数据回退
**目标仓库**：intelligence-agent-frontend（安装器与桌面/TUI 产物）+ intelligence-agent（跨仓集成验收与证据）。

**类型/优先级**：P0 Release。**依赖**：W-15、W-17、W-13。**范围**：Electron 构建/安装包脚本、Python 服务冻结或捆绑、Web/TUI 静态资源、安装烟测；不修改 Agent Core 行为。

## 首版交付物

一个 Windows x64 安装包，安装后无需系统预装 Python/Node；桌面快捷方式与终端 TUI 命令可用，TUI 在桌面未运行时能冷启动 W-11 服务。使用当前用户安装路径，用户数据、SessionEvent、Artifact、模型配置、凭证与工作目录均在安装目录之外。首版允许未签名，但产物有 SHA-256、源码版本/依赖锁与本地构建记录；不提供自动更新服务。

## 工作指令

1. 先做干净 Windows VM/账户打包探针：验证 Python 第三方依赖、原生轮子、keyring、Web assets、TUI Node/runtime、Chrome MCP 可选进程的路径；安装后不能联网下载 Python Core 才能首次启动。OpenHands 首启 `uvx` 下载**不是**本票允许的离线交付。
2. 新版手动安装的 preflight 列出在途 Task/其他客户端；安全暂停+Ledger 结清失败就中止替换。写 schema 前备份并校验旧数据，迁移失败恢复旧目录/旧程序可用；新版首启只 reconcile、不自动续跑。
3. 卸载不默删 SessionEvent、Artifact、workspace、Windows 凭证；需要清理用户数据必须是单独显式动作，并先预览证据 ref 影响。保存安装/升级/卸载日志时不记密钥。

**验收**：干净 Windows x64 VM 装→桌面启动→TUI 冷启动→旧会话与模型配置可见→一次真实短任务→运行中更新安全暂停→新版 reconcile→手动续跑→卸载后用户数据仍在；再做服务暂停失败、迁移中断、磁盘满回退。记录 SHA-256、安装包版本、VM 前置软件列表、实际命令/截图。**不做**：公共签名发布、自动更新、多平台安装包。

**成熟参考/复用**：[DeepSeek Windows installer/烟测](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md) MIT 可 `PORT DESIGN` 测试流程；[`test-windows-installer.mjs`](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/scripts/test-windows-installer.mjs) 可 `ADAPT` 安装/卸载框架并保留来源；[OpenHands Electron builder](https://github.com/OpenHands/OpenHands/blob/main/electron-builder.config.mjs) MIT 供资源布局参考，不复制其首启下载。
