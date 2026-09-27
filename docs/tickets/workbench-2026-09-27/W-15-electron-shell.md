# W-15 · Windows Electron 薄宿主、托盘和安全桥
**目标仓库**：intelligence-agent-frontend（新建 Electron desktop/ 包）。

**类型/优先级**：P0 Desktop。**依赖**：W-11、W-12、W-14。**范围**：新 `desktop/` TypeScript/Electron 包与既有 `web/` 构建接线；Python 服务仍由 W-11 唯一拥有 Session/Tool/权限。

## 工作指令

1. 启动时先申请 Electron 单实例；第二次启动只唤醒已有窗口。按 W-11 协议附着或启动 Python Host，健康检查完成后才加载打包 Web 页面；启动失败显示重试、打开脱敏日志、退出三个可操作选项。
2. 主窗 Windows ×/Alt+F4 默认隐藏到托盘，首次提示任务可能继续；托盘只提供“打开”和“退出应用”。退出前查询桌面托管的 Task 与其他连接客户端；退出只撤销桌面在场登记，仍有客户端托管同一 Task 时 Task 继续运行，仍有任何连接客户端或需服务的 Task 时 Python 服务不得退出。查询失败按有任务的保守路径提示，不默默杀进程；不提供绕过在场检查的强制停止动作。
3. Renderer 开 `contextIsolation`/sandbox，禁 Node integration；preload 只暴露受限目录选择/窗口状态/服务连接 bootstrap，不提供任意文件读写、裸 IPC 或凭证。桌面只允许本地自有页面使用 Host token；外部链接用系统浏览器且不传 token。

**验收**：两个启动只一个窗口/服务；首次启动服务慢/失败、关窗隐藏、托盘恢复、桌面退出但 TUI 仍在线、最后客户端退出安全暂停、恶意页面不能调用桥、目录选择取消无副作用。Windows 实机截图/进程表、前端 e2e/TypeScript check；不能只在浏览器 dev server 测。**不做**：重写 React UI、桌面 GUI 自动操作、Node Agent Runtime。

**成熟参考/复用**：[DeepSeek 官方 Electron desktop](https://github.com/deepseek-ai/deepseek-harness/tree/master/apps/desktop) MIT：可在依赖/许可检查后 `REUSE/ADAPT` [`single-instance.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/src/single-instance.ts)、[`tray.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/src/tray.ts)、[`backend-controller.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/src/backend-controller.ts) 的独立代码，保留来源 commit/MIT；`host-process.ts` 的 Node Host 耦合部分仅 `PORT DESIGN`。另参考 [OpenHands Electron 首启/健康检查](https://github.com/OpenHands/OpenHands/blob/main/electron/main.mjs) MIT，不照搬其 uvx 下载路径。
