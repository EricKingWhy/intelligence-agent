import { configDefaults, defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Perf 手动车道（HANDOFF_PERF_FRONTEND §6 P2-6，仿 DSH vitest.web.perf.config.ts）：
// 预算断言在此执行，防 O(N²)/劣化回潮。不进默认 CI inventory——
// 运行：npx vitest run -c vitest.perf.config.ts
export default defineConfig({
  plugins: [react()],
  test: {
    include: ['src/**/*.perf.test.ts'],
    exclude: [...configDefaults.exclude],
  },
})
