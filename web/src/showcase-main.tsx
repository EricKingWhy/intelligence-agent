// 临时入口（#1369 取证用，完工删除）：只加载 showcase，不引入 App 的模块图。
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './styles/app.css'
import Showcase from './showcase.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Showcase />
  </StrictMode>,
)
