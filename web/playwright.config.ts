import { defineConfig, devices } from '@playwright/test';
import { assertPortFree } from './scripts/preflight-port.mjs';

// T8（#101）E2E 车道（spec 03 §21 浏览器矩阵骨架）：
// - Chromium 单引擎（grill Q5 批准范围）；1280/1920 两档宽度两个 project。
// - webServer 起 vite dev（5173）；API 用 page.route 拦截（e2e/fixtures.ts
//   mock SSE，帧形状 = docs/BACKEND_CONTRACT_STREAMING_UI.md）——核心矩阵
//   不依赖真后端。真实模型/鉴权/长稳场景属联调车道（spec 03 §22 尾注：
//   夜间/手动），不进本骨架。
// - 不污染 vitest 车道：vitest.config.ts exclude e2e/**。
//
// 端口是**单一字面量**：baseURL 与 webServer.url 都从它来（预检脚本
// `scripts/preflight-port.mjs` 的默认值必须与它一致——两处都在 web/ 下，
// 没有共享机制时双份 + 注释互指是成本最低的可维护形态，同 §15 的 CSS token）。
const PORT = 5173;
const ORIGIN = `http://localhost:${PORT}`;

// 端口守卫必须在**配置加载期**（就是这一块），不能只放在 webServer.command 里：
// Playwright 起 webServer **之前**先探测 url，被占时它直接报
// `http://localhost:5173 is already used …` 且**不执行**我们的命令（实测）——
// 那句通用错误说不出占用者是谁，正是本票要消灭的东西。
//
// ⚠ **只在主进程守**：worker 进程会**重新加载**本配置，而那时 Playwright 已经起好
// 了 vite（先起 webServer、再 fork worker）⇒ 守卫会看到"端口被自己占着"并把**整批**
// 用例判失败（实测：384 failed，报错里的 worktree 就是本仓）。`TEST_WORKER_INDEX`
// 是 Playwright 给 worker 进程置的环境变量，只在主进程缺省。
//
// `--list`（只列用例、不跑、不起服务）也跳过：否则本机有 dev server 时连数用例都做不了。
if (!process.env.TEST_WORKER_INDEX && !process.argv.includes('--list')) {
  assertPortFree(PORT);
}

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: true,
  // §16.6 硬要求：e2e 一律 2 worker。4 worker 全量并行存在资源竞争型抖动，
  // 且同一 worktree 里并行跑两个 playwright 会争用 test-results/ 产生
  // `ENOENT … .playwright-artifacts-*` 式的假失败。写进配置让裸跑
  // `npx playwright test` 与门禁命令（`--workers=2`）等价，免得"门禁绿、
  // 本地红"反复消耗排查时间。（e2e-live/ 用独立配置，不受这里影响。）
  workers: 2,
  reporter: [['list']],
  use: {
    baseURL: ORIGIN,
    trace: 'retain-on-failure',
  },
  webServer: {
    // `dev:e2e` = 端口预检 + `vite --strictPort`（#209）：见脚本头注释。
    // 两条一起才成立——只加 strictPort 会在端口被占时给出难读的
    // `exited early`；只加预检则端口检查与"拒不复用"之间仍有缝。
    command: 'npm run dev:e2e',
    url: ORIGIN,
    // 恒 false（**本地也是**）：5173 上可能挂着另一个 clone 的 vite，复用它就是
    // 用本仓的 spec 测别人的代码——绿/红都不可信（#209 实测 18 failed 假红）。
    // 预检脚本负责把"被谁占着"直接打出来，所以这次改动不会让失败信息变差。
    reuseExistingServer: false,
    timeout: 120_000,
  },
  projects: [
    { name: 'chromium-1280', use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } } },
    { name: 'chromium-1920', use: { ...devices['Desktop Chrome'], viewport: { width: 1920, height: 1080 } } },
  ],
});
