/** #353 W-09 任务 diff 与证据审阅界面（选项 1「只呈现不合并」）。
 *
 *  设计权威：`docs/agents/353-research.md` §5（用户已批准选项 1 与 A–F 调整）。
 *  本组件只做两件事：**如实渲染两个服务端投影** + **固定文案消歧**——不合并
 *  `product_state` 与 `freshness`、不自己判"可交付"、不猜 UNKNOWN（不变量 #22）。
 *
 *  回答六件事（→ 区块）：
 *  ① 原目标 / 读写意图 / cwd / 验收项（origin + confirmed）；② 两轴状态：Run 轴
 *  （在途 run）与交付轴（四态 chip）分两区，不混排；③ 逐项结果：六态 verification
 *  + 每条 evidence 的 result + 服务端 freshness；无 evidence = 缺证据；④ 改了哪些
 *  文件 + git diff + 快照（记录时 manifest）vs 当前（工作区现状），一致性只引用
 *  服务端 freshness reasons；⑤ 失败尝试（失败验证项）+ UNKNOWN 留槽不推断；
 *  ⑥ 三操作各自后果：接受 / 带原因接受 / 释放目录（撤销接受可选）。
 *
 *  暂存 / 提交**不做**（那是用户显式调用原有能力的事，不进本组件）。
 *
 *  语义独立于 `ApprovalCard`：本控件是 CAS 裁决（task acceptance），**不复用**
 *  permission allow-once 语义，也不 import `postApproval`。 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { X } from 'lucide-react';
import {
  executeCleanup,
  getSessionUsage,
  previewCleanup,
  type EvidenceRecord,
  type ProductState,
  type SessionUsage,
  type VerificationValue,
} from '../lib/api';
import { formatBytes } from '../lib/format';
import { useTaskReview } from '../hooks/useTaskReview';
import { CleanupPreviewDialog } from './CleanupPreviewDialog';

/** 交付四态（后端 `PRODUCT_STATES`）中文映射——缺省刻意为空串（未定义任务）。 */
const PRODUCT_LABEL: Record<Exclude<ProductState, ''>, string> = {
  executing: '执行中',
  pending_verification: '待验证',
  deliverable: '可交付',
  accepted: '已接受',
};

/** 逐项验证六态（后端 `VERIFICATION_VALUES`）中文映射。 */
const VERIFICATION_LABEL: Record<VerificationValue, string> = {
  not_started: '未开始',
  in_progress: '进行中',
  passed: '通过',
  failed: '失败',
  blocked: '受阻',
  incomplete: '未完成',
};

/** 证据三态（后端 `EVIDENCE_RESULTS`）中文映射。 */
const RESULT_LABEL: Record<EvidenceRecord['result'], string> = {
  pass: '通过',
  fail: '失败',
  blocked: '受阻',
};

const ORIGIN_LABEL: Record<'user' | 'agent', string> = { user: '用户', agent: 'Agent' };

/** 最小的 unified-diff 文本渲染（只读）。既有 `DiffBlock` 要 `{before, after}` 对、
 *  `ChangesPanel` 要 `ToolCall[]`，都与 `git diff` 的 unified 正文不对口，故这里写
 *  一个只读文本渲染，不扭曲既有组件的 props（票面允许）。 */
function UnifiedDiffText({ text }: { text: string }) {
  if (!text.trim()) {
    return <div className="task-review-hint">工作区无差异（git diff 为空）。</div>;
  }
  return (
    <pre className="task-review-diff" aria-label="unified diff">
      {text.split('\n').map((line, i) => {
        const cls =
          line.startsWith('+') && !line.startsWith('+++')
            ? 'diff-add'
            : line.startsWith('-') && !line.startsWith('---')
              ? 'diff-del'
              : line.startsWith('@@')
                ? 'diff-hunk'
                : '';
        return (
          <span key={i} className={cls || undefined}>
            {line}
            {'\n'}
          </span>
        );
      })}
    </pre>
  );
}

/** 收集所有证据记录（跨验收项），用于快照与一致性展示。 */
function allRecords(evidence: Record<string, EvidenceRecord[]>): EvidenceRecord[] {
  return Object.values(evidence).flat();
}

function shortSha(sha: string): string {
  return sha.length > 12 ? `${sha.slice(0, 12)}…` : sha;
}

export function TaskReviewPanel({
  sessionId,
  open,
  onClose,
  onJumpToEvent,
}: {
  sessionId: string;
  open: boolean;
  onClose: () => void;
  /** P1-2：票面①"点开可见原事件来源"——证据 source_event_seq 可点跳回原事件。 */
  onJumpToEvent?: (seq: number) => void;
}) {
  const review = useTaskReview(open ? sessionId : null);
  const [gapReason, setGapReason] = useState('');

  // #368 W-24：存储占用（只读）+ 清理预览浮层。占用随面板打开 / 清理成功后重拉。
  const [usage, setUsage] = useState<SessionUsage | null>(null);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [cleanupOpen, setCleanupOpen] = useState(false);

  const loadUsage = useCallback(async () => {
    try {
      setUsage(await getSessionUsage(sessionId));
      setUsageError(null);
    } catch (e) {
      setUsageError(e instanceof Error ? e.message : '读取存储占用失败');
    }
  }, [sessionId]);

  useEffect(() => {
    if (open) void loadUsage();
  }, [open, loadUsage]);

  // 稳定引用：CleanupPreviewDialog 打开时只拉一次 preview，父组件重渲染不重拉、不重置勾选。
  // 勾选变化时浮层带当前勾选集合重取（方案 a），拿与 execute 一致的新 token。
  const handlePreview = useCallback(
    (sid: string, selectedRefs?: string[]) => previewCleanup(sid, 'unreferenced', selectedRefs),
    [],
  );
  const handleConfirm = useCallback(
    async (sid: string, token: string, refs: string[]) => {
      const result = await executeCleanup(sid, token, refs);
      await loadUsage();
      return result;
    },
    [loadUsage],
  );

  // Esc 关闭（可键盘达；弹层惯例同 ContextUsagePanel）。
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const records = useMemo(() => allRecords(review.evidence), [review.evidence]);
  // 一致性结论**只**引用服务端 freshness reasons（前端不自己算 hash、不做合并判定）。
  const staleReasons = useMemo(
    () => records.flatMap((r) => r.freshness.reasons),
    [records],
  );
  const snapshotFiles = useMemo(() => {
    const seen = new Map<string, string>();
    for (const r of records) {
      for (const f of r.workspace_manifest.files) {
        if (!seen.has(f.path)) seen.set(f.path, f.sha256);
      }
    }
    return [...seen.entries()];
  }, [records]);

  if (!open) return null;

  const task = review.task;
  const productLabel =
    task && task.product_state !== '' ? PRODUCT_LABEL[task.product_state] : null;
  const readWriteIntent = task?.read_write_intent?.trim() || null;
  const authorization = task?.authorization?.trim() || null;

  // 消歧（选项 1-C）：chip 文案**不改**，只在证据 stale / 缺证据时叠加醒目警示与固定文案。
  // P1-1：证据加载失败（不可得）时不计入缺证据/过期——不可得≠缺证据，不触发消歧。
  const evidenceOk = !review.evidenceError;
  const missingEvidence =
    evidenceOk && task !== null && task.criteria.some((c) => !(review.evidence[c.item_id]?.length > 0));
  const hasStale = evidenceOk && records.some((r) => r.freshness.status === 'stale');
  const disambiguate = task !== null && task.product_state === 'deliverable' && (hasStale || missingEvidence);

  const failedItems =
    task?.criteria.filter((c) => task.verification[c.item_id]?.value === 'failed') ?? [];

  /** P2-3：下一步 = 未通过（failed）或缺证据的验收项（P1-1：证据不可得时不计入缺证据）。 */
  const nextStepItems =
    task?.criteria.filter((c) => {
      if (task.verification[c.item_id]?.value === 'failed') return true;
      if (evidenceOk && !(review.evidence[c.item_id]?.length > 0)) return true;
      return false;
    }) ?? [];

  const gitStatusOut = review.gitStatus;
  const gitStatusLines =
    gitStatusOut && gitStatusOut.stdout.trim()
      ? gitStatusOut.stdout.trim().split('\n')
      : [];

  return (
    <div className="ctx-usage-overlay" onClick={onClose}>
      <div
        className="ctx-usage-popover task-review-panel"
        role="dialog"
        aria-label="详细审阅"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="ctx-usage-head">
          <span className="ctx-usage-title">详细审阅</span>
          <button className="ctx-usage-close" onClick={onClose} aria-label="关闭">
            <X size={14} />
          </button>
        </div>

        {/* 操作回执 / 错误：aria-live 如实播报，不与加载错误混排。 */}
        {review.operationNotice && (
          <div className="task-review-notice" role="status" aria-live="polite">
            {review.operationNotice}
          </div>
        )}
        {review.leaseNotice && (
          <div className="task-review-notice" role="status" aria-live="polite">
            {review.leaseNotice}
          </div>
        )}
        {review.operationError && (
          <div className="task-review-error" role="alert">
            {review.operationError}
          </div>
        )}

        {review.error && <div className="task-review-error">加载失败：{review.error}</div>}

        {!review.error && review.notDefined && (
          <div className="task-review-empty">
            此会话尚未建立任务，暂无可审阅详情。
          </div>
        )}

        {!review.error && !review.notDefined && review.loading && task === null && (
          <div className="task-review-empty">加载中…</div>
        )}

        {!review.error && !review.notDefined && task !== null && (
          <>
            {/* ① 原目标 / 约束 / 验收项 */}
            <section className="task-review-section" aria-label="原目标">
              <h3 className="task-review-h">原目标</h3>
              <pre className="task-review-task">{task.task_text || '（空）'}</pre>
              <dl className="task-review-meta">
                {(readWriteIntent || authorization) && (
                  <>
                    <dt>读写意图</dt>
                    <dd>{readWriteIntent ?? '未声明'}</dd>
                    <dt>授权档位</dt>
                    <dd>{authorization ?? '未声明'}</dd>
                  </>
                )}
                <dt>工作目录</dt>
                <dd>{task.cwd ?? '—'}</dd>
              </dl>
              {!readWriteIntent && !authorization && (
                <div className="task-review-meta-empty">读写意图与授权档位未声明</div>
              )}
              {task.criteria.length > 0 && (
                <>
                  <h3 className="task-review-h">验收项（{task.criteria.length}）</h3>
                  <ul className="task-review-criteria">
                    {task.criteria.map((c) => (
                      <li key={c.item_id} className="task-review-criterion">
                        <span className="task-review-criterion-text">{c.text}</span>
                        <span className="task-review-tag">{ORIGIN_LABEL[c.origin]}</span>
                        <span className={`task-review-tag ${c.confirmed ? 'ok' : 'warn'}`}>
                          {c.confirmed ? '已确认' : '未确认'}
                        </span>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </section>

            <section className="task-review-section" aria-label="状态">
              <h3 className="task-review-h">状态</h3>
              <div className="task-review-axes">
                <div className="task-review-axis" aria-label="工作进度">
                  <span className="task-review-axis-label">工作进度</span>
                  {task.open_run_ids.length > 0 ? (
                    <span className="task-review-run-axis running">
                      正在处理中（{task.open_run_ids.length} 项操作）
                    </span>
                  ) : (
                    <span className="task-review-run-axis idle">当前没有进行中的操作</span>
                  )}
                </div>
                <div className="task-review-axis" aria-label="交付情况">
                  <span className="task-review-axis-label">交付情况</span>
                  {productLabel ? (
                    <span className={`task-review-chip state-${task.product_state}`}>
                      {productLabel}
                    </span>
                  ) : (
                    <span className="task-review-chip">—</span>
                  )}
                </div>
              </div>
              {disambiguate && (
                <div className="task-review-disambiguation" role="status">
                  <strong>验收结果与证据状态不一致。</strong>
                  <div>验收项状态：可交付；证据状态：{hasStale ? '已过期' : '有项目缺少证据'}。</div>
                  <div className="task-review-hint">请逐项核对后再接受。</div>
                </div>
              )}
            </section>

            {task.criteria.length > 0 && (
            <section className="task-review-section" aria-label="逐项结果">
              <h3 className="task-review-h">逐项结果</h3>
              {review.evidenceError ? (
                <div className="task-review-evidence-unavailable" role="alert">
                  证据不可得：{review.evidenceError}。逐项证据暂无法展示，这不代表"缺证据"。
                </div>
              ) : task.criteria.length === 0 ? (
                <div className="task-review-hint">无验收项，无可展示的逐项结果。</div>
              ) : (
                task.criteria.map((c) => {
                  const v = task.verification[c.item_id];
                  const recs = review.evidence[c.item_id] ?? [];
                  return (
                    <div className="task-review-result" key={c.item_id}>
                      <div className="task-review-result-head">
                        <span className="task-review-criterion-text">{c.text}</span>
                        <span className={`task-review-verification v-${v?.value ?? 'not_started'}`}>
                          {v ? VERIFICATION_LABEL[v.value] : '未开始'}
                        </span>
                      </div>
                      {v?.evidence && (
                        <div className="task-review-hint">验证依据（自由文本）：{v.evidence}</div>
                      )}
                      {recs.length === 0 ? (
                        <div className="task-review-missing-evidence">缺证据</div>
                      ) : (
                        <ul className="task-review-evidence">
                          {recs.map((r) => (
                            <li key={r.evidence_id} className="task-review-evidence-item">
                              <span className="task-review-tag">{r.kind}</span>
                              <span className={`task-review-result-tag r-${r.result}`}>
                                {RESULT_LABEL[r.result]}
                              </span>
                              <span
                                className={`task-review-freshness f-${r.freshness.status}`}
                              >
                                证据新鲜度：{r.freshness.status === 'stale' ? '已过期' : '新鲜'}
                              </span>
                              {r.command_or_action && (
                                <code className="task-review-evidence-action">{r.command_or_action}</code>
                              )}
                              {r.source_event_seq !== null && (
                                <button
                                  type="button"
                                  className="task-review-evidence-seq"
                                  title="跳转到原事件"
                                  onClick={() => onJumpToEvent?.(r.source_event_seq as number)}
                                >
                                  来源事件 #{r.source_event_seq}
                                </button>
                              )}
                              {r.freshness.reasons.length > 0 && (
                                <ul className="task-review-freshness-reasons">
                                  {r.freshness.reasons.map((reason, i) => (
                                    <li key={i}>{reason}</li>
                                  ))}
                                </ul>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  );
                })
              )}
            </section>
            )}

            {/* ④ 改了哪些文件 + diff + 快照 vs 当前 */}
            <section className="task-review-section" aria-label="文件与差异">
              <h3 className="task-review-h">改了哪些文件</h3>
              {gitStatusOut === null ? (
                <div className="task-review-hint">工作区 git 状态不可得。</div>
              ) : gitStatusOut.exit_code !== 0 ? (
                <div className="task-review-hint">
                  git status 非零退出（{gitStatusOut.exit_code}）：{gitStatusOut.stderr || '（无 stderr）'}
                </div>
              ) : gitStatusLines.length === 0 ? (
                <div className="task-review-hint">工作区无改动文件。</div>
              ) : (
                <ul className="task-review-files">
                  {gitStatusLines.map((line, i) => (
                    <li key={i}>
                      <code>{line}</code>
                    </li>
                  ))}
                </ul>
              )}

              <h3 className="task-review-h">当前 diff</h3>
              {review.gitDiff === null ? (
                <div className="task-review-hint">工作区 diff 不可得。</div>
              ) : review.gitDiff.exit_code !== 0 ? (
                <div className="task-review-hint">
                  git diff 非零退出（{review.gitDiff.exit_code}）：{review.gitDiff.stderr || '（无 stderr）'}
                </div>
              ) : (
                <UnifiedDiffText text={review.gitDiff.stdout} />
              )}
              <div className="task-review-hint">
                暂存 / 提交不在本面板提供——那是用户显式调用原有能力的事。
              </div>

              <h3 className="task-review-h">快照 vs 当前</h3>
              <div className="task-review-snapshot">
                <div className="task-review-snapshot-col">
                  <div className="task-review-axis-label">快照（证据记录时的覆盖清单）</div>
                  {snapshotFiles.length === 0 ? (
                    <div className="task-review-hint">无证据记录快照。</div>
                  ) : (
                    <ul className="task-review-files">
                      {snapshotFiles.map(([path, sha]) => (
                        <li key={path}>
                          <code>{path}</code>
                          <span className="task-review-sha">{shortSha(sha)}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <div className="task-review-snapshot-col">
                  <div className="task-review-axis-label">当前（工作区现状）</div>
                  <div className="task-review-hint">
                    以上方 `git status` / `git diff` 为准（本面板不预览文件内容）。
                  </div>
                </div>
              </div>
              <div className="task-review-consistency">
                <div className="task-review-axis-label">一致性结论（仅引用服务端 freshness reasons）</div>
                {staleReasons.length === 0 ? (
                  <div className="task-review-hint">服务端未报告证据陈旧原因。</div>
                ) : (
                  <ul className="task-review-freshness-reasons">
                    {staleReasons.map((reason, i) => (
                      <li key={i}>{reason}</li>
                    ))}
                  </ul>
                )}
              </div>
            </section>

            {failedItems.length > 0 && (
              <section className="task-review-section" aria-label="失败尝试">
                <h3 className="task-review-h">失败尝试</h3>
                <ul className="task-review-files">
                  {failedItems.map((c) => (
                    <li key={c.item_id}>
                      <code>{c.text}</code>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {nextStepItems.length > 0 && (
              <section className="task-review-section" aria-label="下一步">
                <h3 className="task-review-h">下一步</h3>
                <div className="task-review-hint">处理以下未通过或缺少证据的项目：</div>
                <ul className="task-review-files">
                  {nextStepItems.map((c) => (
                    <li key={c.item_id}>
                      <code>{c.text}</code>
                      <span className="task-review-tag warn">
                        {task.verification[c.item_id]?.value === 'failed' ? '未通过' : '缺证据'}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section className="task-review-section" aria-label="操作">
              <h3 className="task-review-h">操作</h3>
              <div className="task-review-ops">
                <div className="task-review-op">
                  <button
                    className="btn-primary"
                    disabled={review.busy}
                    onClick={() => void review.accept()}
                  >
                    接受
                  </button>
                  <p className="task-review-op-consequence">接受后锁定当前版本</p>
                </div>
                <details className="task-review-disclosure task-review-more-actions">
                  <summary>更多操作</summary>
                  <div className="task-review-disclosure-content">
                    <div className="task-review-op">
                      <label className="task-review-op-label" htmlFor="task-review-reason">
                        带原因接受（原因必填）
                      </label>
                      <textarea
                        id="task-review-reason"
                        className="task-review-reason"
                        value={gapReason}
                        onChange={(e) => setGapReason(e.target.value)}
                        rows={2}
                        placeholder="说明接受缺项的原因"
                      />
                      <button
                        className="btn-ghost"
                        disabled={review.busy}
                        onClick={() => void review.acceptWithGaps(gapReason)}
                      >
                        带原因接受
                      </button>
                      <p className="task-review-op-consequence">
                        接受缺少部分验收项；需要填写原因。
                      </p>
                    </div>
                    <div className="task-review-op">
                      <button
                        className="btn-ghost"
                        disabled={review.busy}
                        onClick={() => void review.releaseLease()}
                      >
                        释放工作目录
                      </button>
                      <p className="task-review-op-consequence">
                        让其他任务可以使用这个目录
                      </p>
                    </div>
                    {task.acceptance !== null && (
                      <div className="task-review-op">
                        <button
                          className="btn-ghost"
                          disabled={review.busy}
                          onClick={() => void review.releaseAcceptance()}
                        >
                          撤销接受
                        </button>
                        <p className="task-review-op-consequence">
                          撤销接受状态，不改变验收项结果。
                        </p>
                      </div>
                    )}
                    <div className="task-review-op">
                      <div className="task-review-usage" aria-label="存储占用">
                        <span className="task-review-axis-label">存储占用</span>
                        {usageError ? (
                          <span className="task-review-hint">存储占用不可得：{usageError}</span>
                        ) : usage === null ? (
                          <span className="task-review-hint">读取中…</span>
                        ) : (
                          <>
                            <span>
                              事件 {formatBytes(usage.events_bytes)} · 原件{' '}
                              {formatBytes(usage.artifacts_bytes)}（{usage.artifact_count} 个） · 进度{' '}
                              {formatBytes(usage.progress_bytes)}
                            </span>
                            <span className="task-review-hint">
                              可回收 {formatBytes(usage.reclaimable_bytes)}
                            </span>
                          </>
                        )}
                      </div>
                      <button className="btn-ghost" onClick={() => setCleanupOpen(true)}>
                        清理预览…
                      </button>
                      <p className="task-review-op-consequence">
                        预览会列出可清理的内容与受影响的证据；执行后无法恢复。
                        <strong>不可恢复</strong>。
                      </p>
                    </div>
                  </div>
                </details>
              </div>
            </section>
            <details className="task-review-disclosure task-review-diagnostic-disclosure">
              <summary>诊断信息</summary>
              <div className="task-review-disclosure-content task-review-diagnostics">
                {/* #1325 S3：按批准文案对照表删掉「交付投影 open_run_ids」一行——
                    普通用户不需要理解内部投影。reconcile / Operation Ledger 的排查
                    说明按对照表留在诊断区（默认收起）。 */}
                <div>需 reconcile（来源未接入）。UNKNOWN / reconcile 属 Operation Ledger 域，本票不接入也不推断。</div>
              </div>
            </details>
          </>
        )}
      </div>
      <CleanupPreviewDialog
        target={cleanupOpen ? { sessionId } : null}
        onOpenChange={setCleanupOpen}
        onPreview={handlePreview}
        onConfirm={handleConfirm}
      />
    </div>
  );
}
