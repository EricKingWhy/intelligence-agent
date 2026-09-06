import { defineConfig, devices } from '@playwright/test';

// T8（#101）E2E 车道（spec 03 §21 浏览器矩阵骨架）：
// - Chromium 单引擎（grill Q5 批准范围）；1280/1920 两档宽度两个 project。
// - webServer 起 vite dev（5173）；API 用 page.route 拦截（e2e/fixtures.ts
//   mock SSE，帧形状 = docs/BACKEND_CONTRACT_STREAMING_UI.md）——核心矩阵
//   不依赖真后端。真实模型/鉴权/长稳场景属联调车道（spec 03 §22 尾注：
//   夜间/手动），不进本骨架。
// - 不污染 vitest 车道：vitest.config.ts exclude e2e/**。
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: true,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  projects: [
    { name: 'chromium-1280', use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } } },
    { name: 'chromium-1920', use: { ...devices['Desktop Chrome'], viewport: { width: 1920, height: 1080 } } },
  ],
});
