# W-16（#361）剩余腿：本机实测读数（安装件 `01477e59…`）

- 场景：#365 的执行协议把 W-16 烟测挂在 Gate 前置上。`43-w16-smoke-artifact.md` 记的是
  **旧安装件 `ce5fd54a…`** 上的 A–F 腿（安装/启动/退出/更新/卸载，开发机），并如实列出未执行项。
  本文件补齐其中**本机可做**的四条，其余仍按未执行登记。
- 安装件：`Intelligence-Agent-Setup-0.1.0.exe`，sha256 `01477e59…`，HEAD `28a382cc`；
  运行中的服务 `pid=8472 port=57257`（桌面派生），数据根 `%APPDATA%\intelligence-agent\workspace`。

## 1. 旧会话与模型配置可见（实测通过）

桌面以 `--remote-debugging-port=9222` 起，CDP 读**渲染层**（脚本 `D:\w21-work\b4-w16-models.mjs`
与 `b4-w16-models2.mjs`）。

| 断言 | 读数 | 判定 |
| --- | --- | --- |
| 旧会话（本次安装**之前**创建的）仍可见 | 会话栏 16 条，`title` 形如 `2ea284fa-… · 1131 事件`、`cbebff65-… · 5725 事件`；相对时间 `6 小时前` / `7 小时前` / `9 小时前`（早于本次安装） | 通过 |
| 会话内容可读 | 点选 `896132bb…` 后会话区渲染 `已完成` + 两轮完整对话（`Reply with exactly: OK…` → `OK`、`…OK3…` → `OK3`） | 通过 |
| 模型配置可见（选择器） | 真实鼠标事件点开 `[aria-label="模型选择"]` → `aria-expanded=true`、5 个 `.picker-item`，渲染文本：`用哪个模型？` / `默认链 系统自动选` / `senseaudio 1 个模型` / `qwen 2 个模型` / `shrimp 1 个模型` / `管理模型 自定义供应商 · API Key · 测试连接` | 通过 |
| 模型配置生效（每轮标注） | 两轮 AI 消息的 `.model-tag` 均为 `glm-5.3-flash`（与 `run/started.data.model` 一致） | 通过 |

证据：`D:\w21-work\evidence\b4\`：`b4-w16-sessions.txt`、`b4-w16-models.json`、`b4-w16-models2.json`、
`b4-w16-model-picker.png`。

**诚实注记**：探针用合成事件点开该浮层后，`Escape` 与「点浮层外」都没能让它关闭（两次读数都是 5 个
`.picker-item`）。**这不能算缺陷**：合成事件未必触发真实的指针/焦点路径，且本票不修产品代码。
只作为「真机 UI 键盘可达性未验」的缺口登记，供后续真机验收车道复核。

## 2. 一次真实短任务（由 Run A / Run B 覆盖）

- Run A（桌面 + 真实模型，完整修复任务）与 Run B（TUI 冷启动 → kill → 桌面重开 → reconcile → 续跑）
  两次独立完整通过，读数见 `49-two-independent-passes.md`；判定器两次 `overall = pass`。
- B-4 期间另跑了一次**真实短任务**（会话 `896132bb…`，`Reply with exactly: OK…` → `run/completed`
  `final_text=OK`，`usage_total.total_tokens=4812`），用于两端状态对照，见
  `50-b4-desktop-tui-consistency.md`。

## 3. 卸载器 / 安装器双语（实测通过，口径见注记）

- 命令（`desktop/`）：`node scripts/test-windows-installer.mjs --uninstall-only`
- 读数（日志 `D:\w21-work\evidence\w16-installer-lang.log`，`rc=0`）：

| 语言 | 构建 | 烟测读数 |
| --- | --- | --- |
| `en_US` | `installer-test.exe` + `installer-test.__uninstaller.exe` 编译并签名成功 | `UNINSTALL_OK userDataPreserved=true` |
| `zh_CN` | 同上（另一份构建，`installerLanguages: ['zh_CN']`） | `UNINSTALL_OK userDataPreserved=true` |

- 脚本末尾：`Installer test artifacts: …\run-T9MlKO (succeeded=true)`。
- **口径注记**：该烟测每语言各构建一次，然后走**静默**安装 + 静默卸载（`windows-uninstall-smoke.ps1`
  用 `Start-Process … '/S'`），断言「安装目录与注册表项消失、用户数据标记文件保留」。
  所以它证明的是「**两种语言的构建都能出包并正确装/卸、且卸载不删用户数据**」，
  **不是**逐页点击 NSIS 界面的双语文案检查。双语字符串本身在
  `desktop/installer/installer.nsh:64-75`（`!ifdef LANG_ENGLISH` / `!ifdef LANG_SIMPCHINESE`
  各四条 `LangString`），构建期由 makensis 校验（语言未加载即 `warning 7025` = 致命，
  这正是 #831 在 en_US 卸载器构建上踩过的坑）。
- 结论：**票面点名的「卸载器双语」在构建 + 装卸路径上已实测通过**；逐页 UI 文案点击检查仍属未执行。

## 4. 磁盘满回退（**仅逻辑 / 单测验证，不真填磁盘**）

用户 2026-10-08 指令：「磁盘满回退你别真把磁盘搞满了啊，你这部分代码逻辑实现就行了」。
故本腿**不构造真实 ENOSPC**，只按代码路径 + 定向单测核对行为。

- 代码路径：
  - `src/agent_harness/web/domain_errors.py:362-379` `storage_http_status`：
    `OSError.errno ∈ {EFBIG, ENOSPC}` → **503**；`sqlite3.Error` 的 primary code ∈
    `{SQLITE_FULL, SQLITE_IOERR}` → **503**；其它 `OperationalError`（坏路径等）→ 仍 500。
  - `:382-397` `storage_http_error`：容量类错误统一回固定 detail `Persistent storage is unavailable`
    （**不回显 OS 路径**，避免内部信息外泄）。
  - `src/agent_harness/web/app.py:1827` 起的全局处理器接入该映射（`#753`）。
  - 会话/审批侧：`tests/session/test_fork_workspace_copy.py` 断言 ENOSPC **向上抛且文案明确、不静默吞**，
    且中断的复制**不留半截 workspace**；`tests/session/test_approval_timeout.py` 断言
    「结清写入失败时取消语义不被顶掉」「写入失败不摘 pending、不结清」——即**失败时不丢事实**。
- 定向单测（本机实跑，`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q -p no:randomly`）：

| 命令 | 读数 |
| --- | --- |
| `tests/web/test_disk_full_507.py tests/session/test_fork_workspace_copy.py` | **9 passed / 1.41s**（含 `test_enospc_during_session_create_returns_503`、`test_efbig_…`、`test_unrecognized_oserror_still_500`、`test_enospc_fails_loudly_with_clear_message`、`test_interrupted_copy_leaves_no_partial_workspace`、`test_cross_volume_fallback_failure_cleans_up`） |
| `tests/session/test_approval_timeout.py -k "resolve_write_failure or cancel_survives" tests/web/test_domain_error_mapping.py tests/web/test_error_contract.py -k "resolve_write_failure or cancel_survives or storage or enospc"` | **20 passed / 4.09s** |

- 判定：**代码逻辑与失败语义成立**（503 + 明确文案 + 不泄路径 + 失败不丢事实 + 不留半截产物）。
  **未做**：真实磁盘写满的真机验收（按用户指令不做）。

## 5. 仍未执行（如实登记，本机不可行或未做）

| 腿 | 状态 | 原因 |
| --- | --- | --- |
| 干净 Windows x64 VM 复跑 | **未执行** | 本机是开发机（有 `.env`、既有用户目录、既有服务）；无干净 VM 可用 |
| 运行中更新安全暂停（列在途 Task/客户端 → 安全暂停 → Ledger 结清，失败则中止替换） | **未执行** | 需在**在途任务**期间就地重装并观察替换中止/结清；本轮所有重装都发生在无在途任务时（`43` 已登记同一缺口） |
| 服务暂停失败 | **未执行** | 同上，需构造在途 + 更新失败的组合 |
| 迁移中断（数据根迁移中途中断） | **未执行** | 需构造迁移中的中断点；本轮未做 |
| 卸载器逐页 UI 双语点击 | **未执行** | 见 §3 注记：烟测走静默装/卸 |

以上缺口不由本文件补齐，也不影响 `49` 里两次 Run 的读数（那两条走的是产品自己的入口）。
本文件**不宣布 #365 Gate 通过**。
