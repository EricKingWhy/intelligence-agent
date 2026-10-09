# 新安装件上的 Run A / Run B 前置验证（安装件 `01477e59…`）

- 场景：#365 的执行协议把 Run A / Run B 挂在两个前提上——「桌面能开窗、TUI 能冷启动附着」。
  旧安装件（`46e7a9e2…`）上这两条里第二条不成立：#846（装完首次启动 35.7–42 s 超出 `tui/src/host.ts`
  的 30 s 窗口）未修。本文件是在**含 #846 / #847 修复的新安装件**上对该前提的实测。
- 安装件：`Intelligence-Agent-Setup-0.1.0.exe`，204,379,033 B，
  sha256 `01477e59779693299216f9595966106e71a7f7d92409856aa18729899b8591e6`
  （HEAD `28a382cc`，`productCodeSha256=1d4688ad…` 与上一版一致）。
- 驱动：`D:\w21-work\pre-0147.py`（stage 各自落账）。工作树证据：`D:\w21-work\evidence\pre0147\`。

## 结论（先给读数）

| 腿 | 要求 | 读数 | 判定 |
| --- | --- | --- | --- |
| 静默安装 | 新安装件装上且带三处修复 | rc=0 / **122.7 s**；安装树 `START_BUDGET_MS = 90_000`（旧 `30_000` **不在**）、launcher `endlocal & exit /b %rc%`（无裸 `endlocal`）、`const USER_TINT = "#262226";`、`CARD_TINTS` 三个 hex 都在 | 通过 |
| TUI 冷启动附着（#846 真机） | 装完**第一次**启动就能在窗口内附着 | `ia-tui.cmd --check` **rc=0 / 39.5 s**；服务 `pid=23752 port=65036 uuid=6c47d4c2…`；带凭据 `GET /api/sessions` 200、不带 401 | 通过 |
| 桌面开窗 | 桌面启动后主窗口可见 | 窗口 **2.3 s** 可见，标题 `Agent Harness Inspector`；桌面 4 进程；服务 pid **仍为 23752 / 65036**（**附着到 TUI 起的那个服务**，不是另起） | 通过 |
| launcher 退出码（#847 真机） | 子进程失败必须被读成失败 | 让数据根不可用（`APPDATA` 指向一个文件）→ 子进程 `ENOTDIR … mkdir` 失败 → `ia-tui.cmd --check` **rc=1** | 通过 |

前置成立：**桌面能开窗、TUI 能冷启动附着**（且两者共存于同一服务、同一数据根）。

## 诚实注记

1. **#846 的 39.5 s 与桌面壳注释的「>60 s」**：`desktop/src/service-host.ts:88-90` 记录 D11 时首次运行
   「not within 60 s」，而今天同一台机上装完第一次启动是 **39.5 s**，与 TUI 超时文案里的「冷启动实测 36-42 s」
   一致。两者不能同时是「首次启动」的典型值，桌面壳那句取的是更坏的一次读数。已按 #848 的 R5 登记（改注释或去掉写死数字）。
2. **#847 探针是合成失败**：安装后的 TUI 没有别的确定性失败路径，故用「`APPDATA` 指向文件」逼出一次子进程失败。
   转发惯用法本身已在修后重审轮里于真 `cmd.exe` 上对 0/1/2/7/42/256/-1/-1073741510/65536 逐一实测。
3. **桌面窗口 2.3 s 是热读数**：此时安装已完成、服务已由 TUI 起好，桌面只做「探测 → 附着」。
   它不是冷启动读数，本文件不把它当作冷启动证据。
4. **本文件不宣布 #365 通过**：它只证明 Run A / Run B 的前置成立。票面要求的「两次独立完整通过」
   仍须在**新样本**上跑完（Run A 桌面 + 真实模型长任务；Run B TUI 冷启动 → 库提交后/ToolResult 前 kill Host →
   桌面重开 → 查询 DB/Ledger → reconcile → 人工确认 → 续跑 → 浏览器验证 + diff 审阅），且桌面/TUI 同时在场与
   各自单独退出两条规则各实测一次。

## 工作树证据清单

`D:\w21-work\evidence\pre0147\`：`pre0147-clear.txt`、`pre0147-install.txt`、`pre0147-verify.txt`、
`install-facts.json`、`pre0147-tui-cold.txt`、`tui-check.txt`、`tui-cold.json`、`pre0147-desktop-open.txt`、
`desktop-open.json`、`pre0147-exitcode.txt`、`exitcode-check.txt`、`exitcode.json`。
