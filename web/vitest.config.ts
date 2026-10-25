import { configDefaults, defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// 默认测试车道（CI / npm test）：排除 perf 基准文件——数字随机器漂移，
// 预算断言由独立手动车道 vitest.perf.config.ts 承担（HANDOFF_PERF_FRONTEND §6 P2-6）。
// 另排除 e2e-live/**（联调车道）：它用 Playwright 的 test() API 且依赖真后端，
// 被 vitest 收集会直接报错（与 e2e/** 同理）。
export default defineConfig({
  plugins: [react()],
  test: {
    exclude: [...configDefaults.exclude, '**/*.perf.test.ts', 'e2e/**', 'e2e-live/**'],
    // #1518：把本地测试时区钉到 UTC，与 CI（GitHub Actions 默认 UTC）一致——
    // 否则渲染墙钟时刻的用例（如 StepDetail.artifacts 的 '10:23'）在 UTC+8
    // 开发机上假红。env 在 worker 初始化时注入，早于测试文件的模块执行。
    env: { TZ: 'UTC' },
  },
})
