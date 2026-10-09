# W-21 D8–D11（#834–#837）修复：证据与结论

父票 #365 [W-21] · 缺陷 D8/D9/D10/D11（冻结树读数见 `40-windows-gate-result.md`；D8 的原始复现见 #834）。
本文件记录**修复后**在真实安装件上的读数，供 #365 的 W-16 烟测、Run A/Run B 与 B-4 引用；
**不宣布 #365 Gate 通过**。

分支 `fix/w21-windows-gate-fixes`，提交：

| 提交 | 内容 |
| --- | --- |
| `0aca596b` | D8 #834：`awaitServiceReady` 每轮重读端点文件、单次健康探测 1.5 s 可重试、失败诊断带上子进程 stdout；D11 #837：端点预算 20 s → 90 s、子进程退出/启动错误即失败 |
| `80557a6b` | D9 #835：`assertStagedProductMatchesSource`（staging 与 checkout 逐文件 sha256）+ `productCodeSha256` 入 `installer-build.json`；D10 #836：`assertDesktopBuildFresh`（`desktop/dist/src/main.js` 旧于 `desktop/src` 即拒绝打包） |

产物：`desktop/dist-installer/Intelligence-Agent-Setup-0.1.0.exe`，204,375,375 B，
sha256 `ce5fd54acf925bb6e17731e35990f18c52a0a90da57f834ad39960d0c6ad9880`；
`installer-build.json`：`productCodeSha256=1d4688ad…`、`lockfileSha256=7f670bd5…`、
`nodeLockfileSha256=63b791d3…`、`builtAt=2026-10-07T17:15:19.592Z`；构建时 HEAD `80557a6b`。

## 关键测量：D11 的判据（同一台机器，安装件内的解释器）

| 条件 | 从 spawn 到端点文件写出 |
| --- | --- |
| 机器空闲、文件缓存已热 | **8.7 s** |
| 刚装完、立刻启动 | **30.1 s / 33.6 s**（两次） |
| 装完后的第一次运行 | **60 s 内未就绪** |

外壳原来的预算 20 s（`awaitServiceReady` 的 `portTimeoutMs`）落在装机窗口之下 ⇒ 外壳杀掉正在健康启动的
子进程、端点文件保持陈旧（D8 描述的「自锁」状态由此产生）。成本几乎全在产品侧的导入链：
`agent_harness.cli` ≈ 5.5 s（其中 `agent_harness.assembly` 3.3 s）、`import agent_harness.web.app` ≈ 8.4 s、
`fastapi+uvicorn` ≈ 1.9 s、解释器本身 0.1 s。

上游对照：DSH 的就绪由子进程消息界定、**没有启动时钟**（`host-process.ts:205-212,310-315`）；
PI-Desktop 给本机宿主的是 `DEFAULT_RPC_TIMEOUT_MS=130_000` / `SERVICE_HEALTHY_MS=60_000`；
同仓库 TUI 给同一个子进程的是 **30 s**（`tui/src/host.ts:190`）。外壳取 90 s（覆盖实测最坏值并留余量），
`service-attach.ts` 的 3 s **附着**预算不动（那是探别人起的服务，探不到就自己起）。

## 逐票 AC 对照

### D8 #834

- AC1（陈旧记录 → 附着到子进程改写的端点、不再弹失败对话框）：**满足**。单测
  `awaitServiceReady over a stale endpoint record` 覆盖「首轮陈旧 + 健康失败 → 重读 → 新端点」；
  实机两次复跑均无失败对话框。**注**：票面写的「20 s 内」在本机对服务自身启动时间不成立
  （实测 30–60 s），该时限由 #837 调整为就绪预算；机制面（不再读到即锁定）与本条一致。
- AC2（持续失败仍在整体截止时间内报错、含最近一次探测端口）：单测
  `keeps the pinned timeout message and reports the last attempt`。
- AC3（stdout 进失败诊断）：单测 `reports what the child printed on stdout, not only stderr`
  （断言信息含 CLI 的「已附着，本进程退出」话术）。
- AC4（`npm test` + `tsc --noEmit`）：**205 tests pass / 0 fail（exit 0）**；`npm run check` exit 0
  （`D:/w21-work/evidence/d9-d11-npm-test.log`）。
- AC5（重打包后在真实安装件上复跑开窗）：见下文两次复跑。

### D9 #835

- AC1（四种单测形态）：`desktop/test/installer-runtime-product.test.mjs`（一致通过 / 内容漂移拒绝 /
  缺件拒绝 / `__pycache__` 不影响判定）。
- AC2（真实仓库一致性 + 故意改一个字符即拒绝）：`D:/w21-work/evidence/d9-ac2.txt` ——
  249 个 `.py` 逐文件一致 → 通过；在 staging 的 `host_service.py` 末尾追加一行注释 → **拒绝**，
  报 `staged product differs from the checkout: host_service.py, 1 file(s)` 与刷新指令；
  恢复后（24888 B、逐字节相同）再次通过。
- AC3（安装件的 `agent_harness` 含 D5 的 `_bundled_web_dist`）：用安装件的
  `resources\python\python.exe` 实测 —— 模块路径 `…\resources\python\Lib\site-packages\agent_harness\web\app.py`，
  `_bundled_web_dist` 2 处（定义 + 调用）。
- AC4（TUI 冷启动的服务 `GET /` 返回 200 `index.html`）：`D:/w21-work/evidence/d9-ac4.txt` ——
  由 `ia-tui.cmd --check` 冷启动的服务（pid 7920 / port 58469，**未传 `WEB_DIST_DIR`**）：
  带凭据 `GET /` → **HTTP 200**，正文 `<!doctype html> … id="root"`；`GET /api/sessions` 带凭据 200 /
  不带 401。桌面附着同一服务后窗口渲染真实 UI（D11 复跑 2 的 B 腿截图）。
- AC5（`productCodeSha256` 与 HEAD 同记）：`installer-build.json:productCodeSha256=1d4688ad…`，
  HEAD `80557a6b`，见本文件头。

### D10 #836

- AC1（注入 mtime 的单测：陈旧拒绝 / 新鲜通过）：`desktop/test/installer-tui.test.mjs`。
- AC2（真实树负向 + 正向）：`D:/w21-work/evidence/d10-ac2.txt` 与 `d10-ac2-build.log` ——
  改动 `desktop/src/main.ts`（touch，不重编译）后跑 `node scripts/build-windows-installer.mjs` →
  **exit 1**：`stale build: …\desktop\dist\src\main.js is older than its sources — rebuild before packing`；
  `npm run build` 后同一条命令继续通过守卫（`installer inputs OK` / `staged product matches the checkout`）
  并进入 electron-builder 完成打包。**注**：陈旧分支的文案复用 `assertFreshBuild` 的既有话术
  （`rebuild before packing`），字面串 `npm run build` 出现在缺件分支
  （`desktop shell build missing: … — run npm run build in desktop/`）。
- AC3（安装件的 `resources/app/dist/src/service-host.js` 含 D8 修复标记）：`last attempt`、
  `isProtocolIncompatible`、`90_000` 三个标记在安装件内各命中 1 处。
- AC4（`npm test` + `npm run check`）：同 D8 AC4（205 pass / exit 0 / tsc exit 0）。

### D11 #837

- AC1（子进程就绪前退出 → 立即失败且带退出码与输出）：单测 `readiness bounded by the child, not by the clock`
  —— `exit-before-ready` 用例在 **2 次端点读取**内失败，报 `the service process exited before it was ready
  (exit code 3)`，耗时 < 90 s；`spawn error` 用例报 `spawn ENOENT`。
- AC2（子进程存活但端点不出现 → 到预算才失败）：同组第三例（`elapsed >= 90_000`、端点读取 > 100 次）。
- AC3（D8 场景仍在预算内成功返回）：D8 单测组保持绿。
- AC4（预算常量单一来源 + 注释写实测数字）：`DEFAULT_START_BUDGET_MS = 90_000`，注释含
  8.7 s / 30.1 s / 33.6 s / 60 s 的实测依据与 TUI 的 30 s 对照。
- AC5：同 D8 AC4。
- AC6（重装后第一次启动开窗）：见下文两次复跑。

## 两次实机复跑（同一安装件 sha256 `ce5fd54a…`）

| 读数 | 复跑 1（`evidence/d11/`） | 复跑 2（`evidence/d11b/`） |
| --- | --- | --- |
| A 腿：装好后第一次启动，**陈旧端点记录在场**（pid 999999 / port 64937） | 子进程 pid 12100，t≈3–4 s spawn | 子进程 pid 38652，t=3.4 s spawn |
| 端点被**子进程自己改写**的时刻 | **t=56.8 s**（port 50922） | **t=44.4 s**（port 50935） |
| 主窗口可见 | t=59.2 s | t=49.6 s |
| 失败对话框 | **never** | **never** |
| B 腿：TUI 冷启动后桌面附着 | `ia-tui.cmd --check` 起服务 pid 7968 / port 54511 → 桌面附着、无对话框、窗口可见、端点不变 | pid 7920 / port 58469 → 桌面附着、无对话框、窗口可见、端点不变（`failureDialog=False mainWindowVisible=True endpointStill=pid=7920 port=58469`） |
| 窗口内容 | `evidence/d11/A-window-foreground.png`（渲染出产品 UI） | `evidence/d11b/B-tui-attached-window.png`（渲染出产品 UI） |

两次复跑都覆盖了 #834 的「自锁」路径与 #837 的「装机窗口」路径：外壳等到了子进程改写端点、
没有杀子进程，装完第一次启动即可开窗，A 腿不再需要手工删端点文件。

## 诚实注记

1. **D8 AC1 的「20 s」**（上文已注）在本机对服务自身启动时间不成立，已由 #837 以 90 s 就绪预算替代；
   两票在同一次提交 `0aca596b`。
2. **D10 AC2 的字面串**：陈旧分支复用共享话术 `rebuild before packing`，未逐字含 `npm run build`
   （该字面串出现在缺件分支）。行为判据（拒绝 + 点名入口 + 给出动作）满足。
3. **不带凭据的 `GET /` 返回 401** 属服务端 fail-closed 设计（`auth_required` 恒真），不是缺陷；
   渲染层证明取「带凭据 200 + 桌面窗口截图」两条读数。
4. 本文件只覆盖 D8–D11。W-16 烟测（install/start/exit/update/恢复）、Run A/Run B、B-5 与 #365 关单
   均未执行，**不构成本批的 Gate 结论**。
