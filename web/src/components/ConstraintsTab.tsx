/** ConstraintsTab — Session Inspector「约束管理」页（#1190 / #1176 Phase 1）.
 *
 * 数据来自后端实时投影（`GET /api/sessions/{id}/protected-facts`），**前端不维护
 * 第二套状态**（不变量 #22）：每次挂载重拉，撤销后重拉对齐，状态/类型筛选纯粹是
 * 展示层过滤。
 *
 * 结构（对照 issue §四 / 效果图 v2）：页眉（eyebrow + 标题）→ 会话行 → 筛选条
 * （状态 pill-tabs 默认「生效中」+ 类型下拉）→ 列表行（类型圆点 / 值 3 行折叠 /
 * 元信息行 / 状态 pill + 撤销按钮）→ 页脚注 → 撤销二次确认弹窗。
 *
 * 纯展示子组件 `ConstraintsView` 从数据 props 驱动（SSR 可测）；有状态外壳
 * `ConstraintsTab` 负责请求生命周期。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import type { ProtectedFact, ProtectedFactStatus } from '../lib/api';
import {
  SessionError,
  UnauthorizedError,
  listProtectedFacts,
  revokeProtectedFact,
} from '../lib/api';
import { formatFactTime, operatorLabel } from '../lib/constraints';
import '../styles/constraints.css';

/** 状态筛选 tab：`revoked` 覆盖 revoked + superseded（issue §四.3）。 */
type StatusFilter = 'all' | 'active' | 'revoked';

/** 类型圆点固定色（issue §四.4）；未知类型回落到 ink-subtle 灰。 */
const TYPE_DOT: Record<string, string> = {
  constraint: 'var(--cn-type-constraint)',
  user_goal: 'var(--cn-type-user-goal)',
  authorization: 'var(--cn-type-authorization)',
  work_boundary: 'var(--cn-type-work-boundary)',
  failed_approach: 'var(--cn-type-failed-approach)',
  evidence_ref: 'var(--cn-type-evidence-ref)',
};

/** 类型下拉的 6 个具体类型（issue §四.3）；「全部类型」是另立的空值 option，
 *  两者合计 = 票面所称的「下拉 7 值」。 */
const TYPE_OPTIONS: readonly string[] = [
  'constraint',
  'user_goal',
  'authorization',
  'work_boundary',
  'failed_approach',
  'evidence_ref',
];

const STATUS_TABS: readonly { id: StatusFilter; label: string }[] = [
  { id: 'all', label: '全部' },
  { id: 'active', label: '生效中' },
  { id: 'revoked', label: '已撤销' },
];

function statusLabel(status: ProtectedFactStatus): string {
  if (status === 'active') return '生效中';
  if (status === 'revoked') return '已撤销';
  return '已被取代';
}

/** 行内状态 pill 的点色：生效中→绿，其余→灰（issue §四.4）。 */
function statusDot(status: ProtectedFactStatus): string {
  return status === 'active' ? 'var(--cn-dot-active)' : 'var(--cn-dot-inactive)';
}

function matchesStatus(status: ProtectedFactStatus, filter: StatusFilter): boolean {
  if (filter === 'all') return true;
  if (filter === 'active') return status === 'active';
  return status === 'revoked' || status === 'superseded';
}

/** 值渲染：字符串原样；结构化值 pretty JSON（不截断——折叠在视图层）。 */
function renderValue(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** 值块：超过 3 行折叠，点击展开/收起（量 overflow 决定是否给开关）。 */
function CollapsibleValue({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setOverflows(el.scrollHeight > el.clientHeight + 1);
  }, [text]);
  return (
    <div className="cn-value-wrap">
      <div ref={ref} className={`cn-value${expanded ? ' expanded' : ''}`}>
        {text}
      </div>
      {overflows && (
        <button
          type="button"
          className="cn-value-toggle"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? '收起' : '展开'}
        </button>
      )}
    </div>
  );
}

function FactRow({
  fact,
  onRequestRevoke,
}: {
  fact: ProtectedFact;
  onRequestRevoke: (fact: ProtectedFact) => void;
}) {
  const revoked = fact.status === 'revoked';
  const superseded = fact.status === 'superseded';
  const inactive = revoked || superseded;
  const dot = TYPE_DOT[fact.type] ?? 'var(--cn-type-default)';
  return (
    <div className={`cn-row${inactive ? ' revoked' : ''}`} data-status={fact.status}>
      <div className="cn-row-main">
        <div className="cn-kicker">
          <span className="cn-type">
            <span className="cn-tdot" style={{ background: dot }} aria-hidden="true" />
            {fact.type}
          </span>
        </div>
        <CollapsibleValue text={renderValue(fact.value)} />
        <div className="cn-meta">
          <code className="cn-fact-id">{fact.fact_id}</code>
          {fact.registered_at && <span>{formatFactTime(fact.registered_at)} 登记</span>}
          <span>来源事件 #seq {fact.source_seq}</span>
          {fact.revoked_at && (
            <span>
              {formatFactTime(fact.revoked_at)} 撤销
              {fact.revoked_by ? ` · ${operatorLabel(fact.revoked_by)}` : ''}
            </span>
          )}
        </div>
      </div>
      <div className="cn-row-side">
        <span className="cn-pill">
          <span className="cn-pill-dot" style={{ background: statusDot(fact.status) }} aria-hidden="true" />
          {statusLabel(fact.status)}
        </span>
        <button
          type="button"
          className="cn-revoke"
          disabled={inactive}
          onClick={() => onRequestRevoke(fact)}
        >
          {inactive ? statusLabel(fact.status) : '撤销'}
        </button>
      </div>
    </div>
  );
}

/** 纯展示视图：全部交互回调由外壳注入（SSR / 单测直接喂 props）。 */
export function ConstraintsView({
  sessionId,
  facts,
  loading,
  error,
  forbidden,
  statusFilter,
  typeFilter,
  onStatusFilter,
  onTypeFilter,
  onRetry,
  onRequestRevoke,
}: {
  sessionId: string;
  facts: ProtectedFact[];
  loading: boolean;
  error: string | null;
  forbidden: boolean;
  statusFilter: StatusFilter;
  typeFilter: string;
  onStatusFilter: (filter: StatusFilter) => void;
  onTypeFilter: (type: string) => void;
  onRetry: () => void;
  onRequestRevoke: (fact: ProtectedFact) => void;
}) {
  const visible = facts.filter(
    (fact) => matchesStatus(fact.status, statusFilter) && (!typeFilter || fact.type === typeFilter),
  );
  const countFor = (filter: StatusFilter) =>
    facts.filter((fact) => matchesStatus(fact.status, filter)).length;

  let body;
  if (forbidden) {
    body = (
      <div className="cn-state cn-forbidden" role="alert">
        无权限查看约束
      </div>
    );
  } else if (error) {
    body = (
      <div className="cn-state cn-error" role="alert">
        <span>加载失败，请重试</span>
        <span className="cn-error-detail">{error}</span>
        <button type="button" className="cn-retry" onClick={onRetry}>
          重试
        </button>
      </div>
    );
  } else if (loading && facts.length === 0) {
    body = <div className="cn-state cn-loading">正在加载约束…</div>;
  } else if (visible.length === 0) {
    body = <div className="cn-state cn-empty">没有符合条件的约束</div>;
  } else {
    body = visible.map((fact) => (
      <FactRow key={fact.fact_id} fact={fact} onRequestRevoke={onRequestRevoke} />
    ));
  }

  return (
    <div className="cn-panel" data-testid="constraints-panel">
      <div className="cn-eyebrow">CONSTRAINTS</div>
      <h2 className="cn-title">约束管理</h2>
      <div className="cn-session-line">
        <span className="cn-livedot" aria-hidden="true" />
        当前会话 <code className="cn-session-id">{sessionId}</code>
        <span className="cn-session-hint">数据来自后端实时查询，前端不存第二份状态</span>
      </div>

      <div className="cn-toolbar">
        <div className="cn-tabs" role="tablist" aria-label="约束状态筛选">
          {STATUS_TABS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={statusFilter === tab.id}
              className={`cn-tab${statusFilter === tab.id ? ' on' : ''}`}
              onClick={() => onStatusFilter(tab.id)}
            >
              {tab.label}
              <span className="cn-tab-count">{countFor(tab.id)}</span>
            </button>
          ))}
        </div>
        <select
          className="cn-type-select"
          aria-label="按类型筛选"
          value={typeFilter}
          onChange={(e) => onTypeFilter(e.target.value)}
        >
          <option value="">全部类型</option>
          {TYPE_OPTIONS.map((type) => (
            <option key={type} value={type}>
              {type}
            </option>
          ))}
        </select>
      </div>

      <div className="cn-list">{body}</div>

      <div className="cn-footer">撤销走 superseded 历史链 —— 来源记录保留，可审计、可追溯。</div>
    </div>
  );
}

/** 有状态外壳：负责请求生命周期 + 撤销二次确认。 */
export function ConstraintsTab({ sessionId }: { sessionId: string }) {
  const [facts, setFacts] = useState<ProtectedFact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('active');
  const [typeFilter, setTypeFilter] = useState('');
  const [pendingRevoke, setPendingRevoke] = useState<ProtectedFact | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await listProtectedFacts(sessionId);
      setFacts(next);
      setError(null);
      setForbidden(false);
    } catch (err) {
      if (err instanceof UnauthorizedError || (err instanceof SessionError && err.status === 403)) {
        setForbidden(true);
        setError(null);
      } else {
        // 不保留旧数据（不许用缓存冒充）：失败即清空，展示错误态。
        setFacts([]);
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const confirmRevoke = useCallback(async () => {
    if (!pendingRevoke) return;
    setRevoking(true);
    setRevokeError(null);
    try {
      await revokeProtectedFact(sessionId, pendingRevoke.fact_id);
      setPendingRevoke(null);
      // 撤销后重拉对齐——真相在后端，前端不做乐观本地更新（不变量 #22）。
      await load();
    } catch (err) {
      setRevokeError(err instanceof Error ? err.message : String(err));
    } finally {
      setRevoking(false);
    }
  }, [pendingRevoke, sessionId, load]);

  return (
    <>
      <ConstraintsView
        sessionId={sessionId}
        facts={facts}
        loading={loading}
        error={error}
        forbidden={forbidden}
        statusFilter={statusFilter}
        typeFilter={typeFilter}
        onStatusFilter={setStatusFilter}
        onTypeFilter={setTypeFilter}
        onRetry={() => void load()}
        onRequestRevoke={(fact) => {
          setRevokeError(null);
          setPendingRevoke(fact);
        }}
      />
      <Dialog.Root
        open={pendingRevoke !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPendingRevoke(null);
            setRevokeError(null);
          }
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="cn-modal-overlay" />
          <Dialog.Content className="cn-modal" aria-describedby="cn-modal-desc">
            <Dialog.Title className="cn-modal-title">撤销这条约束？</Dialog.Title>
            <p className="cn-modal-desc" id="cn-modal-desc">
              撤销后，新的会话将不再注入该条约束。来源记录保留，可审计。
            </p>
            {pendingRevoke && (
              <div className="cn-modal-quote">
                <span className="cn-modal-value">{renderValue(pendingRevoke.value)}</span>
                <code className="cn-modal-factid">{pendingRevoke.fact_id}</code>
              </div>
            )}
            {revokeError && (
              <div className="cn-modal-error" role="alert">
                {revokeError}
              </div>
            )}
            <div className="cn-modal-actions">
              <button
                type="button"
                className="cn-btn"
                onClick={() => setPendingRevoke(null)}
                disabled={revoking}
              >
                取消
              </button>
              <button
                type="button"
                className="cn-btn cn-btn-primary"
                onClick={() => void confirmRevoke()}
                disabled={revoking}
              >
                {revoking ? '撤销中…' : '确认撤销'}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}
