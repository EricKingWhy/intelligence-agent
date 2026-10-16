/** 约束继承展示助手（#1192 / #1176 Phase 3）——纯展示、无副作用。
 *
 *  放在 `lib/` 而不是组件模块内（react-refresh `only-export-components`，
 *  同 `constraints.ts` 的先例）：解析函数直接单测，组件只做渲染。
 *
 *  数据来源：`agent/constraint-inheritance` 事件（后端
 *  `multiagent/constraint_inheritance.py` 落**子会话**事件流的快照，
 *  单一事实源仍是事件流——这里只投影，不做第二真相）。
 *  坏数据容错不 throw：条目形状不对就跳过，绝不伪造运行时事实。 */

import type { AgentEvent } from '../types';
import type { ConstraintScope } from './api';
import { SCOPE_LABELS } from './constraints';

/** 后端 `session/event.py::AGENT_CONSTRAINT_INHERITANCE`（单一 owner 在后端）。 */
export const CONSTRAINT_INHERITANCE_EVENT_TYPE = 'agent/constraint-inheritance';

/** 一条继承集/未继承集条目（展示投影；`source_event_id`/`source_seq` 是审计
 *  字段，子详情视图不展示，不投影）。 */
export interface InheritanceRowView {
  factId: string;
  type: string;
  value: string;
  scope: ConstraintScope;
}

/** 快照展示投影（事件 data 的形状安全版）。 */
export interface InheritanceSnapshotView {
  omitConstraints: boolean;
  inherited: InheritanceRowView[];
  notInherited: InheritanceRowView[];
}

function valueText(v: unknown): string {
  if (typeof v === 'string') return v;
  if (v === undefined || v === null) return '';
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

function parseRow(raw: unknown): InheritanceRowView | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  const factId = typeof r.fact_id === 'string' ? r.fact_id : '';
  const type = typeof r.type === 'string' ? r.type : '';
  if (factId === '' || type === '') return null;
  const scope: ConstraintScope = r.scope === 'project' || r.scope === 'global' ? r.scope : 'session';
  return { factId, type, value: valueText(r.value), scope };
}

function parseRows(raw: unknown): InheritanceRowView[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(parseRow).filter((row): row is InheritanceRowView => row !== null);
}

/** 从子会话事件流取约束继承快照（第一条匹配事件；无则 null——数据不存在
 *  就不渲染，不伪造）。 */
export function parseInheritanceEvent(events: AgentEvent[]): InheritanceSnapshotView | null {
  const event = events.find((e) => e?.type === CONSTRAINT_INHERITANCE_EVENT_TYPE);
  if (!event) return null;
  const data: unknown = event.data;
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return null;
  const d = data as Record<string, unknown>;
  return {
    omitConstraints: d.omit_constraints === true,
    inherited: parseRows(d.inherited),
    notInherited: parseRows(d.not_inherited),
  };
}

const SCOPE_EYEBROW_EN: Record<ConstraintScope, string> = {
  session: 'SESSION',
  project: 'PROJECT',
  global: 'GLOBAL',
};

/** scope → 眉标（mockup phase3 口径：`PROJECT · 本项目`；词汇与 ConstraintsTab
 *  同源 `SCOPE_LABELS`，避免同一份词汇两处漂移）。 */
export function scopeEyebrow(scope: ConstraintScope): string {
  return `${SCOPE_EYEBROW_EN[scope]} · ${SCOPE_LABELS[scope]}`;
}
