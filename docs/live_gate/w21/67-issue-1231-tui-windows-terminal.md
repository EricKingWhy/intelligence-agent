# #1231 MM-08 TUI Windows Terminal 实测证据

**Issue**: #1231（MM-08 TUI Windows Terminal 实测，非占位）
**日期**: 2026-10-17
**环境**: 腾讯云 Windows Server 2022（`Windows Server-jYPZ`，114.132.241.26），`C:\ia` = main tip `60e167fc`（4 文件 sha256 验证）
**分支**: `docs/1231-tui-terminal`

## 终端说明（重要）

Windows Terminal GUI 在该 Server 2022 上**无法运行**：`WindowsTerminal.exe` 启动即 BEX64 崩溃（`ucrtbase.dll` c0000409）。
微软官方二进制（1.25 与 1.21，解包直跑）同样崩溃——判定为该 Server 环境不兼容，非打包问题。
完整自签 sideload 链已走通（msixbundle → 框架 → 改 Publisher → 重打包 → 签名 → `Add-AppxPackage` 安装成功，
`wt.exe` 可解析），但 GUI 进程崩溃与打包无关。

**替代方案**：在 `C:\1231\wt-x64\OpenConsole.exe`（Windows Terminal 项目的 conpty console host，真 TTY）
里跑 TUI。实测可交互（`echo` 回显、`ia-tui --check` 通过）。证据中的"终端"均指该真 TTY。

## 服务启动

- 模型：`hy4-preview-f`（haoyu 网关，`https://web.haoyumew.xyz/v1`），key 来自
  `%APPDATA%\intelligence-agent\.env`（程序化读取，未打印）。
- 视觉探针（2026-10-17）：`hy4-preview-f` 正确读出测试图校验码 `1231-VT88`，确认支持 vision。
- 凭据通道：`AGENT_HARNESS_HOST_CREDENTIALS=file:%APPDATA%\intelligence-agent\host-credentials.json`
 （SSH 会话无 logon session，CredWrite 报 1312，改用文档化测试接缝）。
- **sandbox 后端**：Windows 部署默认 `docker`（fail-closed），本机无 Docker；
  建会话时经 `CreateSessionRequest.sandbox_backend` 显式选 `"local"`（文档化机制）。
  不指定则 `POST /api/sessions` 直接 409（W-19 诊断）。
- **视觉声明**：产品按"不猜"契约，`supports_vision` 须经 `AGENT_MODELS` 显式声明。
  已用探针验证过的能力声明：`[{"name":"hy4-preview-f","provider":"deepseek",
  "model_name":"hy4-preview-f","supports_vision":true}]`（`GET /api/models` 确认
  `supports_vision=True, metadata_source=agent_models`）。
- 服务跑在 RDP 会话内（detached），SSH 起的进程在 SSH 会话结束时被杀（已实测证实）。

## 场景一：换客户端 ✅ 通过

- 会话：`afc8dc62-e2c9-4619-9485-9ccad33381bf`
- TUI（客户端 A）：`--session <sid> @C:\1231\img-s1.png`，发送
  "Read the CODE after CODE: in the image. Reply with ONLY the code, nothing else."
- 模型回答：`1231-S1A7`（与图片校验码一致，视觉验证通过）
- 附件：`sha256:da4920a03a27391f6d92777d11d2a18c8f2aa6b180412f889051a0691ecafa82`
 （`image/png`, 13941 bytes, 720x300），与原文件 sha256 逐字符一致
- 换客户端：新 TUI 实例（客户端 B）`--session <sid>` 打开同一会话，
  看到同一张图（同一 sha256）与同一事件历史（9 事件）
- 验证：客户端 B 未触发新 run（事件数仍为 9）；`model/completed` 事件 content 为 `1231-S1A7`
- 截图：`shots-1231/s1-client-switch-second-client.png`

## 场景二：重启 ✅ 通过

- 会话：`5559a12b-e301-4e62-b59b-fa2fbc86165e`
- TUI 附图 `img-s2.png`，模型回答：`1231-S2B3`（视觉验证通过）
- 附件：`sha256:be2612002999d8271ff7a47f04d5f8f4a97d73645739491651b3fc9c17c3e53b`
 （13295 bytes），kill 前记录
- Kill：`taskkill /F /IM agent-harness.exe`（SSH 执行，PID 1440 被终止，真实 kill）；
  health 检查随后连接失败，确认服务已死
- 重启：RDP 窗口执行 `svc2.ps1`，新 PID 1104，health 200
- Resume：TUI `--session <sid>` 重开会话，历史完整加载
  - 事件数：kill 前 9 → 重启后 9（无丢失、无新增）
  - 附件引用：`sha256:be261200…` 不变
  - 附件字节：经 `/attachments/{ref}/content` 下载，sha256 与原文件一致
  - 图片仍可显示/解释：resume 后追问"Look at the image in our history and tell me
    the CODE again"，模型回答 `1231-S2B3`（正确）
- 截图：`shots-1231/s2-restart-resume-reread.png`
- 附带：旧 TUI 实例在 kill 后显示"Host 连接已断开（可能已重启），已停止重连；
  请重开 TUI"，行为符合预期

## 场景三：fork ❌ 失败（阻塞）

- 会话：`3fa84d95-392b-43a6-93f1-0628f02ed106`
- TUI 附图 `img-s3.png`，模型回答：`1231-S3C9`（视觉验证通过）
- 附件：`sha256:b97ed75c070d19af753a4d1a2acf975db1ce9bfbb5de3f98694fc45db8d7877e`
- 追加两轮文本对话（共 37 事件），user message seq 为 [2, 10]
- Fork：`POST /api/sessions/{id}/forks {"from_seq": 10}` → **500 Internal Server Error**
- 根因（服务端日志）：fork 建 child session 时走部署默认 sandbox 后端 `docker`，
  `SandboxUnavailableError: Docker Python SDK 未安装`
  - `ForkRequest` 只有 `from_seq` 字段，无 `sandbox_backend`
  - `SessionService.fork()` 与 `fork_session()` 均不接受 backend 参数，
    不继承父会话显式选择的 `"local"`
  - 在 Windows 无 Docker 环境下 fork 完全不可用（500）
- 结论：S3 核心 AC（child 看到 fork 边界之前的图）**未能测试**，如实记录为失败。
  不自行开票（待用户批准）。
- 截图：`shots-1231/s3-image-answer.png`（fork 前的图片问答成功部分）
- 备注：`from_seq=36`（非 user message）按设计返回 422（可选边界 [2, 10]），该行为正确

## 场景四：压缩 ❌ 失败（阻塞）

- 会话：`0d5c9ce5-6af5-4983-81da-ec36f0be5dfc`
- TUI 附图 `img-s4.png`，模型回答：`1231-S4D2`（视觉验证通过）
- 附件：`sha256:a9def10d324d44406f6695336b0e43f8e2cda92431869453feebd39da4803ace`
- 追加 6 轮文本对话（共 155 事件），触发压缩
- 压缩：`POST /api/sessions/{id}/context/compact`（3 次）+ TUI `/compact`（1 次），
  全部失败
- 失败事件：`context/compaction_failed`，`{"attempt": 1, "error_class": "timeout",
  "message": "TimeoutError", "duration_ms": 30159, "summary_model_id": "hy4-preview-f"}`
- 根因：`ContextCompactor` 的 `summary_timeout_seconds` 默认 30.0 秒；
  `hy4-preview-f` 经 haoyu 网关实测单轮回答需 45–60 秒，摘要请求必超时。
  4 次尝试全部 `TimeoutError`，无一次成功。
- 结论：压缩从未成功，S4 的 AC（摘要保留 artifact refs、无 base64、原事件未改写、
  旧图可取回、模型重答 `1231-S4D2`）**未能验证**，如实记录为失败。
  不自行开票（待用户批准）。
- 截图：`shots-1231/s4-image-answer.png`、`s4-compact-requested.png`、`s4-compact-failed.png`

## 其他如实记录的观察

1. **图片在追问中的可见性不稳定**：S3 第二轮追问"what color is the bar in the image?"
   模型回答"I can't see the image — image content isn't available to me"；
   而 S2 同类追问却正确答出 `1231-S2B3`。同一模型、同一会话结构下行为不一致，
   值得后续专项验证（本次不展开）。
2. **TUI 退出命令**：`/quit`（`/exit` 为未知命令）；`/help` 列出
   `/sessions /new /cancel /compact /budget /progress /resume /quit`。
3. **VM 中途重置**：Linux 云电脑在 S2 收尾时被平台重置，`/tmp/1231` 截图丢失；
   Windows 侧会话与服务不受影响，RDP 工具链重建后继续。S1/S2 的早期截图已用
   会话事件数据 + 重拍补齐。

## 判定汇总

| 场景 | 结果 | 说明 |
|------|------|------|
| S1 换客户端 | ✅ 通过 | 第二客户端看到同一图与同一历史，视觉校验码正确 |
| S2 重启 | ✅ 通过 | kill/重启后事件与附件字节无丢失，图仍可解释 |
| S3 fork | ❌ 失败 | fork 500（sandbox 后端不可继承），child 未建出 |
| S4 压缩 | ❌ 失败 | 摘要模型 30s 超时，压缩从未成功 |

## 日志与命令索引

- 服务启动：`C:\1231\svc2.ps1`（RDP 窗口，detached，`AGENT_MODELS` 含 vision 声明）
- API 辅助：`C:\1231\api.py`（health/create/events/upload/send/read/fork/compact/resume/models/sessions）
- TUI：`C:\node-v22.23.3-win-x64\node.exe C:\ia\tui\dist\src\index.js --session <sid> [@img]`
- 视觉探针：`hy4-preview-f` 读测试图得 `1231-VT88`（通过）
- 四图校验码：S1=`1231-S1A7` / S2=`1231-S2B3` / S3=`1231-S3C9` / S4=`1231-S4D2`
  （模型回答与图片校验码全部一致）
