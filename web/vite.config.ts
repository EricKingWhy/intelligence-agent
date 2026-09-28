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
        // `ws: true` 不是可选调优：**live 流的真实通道是 `WebSocket /api/ws`**
        // （`lib/wsStream.ts` 有实测表——交付层会把整个 HTTP 响应攒到流结束才下发，
        // 所以 POST/GET 的 SSE 在部署链路上要 41~44s 才到响应头）。Vite 的 proxy
        // 默认**不处理 Upgrade 请求**，少了这一行，dev 下 `/api/ws` 的握手永远
        // 完不成：客户端 `onopen` 不触发 → 不发 subscribe → 浏览器 ~11s 后才放弃。
        // 现象是"直播卡住 11 秒以上"（真机取证：排队一条消息后正文长度停在 39
        // 整整 8s 不动，见 `docs/LIVE_BROWSER_TEST_20260917.md` §9.4 F14）。
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    // 生产不发布 sourcemap：.map 会把全部原始源码暴露给任何能拉到静态资源的人
    // （安全审查发现 3）。本地排障用 npm run build -- --sourcemap 临时开启。
    sourcemap: false,
    // 0 = 任何资源都不内联为 data: URI。后端 CSP `default-src 'self'` 下
    // font-src 回退 self，data: 字体会被拦（实测 2026-09-05：fontsource 最小的
    // vietnamese 子集默认被内联后遭浏览器拦截）。字体必须始终是同源文件。
    assetsInlineLimit: 0,
  },
})
