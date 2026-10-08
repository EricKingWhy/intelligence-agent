# B-1…B-5 收口报告（#365 [W-21]）

- 本文件按 #365 交接注释的 B-1…B-5 逐条给判定与证据指针，**不重复正文**（正文在
  `43`/`48`/`49`/`50`/`51` 各文件）。判据一律引用**实际读数**，不引用计划。
- 冻结安装件：`Intelligence-Agent-Setup-0.1.0.exe`，204,379,033 B，
  sha256 `01477e59779693299216f9595966106e71a7f7d92409856aa18729899b8591e6`（HEAD `28a382cc`，
  代码树 `d8dfa93c52049a5c2166a8d247aa407321d016f7`，Gate-0 6/6 见 `docs/gate/28a382cc….json`）。

## 逐条判定

| 项 | 要求（票面/交接原文） | 判定 | 证据 |
| --- | --- | --- | --- |
| B-1 | Windows 安装→启动→退出→更新→恢复 的 W-16 烟测通过 | **部分通过**：开发机上 A–F（旧件）+ 新件「静默安装 / TUI 冷启动附着 / 桌面开窗 / launcher 失败退出码 / 旧会话与模型配置可见 / 双语装卸 / 磁盘满回退逻辑」全部有读数；**干净 Windows x64 VM 复跑、运行中更新安全暂停、服务暂停失败、迁移中断、卸载器逐页 UI 双语 未执行** | `43-w16-smoke-artifact.md`、`48-pre-run-artifact-verification.md`、`51-w16-remaining-legs.md` |
| B-2 | Run A：桌面 + 真实模型长任务完整通过 | **通过 ×2** | `49-two-independent-passes.md` Run A 第 1/2 次（判定器 `overall=pass`，`missing=[]`） |
| B-3 | Run B：TUI 冷启动 → 库提交后/ToolResult 前 kill Host → 桌面重开 → 查 DB/Ledger → reconcile → 人工确认 → 续跑 → 浏览器验证 + diff 审阅 | **通过 ×2** | `49` 的 Run B 第 1/2 次（第 2 次补齐了裸 `POST /recover` 409 + `pending_decisions` 分支） |
| B-4a | 桌面/TUI **单独退出规则**各实测一次 | **通过** | 服务由 TUI 冷启动 ⇒ 桌面退出后服务存活（`47`）；服务由桌面派生 ⇒ 桌面退出服务随之消失、TUI 单独退出服务存活（`49` Run B 第 1 次） |
| B-4b | 桌面/TUI **同一 Task 状态与 Event seq 一致** | **不通过** | 一致的部分有读数（同一会话 701 事件 ↔ TUI `701 events` ↔ API `event_count=701`；`已完成` ↔ `─ completed`）；**同时在场时实测到不一致**：同一会话同一时刻桌面显示 2 轮已完成、TUI 显示「还没有会话内容」+ `idle`（`50-b4-desktop-tui-consistency.md`，缺陷 #853 / #854） |
| B-5 | 两次独立完整通过（Run A/B 均 pass 才可标 Gate passed） | **判据满足** | Run A 第 1/2 次、Run B 第 1/2 次，四次判定器 `overall=pass`，均在冻结件 `01477e59…` 上 |

## 结论（诚实版）

1. **B-5 满足**：票面要求的「用新样本跑两次独立完整通过」已完成——Run A 与 Run B 各两次，
   判定器四次 `pass`，真实浏览器读数与 DB 一致，恢复/reconcile 链齐全（`run/interrupted` →
   `operation/reconcile-required` → `operation/reconciled` → `run/completed`）。
2. **B-4 不通过**：本轮实测出两条 TUI 缺陷（#853 假 `send failed`；#854 空闲附着收不到后续 run 的
   直播帧），使「桌面/TUI 同一 Task 状态一致」在同时在场这一条上不成立。**#853 在 Run B 第 1 次的
   TUI 证据里就已经存在**（当时未记录），但不影响那两次 Run 的判定器读数（run 由服务端驱动并正常收尾）。
3. **#365 不关单**：B-4 的票面断言未成立，且 W-16 仍有本机不可行的腿（干净 VM 等）。
   按 #365「不在本票修产品代码」，两条缺陷只登记不修。
4. 本批**没有代码改动**（`git diff` 相对冻结树只有 `docs/`）：两条缺陷与 W-16 缺口都在本票范围外，
   因此不触发代码面门禁的重跑判据；但按 §14.10 仍在当前 tip 上跑完整门禁并留读数。

## 未决 / 待批准（逐项分开，按 §14.4）

1. `fix/w21-windows-gate-fixes` 分支 **push** + 开 PR（push 与 PR merge 是两次独立批准）。
2. PR #811 merge（既有待批准项）。
3. #815 第三路径裁决（现携带 D3-proxy 的 P2 读数）。
4. #848 的 R1–R7、#849 / #850 / #851 / #853 / #854 是否在关单前修。
5. 缺陷披露项（`49` / `50` 已逐条落账）：mimo 402 账号耗尽 + 换用 senseaudio/glm-5.3-flash；
   attempt 2 的 TASK.md 里操作者两条环境注记（绕开了 #850/#851）；MSYS `find` 的 PATH 产物 +
   bash 60 s 超时 → UNKNOWN → quiescence 阻塞 → 第二次 reconcile；restart 扫描回填
   `run/interrupted`（含恢复后仍 `resume_available: true` 的读数）；D5 node runtime（+~24 MB）；
   `file:<path>` 凭据脚枪；缺模型键时的 500；`crashhelper.exe` BEX 崩溃；TUI 不能选 cwd/workspace；
   R-042 CSV 行数差（40 vs 20，提示词未钉死）；`GET /api/sessions/{id}` 404 的脚本瑕疵。
