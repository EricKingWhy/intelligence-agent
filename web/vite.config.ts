import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev: Vite serves frontend on :5173, proxies /api to FastAPI on :8000.
// Prod: FastAPI serves web/dist statically (see src/agent_harness/web/app.py).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
