import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { initTheme } from './lib/theme'
import './index.css'
import App from './App.tsx'

// Paint 前解析主题（localStorage → 系统偏好），避免首帧闪错主题
initTheme()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
