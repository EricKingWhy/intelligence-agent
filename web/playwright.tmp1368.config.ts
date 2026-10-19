import { defineConfig, devices } from '@playwright/test';

// 临时配置（#1368 施工取证用，**不入库**）：5173 被另一条 worktree 的 vite 占着，
// 这里换 5199 起本 worktree 自己的 dev server。
const PORT = 5199;
const ORIGIN = `http://localhost:${PORT}`;

export default defineConfig({
  testDir: './e2e',
  timeout: 90_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: { baseURL: ORIGIN, trace: 'off' },
  webServer: {
    command: `npx vite --strictPort --port ${PORT}`,
    url: ORIGIN,
    reuseExistingServer: false,
    timeout: 120_000,
  },
  projects: [
    {
      name: 'shots',
      use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 } },
    },
  ],
});
