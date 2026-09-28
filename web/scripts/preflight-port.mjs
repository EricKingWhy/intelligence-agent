#!/usr/bin/env node
/**
 * e2e 门禁前置检查：dev server 端口是否已被**别的 vite** 占着（#209）。
 *
 * ## 为什么需要它
 *
 * `playwright.config.ts` 的 `webServer` 过去是 `reuseExistingServer: !CI`
 * （本地恒 true）+ `vite` 默认行为（端口被占就**自动换一个**）。于是本地跑门禁时：
 *
 * - 5173 上可能挂着**另一个 clone** 的 vite（实测踩到：spec 来自 main、代码来自
 *   `D:\intelligence-agent-frontend`）⇒ 用本仓的 spec 测另一个仓的代码，
 *   得到 **18 failed** 的假红，排查花了约 20 分钟（先怀疑 spec/时序，最后才查端口）；
 * - 反过来也成立：假绿同样可能（spec 与代码错配但恰好都通过）。
 *
 * 门禁数字是关单与集成的依据（`AGENTS.md` §14.10），"绿""红"都必须指向**本仓这次的
 * 代码**。所以宁可**响亮地失败**，也不静默复用。
 *
 * ## 它做什么
 *
 * 只做一件事：如果端口被占用，把它**是谁**打印出来并以 1 退出（于是 `npm run dev:e2e`
 * 的 `&&` 短路，vite 根本不会起来，Playwright 拿到的是这段可读的报错而不是
 * 一句 `Process from config.webServer exited early`）。
 *
 * 配合配置侧的两条硬约束一起才成立：
 * 1. `reuseExistingServer: false`（本地也强制自己起服务）；
 * 2. `vite --strictPort`（被占时拒绝启动，而不是换端口——换端口会让 Playwright
 *    等一个永远不会有人监听的地址）。
 *
 * ## 用法
 *
 * ```bash
 * node scripts/preflight-port.mjs        # 默认 5173（门禁走这条）
 * node scripts/preflight-port.mjs 5174   # 手工排查别的端口
 * ```
 *
 * ⚠ 默认端口必须与 `playwright.config.ts` 的 `PORT` 一致（两处都在 `web/` 下；
 * 与 `index.css` 的主题 token 同理——没有共享机制时，双份 + 注释互指是成本最低
 * 的可维护形态）。
 *
 * ## 两个入口（都在真实门禁链路上，无需人工步骤）
 *
 * 1. `playwright.config.ts` **配置加载期**调用 `assertPortFree(PORT)`——这是必须的
 *    位置：Playwright 在起 webServer **之前**就先探测 url，端口被占时它自己直接
 *    报一句 `http://localhost:5173 is already used …` 并且**根本不执行**我们的
 *    webServer 命令（实测）。那句通用错误说不出占用者是谁，正是本票要消灭的东西。
 * 2. `npm run dev:e2e`（webServer.command）也带一次预检——给"绕过配置守卫"的场合
 *    留一道兜底，`--strictPort` 与之配合。
 */

import { execFileSync, spawnSync } from 'node:child_process';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const DEFAULT_PORT = 5173;

/** 监听该端口的 PID 列表（跨平台：Windows 用 netstat，其余用 lsof）。
 *
 * ⚠ Windows **不要**加 `-p TCP`：那个过滤只放 IPv4（IPv6 要 `TCPv6`），而 vite 默认
 * 绑的是 `[::1]` —— 加了就等于对真实的 vite 监听**视而不见**（本脚本第一版正是
 * 这么漏报的：日志里明明写着 `Port 5173 is already in use`，预检却说"空闲"）。
 * 所以全量取，自己在协议列上过滤（`TCP` / `TCPv6`）。
 */
function listeningPids(port) {
  try {
    if (process.platform === 'win32') {
      const out = execFileSync('netstat', ['-ano'], { encoding: 'utf8' });
      const pids = new Set();
      for (const line of out.split(/\r?\n/)) {
        // `TCP   [::1]:5173   [::]:0   LISTENING   13776`（IPv6 是 TCPv6）
        const cols = line.trim().split(/\s+/);
        if ((cols[0] !== 'TCP' && cols[0] !== 'TCPv6') || cols[3] !== 'LISTENING') continue;
        const local = cols[1] ?? '';
        if (Number(local.slice(local.lastIndexOf(':') + 1)) === port) pids.add(cols[4]);
      }
      return [...pids];
    }
    // POSIX：**必须**用 spawnSync 而不是 execFileSync —— `lsof` 在"没有匹配项"
    // （也就是端口空闲，本脚本最该判绿的情况）时返回 **exit 1 且 stdout 为空**，
    // execFileSync 会把这个当成错误抛出去，于是"空闲"被误判成"探测不了"，
    // 每次门禁都在纯净环境上假红。用退出码区分三件事：
    //   0 → stdout 是 PID 列表；1 + 两个流都空 → 确实没人监听（→ 空闲）；
    //   其余（ENOENT、权限、stderr 有话说）→ 探测不了（→ 不猜，让门禁停）。
    const r = spawnSync('lsof', ['-nP', `-iTCP:${port}`, '-sTCP:LISTEN', '-t'], {
      encoding: 'utf8',
    });
    if (r.error) return null;
    if (r.status === 0) {
      return (r.stdout ?? '').split('\n').map((s) => s.trim()).filter(Boolean);
    }
    if (r.status === 1 && !(r.stdout ?? '').trim() && !(r.stderr ?? '').trim()) return [];
    return null;
  } catch {
    // 查不动（netstat 不可用等）：**不**当成"空闲"——那正好会退回静默复用。
    return null;
  }
}

/** 某个 PID 的命令行（Windows 走 PowerShell；wmic 在 Win11 已移除）。 */
function commandLineOf(pid) {
  try {
    if (process.platform === 'win32') {
      return execFileSync(
        'powershell',
        [
          '-NoProfile', '-NonInteractive', '-Command',
          `(Get-CimInstance Win32_Process -Filter 'ProcessId=${pid}').CommandLine`,
        ],
        { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
      ).trim();
    }
    return execFileSync('ps', ['-o', 'command=', '-p', pid], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    return '(命令行读取失败)';
  }
}

/** 从命令行里认出**哪个 clone / 仓库根**（vite 的命令行含 `<clone>\web\node_modules\...`）。
 *
 * 输出形如 `D:\intelligence-agent-frontend` —— 这一行是给人看的**判据**：
 * 它不是本仓（`D:\intelligence-agent-backend`）就说明门禁会测到别人的代码。
 * 取不到（自研脚本、被裁剪的命令行）就如实不显示，不编。
 *
 * ⚠ 三个 clone 是**独立仓库**（`AGENTS.md` §13.1），不是同一仓库的 worktree；
 * 这里认的是"哪个 checkout 的 vite 占着端口"，所以措辞一律用 clone / 仓库根。
 */
function cloneRootOf(commandLine) {
  const win = commandLine.match(/([A-Za-z]:\\[^"]*?)[\\/]web[\\/]node_modules/i);
  if (win) return win[1];
  const posix = commandLine.match(/(\/[^\s"]*?)\/web\/node_modules/);
  return posix ? posix[1] : null;
}

/** 端口被占时的那段人话（含**占用者是谁**）。纯格式化，便于测试与复用。 */
export function portBusyMessage(port, owners) {
  return [
    '',
    `[preflight] 端口 ${port} 已被占用 —— 拒绝复用（#209）。`,
    '',
    ...owners.map((o) => {
      const root = cloneRootOf(o.cmd);
      return (
        `  PID ${o.pid}` +
        (root ? `\n    仓库根: ${root}` : '') +
        `\n    command : ${o.cmd || '(空)'}`
      );
    }),
    '',
    `为什么不能复用：${port} 上那个 server 属于**另一个 clone**时，Playwright 会用它`,
    '跑本仓的 spec —— 绿/红都指向别的代码（#209 实测：18 failed 假红，排查约 20 分钟）。',
    '换端口也不是办法：Playwright 等的是配置里写死的那个 URL。',
    '',
    '怎么办：确认上面那个进程不再是需要的（例如它是上一次 e2e 留下的、或另一个 clone',
    '        的常驻 dev server），结束它（Windows: taskkill /PID <pid> /F）后重跑门禁。',
    '',
  ].join('\n');
}

/** 端口空闲则返回；被占（或探测不了）就**抛**——调用方决定怎么呈现。
 *
 * 抛而不是 `process.exit`：本函数被 `playwright.config.ts` 在配置加载期调用，
 * 在那里 exit 会静默杀掉整个 runner（连"为什么"都打不出来）；抛出的异常由
 * Playwright 打印成 config 加载失败，附带下面这段原文。 */
export function assertPortFree(port = DEFAULT_PORT) {
  const pids = listeningPids(port);
  if (pids === null) {
    throw new Error(
      `[preflight] 无法探测端口 ${port} 的占用者（netstat/lsof 都不可用）。\n` +
        '           门禁要求"端口要么空闲、要么由本次启动的 vite 监听"：请在纯净环境重跑，\n' +
        '           或先手工确认该端口没人监听再继续（不要跳过这条——#209 的假红就是这么来的）。',
    );
  }
  if (pids.length > 0) {
    throw new Error(portBusyMessage(port, pids.map((pid) => ({ pid, cmd: commandLineOf(pid) }))));
  }
}

/** CLI 入口（`node scripts/preflight-port.mjs [port]`）。 */
function main() {
  const raw = process.argv[2];
  const port = Number(raw ?? DEFAULT_PORT);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    console.error(`[preflight] 端口参数非法：${raw}`);
    process.exit(1);
  }
  try {
    assertPortFree(port);
  } catch (e) {
    console.error(e && e.message ? e.message : String(e));
    process.exit(1);
  }
  console.log(`[preflight] 端口 ${port} 空闲，由本次启动的 vite 独占。`);
}

// 直接运行时才走 CLI；被 import（playwright.config.ts）时只导出上面两个函数。
// 比对用 URL 形式（本路径含非 ASCII 时 `path.resolve` 与 `fileURLToPath` 的
// 编码形态可能不一致，URL 形式两边都归一化，不依赖盘符大小写/分隔符）。
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  main();
}
