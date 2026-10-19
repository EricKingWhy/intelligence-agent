// 临时组件状态展示页（#1369 截图取证用，完工删除）。
// 只渲染本票涉及的 4 个按钮族 / 输入框 / spinner，状态全部由 Playwright 真实交互驱动。
import type { ReactNode } from 'react'
import { Loader, Trash2 } from 'lucide-react'

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 16, minHeight: 44 }}>
      <span style={{ width: 150, color: 'var(--text-tertiary)', fontSize: 12 }}>{label}</span>
      {children}
    </div>
  )
}

export default function Showcase() {
  return (
    <div
      style={{
        minHeight: '100vh',
        padding: 32,
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
        background: 'var(--bg-workspace)',
        color: 'var(--text-primary)',
      }}
    >
      <Row label="btn-primary">
        <button className="btn-primary" data-shot="btn-primary_default">保存</button>
        <button className="btn-primary" data-shot="btn-primary_active">保存</button>
        <button className="btn-primary" disabled data-shot="btn-primary_disabled">保存</button>
        <button className="btn-primary is-loading" data-shot="btn-primary_loading">
          <span className="status-spinner" aria-hidden="true" />
          保存中
        </button>
      </Row>
      <Row label="btn-danger">
        <button className="btn-danger" data-shot="btn-danger_default">删除</button>
        <button className="btn-danger" data-shot="btn-danger_active">删除</button>
        <button className="btn-danger" disabled data-shot="btn-danger_disabled">删除</button>
        <button className="btn-danger is-loading" data-shot="btn-danger_loading">
          <span className="status-spinner" aria-hidden="true" />
          删除中
        </button>
      </Row>
      <Row label="btn-ghost">
        <button className="btn-ghost" data-shot="btn-ghost_default">取消</button>
        <button className="btn-ghost" data-shot="btn-ghost_active">取消</button>
        <button className="btn-ghost" disabled data-shot="btn-ghost_disabled">取消</button>
        <button className="btn-ghost is-loading" data-shot="btn-ghost_loading">
          <span className="status-spinner" aria-hidden="true" />
          处理中
        </button>
      </Row>
      <Row label="icon-btn">
        <button className="icon-btn" aria-label="删除" data-shot="icon-btn_default">
          <Trash2 size={16} />
        </button>
        <button className="icon-btn" aria-label="删除" data-shot="icon-btn_active">
          <Trash2 size={16} />
        </button>
        <button className="icon-btn" aria-label="删除" disabled data-shot="icon-btn_disabled">
          <Trash2 size={16} />
        </button>
        <button className="icon-btn is-loading" aria-label="删除中" data-shot="icon-btn_loading">
          <span className="status-spinner" aria-hidden="true" />
        </button>
      </Row>

      <Row label="project-input">
        <input className="project-input" style={{ width: 220 }} defaultValue="项目名称" data-shot="input_default" />
        <input className="project-input" style={{ width: 220 }} defaultValue="项目名称" data-shot="input_focus" />
        <input
          className="project-input"
          style={{ width: 220 }}
          defaultValue="项目名称"
          aria-invalid="true"
          data-shot="input_error"
        />
        <input
          className="project-input"
          style={{ width: 220 }}
          defaultValue="项目名称"
          disabled
          data-shot="input_disabled"
        />
      </Row>

      <Row label="spinner">
        <span style={{ display: 'inline-flex', width: 32, height: 32, alignItems: 'center', justifyContent: 'center' }} data-shot="wrap_spinner_ctx">
          <span className="ctx-usage-spinner" />
        </span>
        <span style={{ display: 'inline-flex', width: 32, height: 32, alignItems: 'center', justifyContent: 'center' }} data-shot="wrap_spinner_status">
          <span className="status-spinner" />
        </span>
        <span style={{ display: 'inline-flex', width: 32, height: 32, alignItems: 'center', justifyContent: 'center' }} data-shot="wrap_spinner_dir">
          <Loader size={13} className="dir-browser-spinner" />
        </span>
      </Row>
    </div>
  )
}
