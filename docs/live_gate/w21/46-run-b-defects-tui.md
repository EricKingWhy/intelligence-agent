# Run B 附带缺陷：TUI 附着既有会话即崩（D12）与崩溃后端点不重解析（D13）

- 场景：Run B（#365）真实运行中的实测；安装件 sha256 `ce5fd54acf925bb6e17731e35990f18c52a0a90da57f834ad39960d0c6ad9880`。
- 会话：`d4d78a49-c5a4-429f-b1a3-db551447842d`（TUI 冷启动创建；桌面重开后的服务 pid 5740 / port 59380）。
- 证据文件（`D:\w21-work\evidence\run-b\`）：`tui2-screen-attached.txt`、`tui2-opened.json`、`tui2-console.txt`、`tui-console.txt`、`tui-screen-after-reopen.txt`。
- 本文件只做缺陷取证与影响记录；**#365 不修改产品代码**，修复另开票。

## D12：附着有用户消息的会话，TUI 启动即退出

### 现象

```
ia-tui failed: Error: Invalid color value: rgb(38, 34, 38)
```

`ia-tui.cmd --session d4d78a49-c5a4-429f-b1a3-db551447842d`（真实控制台 120x40，产品安装件原样调用）
在附着阶段直接失败退出：控制台进程 `cmd_alive=false`，屏幕只剩这一行错误（`tui2-screen-attached.txt`、`tui2-opened.json`）。

对照：同一安装件 `--session new` 冷启动正常（`tui-console.txt`：console allocated → `TUI launched cmd pid=24196 --session new` → node pids `[24652]`，
随后该 TUI 成功投递了任务消息并跟完 8 分钟真实运行）。

### 定位（仓库源码，不是 dist）

- `tui/src/views/chat.ts:37` — `const USER_TINT = "rgb(38, 34, 38)";`；`:42` — `new Box(1, 0, tintFn(parseColor(USER_TINT), theme.mode))`。
- `tui/src/theme.ts:47-51` — `CARD_TINTS = { running: "rgb(38, 40, 46)", success: "rgb(30, 40, 33)", error: "rgb(46, 30, 33)" }`；
  消费点 `tui/src/views/toolcard.ts:64` — `new Box(1, 0, tintFn(parseColor(tint), theme.mode))`。
- 契约方 `@earendil-works/pi-tui` `dist/colors.js:58-78` 的 `parseColor` 只接受 **number / `#rgb` / `#rrggbb` / `oklch(...)` / `okhsl(...)`**，
  其它一律 `throw new Error("Invalid color value: …")`；`rgb(...)` 从来不在集合内。
- `theme.ts` 的注释写着「oklch/rgb 字面量交给 parseColor」——注释与依赖的真实契约不符，这是这一家族的根因。

### 机制证明（依赖自带的同一函数，机械可复跑）

```
node -e "const {parseColor}=require('@earendil-works/pi-tui'); parseColor('rgb(38, 34, 38)')"
→ THROW Invalid color value: rgb(38, 34, 38)

node -e "const {parseColor}=require('@earendil-works/pi-tui'); parseColor('rgb(38, 40, 46)')"
→ THROW Invalid color value: rgb(38, 40, 46)
```

四个字面量全部命中；等值十六进制（RGB 分量逐位相同，视觉零变化）：

| 现字面量 | 等值 hex | 机械核对 |
| --- | --- | --- |
| `rgb(38, 34, 38)`（USER_TINT） | `#262226` | `identical: true` |
| `rgb(38, 40, 46)`（running） | `#26282e` | `identical: true` |
| `rgb(30, 40, 33)`（success） | `#1e2821` | `identical: true` |
| `rgb(46, 30, 33)`（error） | `#2e1e21` | `identical: true` |

### 影响

- **Run B 的 TUI 重附着腿被阻断**：真机 kill Host → 桌面重开服务后，TUI 无法重新附着到该会话（本会话已有用户消息）。
  操作者只能用 `POST /api/sessions/{id}/messages`（与 TUI `sendMessage` 同端点同 body）投递续跑指令，并在证据里如实标注。
- 用户视角：TUI 会话一旦有过用户消息，重启 TUI 就打不开该会话；崩溃恢复流程里「重开 TUI 继续」这条路径不可用。
- 测试缺口：`tui/test/` 内没有任何用例约束包内 tint 字面量可被 `parseColor` 解析（本次全仓 grep 无命中），
  所以这个错误一路进到安装件。修 D12 时应同时补一条这样的单测。

### 前/后对照（hermetic，安装件原树 vs 只改一个字面量的副本）

操作者在 `D:\w21-work\d12-probe\` 复制了安装件的 `resources\tui`，副本只把 `USER_TINT` 从 `rgb(38, 34, 38)` 换成 `#262226`，
用同一段探针（`probe.mjs`，对同一份 `{turns:[{role:'user',text:'hello',tools:[]}]}` 状态调用两边的 `conversationComponents()`）：

```
install (shipped)    THROWS -> Invalid color value: rgb(38, 34, 38)
patched copy         renders a user turn -> 1 component(s); no throw
```

输出留档：`D:\w21-work\evidence\run-b\d12-probe-output.txt`。安装件原树未被修改（只读复制）。

### 本轮未定位到的部分（据实记录）

第一个 TUI（`--session new`）在真实运行中活了 8 分钟，期间接收过 1 条 `user/message` 与 44 条 `tool/call` 事件却未崩；
而 `--session <id>` 附着立即崩。上面的 hermetic 对照说明**全量重建路径**（`conversationComponents`）在用户轮上必抛，
但**增量渲染路径与全量重建路径的差异本轮没有定位到具体行**——为什么 live 的 8 分钟没走到这个构造点，仍待修复票查清，
不要只改字面量。


## D13：Host 重启换端口后，已附着的 TUI 永久重连失败（待裁决）

- 现象：Host 被 kill、桌面以新端口重开服务后，仍活着的 TUI（node pid 24652）屏幕持续打印
  `stream reconnect: TypeError: fetch failed`（`tui-screen-after-reopen.txt`），永不恢复。
- 机制事实：`tui/dist/src/host.js:138` 只在自己的启动/派生路径上读 `.host-service.json` 并做健康检查；
  客户端 `api.js` 对会话内请求使用固定 base（`this.url(...)`），SSE 重连不会重新解析端点文件。
- 判定：这可能是设计意图（TUI 是某个服务实例的客户端，Host 由桌面托管），也可能是缺陷；**本轮不擅自定性**，
  交维护者裁决。仅记录它对 #365 的影响：Run B 的「桌面/TUI 同时在场」只能改用新开会话（无用户消息）的 TUI 实测，
  且必须在证据里注明该 TUI 附着的不是被恢复的那个会话。
