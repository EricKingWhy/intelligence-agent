/** 会话「重放」浮层（`#1322`，spec 03 §6 / ADR-0017 决策 4）。
 *
 *  与「硬删」确认面（`DeleteSessionDialog`）同构：二次确认浮层 + 表单状态放在
 *  **只在打开时挂载**的内层组件里（`{target && <Form/>}`），关闭即丢弃——上一次的
 *  进度 / 错误不会漏到下一次。
 *
 *  为什么是浮层而不是行内：README 把「可重放」写成核心能力，入口却只在 CLI。
 *  本浮层把这条能力搬到 Web 的可观测工作台里（对齐 Langfuse session view 的
 *  「Replay the entire interaction」、LangSmith trace 的 replay——replay 是工作台
 *  里的一等入口，不是隐藏命令）。二次确认在这里的作用**不是防误触**（重放只读、
 *  零副作用、可无限重来），而是把「零副作用」这条语义在动作**之前**讲清楚：
 *  用户会以为 replay = 重跑，必须明说它只读重建、不触发模型 / 工具、不写入原会话。
 *
 *  分工：本组件只管「确认 → 逐事件推进 → 结果 / 失败留在原地」，**不碰会话状态**
 *  （重放零副作用，不改任何会话真相）。数据来自 `replaySession`（只读端点）。
 */

import { useEffect, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Play, X } from 'lucide-react';
import { describeSessionError, replaySession, type SessionReplayStep } from '../lib/api';

/** 确认面要显示的会话标识（与 `DeleteSessionTarget` 同形：只够"认出点的是哪一条"）。 */
export interface ReplaySessionTarget {
  id: string;
  /** 行标题（首条用户消息投影）；空串 = 只显示 id 片段。 */
  title: string;
  /** #1372 改动 3：关闭时把焦点还给谁。菜单项在菜单关闭时已卸载，Radix 的默认
   *  归还目标会变成 body；调用方传行内那个稳定的 ⋯ 触发按钮。 */
  restoreFocus?: HTMLElement | null;
}

interface Props {
  /** 待回放会话；null = 关闭。 */
  target: ReplaySessionTarget | null;
  onOpenChange: (open: boolean) => void;
}

/** 逐事件推进的节奏（毫秒 / 步）。够慢到肉眼可见"事件在往前走"，又不至于让长会话
 *  等太久——回放本身是本地只读投影，快得没有可展示的"等待"，进度是**重建推进**的
 *  可视化，不是网络延迟。 */
const REVEAL_MS = 60;

export function SessionReplayDialog({ target, onOpenChange }: Props) {
  // #1372 改动 3：关闭时父组件立即把 target 置空，若这里跟着卸载 Content，
  // Radix 的 FocusScope 会连同 onCloseAutoFocus 一起消失，焦点掉回 body。
  // 保留最后一个 target 让 Content 撑到 Presence 完成退场（DOM 仍由 Radix 卸载），
  // onCloseAutoFocus 才有机会把焦点还给行内 ⋯。
  const [lastTarget, setLastTarget] = useState(target);
  if (target && target !== lastTarget) setLastTarget(target);
  return (
    <Dialog.Root
      open={target !== null}
      onOpenChange={(open) => {
        if (!open) onOpenChange(false);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay" />
        {lastTarget && (
          <SessionReplayForm
            target={lastTarget}
            /* 只释放 DOM 引用：Content 若在退场时卸载，FocusScope 的卸载清理会
               再把焦点抢回 body（AC2 实测红）。target 本身保留到下次打开。 */
            onClosed={() =>
              setLastTarget((prev) => (prev ? { ...prev, restoreFocus: null } : prev))
            }
          />
        )}
      </Dialog.Portal>
    </Dialog.Root>
  );
}

type Phase = 'confirm' | 'running' | 'done';

function SessionReplayForm({
  target,
  onClosed,
}: {
  target: ReplaySessionTarget;
  /** 退场完成、焦点已归还后清掉保留的 target（不再持有一个已卸载的 DOM 引用）。 */
  onClosed: () => void;
}) {
  const [phase, setPhase] = useState<Phase>('confirm');
  const [steps, setSteps] = useState<SessionReplayStep[]>([]);
  /** 已推进到的步数（0..steps.length）。`running` 且 `loaded` 时由定时器自增。 */
  const [revealed, setRevealed] = useState(0);
  /** 请求已返回（区分"还在读事件"与"读完了 0 条"）。 */
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  // 逐事件推进：请求返回后每 REVEAL_MS 揭示一条；到齐即收口为 done。
  useEffect(() => {
    if (phase !== 'running' || !loaded) return;
    if (revealed >= steps.length) {
      setPhase('done');
      return;
    }
    const timer = window.setTimeout(() => setRevealed((n) => n + 1), REVEAL_MS);
    return () => window.clearTimeout(timer);
  }, [phase, loaded, revealed, steps.length]);

  const start = async () => {
    setPhase('running');
    setError(null);
    setSteps([]);
    setRevealed(0);
    setLoaded(false);
    try {
      const result = await replaySession(target.id);
      setSteps(result.steps);
      setLoaded(true);
    } catch (e) {
      // 失败留在原地可重试：回到确认态，把后端 detail 原样贴出来（不自己编文案）。
      setError(describeSessionError(e, '重放会话失败'));
      setPhase('confirm');
    }
  };

  const progressPct =
    steps.length === 0 ? 0 : Math.round((revealed / steps.length) * 100);

  return (
    <Dialog.Content
      className="project-dialog replay-dialog"
      /* #1372 改动 3：radix 1.1.23 的 Dialog.Content 不渲染 aria-modal（本地源码无该
         输出，Lane D 实测 ariaModal=null）。显式补上，读屏才知道这是模态层。 */
      aria-modal="true"
      data-replay-phase={phase}
      data-replay-revealed={revealed}
      data-replay-count={steps.length}
      /* 初焦落在安全的出口（「取消」）：与 DeleteSessionDialog 同一理由——Radix 默认
         把初焦给右上角「关闭(X)」，键盘用户落地第一眼看到的是最不该先碰的控件。 */
      onOpenAutoFocus={(e) => {
        if (!cancelRef.current) return;
        e.preventDefault();
        cancelRef.current.focus();
      }}
      onCloseAutoFocus={(event) => {
        // Esc / 关闭后回到打开它的那个 ⋯ 按钮。目标已不在文档里就交回 Radix 默认。
        const restore = target.restoreFocus;
        if (restore?.isConnected) {
          event.preventDefault();
          restore.focus();
        }
        onClosed();
      }}
    >
      <div className="project-dialog-head">
        <Dialog.Title className="project-dialog-title">
          <Play size={14} aria-hidden="true" /> 重放会话
        </Dialog.Title>
        <Dialog.Close asChild>
          <button className="icon-btn project-dialog-close" aria-label="关闭">
            <X size={14} />
          </button>
        </Dialog.Close>
      </div>

      <div className="project-dialog-target">
        <div className="project-dialog-target-title" title={target.title || target.id}>
          {target.title || '（这条会话没有标题）'}
        </div>
        <div className="project-dialog-target-id">id {target.id.slice(0, 12)}</div>
      </div>

      {phase === 'confirm' ? (
        <Dialog.Description asChild>
          <div className="project-dialog-desc">
            将<strong>零副作用</strong>地重放该会话历史：
            <ul className="project-dialog-list">
              <li>从已持久化的事件重新派生视图，工具结果一律呈现<strong>冻结终态</strong></li>
              <li>不触发模型 / 工具调用，不产生任何外部副作用</li>
              <li>
                <strong>不写入</strong>原会话事件流——原会话一字不变
              </li>
            </ul>
          </div>
        </Dialog.Description>
      ) : (
        <div className="replay-body">
          {/* 进度：`running` 阶段逐事件推进；完成态满格。`role="progressbar"` 让
              "回放过程可见"这条 AC 有可机读的形状（e2e 断言它，而不是靠看动画）。 */}
          <div className="replay-progress-head">
            {phase === 'running' && !loaded
              ? '正在读取事件…'
              : phase === 'running'
                ? `重放中 ${revealed}/${steps.length}`
                : `已重放 ${revealed}/${steps.length}`}
          </div>
          <div
            className="replay-progress"
            role="progressbar"
            aria-label="回放进度"
            aria-valuemin={0}
            aria-valuemax={steps.length}
            aria-valuenow={revealed}
          >
            <div className="replay-progress-fill" style={{ width: `${progressPct}%` }} />
          </div>
          {steps.length === 0 && loaded ? (
            <div className="replay-empty">这条会话没有可回放的内容。</div>
          ) : (
            <ol className="replay-steps">
              {steps.slice(0, revealed).map((step) => (
                <li key={step.seq} className="replay-step">
                  <span className="replay-step-seq" title={`seq ${step.seq}`}>
                    #{step.seq}
                  </span>
                  <span className="replay-step-type">{step.type}</span>
                  <pre className="replay-step-line">{step.line}</pre>
                </li>
              ))}
            </ol>
          )}
          {phase === 'done' && (
            <div className="replay-done" role="status">
              回放完成 · {steps.length} 个可渲染事件（零副作用，原会话未改动）
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="project-error" role="alert">
          {error}
        </div>
      )}

      <div className="project-dialog-actions">
        {phase === 'done' ? (
          <Dialog.Close asChild>
            <button className="project-btn project-btn-primary">完成</button>
          </Dialog.Close>
        ) : (
          <>
            <Dialog.Close asChild>
              <button className="project-btn" ref={cancelRef}>
                取消
              </button>
            </Dialog.Close>
            <button
              className="project-btn project-btn-primary"
              onClick={() => void start()}
              disabled={phase === 'running'}
            >
              {phase === 'running' ? '正在重放…' : error ? '重试' : '开始重放'}
            </button>
          </>
        )}
      </div>
    </Dialog.Content>
  );
}
