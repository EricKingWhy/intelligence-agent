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

import { useEffect, useMemo, useState } from 'react';
import { X } from 'lucide-react';
import type { EvidenceRecord, ProductState, VerificationValue } from '../lib/api';
import { useTaskReview } from '../hooks/useTaskReview';

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

  // 消歧（选项 1-C）：chip 文案**不改**，只在证据 stale / 缺证据时叠加醒目警示与固定文案。
  // P1-1：证据加载失败（不可得）时不计入缺证据/过期——不可得≠缺证据，不触发消歧。
  const evidenceOk = !review.evidenceError;
  const missingEvidence =
    evidenceOk && task !== null && task.criteria.some((c) => !(review.evidence[c.item_id]?.length > 0));
  const hasStale = evidenceOk && records.some((r) => r.freshness.status === 'stale');
  const disambiguate = task !== null && task.product_state === 'deliverable' && (hasStale || missingEvidence);

  const failedItems =
    task?.criteria.filter((c) => task.verification[c.item_id]?.value === 'failed') ?? [];

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
        aria-label="任务审阅"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="ctx-usage-head">
          <span className="ctx-usage-title">任务审阅</span>
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
            未定义任务：该会话尚无 `task/defined` 事件，没有可审阅的交付状态。
          </div>
        )}

        {!review.error && !review.notDefined && review.loading && task === null && (
          <div className="task-review-empty">加载中…</div>
        )}

        {!review.error && !review.notDefined && task !== null && (
          <>
            {/* ① 原目标 / 约束 / 验收项 */}
            <section className="task-review-section" aria-label="原目标与验收项">
              <h3 className="task-review-h">原目标</h3>
              <pre className="task-review-task">{task.task_text || '（空）'}</pre>
              <dl className="task-review-meta">
                <dt>读写意图</dt>
                <dd>{task.read_write_intent ?? '—'}</dd>
                <dt>工作目录</dt>
                <dd>{task.cwd ?? '—'}</dd>
                <dt>授权档位</dt>
                <dd>{task.authorization ?? '未声明'}</dd>
              </dl>
              <h3 className="task-review-h">验收项（{task.criteria.length}）</h3>
              {task.criteria.length === 0 ? (
                <div className="task-review-hint">尚无验收项（Agent 可稍后提出清单）。</div>
              ) : (
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
              )}
            </section>

            {/* ② 两轴状态（Run 轴 / 交付轴，分两区不混排） */}
            <section className="task-review-section" aria-label="状态（两轴）">
              <h3 className="task-review-h">状态</h3>
              <div className="task-review-axes">
                <div className="task-review-axis" aria-label="Run / 操作态">
                  <span className="task-review-axis-label">Run / 操作态</span>
                  {task.open_run_ids.length > 0 ? (
                    <span className="task-review-run-axis running">
                      执行中（{task.open_run_ids.length} 个在途 run）
                    </span>
                  ) : (
                    <span className="task-review-run-axis idle">无在途 run</span>
                  )}
                  <span className="task-review-hint">
                    在途 run 来自交付投影 `open_run_ids`；run 终态属事件流事实，本投影只报在途。
                  </span>
                </div>
                <div className="task-review-axis" aria-label="交付态">
                  <span className="task-review-axis-label">Task 交付态</span>
                  {productLabel ? (
                    <span className={`task-review-chip state-${task.product_state}`}>
                      {productLabel}
                    </span>
                  ) : (
                    <span className="task-review-chip">—</span>
                  )}
                  <span className="task-review-hint">
                    服务端判定依据：验收项验证值（不依据证据新鲜度）。
                  </span>
                </div>
              </div>
              {/* 选项 1-C：不合并、不改 chip 文案，只叠加醒目警示 + 固定消歧文案。 */}
              {disambiguate && (
                <div className="task-review-disambiguation" role="status">
                  <strong>注意：交付判定与证据新鲜度不一致。</strong>
                  <div>服务端判定：可交付（基于验收项验证）；证据新鲜度：{hasStale ? '已过期' : '存在缺证据项'}。</div>
                  <div className="task-review-hint">
                    这是两个独立事实的并列呈现，不是前端合并出的结论；请按下方逐项证据判断。
                  </div>
                </div>
              )}
            </section>

            {/* ③ 逐项结果 */}
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

            {/* ⑤ 失败尝试 + UNKNOWN 留槽 */}
            <section className="task-review-section" aria-label="失败尝试与待对账">
              <h3 className="task-review-h">失败尝试</h3>
              {failedItems.length === 0 ? (
                <div className="task-review-hint">投影中无失败的验收项。</div>
              ) : (
                <ul className="task-review-files">
                  {failedItems.map((c) => (
                    <li key={c.item_id}>
                      <code>{c.text}</code>
                    </li>
                  ))}
                </ul>
              )}
              <div className="task-review-unknown">
                需 reconcile（来源未接入）
                <span className="task-review-hint">
                  UNKNOWN / reconcile 属 Operation Ledger 域，本票不接入也不推断。
                </span>
              </div>
            </section>

            {/* ⑥ 三操作（各自后果分别说明；不与权限审批共用语义与文案） */}
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
                  <p className="task-review-op-consequence">
                    后果：服务端交付态变为「已接受」（CAS；版本取自当前投影）。若已接受过，重复请求按幂等处理。
                  </p>
                </div>
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
                    后果：接受但标注「带缺项」；服务端保留未通过 / 缺失的验收项。原因必填，客户端先校验，服务端仍会复核。
                  </p>
                </div>
                <div className="task-review-op">
                  <button
                    className="btn-ghost"
                    disabled={review.busy}
                    onClick={() => void review.releaseLease()}
                  >
                    释放目录
                  </button>
                  <p className="task-review-op-consequence">
                    后果：释放该目录的写租约（幂等；非持有者 `released:false` 会如实显示）。**这不是撤销接受**。
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
                      后果：撤销接受裁决（同样 CAS）；验收项的验证值保持不变。与「释放目录」是两个不同事实。
                    </p>
                  </div>
                )}
              </div>
            </section>
          </>
        )}
      </div>
    </div>
  );
}
