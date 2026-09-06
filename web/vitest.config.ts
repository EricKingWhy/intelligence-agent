import { configDefaults, defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// 默认测试车道（CI / npm test）：排除 perf 基准文件——数字随机器漂移，
// 预算断言由独立手动车道 vitest.perf.config.ts 承担（HANDOFF_PERF_FRONTEND §6 P2-6）。
export default defineConfig({
  plugins: [react()],
  test: {
    exclude: [...configDefaults.exclude, '**/*.perf.test.ts'],
  },
})
