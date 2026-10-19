/** ConstraintInheritance — 子 agent 详情「约束继承」区（#1192 / #1176 Phase 3）。
 *
 *  数据：`agent/constraint-inheritance` 快照事件（后端落**子会话**事件流的
 *  独立审计事件）——子详情视图（StepDetail 的 ChildSessionView）已加载该
 *  会话事件流，这里只读投影，不做第二真相、不发请求。
 *
 *  视觉对照 `docs/plans/1176-constraint-panel/mockups/phase3.html`：
 *  - INHERITED 组：scope 眉标 + 类型点/标签 + 条目值 + 「已注入」状态；
 *  - NOT INHERITED 组：会话专属事实灰显 + 「未注入」+ 转述提示（票面 §三.4）；
 *  - 干净启动：`omit_constraints=true` 时继承区置灰 + 顶部提示（开关本身是
 *    委派时的参数，子详情里是**只读事实展示**，不提供交互开关——mockup 的
 *    可点开关是效果图演示态）。
 *
 *  无快照事件（旧会话 / 非委派会话）渲染 null——数据不存在就不渲染。
 */

import { ShieldCheck } from 'lucide-react';
import type { AgentEvent } from '../types';
import {
  parseInheritanceEvent,
  scopeEyebrow,
  type InheritanceRowView,
} from '../lib/constraintInheritance';
import { FACT_TYPE_DOTS, FACT_TYPE_LABELS } from '../lib/constraints';

function Row({ row, injected }: { row: InheritanceRowView; injected: boolean }) {
  return (
    <div className={`ci-row${injected ? '' : ' ci-skip'}`}>
      <div className="ci-row-main">
        <div className="ci-scope">{scopeEyebrow(row.scope)}</div>
        <div className="ci-val">
          <span className="ci-type">
            <span
              className="ci-tdot"
              style={{ background: FACT_TYPE_DOTS[row.type] ?? 'var(--cn-type-default)' }}
              aria-hidden="true"
            />
            {FACT_TYPE_LABELS[row.type] ?? row.type}
          </span>
          {row.value}
        </div>
        {!injected && (
          <div className="ci-why">本会话专属的要求不会自动沿用；如需遵守，由父任务在委派描述里转述。</div>
        )}
      </div>
      <span className="ci-status">
        <span className="ci-dot" aria-hidden="true" />
        {injected ? '已沿用' : '未沿用'}
      </span>
    </div>
  );
}

export function ConstraintInheritance({ events }: { events: AgentEvent[] }) {
  const snapshot = parseInheritanceEvent(events);
  if (snapshot === null) return null;
  const { omitConstraints, inherited, notInherited } = snapshot;
  return (
    <div className="detail-section ci-section" data-omit={omitConstraints || undefined}>
      <div className="detail-section-title">
        <ShieldCheck size={14} /> 要求沿用
      </div>
      {omitConstraints && (
        <div className="ci-clean-msg" role="note">
          干净启动已开启——子任务不沿用任何要求，只按任务描述工作。
        </div>
      )}
      <div className={`ci-rows${omitConstraints ? ' ci-dim' : ''}`}>
        <div className="ci-group-label">
          已沿用 <span className="ci-group-tag">项目 + 全局生效中的要求，默认会沿用</span>
        </div>
        {inherited.length === 0 ? (
          <div className="ci-empty">本次委派没有沿用任何要求。</div>
        ) : (
          inherited.map((row) => <Row key={row.factId} row={row} injected />)
        )}
      </div>
      {notInherited.length > 0 && (
        <div className="ci-rows">
          <div className="ci-group-label">
            没有沿用 <span className="ci-group-tag">本会话专属要求，父任务按需转述</span>
          </div>
          {notInherited.map((row) => (
            <Row key={row.factId} row={row} injected={false} />
          ))}
        </div>
      )}
    </div>
  );
}
