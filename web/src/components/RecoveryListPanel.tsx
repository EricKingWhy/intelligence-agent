/** RecoveryListPanel（#357 W-13 契约 5）——「先列后继续」的恢复列表。
 *
 *  数据全部来自后端只读端点 `GET /api/recovery/interrupted`（`listInterruptedRecoveries`）：
 *  前端**不缓存、不派生第二套中断真相**（不变量 #22）。页面按修订 A §9.4 与产品
 *  要求做三件诚实的事：
 *  - 四要素（Task / 无终态 run / 工作目录 / 进度文件版本）如实展示；
 *  - 进度文件缺失/不可读/不匹配如实标注「未知」，**绝不伪造「最新」**；
 *  - `snapshot_available=false` 或载荷形状不合法时整页降级，不展示编造列表。
 *
 *  「先列后继续」：列表加载完成前不出现可点的「继续」——只给一个 disabled 的
 *  占位动作 + 原因说明（`aria-disabled` 如实）。加载完成后，逐行按后端
 *  `resume_available` 决定是否亮 Resume（状态不可读行后端已置 true，抄 Cline
 *  的 fail-safe：隐藏 Resume 才是数据丢失的假象）。
 *
 *  键盘可达：Radix Dialog 自带 Esc 关闭与焦点管理。 */

import { useCallback, useEffect, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { RefreshCw, RotateCcw, X } from 'lucide-react';
import { listInterruptedRecoveries, type InterruptedRecoveries } from '../lib/api';
import { formatProgressVersion, progressTone } from '../lib/recovery';
import '../styles/recovery.css';

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 用户点「继续」——由 App 接上（打开该会话，恢复仍走会话内的既有恢复链路）。 */
  onResume: (sessionId: string) => void;
}

type LoadState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'loaded'; data: InterruptedRecoveries | undefined };

export function RecoveryListPanel({ open, onOpenChange, onResume }: Props) {
  const openerRef = useRef<HTMLElement | null>(null);
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay" />
        {open && <RecoveryListBody openerRef={openerRef} onResume={onResume} />}
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** 「先列后继续」的加载门控文案（说明为什么现在不能继续）。 */
const GATE_REASON = '加载完成前不能继续——先核对完上次运行的完整状态。';

function ResumeButton({
  sessionId,
  enabled,
  reasonId,
  onResume,
}: {
  sessionId: string;
  enabled: boolean;
  reasonId: string;
  onResume: (sessionId: string) => void;
}) {
  return (
    <button
      type="button"
      className="recovery-resume-btn"
      disabled={!enabled}
      aria-disabled={!enabled}
      aria-describedby={enabled ? undefined : reasonId}
      onClick={() => {
        if (enabled) onResume(sessionId);
      }}
    >
      继续
    </button>
  );
}

function RecoveryListBody({
  openerRef,
  onResume,
}: {
  openerRef: { current: HTMLElement | null };
  onResume: (sessionId: string) => void;
}) {
  const [state, setState] = useState<LoadState>({ kind: 'loading' });

  const load = useCallback(async () => {
    setState({ kind: 'loading' });
    try {
      setState({ kind: 'loaded', data: await listInterruptedRecoveries() });
    } catch (error) {
      setState({ kind: 'error', message: (error as Error).message });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Dialog.Content
      className="recovery-list-panel"
      onOpenAutoFocus={() => {
        openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      }}
      onCloseAutoFocus={(event) => {
        if (openerRef.current?.isConnected) {
          event.preventDefault();
          openerRef.current.focus();
        }
      }}
    >
      <div className="project-dialog-head">
        <Dialog.Title className="project-dialog-title">
          <RotateCcw size={14} aria-hidden="true" /> 恢复列表
        </Dialog.Title>
        <Dialog.Close asChild>
          <button className="icon-btn project-dialog-close" aria-label="关闭"><X size={14} /></button>
        </Dialog.Close>
      </div>
      <Dialog.Description className="project-dialog-desc">
        上次运行留下的中断会话。列表只读、来自服务端启动扫描快照；在核对完完整状态之前不会出现可点的「继续」。
      </Dialog.Description>

      <div className="recovery-list-body">
        {state.kind === 'loading' && (
          <div className="recovery-list-loading">
            <p role="status">正在核对上次运行信息…（事件 → 工作区 → Ledger → 对账 → 工具配对 → 上下文）</p>
            <button type="button" className="recovery-resume-btn" disabled aria-disabled="true" aria-describedby="recovery-gate-reason">
              继续
            </button>
            <p className="recovery-gate-reason" id="recovery-gate-reason">{GATE_REASON}</p>
          </div>
        )}

        {state.kind === 'error' && (
          <div className="recovery-list-error" role="alert">
            <span>{state.message || '读取恢复列表失败'}</span>
            <button type="button" className="recovery-list-retry" onClick={() => void load()}>
              <RefreshCw size={13} aria-hidden="true" /> 重试
            </button>
          </div>
        )}

        {state.kind === 'loaded' && (state.data === undefined || state.data.snapshot_available === false) && (
          <div className="recovery-list-degraded" role="status">
            <strong>上次运行信息不可用</strong>
            <span>服务端启动扫描没有可用的快照，因此无法列出中断会话——不展示任何推测出来的列表。</span>
            <button type="button" className="recovery-resume-btn" disabled aria-disabled="true" aria-describedby="recovery-degraded-reason">
              继续
            </button>
            <p className="recovery-gate-reason" id="recovery-degraded-reason">
              信息不可用时不能继续。
            </p>
          </div>
        )}

        {state.kind === 'loaded' && state.data?.snapshot_available === true && state.data.items.length === 0 && (
          <div className="recovery-list-empty" role="status">没有发现中断的会话。</div>
        )}

        {state.kind === 'loaded' && state.data?.snapshot_available === true && state.data.items.length > 0 && (
          <ul className="recovery-list">
            {state.data.items.map((item) => {
              const tone = progressTone(item.progress);
              const reasonId = `recovery-item-reason-${item.session_id}`;
              return (
                <li className="recovery-list-item" key={item.session_id}>
                  <p className="recovery-list-task">{item.task ?? '—（未记录任务）'}</p>
                  <dl className="recovery-list-facts">
                    <dt>中断的 run</dt>
                    <dd>
                      {item.interrupted_runs.length === 0
                        ? '—'
                        : item.interrupted_runs.map((run, index) => (
                          <span className="recovery-run" key={`${run.run_id ?? 'none'}-${index}`}>
                            {run.run_id ?? '（无 run id）'} · 中断于事件 #{run.interrupted_seq}
                            {run.step_id !== null ? ` · 第 ${run.step_id} 步` : ''}
                            {run.agent_id ? ` · ${run.agent_id}` : ''}
                          </span>
                        ))}
                    </dd>
                    <dt>工作目录</dt>
                    <dd>{item.workspace_root ?? '—（未记录）'}</dd>
                    <dt>进度文件</dt>
                    <dd className={tone === 'unknown' ? 'recovery-fact-unknown' : undefined}>
                      {formatProgressVersion(item.progress)}
                    </dd>
                  </dl>
                  <div className="recovery-item-actions">
                    {!item.resume_available && (
                      <p className="recovery-gate-reason" id={reasonId}>
                        该行没有可恢复的中断运行。
                      </p>
                    )}
                    <ResumeButton
                      sessionId={item.session_id}
                      enabled={item.resume_available}
                      reasonId={reasonId}
                      onResume={onResume}
                    />
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="project-dialog-actions">
        <Dialog.Close asChild>
          <button className="project-btn project-btn-primary">关闭</button>
        </Dialog.Close>
      </div>
    </Dialog.Content>
  );
}
