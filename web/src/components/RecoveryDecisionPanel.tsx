/** RecoveryDecisionPanel（#357 W-13 契约 2/3/4）——把 UNKNOWN 摆给用户的**可行动选择题**。
 *
 *  设计权威：`docs/agents/357-research.md` §9（修订 A）。这一层只做「把后端
 *  `pending_decisions` 摆出来 + 收集用户裁决」，不发明任何裁决事实：
 *  - 默认选中项 = 后端 `default_action`（`RETRY`/`DEFER`，安全侧）；
 *  - probe 文案 = 后端 `probe` 逐字（工具既有 `ReconcileHint`），查不到就写「未查到」；
 *  - 提交走调用方的 `onSubmit`（App 接到 useSession 的 `submitDecisions`，同一
 *    #547 合同、同一 token-CAS 链——不造第二套端点，不变量 #22）。
 *
 *  键盘可达：三选一是**原生 radio**（Tab 进组、方向键切换、`aria-checked` 隐式如实）；
 *  Esc 触发 `onClose`。 */

import { useState } from 'react';
import { AlertTriangle, X } from 'lucide-react';
import type { PendingDecision, RecoverDecisionInput } from '../lib/api';
import {
  assembleDecisions,
  DECISION_OPTIONS,
  humanizeToolName,
  initialDrafts,
  probeFact,
  RECOVERY_SOURCE_MAX,
  SOURCE_PRESETS,
  type DecisionDraft,
} from '../lib/recovery';
import '../styles/recovery.css';

interface Props {
  decisions: PendingDecision[];
  /** true = 提交在途（禁用提交/批量，不清空已有选择）。 */
  submitting: boolean;
  /** 后端 409 的给人看的原因（展示用，不参与裁决语义）。 */
  message: string | null;
  onSubmit: (payload: RecoverDecisionInput[]) => void;
  /** Esc 关闭面板。 */
  onClose: () => void;
}

/** Ledger 非终态的可读标签（只读展示，不参与裁决）。 */
const STATE_LABELS: Record<PendingDecision['state'], string> = {
  RUNNING: '运行中（结果未落库）',
  UNKNOWN: '结果未知',
  NEED_RECONCILE: '待对账',
};

export function RecoveryDecisionPanel({ decisions, submitting, message, onSubmit, onClose }: Props) {
  // App 以 decisions 的键集为 `key` 重挂本组件，故初值直接取自 props 安全。
  const [drafts, setDrafts] = useState<Record<string, DecisionDraft>>(() => initialDrafts(decisions));
  const [error, setError] = useState<string | null>(null);

  const setVerdict = (id: string, verdict: DecisionDraft['verdict']) => {
    setError(null);
    setDrafts((prev) => ({ ...prev, [id]: { ...prev[id], verdict } }));
  };
  const setSource = (id: string, patch: Partial<DecisionDraft>) => {
    setError(null);
    setDrafts((prev) => ({ ...prev, [id]: { ...prev[id], ...patch } }));
  };

  const submit = () => {
    try {
      onSubmit(assembleDecisions(decisions, drafts));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <section
      className="recovery-decision-panel"
      role="region"
      aria-labelledby="recovery-decision-title"
      onKeyDown={(event) => {
        if (event.key === 'Escape') onClose();
      }}
    >
      <header className="recovery-decision-head">
        <AlertTriangle size={16} className="recovery-decision-icon" aria-hidden="true" />
        <h3 className="recovery-decision-title" id="recovery-decision-title">
          有 {decisions.length} 个操作的结果不确定——不知道它们到底生没生效。
        </h3>
        <button
          type="button"
          className="recovery-decision-close"
          aria-label="关闭裁决面板"
          onClick={onClose}
        >
          <X size={14} />
        </button>
      </header>

      {message && <p className="recovery-decision-message">{message}</p>}

      <ol className="recovery-cards">
        {decisions.map((decision) => {
          const draft = drafts[decision.tool_call_id];
          const fact = probeFact(decision.probe);
          return (
            <li className="recovery-card" key={decision.tool_call_id}>
              <div className="recovery-card-head">
                <span className="recovery-tool-name">{humanizeToolName(decision.tool_name)}</span>
                <span className="recovery-card-state">
                  最后已知状态：{STATE_LABELS[decision.state]}
                </span>
              </div>
              <p className="recovery-args">
                参数摘要：
                <span className="recovery-args-value" title="后端未在待裁决清单中提供参数摘要">—</span>
              </p>
              <p className="recovery-probe">
                <span
                  className={`recovery-probe-state recovery-probe-state--${
                    fact.state === '已查到' ? 'found' : 'missing'
                  }`}
                >
                  {fact.state}
                </span>
                {fact.detail && <span className="recovery-probe-detail">{fact.detail}</span>}
              </p>

              <div
                className="recovery-options"
                role="radiogroup"
                aria-label={`${humanizeToolName(decision.tool_name)} 的处理方式`}
              >
                {DECISION_OPTIONS.map((option) => (
                  <label className="recovery-option" key={option.verdict}>
                    <input
                      type="radio"
                      name={`recovery-${decision.tool_call_id}`}
                      value={option.verdict}
                      checked={draft.verdict === option.verdict}
                      disabled={submitting}
                      onChange={() => setVerdict(decision.tool_call_id, option.verdict)}
                    />
                    <span className="recovery-option-text">
                      <span className="recovery-option-label">{option.label}</span>
                      <span className="recovery-option-help">{option.help}</span>
                    </span>
                  </label>
                ))}
              </div>

              {draft.verdict === 'CONFIRM_SUCCESS' && (
                <div className="recovery-source">
                  <label className="recovery-source-field">
                    <span>来源（可选）</span>
                    <select
                      className="recovery-source-choice"
                      value={draft.sourceChoice}
                      disabled={submitting}
                      onChange={(event) => setSource(decision.tool_call_id, { sourceChoice: event.target.value })}
                    >
                      <option value="">不填写</option>
                      {SOURCE_PRESETS.map((preset) => (
                        <option value={preset} key={preset}>{preset}</option>
                      ))}
                    </select>
                  </label>
                  <label className="recovery-source-field">
                    <span>或补充说明</span>
                    <input
                      className="recovery-source-custom"
                      type="text"
                      value={draft.sourceCustom}
                      disabled={submitting}
                      placeholder="例如：查了数据库 / 文件 / 外部系统"
                      onChange={(event) => setSource(decision.tool_call_id, { sourceCustom: event.target.value })}
                      onKeyDown={(event) => {
                        // 输入框内按 Esc 只收起输入焦点，不关闭面板，避免丢掉已输入的来源说明
                        if (event.key === 'Escape') {
                          event.stopPropagation();
                          event.currentTarget.blur();
                        }
                      }}
                    />
                  </label>
                  <p className="recovery-source-hint">最多 {RECOVERY_SOURCE_MAX} 个字符；来源可选，不写也不影响裁决。</p>
                </div>
              )}
            </li>
          );
        })}
      </ol>

      {error && <p className="recovery-decision-error" role="alert">{error}</p>}

      <div className="recovery-actions">
        <button
          type="button"
          className="recovery-batch-btn"
          disabled={submitting}
          onClick={() => {
            setDrafts(initialDrafts(decisions));
            setError(null);
          }}
        >
          全部按默认安全动作处理
        </button>
        <button
          type="button"
          className="recovery-submit-btn"
          disabled={submitting}
          onClick={submit}
        >
          {submitting ? '提交中…' : '提交裁决'}
        </button>
      </div>
    </section>
  );
}
