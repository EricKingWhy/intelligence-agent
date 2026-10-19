/** #1367 临时 e2e 配置（跑完即删，不入库）：5173 被兄弟 worktree 的 dev server
 *  占着，而主配置的端口是写死的。这里换成 5273，其余与 playwright.config.ts 等价。 */
import { defineConfig, devices } from '@playwright/test';

const PORT = 5273;
const ORIGIN = `http://localhost:${PORT}`;

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: true,
  workers: 2,
  reporter: [['list']],
  use: {
    baseURL: ORIGIN,
    trace: 'retain-on-failure',
    /* 本机 egress 代理会被 Chromium 采用（实测：不加此参数 page.goto('http://localhost')
       挂死 30s；加后正常加载）。沙箱/CI 无代理，等价无影响。 */
    launchOptions: { args: ['--no-proxy-server', '--no-sandbox'] },
  },
  webServer: {
    command: `node scripts/preflight-port.mjs ${PORT} && npx vite --port ${PORT} --strictPort`,
    url: ORIGIN,
    reuseExistingServer: false,
    timeout: 120_000,
  },
  projects: [
    { name: 'chromium-1280', use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } } },
    { name: 'chromium-1920', use: { ...devices['Desktop Chrome'], viewport: { width: 1920, height: 1080 } } },
  ],
});
