# W-21 D5（#817）产物内 TUI：证据与结论

父票 #365 [W-21] · 缺陷 D5（冻结树读数见 `40-windows-gate-result.md` §D5）。
本文件记录 **修复后** 的实机读数，供 #365 的 Run A/Run B 与 B-4 引用；
不宣布 #365 Gate 通过。

分支 `fix/w21-windows-gate-fixes`，提交：

| 提交 | 内容 |
| --- | --- |
| `9d0214e9` | 桌面「先附着后启动」+ `mount_static` 的产物内默认目录（`resources/web`） |
| `0e3d4d6b` | TUI host 层：端点发现 / 冷启动 / 附着 / 凭据通道 / 单写者测试 |
| `ac37371a` | 出件：`resources/tui` + `resources/node` + `ia-tui.cmd` 与两条 afterPack 断言 |

产物：`desktop/dist-installer/Intelligence-Agent-Setup-0.1.0.exe`，
204,370,634 B，sha256 `5e2c0407ec5c2103c3043440d4bc61af6589f2126826cacb82d41323037bffd0`
（`installer-build.json`：`lockfileSha256=7f670bd5…`、`nodeLockfileSha256=63b791d3…`）。

## 关键测量：为什么产物自带 Node 运行时

D5 的启动器原设计沿用 VS Code `bin/code.cmd` 形状（`ELECTRON_RUN_AS_NODE=1` +
应用 exe）。实机测量否定了它，四次探针（Electron 44.5.1 / Node 24.21.0）：

| 探针 | 读数 |
| --- | --- |
| `tty-probe.mjs`（真实控制台窗口内） | `isTTY {stdin,stdout,stderr} = false`；`setRawMode=undefined`；`columns/rows=null` |
| `tty-probe3`（同控制台对比系统 node 22.21.1） | fd 0 两者都是字符设备；系统 node `setRawMode=function`，Electron node 模式仍是 `undefined` |
| `tty-probe5.mjs` | 显式 `new tty.ReadStream(0)` / `WriteStream(1|2)` 全部 `ERR_TTY_INIT_FAILED` |
| `tty-probe2.cmd`（`< CON` 显式继承） | 仍无 TTY |

结论：Electron 的 node 模式**没有控制台句柄可用**，无法承载 raw-mode TUI
（VS Code CLI 是行导向，所以那条路对它够用）。因此产物改为自带 Node
运行时（`resources/node/node.exe`，`node-runtime.lock.json` 锚定
nodejs.org 的 SHA-256SUMS），这是 DSH `primary-runtime` 的「为需要真控制台
的界面单独带运行时」做法，而非其 CLI 复用 Electron 的做法。**这是本票内的
实现选择，不改 AC**；代价是安装包 +约 24 MB（装后 +93.5 MB）。

## AC 对照

### AC1 出件 + 产物内可运行

- 断言：`assertTuiRuntimeClosure`（入口 + 启动器 + **node.exe** + 从*出货*
  manifest 读出的依赖闭包）、`assertNodeRuntime`（afterPack 跑
  `node --version` 比对 pin）、`assertFreshBuild`（`tui/dist` 旧于 `tui/src`
  即失败）。`desktop/test/installer-tui.test.mjs`、`installer-node-runtime.test.mjs`。
- 实机：`D:/w21-work/evidence/d5e-tui-interactive.png` —— 打包产物
  `d5-pkg2\ia-tui.cmd --session new` 在真实控制台里渲染出 TUI
  （`○ idle` + 空会话提示 + 输入行 + 光标）；
  `d5e-tui-interactive-help.png` —— 键盘输入进入编辑器（回显 `en/help`，
  SendKeys 首字符被吞，属测试手法而非产品问题）。

### AC2 冷启动 + 附着

- `d5e-tui-check-1.txt`：`service: started http://127.0.0.1:57126 (pid 21844 …
  credential present)`，`GET /api/sessions` 带凭据 `HTTP 200` / 不带 `HTTP 401`。
- `d5e-tui-check-2.txt`：第二次调用 `service: attached http://127.0.0.1:57126
  (pid 21844 …)` —— 同一 pid/端口，未起第二个服务。
- 冷启动用的解释器是产物内的 `resources\python\python.exe`（pid 25452 的
  命令行为 `d5-pkg2\resources\python\python.exe -m agent_harness.cli serve`）。

### AC3 凭据通道

服务端未改（`auth_required: true`，端点文件 `.host-service.json` 照旧）；
TUI/桌面都走 `file:<path>` 通道（`AGENT_HARNESS_HOST_CREDENTIALS`），实机
200/401 如上。TUI 自身不打印 token（`describeService` 只报「credential
present」）。

> 读数注记：`AGENT_HARNESS_HOST_CREDENTIALS` 的值是 **`memory` 或
> `file:<path>` 两种形式**，写成裸路径会**静默**落到系统凭据管理器（探针期
> 间我这样起步过一次服务，客户端随即 401）。TUI 的失败文案已覆盖该情形
> （「该服务是用别的凭据通道（如系统凭据管理器）启动的…」），无需服务端改动；
> 本票据此观察备查。

### AC4 语义（B-4 前置）

规则（写进 `desktop/installer/README.md`「Terminal client」节）：

1. **一个数据根一个服务**：`InstanceLock` + 端点文件；桌面与 TUI 都是客户端，
   谁先启动谁拉服务，后到的**附着**（实测：TUI 起的 25452 端口/pid 在桌面启动
   后不变）。
2. **客户端从不杀服务**：退出只发 `client-exit`；`tui/test/host.test.ts` 的
   结构性用例遍历 `tui/src/**/*.ts`，禁止 `SIGTERM/SIGKILL/taskkill` 与
   `.kill(`，并限定 `node:child_process` 只出现在 `host.ts`。
3. **退出影响**：实测硬杀 TUI 客户端进程（node 22392）后服务 25452 仍存活，
   下一次 `--check` 附着到同一 pid/端口。

### AC5 Run B 前置动作

已跑：冷启动 → 附着 → 客户端退出不破坏服务（上面 AC2/AC4 的三条读数）。
Run B 本体（真实模型 + DB 提交后 kill + 桌面 reconcile + 手动续跑）留 #365；
本票不折抵。

## 其他读数（供 #365 准备）

- **打包安装的模型配置来源**：`Settings` 的 `.env` 锚定在
  `agent_harness/config.py` 的 `parents[2]`，装成 site-packages 后即
  `<install>\resources\python\Lib\.env`（该文件不存在时**退化为环境变量**）。
  实机验证：把仓库 `.env` 的键值注入进程环境后（不落盘、不打印），
  `POST /api/sessions` 从 500 `ConfigError: provider 'deepseek' 缺少 API key`
  变为可创建会话。→ Run A/Run B 与 W-16 烟测需要在启动客户端的环境里提供
  `MODEL_API_KEY`（及 `MODEL_NAME` 等）**或**在上述锚点放 `.env`。
- 未配置模型时 `POST /api/sessions` 返回 500（`ConfigError` 未映射为 4xx）：
  属服务端既有行为，不在本票范围，仅记录。
- 环境限制备查：本机的默认终端宿主是 Windows Terminal（`CASCADIA_HOSTING_WINDOW_CLASS`）。
  从无控制台的父进程（mintty 链路）直接 `Start-Process` 出来的 `cmd.exe`
  不会得到控制台；可用的交互窗口开法是
  `cmd /c start "" cmd /c <脚本>`（见 `D:/w21-work/run-tui-interactive.ps1`）。
