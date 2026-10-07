/** 会话证据原件的清理预览与执行浮层（#368 / W-24）。
 *
 *  为什么是浮层：执行清理会**不可恢复地**删掉证据原件（无墓碑 / 无回收站）。一次
 *  不可逆动作的确认面必须与动作本身分离，风险由入口层的显式确认承担——与
 *  DeleteSessionDialog 同源（ADR-0029）。
 *
 *  结构纪律照抄 DeleteSessionDialog：
 *  - 表单状态放在**只在打开时挂载**的内层组件里（`{target && <Form/>}`），关闭即丢弃
 *    ——上次的勾选 / 回执不会漏到下一次；
 *  - `onConfirm` 返回 Promise，成功 / 失败回执都**留在浮层**里，调用方负责刷新；
 *  - 打开即拉一次 preview（`onPreview` 走 ref：父组件重渲染不重拉、不重置勾选）。
 *
 *  诚实红线（不变量 #22 / 本票验收）：回执**分列** deleted / failed / not_deleted，
 *  有未删项时逐条明示原因，**绝不出现"全部清理"**这类合并结论。 */

import { useEffect, useMemo, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { TriangleAlert, X } from 'lucide-react';
import { formatBytes } from '../lib/format';
import type { CleanupPreview, CleanupResult } from '../lib/api';

/** 确认面上的会话标识：只需 sessionId——本浮层不读会话标题，也不带可能过期的计数。 */
export interface CleanupTarget {
  sessionId: string;
}

interface Props {
  /** 待清理会话；null = 关闭。 */
  target: CleanupTarget | null;
  onOpenChange: (open: boolean) => void;
  /** 取预览（**只读**）。reject = 预览失败，原因留在浮层。 */
  onPreview: (sessionId: string) => Promise<CleanupPreview>;
  /** 执行清理（**不可恢复**）。resolve = 后端回执；reject = 留在浮层的原因。 */
  onConfirm: (sessionId: string, snapshotToken: string, refs: string[]) => Promise<CleanupResult>;
}

/** 后端 reason 枚举的中文解释；未知原因**原样显示**，不编（与 deleteSession 的
 *  "409 只靠 detail 区分、绝不自己编文案"同一纪律）。
 *
 *  全集对齐后端（#368 P3-5）：preview.blocked 用 active_task /
 *  unreconciled_operation / fork_child_reference / referenced / evidence
 *  （_CLEANUP_BLOCK_PRIORITY + 在途 run）；execute.not_deleted 另加
 *  not_found / invalid（delete_local_artifacts 的返回分类）。 */
const BLOCK_REASON_LABEL: Record<string, string> = {
  active_task: '有在途任务',
  unreconciled_operation: '有未对账操作',
  referenced: '仍被事件引用',
  fork_child_reference: '被 fork 子会话引用',
  evidence: '仍有新鲜证据引用',
  not_found: '原件已不存在',
  invalid: '引用格式非法',
};

function blockReasonLabel(reason: string): string {
  return BLOCK_REASON_LABEL[reason] ?? reason;
}

/** ref 短显：超过 20 字符截断加省略号（完整值放 title）。 */
function shortRef(ref: string): string {
  return ref.length > 20 ? `${ref.slice(0, 20)}…` : ref;
}

export function CleanupPreviewDialog({ target, onOpenChange, onPreview, onConfirm }: Props) {
  return (
    <Dialog.Root
      open={target !== null}
      onOpenChange={(open) => {
        if (!open) onOpenChange(false);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay" />
        {target && <CleanupForm target={target} onPreview={onPreview} onConfirm={onConfirm} />}
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function CleanupForm({
  target,
  onPreview,
  onConfirm,
}: {
  target: CleanupTarget;
  onPreview: Props['onPreview'];
  onConfirm: Props['onConfirm'];
}) {
  const [preview, setPreview] = useState<CleanupPreview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<CleanupResult | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  /** 初焦落在「取消」而非右上「关闭(X)」——不可逆动作的确认面，初焦应在安全出口。 */
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const p = await onPreview(target.sessionId);
        if (!alive) return;
        setPreview(p);
        // 默认全选可清理项；用户可逐项取消。
        setSelected(new Set(p.affected.map((a) => a.artifact_ref)));
      } catch (e) {
        if (alive) setLoadError(e instanceof Error ? e.message : '清理预览失败');
      }
    })();
    return () => {
      alive = false;
    };
  }, [target.sessionId, onPreview]);

  const selectedCount = useMemo(
    () => (preview ? preview.affected.filter((a) => selected.has(a.artifact_ref)).length : 0),
    [preview, selected],
  );

  const toggle = (ref: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(ref)) next.delete(ref);
      else next.add(ref);
      return next;
    });
  };

  const confirm = async () => {
    if (pending || !preview) return;
    const refs = preview.affected
      .filter((a) => selected.has(a.artifact_ref))
      .map((a) => a.artifact_ref);
    if (refs.length === 0) return;
    setPending(true);
    setConfirmError(null);
    try {
      setResult(await onConfirm(target.sessionId, preview.snapshot_token, refs));
    } catch (e) {
      // 409（快照过期）与 403（跨源）状态码不同但都只能靠 detail 区分——原样显示，
      // 绝不自己编一套文案把唯一可行动的信息抹掉。
      setConfirmError(e instanceof Error ? e.message : '清理原件失败');
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog.Content
      className="project-dialog"
      onOpenAutoFocus={(e) => {
        if (!cancelRef.current) return;
        e.preventDefault();
        cancelRef.current.focus();
      }}
    >
      <div className="project-dialog-head">
        <Dialog.Title className="project-dialog-title">
          <TriangleAlert size={14} aria-hidden="true" /> 清理预览
        </Dialog.Title>
        <Dialog.Close asChild>
          <button className="icon-btn project-dialog-close" aria-label="关闭">
            <X size={14} />
          </button>
        </Dialog.Close>
      </div>

      {result ? (
        <CleanupReceipt result={result} />
      ) : loadError ? (
        <div className="project-error" role="alert">
          {loadError}
        </div>
      ) : preview === null ? (
        <div className="project-dialog-desc" role="status">
          正在读取清理预览…
        </div>
      ) : (
        <PreviewBody preview={preview} selected={selected} onToggle={toggle} />
      )}

      {confirmError && (
        <div className="project-error" role="alert">
          {confirmError}
        </div>
      )}

      <div className="project-dialog-actions">
        {result ? (
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
              className="project-btn project-btn-danger"
              onClick={() => void confirm()}
              disabled={pending || preview === null || selectedCount === 0}
            >
              {pending ? '正在清理…' : `确认清理 ${selectedCount} 个原件`}
            </button>
          </>
        )}
      </div>
    </Dialog.Content>
  );
}

function PreviewBody({
  preview,
  selected,
  onToggle,
}: {
  preview: CleanupPreview;
  selected: Set<string>;
  onToggle: (ref: string) => void;
}) {
  return (
    <>
      <Dialog.Description asChild>
        <div className="project-dialog-desc">
          将删除下列<strong>可回收原件</strong>（<strong>不可恢复</strong>）。被引用 / 有在途任务的
          项已被挡下，不在此列。
        </div>
      </Dialog.Description>

      <div className="cleanup-summary">
        <span>可回收 {formatBytes(preview.reclaimable_bytes)}</span>
        <span className="task-review-hint">共 {preview.affected.length} 个可清理原件</span>
      </div>

      {preview.evidence_invalidated.length > 0 && (
        <div className="project-error" role="alert">
          将有 {preview.evidence_invalidated.length} 条证据失去可回读原件。
        </div>
      )}

      {preview.affected.length === 0 ? (
        <div className="project-dialog-desc">没有可清理的原件。</div>
      ) : (
        <ul className="cleanup-list">
          {preview.affected.map((a) => (
            <li key={a.artifact_ref} className="cleanup-item">
              <label className="cleanup-item-label">
                <input
                  type="checkbox"
                  checked={selected.has(a.artifact_ref)}
                  onChange={() => onToggle(a.artifact_ref)}
                />
                <code title={a.artifact_ref}>{shortRef(a.artifact_ref)}</code>
                <span className="cleanup-size">{formatBytes(a.size)}</span>
              </label>
              {a.referenced_by.length > 0 && (
                <span className="task-review-hint">
                  被 {a.referenced_by.length} 处引用：{a.referenced_by.map(shortRef).join('、')}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}

      {preview.blocked.length > 0 && (
        <div className="cleanup-blocked">
          <div className="task-review-axis-label">已挡下（{preview.blocked.length}）</div>
          <ul className="cleanup-list">
            {preview.blocked.map((b) => (
              <li key={b.artifact_ref} className="cleanup-item">
                <code title={b.artifact_ref}>{shortRef(b.artifact_ref)}</code>
                <span className="task-review-tag warn">{blockReasonLabel(b.reason)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}

/** 回执：deleted / failed / not_deleted **分列**——有未删项时逐条明示，绝不合并成
 *  一句"完成"。 */
function CleanupReceipt({ result }: { result: CleanupResult }) {
  return (
    <div className="cleanup-receipt" role="status">
      <div className="project-dialog-done">已删除 {result.deleted.length} 个原件。</div>
      {result.failed.length > 0 && (
        <div className="project-error" role="alert">
          未能删除 {result.failed.length} 个原件：{result.failed.map(shortRef).join('、')}
        </div>
      )}
      {result.not_deleted.length > 0 && (
        <div className="project-error" role="alert">
          {result.not_deleted.length} 个原件未删除：
          <ul className="project-dialog-list">
            {result.not_deleted.map((n) => (
              <li key={n.artifact_ref}>
                <code title={n.artifact_ref}>{shortRef(n.artifact_ref)}</code>：
                {blockReasonLabel(n.reason)}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
