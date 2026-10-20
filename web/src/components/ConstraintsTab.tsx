/** ConstraintsTab — Session Inspector「约束管理」页（#1190 Phase 1 / #1191 Phase 2）.
 *
 * 数据来自后端实时投影（`GET /api/sessions/{id}/effective-constraints`），**前端不维护
 * 第二套状态**（不变量 #22）：每次挂载重拉，撤销后重拉对齐，状态/类型筛选纯粹是展示层
 * 过滤。
 *
 * 结构（对照 issue §五 效果图 v2）：页眉（eyebrow + 标题）→ 会话行 → 顶部优先级条 →
 * 筛选条（状态 pill-tabs 默认「生效中」+ 类型下拉）→ 三个 scope 组（SESSION 本会话 /
 * PROJECT 本项目 / GLOBAL 我的全局，每组眉标 + 计数）→ 组内列表行 → 页脚注 →
 * 撤销二次确认弹窗。
 *
 * #1191 增量：三组按 `scope` 分组渲染（后端已合流，前端不做二次计算）；项目组眉标带
 * `git 根 · owner/repo`；项目/全局组中被更高优先级覆盖的条目加「被本会话覆盖」标签；
 * 非 git 目录的会话不渲染 PROJECT 组；撤销按 `scope` 分派到会话级 / 规则层两个端点。
 *
 * 纯展示子组件 `ConstraintsView` 从数据 props 驱动（SSR 可测）；有状态外壳
 * `ConstraintsTab` 负责请求生命周期。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import type {
  ConstraintScope,
  EffectiveConstraintRow,
  EffectiveProjectMeta,
} from '../lib/api';
import {
  SessionError,
  UnauthorizedError,
  listEffectiveConstraints,
  revokeProtectedFact,
  revokeScopedConstraint,
} from '../lib/api';
import { FACT_TYPE_DOTS, SCOPE_LABELS, formatFactTime, operatorLabel } from '../lib/constraints';
import '../styles/constraints.css';

/** 状态筛选 tab：`revoked` 覆盖 revoked + superseded（issue §四.3）。 */
type StatusFilter = 'all' | 'active' | 'revoked';

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

/** 顶部优先级条文案（#1191 §五.1，逐字）。`overridden_by` 用等宽字体呈现，
 *  与票面用反引号标注代码标识符的意图一致。 */
const PRIORITY_BAR = '同样内容的要求，生效优先级：会话＞项目＞全局——不同内容会全部提供给 AI，由它决定怎么处理。';
const PRIORITY_BAR_TAIL = ' 标记的条目不生效。撤销后仍保留历史记录。';

/** 三组眉标（#1191 §五.2）。 */
const GROUPS: readonly {
  scope: ConstraintScope;
  en: string;
  zh: string;
}[] = [
  { scope: 'session', en: 'SESSION', zh: SCOPE_LABELS.session },
  { scope: 'project', en: 'PROJECT', zh: SCOPE_LABELS.project },
  { scope: 'global', en: 'GLOBAL', zh: SCOPE_LABELS.global },
];

function statusLabel(status: EffectiveConstraintRow['status']): string {
  if (status === 'active') return '生效中';
  if (status === 'revoked') return '已撤销';
  return '已被取代';
}

/** 行内状态 pill 的点色：生效中→绿，其余→灰（issue §四.4）。 */
function statusDot(status: EffectiveConstraintRow['status']): string {
  return status === 'active' ? 'var(--cn-dot-active)' : 'var(--cn-dot-inactive)';
}

function matchesStatus(status: EffectiveConstraintRow['status'], filter: StatusFilter): boolean {
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
    <div>
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
  fact: EffectiveConstraintRow;
  onRequestRevoke: (fact: EffectiveConstraintRow) => void;
}) {
  const revoked = fact.status === 'revoked';
  const superseded = fact.status === 'superseded';
  const inactive = revoked || superseded;
  const dot = FACT_TYPE_DOTS[fact.type] ?? 'var(--cn-type-default)';
  return (
    <div className={`cn-row detail-card${inactive ? ' revoked' : ''}`} data-status={fact.status}>
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
          {fact.source_seq !== null && <span>来源事件 #seq {fact.source_seq}</span>}
          {fact.revoked_at && (
            <span>
              {formatFactTime(fact.revoked_at)} 撤销
              {fact.revoked_by ? ` · ${operatorLabel(fact.revoked_by)}` : ''}
            </span>
          )}
        </div>
      </div>
      <div className="cn-row-side">
        {fact.overridden_by !== null && <span className="cn-ov">被本会话覆盖</span>}
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

/** 一个 scope 组（眉标 + 计数 + 组内行）。PROJECT 组额外带 `git 根 · label`。 */
function ScopeGroup({
  en,
  zh,
  gitKey,
  count,
  rows,
  onRequestRevoke,
}: {
  en: string;
  zh: string;
  gitKey?: string;
  count: number;
  rows: EffectiveConstraintRow[];
  onRequestRevoke: (fact: EffectiveConstraintRow) => void;
}) {
  return (
    <div className="cn-group" data-scope={en.toLowerCase()}>
      <div className="cn-ghead">
        <span className="cn-gen">{en}</span>
        <span className="cn-gzh">{zh}</span>
        {gitKey && <span className="cn-gkey">git 根 · {gitKey}</span>}
        <span className="cn-gcnt">{count} 条生效中</span>
      </div>
      {rows.map((fact) => (
        <FactRow key={fact.fact_id} fact={fact} onRequestRevoke={onRequestRevoke} />
      ))}
    </div>
  );
}

/** 纯展示视图：全部交互回调由外壳注入（SSR / 单测直接喂 props）。 */
export function ConstraintsView({
  sessionId,
  facts,
  project,
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
  facts: EffectiveConstraintRow[];
  project: EffectiveProjectMeta | null;
  loading: boolean;
  error: string | null;
  forbidden: boolean;
  statusFilter: StatusFilter;
  typeFilter: string;
  onStatusFilter: (filter: StatusFilter) => void;
  onTypeFilter: (type: string) => void;
  onRetry: () => void;
  onRequestRevoke: (fact: EffectiveConstraintRow) => void;
}) {
  const visible = facts.filter(
    (fact) => matchesStatus(fact.status, statusFilter) && (!typeFilter || fact.type === typeFilter),
  );
  const countFor = (filter: StatusFilter) =>
    facts.filter((fact) => matchesStatus(fact.status, filter)).length;
  /** 组计数 = 该组**生效中**条数（票面 §五.2 的「N 条生效中」）。
   *
   *  生效判据与注入侧同源（§五.1）：`status==='active'` **且** `overridden_by===null`
   *  ——被更高优先级同文约束覆盖的条目不注入，也就不是「生效中」（审查 finding C7：
   *  旧实现把它计入，导致同一行同时挂「生效中」与「被本会话覆盖」、计数与优先级条自相矛盾）。 */
  const activeCount = (scope: ConstraintScope) =>
    facts.filter(
      (fact) => fact.scope === scope && fact.status === 'active' && fact.overridden_by === null,
    ).length;

  let body;
  if (forbidden) {
    body = (
      <div className="cn-state" role="alert">
        无权限查看要求
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
    body = <div className="cn-state">正在加载要求…</div>;
  } else if (visible.length === 0) {
    body = <div className="cn-state">没有符合条件的要求</div>;
  } else {
    // 非 git 目录的会话无项目键（`project === null`）→ 不渲染 PROJECT 组（票面 §五.5）。
    const groups = GROUPS.filter((g) => g.scope !== 'project' || project !== null);
    body = (
      <>
        {groups.map((group) => (
          <ScopeGroup
            key={group.scope}
            en={group.en}
            zh={group.zh}
            gitKey={group.scope === 'project' ? project?.label ?? undefined : undefined}
            count={activeCount(group.scope)}
            rows={visible.filter((fact) => fact.scope === group.scope)}
            onRequestRevoke={onRequestRevoke}
          />
        ))}
      </>
    );
  }

  return (
    <div className="cn-panel" data-testid="constraints-panel">
      <div className="cn-eyebrow">我的要求</div>
      <h2 className="cn-title">我的要求</h2>
      <div className="cn-session-line">
        <span className="cn-livedot" aria-hidden="true" />
        当前会话 <code className="cn-session-id">{sessionId}</code>
        <span>当前生效的要求</span>
      </div>

      <div className="cn-prio" data-testid="constraints-priority">
        {PRIORITY_BAR}
        <code>overridden_by</code>
        {PRIORITY_BAR_TAIL}
      </div>

      <div className="cn-toolbar">
        <div className="cn-tabs" role="tablist" aria-label="要求状态筛选">
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

      <div className="cn-footer">撤销后会标记为「已被取代」并保留来源记录，可审计、可追溯。</div>
    </div>
  );
}

/** 撤销二次确认的影响面文案（#1191 §六.3：项目/全局必须明示影响面）。 */
function revokeImpact(scope: ConstraintScope): string {
  if (scope === 'project') {
    return '撤销后，将影响本仓库所有会话。来源记录保留，可审计。';
  }
  if (scope === 'global') {
    return '撤销后，将影响所有项目的新会话。来源记录保留，可审计。';
  }
  return '撤销后，新的会话将不再带上这条要求。来源记录保留，可审计。';
}

/** 有状态外壳：负责请求生命周期 + 撤销二次确认。 */
export function ConstraintsTab({ sessionId }: { sessionId: string }) {
  const [facts, setFacts] = useState<EffectiveConstraintRow[]>([]);
  const [project, setProject] = useState<EffectiveProjectMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('active');
  const [typeFilter, setTypeFilter] = useState('');
  const [pendingRevoke, setPendingRevoke] = useState<EffectiveConstraintRow | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const loadedFor = useRef(sessionId);

  const load = useCallback(async () => {
    // 会话切换：先清上一会话的投影，避免新 GET 落定前短暂显示旧行/旧 tab 计数
    // （渲染分支不区分「上一会话的数据」与「本会话的缓存」）。
    if (loadedFor.current !== sessionId) {
      loadedFor.current = sessionId;
      setFacts([]);
      setProject(null);
      setForbidden(false);
    }
    // 每次（含重试）开始都先清错误文案：错误态由本次结果决定，不残留上一次的。
    setError(null);
    setLoading(true);
    try {
      const next = await listEffectiveConstraints(sessionId);
      setFacts(next.facts);
      setProject(next.project);
      setError(null);
      setForbidden(false);
    } catch (err) {
      if (err instanceof UnauthorizedError || (err instanceof SessionError && err.status === 403)) {
        // 清空旧数据：渲染分支已互斥（forbidden > error > facts），但内存里不该留着
        // 上一会话的行——不让任何路径出现「缓存冒充真相」的可能。
        setFacts([]);
        setProject(null);
        setForbidden(true);
        setError(null);
      } else {
        // 不保留旧数据（不许用缓存冒充）：失败即清空，展示错误态。
        // 同时复位 `forbidden`——否则「先 403、后 5xx」会让内存态两个标志同时为真
        // （渲染分支互斥，用户看不到脏数据，但不变量该单一为真）。
        setFacts([]);
        setProject(null);
        setForbidden(false);
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
      // 会话级走 #1190 的会话撤销端点；项目/全局级走 #1191 的规则层撤销端点。
      if (pendingRevoke.scope === 'session') {
        await revokeProtectedFact(sessionId, pendingRevoke.fact_id);
      } else {
        await revokeScopedConstraint(pendingRevoke.fact_id, {
          scope: pendingRevoke.scope,
          sessionId,
        });
      }
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
        project={project}
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
          <Dialog.Overlay className="palette-overlay" />
          <Dialog.Content className="cn-modal" aria-describedby="cn-modal-desc">
            <Dialog.Title className="cn-modal-title">撤销这条要求？</Dialog.Title>
            <p className="cn-modal-desc" id="cn-modal-desc">
              {pendingRevoke ? revokeImpact(pendingRevoke.scope) : ''}
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
