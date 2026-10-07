# #842 修复后的 Run B TUI 腿重跑（安装件 46e7a9e2…）

- 场景：#365 Run B 的 TUI 重附着 / 桌面+TUI 共存 / 两条退出规则——在旧安装件（`ce5fd54a…`）上被 D12 阻断，
  只能在 #842 修复并重打包后补跑。本文件是这次补跑的证据件。
- 安装件：`Intelligence-Agent-Setup-0.1.0.exe`，204,377,641 B，
  sha256 `46e7a9e2c22bd2ea16f9e82e1cbfe9c3f22f0f5a626d72bd979e4ef5eb7e2fd6`（HEAD `14898f1a`，含 #842 修复）。
- 会话：`d4d78a49-c5a4-429f-b1a3-db551447842d`（**被恢复的那个会话本身**，不是新开会话；4934 条事件、2 条 `user/message`）。
- 工作树证据（`D:\w21-work\evidence\run-b-842\`）：`install-facts.json`、`tui842-console.txt`、`tui842-screen-attached.txt`、
  `screen-verdict.json`、`tui842-live.json`、`coexist.json`、`tui-exit.json`、`desktop-exit.json`、
  `cold-timing.json`、`first-start-after-reinstall.txt`、`cold-start-pyc-experiment.txt`、`d12-installed-replay.txt`。
- 驱动：`D:\w21-work\tui-legs-842.py`（stage 各自落账；控制台驱动沿用 `tui-console.ps1`，本轮给它加了整屏缓冲 DUMP 动词）。

## 结论（先给读数）

| 腿 | 要求 | 读数 | 判定 |
| --- | --- | --- | --- |
| 静默安装 | 同一安装件装上且带 #842 修复 | rc=0，127.3 s；安装树 `const USER_TINT = "#262226";`、`CARD_TINTS` 三个 hex 都在 | 通过 |
| TUI 附着**已有用户消息**的会话 | 能附着并正常渲染（#842 AC） | 附着后 `cmd_alive=True`、无 `ia-tui failed`、无 `stream reconnect` note，屏幕渲染出工具卡与计划更新（`tui842-screen-attached.txt`） | 通过 |
| 桌面 + TUI 同时在场（**同一被恢复会话**） | 一个服务、两个客户端、同一会话 | 服务 pid 25264 / port 62511；桌面 4 进程 `[10440,15464,22216,24880]`；TUI node `[10152]`；`GET /api/sessions` 200，会话在列表里 | 通过 |
| TUI 单独退出 | 服务与桌面存活 | node 退出后 `[]`；服务 pid 不变（25264→25264）；桌面 pid 不变 | 通过 |
| 桌面单独退出 | 记录服务归属行为 | 桌面 `[…]→[]`；服务 pid 25264 **存活** | 通过（与 Run B 不同，见下） |
| TUI **冷启动**服务并附着 | 装完即用的第一条路径 | **失败**：服务 36 s 才就绪，TUI 30 s 窗口到期退出（`本机服务 30s 内未就绪`）→ 见 D14 / #846 | 不通过 |
| `ia-tui.cmd` 退出码 | 失败要能被脚本看见 | `ia-tui --check` 打印失败行却返回 0 → 见 D14b / #847 | 不通过 |

## 附着腿为什么就是 #842 的验收

`app.ts` 的附着路径是 `rebuildFromHistory()` → `applyEvent` 全部事件 → `renderAll()` → **对每一轮**调 `turnComponents`。
该会话有 4 轮、其中 2 轮是用户轮，所以只要 `USER_TINT` 还是 `rgb(...)`，进程会在构造第一轮用户消息的 Box 时立刻
`ia-tui failed: Invalid color value: …` 退出（旧安装件实测如此）。本轮附着**没有**该错误行、进程存活、屏幕有真实内容，
即用户轮 Box 在安装件上装配成功。

机械补充（同一安装件原树，只读）：

```
node d12-probe/live-path.mjs "<install>/resources/tui" <run-b/events-final.json>
→ live 投影跑完全部 4934 帧无抛出（末轮 role=assistant，共 4 轮）      # d12-installed-replay.txt
```

单测：`tui/test/tints.test.ts` 旧字面量 3 条红 → 修复后 **54 tests / 0 fail**，`tsc --noEmit` exit 0（提交 `14898f1a`）。

## 两条退出规则的归属差异（本轮新读数）

- 本轮服务是 **TUI 冷启动**的（父进程是 TUI 的 cmd）：TUI 退出、桌面退出都不影响它（`tui-exit.json`、`desktop-exit.json` 都是同 pid 存活）。
- Run B 那次服务是**桌面**拉起的：桌面退出后服务随之消失（`45-run-b-kill-reconcile.md` 的 `service_after: null`）。
- 结论：服务存活与否取决于**谁启动的它**，不取决于谁退出。两次读数合起来才是完整事实，本轮不把它当缺陷。

## D14 / D14b（新缺陷，已开票）

装完安装件后的**第一次**启动，服务就绪耗时 **35.7 s（`--check`）/ 36–42 s（真实启动）**，超过
`tui/src/host.ts:190` 的 `START_BUDGET_MS = 30_000`，TUI 硬失败退出并留下孤儿服务；热态同一条路径 6.6–6.9 s 成功。
**不是 pyc 首次编译**：把 `agent_harness` 全部 249 个 `.pyc` 删掉再冷启动仍只花 6.9 s。真实原因本轮未定位（嫌疑：安装刚写完
500 MB 资源树后首次执行其中二进制触发 Defender 实时扫描/IO 竞争，`RealTimeProtectionEnabled=True`，未做对照实验）。
明细见 #846；`ia-tui.cmd` 吞退出码见 #847。

## 操作者侧如实记录（不影响上面的判定，但必须写明）

1. **误注入**：`tui-exit` 第一次尝试时把 `END` 当成 TEXT（应写 `/quit`）注入 TUI 输入，TUI 把它当聊天消息投递；
   该服务是操作者无模型凭据启的，服务端返回 **HTTP 500**，屏幕记 `send failed: ApiError: HTTP 500: Internal Server Error`，
   **会话没有任何副作用**（事后核对仍是 4934 条事件、max seq 4933，无新 `user/message`）。
   修法已落到驱动里：退出走 TUI 自己的 `/quit`（`tui-exit.json` 的 `tui_quit_command`），驱动 `END` 只关控制台。
2. **控制台无回滚缓冲**：本环境控制台缓冲区就是窗口大小（`buffer=120x40`），用户消息块被顶出可视区，屏幕读数里看不到它；
   该渲染路径由上面「附着腿 + 安装件原树 4934 帧回放 + 单测」三处覆盖，不靠屏幕文本。
3. **截图缺失**：控制台窗口 `GetWindowRect` 返回 `0,0,0,0`（Run B 同样），`SHOT` 拿不到 PNG；证据是屏幕文本，不是截图。
4. `install-facts.json` 里 `chat_js_has_rgb: true` 是**注释文本**造成的假阳性（注释里写了 `rgb(38, 34, 38)` 作为对照），
   权威读数是 `user_tint_line: const USER_TINT = "#262226";`。

## 本文件覆盖与不覆盖

- 覆盖：Run B 的 TUI 腿（重附着 / 共存 / 两条退出规则）在**修复后的同一安装件**上的读数；#842 的真机验收。
- 不覆盖：D13（#843，端点不重解析）仍未裁决；D14/D14b 未修；**Run B 的两次独立完整通过尚未在 46e7a9e2… 上重跑**
  （旧读数属于 `ce5fd54a…`），所以本文件不构成 #365 的通过结论。
