# #1228 卸载器逐页 UI 双语点击测试

> 源自 #926 leg5。当年只测了静默卸载，卸载器的逐页 UI（中英文双语）从未被点击验证过。
> 本文件在腾讯云 Windows Server 2022 真机上，经 RDP 图形会话逐页走完卸载器 UI（中文 + 英文）。
>
> **本文件不宣布任何 Gate 通过、不 merge、不关单。**

---

## 0 执行器与环境

| 项 | 值 |
|---|---|
| 机器 | 腾讯云 Windows Server 2022 Datacenter，build 20348，x64 |
| 执行方式 | RDP 图形会话（xfreerdp3 经隧道 → 114.132.241.26:3389）+ Xvfb :99 + scrot/xdotool |
| 安装器 | `C:\ia-1251\desktop\dist-installer\Intelligence-Agent-Setup-0.1.0.exe`（213,389,835 B，含 #1251 修复） |
| 安装方式 | `/S` 静默安装（exit 0，约 185s） |
| 卸载器 | `%LOCALAPPDATA%\Programs\Intelligence Agent\Uninstall Intelligence Agent.exe` |

---

## 1 中文卸载器逐页测试

系统默认语言为简体中文，卸载器自动使用中文 UI。

### 第1页：欢迎页 ✅
- 标题：`Intelligence Agent 解除安装`
- 正文：`欢迎使用 Intelligence Agent 解除安装向导`
- `这个向导将全程指引你 Intelligence Agent 的解除安装`
- `在开始解除安装之前，请确认 Intelligence Agent 并未运行当中。`
- `单击「下一步(N)」继续。`
- 按钮：`下一步(N) >`、`取消(C)`
- **无乱码**，点击"下一步"正常进入第2页
- 截图：`1228-shots/zh-p1.png`

### 第2页：进度页 ✅
- 标题：`正在解除安装`
- 正文：`Intelligence Agent 正在解除安装，请等候。`
- 进度条正常推进
- **无乱码**
- 截图：`1228-shots/zh-p2.png`

### 第3页：完成页 ✅
- 标题：`正在完成 Intelligence Agent 解除安装向导`
- 正文：`Intelligence Agent 已从你的计算机解除安装。`
- `单击「完成」关闭这个向导。`
- 按钮：`上一步(B)`、`完成(F)`、`取消(C)`
- **无乱码**，点击"完成"正常关闭
- 截图：`1228-shots/zh-p3.png`

### 中文卸载后系统状态 ✅
- 安装目录 `%LOCALAPPDATA%\Programs\Intelligence Agent`：**不存在**
- 注册表 `HKCU\...\Uninstall\*` 中 Intelligence 相关项：**0 个**

---

## 2 英文卸载器逐页测试

### 语言切换方法
NSIS 卸载器的 `/LANG=1033` 参数**不生效**（实测：带引号路径 + `/LANG=1033` 仍显示中文）。
改用 `Set-WinUILanguageOverride -Language en-US` + 注销重登 RDP，桌面切为英文后卸载器自动显示英文 UI。
测试完成后已用 `Remove-WinUILanguageOverride` 恢复中文（需下次登录生效）。

### 第1页：欢迎页 ✅
- 标题：`Intelligence Agent Uninstall`
- 正文：`Welcome to Intelligence Agent Uninstall`
- `Setup will guide you through the uninstallation of Intelligence Agent.`
- `Before starting the uninstallation, make sure Intelligence Agent is not running.`
- `Click Next to continue.`
- 按钮：`Next >`、`Cancel`
- **无乱码**，点击 Next 正常进入第2页
- 截图：`1228-shots/en-p1.png`

### 第2页：进度页 ✅
- 标题：`Uninstalling`
- 正文：`Please wait while Intelligence Agent is being uninstalled.`
- 进度条正常推进
- **无乱码**
- 截图：`1228-shots/en-p2.png`

### 第3页：完成页 ✅
- 标题：`Completing Intelligence Agent Uninstall`
- 正文：`Intelligence Agent has been uninstalled from your computer.`
- `Click Finish to close Setup.`
- 按钮：`< Back`、`Finish`、`Cancel`
- **无乱码**，点击 Finish 正常关闭
- 截图：`1228-shots/en-p3.png`

### 英文卸载后系统状态 ✅
- 安装目录：**不存在**
- 用户数据 `%APPDATA%\intelligence-agent`：**保留**（符合 #361 设计：卸载永不删用户数据）

---

## 3 结论

| 验收项 | 结果 |
|---|---|
| 中文逐页点击（3页） | **通过**，无乱码、无崩溃 |
| 英文逐页点击（3页） | **通过**，无乱码、无崩溃 |
| 卸载后系统干净 | **通过**（程序文件、注册表项清除；用户数据保留符合设计） |

**未发现 UI 缺陷，无需另开票。**

## 4 诚实说明
- 英文测试需切换系统 UI 语言（`Set-WinUILanguageOverride`），因 NSIS 卸载器不支持 `/LANG` 参数覆盖。这是环境手段，不影响结论。
- 卸载器只有 3 页（欢迎 → 进度 → 完成），无额外的确认页或选项页。
- 截图为 RDP 会话截屏，含桌面背景；关键 UI 文字已在上文逐页转录。
